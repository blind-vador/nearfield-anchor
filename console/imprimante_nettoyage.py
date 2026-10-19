"""Nettoyage guide de l'imprimante 3D (2026-10-16) : commande « nettoie
imprimante » (aussi « nettoyage buse », « nettoyage plateau »).

L'assistant ne presume pas du materiel : il presente un catalogue de
methodes. Chacune dit ce qu'elle nettoie, a chaud ou a froid, sa duree, le
materiel (decrit au toucher) et les precautions ; la personne choisit selon
ce qu'elle a, puis est guidee consigne par consigne.

Sources : Prusa Knowledge Base « Cold pull » (tirage a froid : chauffer,
pousser le filament, refroidir, tirer vers 100 degres, tout droit) ; manuel
Prusa (plateau : alcool isopropylique, ou eau et liquide vaisselle). Tete
Ender 3 d'origine : tube PTFE jusqu'a la buse, jamais au-dela de 240 degres.

Securite : la buse n'est dite touchable que sous BUSE_TOUCHABLE ; ESPACE,
Echap, la fin d'une methode et un delai sans action coupent les chauffes.

Demontage ou remontage de la buse (2026-10-19) : une buse se desserre et se
serre A CHAUD (le laiton et le bloc de chauffe se dilatent ensemble ; a froid
on arrache le filetage ou on laisse une fuite). Deroule : sens (montage ou
demontage), type de tete (memorise), Z tout en haut, tete contre le bord
droit (cote oppose au sens de devissage, l'anti-horaire vu d'en dessous : la
cle ne bute pas sur le plateau), gants, chauffe stabilisee, la personne
serre ou desserre, chauffe coupee ; au remontage la tete revient au centre
et redescend au repos. Temperature : 240 degres avec un tube PTFE jusqu'a la
buse (Ender 3 d'origine : au-dela, le PTFE se degrade et degage des fumees
toxiques ; Marlin d'origine plafonne d'ailleurs vers 260), 285 pour une
tete tout metal (valeur de serrage a chaud recommandee par E3D).

Touches : fleches haut et bas ou chiffres pour choisir, Entree, Echap
(revenir a la liste, puis quitter), R repeter, T temperatures, F1 aide,
ESPACE arret.
"""
import sys
import threading
import time

import imprimante_calibrage as base
from imprimante_calibrage import Arrete, ENTER, op, dire  # noqa: F401

FLECHE_HAUT, FLECHE_BAS = "\x1b[A", "\x1b[B"
BUSE_TOUCHABLE = 45            # degres : en dessous, la buse peut etre touchee
PLATEAU_TOUCHABLE = 35
ANNONCE_DEGRES = 20            # une annonce tous les 20 degres
CHAUFFE_MAX_SEC = 600          # chauffe coupee seule apres 10 min sans action
TIRAGE_CHAUD = 220             # Ender 3 d'origine (PTFE) : rester bien sous 240
TIRAGE_POUSSER_JUSQUA = 170    # on pousse encore pendant le refroidissement
TIRAGE_TIRER = 90              # « tire maintenant »
PURGE_MM = 50
BUSE_SERRAGE = {"ptfe": 240, "tout_metal": 285}   # demontage / remontage a chaud (voir plus haut)
Z_HAUT_MARGE = 10            # mm sous le haut de la course
X_BORD_MARGE = 2             # mm avant la butee droite
Z_REPOS = 10                 # hauteur de repos au centre apres un remontage
Z_PAS_MM = 20                # montee tout en haut par paliers de 2 cm, position relue a chaque palier

# Materiel : code -> cle de phrase (description au toucher).
MATERIEL = ("alcool", "essuie_tout", "eau_savon", "carte", "pince_longue", "brosse_laiton", "filament", "pince_plate",
            "gants", "cle_buse", "pince_bloc")

# Methodes : code -> (materiel, chauffe buse, deroule). Le deroule est une
# liste d'etapes : ("dire", cle) consigne et Entree ; ("plateau_devant",)
# plateau vers soi ; ("tete_haute",) place sous la buse ; ("chauffer", t) ;
# ("pousser_jusqua", t) annonce en refroidissant ; ("attendre_froid", t) ;
# ("purger",) ; ("refroidir",) coupe et attend BUSE_TOUCHABLE.
METHODES = {
    "plateau_alcool": (("alcool", "essuie_tout"), 0, (
        ("plateau_froid",), ("tete_haute",), ("plateau_devant",),
        ("dire", "alcool_1"), ("dire", "alcool_2"), ("dire", "alcool_3"), ("dire", "plateau_fin"), ("centrer_plateau",))),
    "plateau_savon": (("eau_savon", "essuie_tout"), 0, (
        ("plateau_froid",), ("tete_haute",), ("plateau_devant",),
        ("dire", "savon_1"), ("dire", "savon_2"), ("dire", "savon_3"), ("dire", "savon_4"),
        ("dire", "plateau_fin"), ("centrer_plateau",))),
    "plateau_grattage": (("carte", "essuie_tout"), 0, (
        ("plateau_froid",), ("tete_haute",), ("plateau_devant",),
        ("dire", "grattage_1"), ("dire", "grattage_2"), ("dire", "plateau_fin"), ("centrer_plateau",))),
    "buse_papier": (("pince_longue", "essuie_tout"), 200, (
        ("tete_haute",), ("dire", "chaud_avant"), ("chauffer", 200),
        ("dire", "papier_1"), ("dire", "papier_2"), ("refroidir",))),
    "buse_brosse": (("brosse_laiton",), 200, (
        ("tete_haute",), ("dire", "chaud_avant"), ("chauffer", 200),
        ("dire", "brosse_1"), ("dire", "brosse_2"), ("refroidir",))),
    "buse_purge": ((), 210, (
        ("tete_haute",), ("centrer",), ("dire", "purge_avant"), ("chauffer", 210), ("purger",),
        ("dire", "purge_apres"), ("refroidir",))),
    "buse_tirage": (("filament", "pince_plate"), TIRAGE_CHAUD, (
        ("tete_haute",), ("dire", "tirage_avant"), ("chauffer", TIRAGE_CHAUD),
        ("dire", "tirage_pousser"), ("pousser_jusqua", TIRAGE_POUSSER_JUSQUA),
        ("attendre_froid", TIRAGE_TIRER), ("dire", "tirage_apres"), ("refroidir",))),
    # Deroule construit selon les reponses (sens, type de tete) : _deroule_buse().
    "buse_changer": (("gants", "cle_buse", "pince_bloc"), 240, ()),
}
ORDRE = tuple(METHODES)


class Nettoyage(base.Calibrage):
    """Reprend du calibrage la parole, les taches de fond, l'arret (M410),
    les commandes a jeton (M118) et le journal."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.choix = 0                # methode sous le curseur
        self.methode = None           # methode en cours
        self.pas = 0                  # etape en cours dans le deroule
        self.attente_entree = False   # une consigne attend Entree
        self.chauffe_depuis = None    # heure ou la buse a atteint sa consigne
        self.chaud = False            # une chauffe a ete demandee
        self.tete_prete = False       # place sous la buse confirmee (H monte)
        self.question = None          # "sens" ou "tete" : buse_changer pose ses questions
        self.sens = None              # "montage" ou "demontage"

    # -- catalogue -------------------------------------------------------------
    def _nom(self, code):
        return dire(f"imprimante.net.nom.{code}")

    def presenter(self, code):
        """Ce que nettoie la methode, sa duree, chaud ou froid."""
        return dire(f"imprimante.net.resume.{code}")

    def details(self, code):
        materiel, chauffe, _ = METHODES[code]
        parts = [dire("imprimante.net.methode", n=ORDRE.index(code) + 1, nom=self._nom(code)),
                 self.presenter(code)]
        if materiel:
            parts.append(dire("imprimante.net.materiel_intro", n=len(materiel)))
            parts += [dire(f"imprimante.net.mat.{m}") for m in materiel]
        else:
            parts.append(dire("imprimante.net.materiel_aucun"))
        parts.append(dire("imprimante.net.securite_chaud" if chauffe else "imprimante.net.securite_froid"))
        parts.append(dire("imprimante.net.choisir"))
        return " ".join(parts)

    def liste(self):
        noms = ", ".join(f"{i + 1}, {self._nom(c)}" for i, c in enumerate(ORDRE))
        return dire("imprimante.net.liste", n=len(ORDRE), noms=noms)

    # -- deroule -------------------------------------------------------------
    def commencer(self, code):
        self.methode, self.pas = code, 0
        self.journal(f"NETTOYAGE debut {code}")
        if code == "buse_changer":
            self.question, self.sens = "sens", None
            return dire("imprimante.net.buse_q_sens")
        return self.avancer()

    # -- demontage / remontage de la buse ----------------------------------------
    @staticmethod
    def _tete():
        import reglages
        return reglages.get("imprimante").get("tete")   # "ptfe", "tout_metal" ou None

    @staticmethod
    def _noter_tete(tete):
        import reglages
        data = dict(reglages.get("imprimante"), tete=tete)
        reglages.save("imprimante", data)

    def _temperature_buse(self):
        return BUSE_SERRAGE.get(self._tete() or "ptfe", BUSE_SERRAGE["ptfe"])

    def _deroule_buse(self):
        t = self._temperature_buse()
        prep = "buse_prep_montage" if self.sens == "montage" else "buse_prep_demontage"
        geste = "buse_geste_montage" if self.sens == "montage" else "buse_geste_demontage"
        etapes = [("dire", prep), ("tete_haute",), ("origine",), ("z_haut",), ("tete_droite",),
                  ("dire", "buse_gants"), ("chauffer_stable", t), ("dire", geste), ("refroidir",)]
        if self.sens == "montage":
            etapes += [("centrer",), ("z_repos",)]
        return tuple(etapes)

    def _reponse_buse(self, key):
        """Questions de buse_changer : sens (M ou D), puis type de tete (P ou T)."""
        k = key.lower()
        if self.question == "sens":
            if k not in ("m", "d"):
                return dire("imprimante.net.buse_q_sens")
            self.sens = "montage" if k == "m" else "demontage"
            if self._tete() is None:
                self.question = "tete"
                return dire("imprimante.net.buse_q_tete")
        elif self.question == "tete":
            if k not in ("p", "t"):
                return dire("imprimante.net.buse_q_tete")
            self._noter_tete("ptfe" if k == "p" else "tout_metal")
        self.question = None
        t = self._temperature_buse()
        intro = dire("imprimante.net.buse_annonce_" + self.sens, t=t,
                     tete=dire("imprimante.net.buse_tete_" + (self._tete() or "ptfe")))
        return intro + " " + self.avancer()

    def avancer(self):
        """Joue l'etape courante : consigne (attend Entree) ou action en fond."""
        if self.methode is None:        # Echap ou Espace entre-temps : plus rien a dire
            return ""
        _, _, deroule = METHODES[self.methode]
        if self.methode == "buse_changer":
            deroule = self._deroule_buse()
        if self.pas >= len(deroule):
            self.journal(f"NETTOYAGE fin {self.methode}")
            self.methode = None
            return dire("imprimante.net.methode_finie") + " " + self.liste()
        etape = deroule[self.pas]
        self.pas += 1
        quoi = etape[0]
        if quoi == "dire":
            self.attente_entree = True
            return dire(f"imprimante.net.{etape[1]}") + " " + dire("imprimante.net.entree_suite")
        if quoi == "tete_haute":
            if not self.tete_prete:
                self.pas -= 1                 # on revient ici apres la reponse
                self.attente_entree = True
                self.tete_prete = None        # question posee
                return dire("imprimante.net.tete_place")
            return self.avancer()
        self.attente_entree = False
        return self._action(etape)

    def _action(self, etape):
        quoi = etape[0]

        def travail():
            if quoi == "plateau_froid":
                self._attendre_temp("plateau", PLATEAU_TOUCHABLE, "imprimante.net.plateau_refroidit", monter=False)
                return dire("imprimante.net.plateau_ok_froid") + " " + self._suite()
            if quoi == "plateau_devant":
                self.mouvement = "doux"
                try:
                    self._suivre(["G28 Y", "G90", "G1 Y235 F3000", "M400", "M114"], op.COUNT_RE, 90)
                finally:
                    self.mouvement = None
                return dire("imprimante.net.plateau_devant") + " " + self._suite()
            if quoi in ("centrer", "centrer_plateau"):
                # Purge : buse au-dessus du centre du plateau, jamais au bord
                # (16/10). R0 : pas de remontee avant les butees.
                gcodes = (["G28 X Y R0", "G90", "G1 X117.5 Y117.5 F3000"] if quoi == "centrer"
                          else ["G90", "G1 Y117.5 F3000"])
                self.mouvement = "doux"
                try:
                    self._suivre(gcodes + ["M400", "M114"], op.COUNT_RE, 90)
                finally:
                    self.mouvement = None
                return dire("imprimante.net.centre" if quoi == "centrer" else "imprimante.net.plateau_recentre") + \
                    " " + self._suite()
            if quoi == "chauffer":
                self.chaud = True
                op.commande("M104 S%d" % etape[1])
                self._attendre_temp("buse", etape[1] - 2, "imprimante.net.chauffe", monter=True)
                self.chauffe_depuis = time.time()
                threading.Thread(target=self._garde_chauffe, daemon=True).start()
                return dire("imprimante.net.buse_chaude", t=etape[1]) + " " + self._suite()
            if quoi == "origine":
                # Origine sure (sonde au centre du plateau s'il y en a une) : Z doit etre connu pour monter.
                self.mouvement = "doux"
                try:
                    ok, detail, _ = op.homing_sur("xyz", timeout=180)
                finally:
                    self.mouvement = None
                if not ok:
                    self.methode = None
                    return dire("imprimante.net.buse_origine_ratee", detail=str(detail))
                return dire("imprimante.net.buse_origine_faite") + " " + self._suite()
            if quoi == "z_haut":
                # Montee par paliers (Z_PAS_MM) : a chaque palier, position relue
                # (M114) ; arret aussitot si le palier n'est pas atteint, si une
                # butee ou la sonde se declenche, ou si le firmware se plaint.
                haut = op.profil()["hauteur"] - Z_HAUT_MARGE
                self.mouvement = "doux"
                try:
                    z = 0.0
                    while z < haut - 0.5:
                        cible = min(haut, (int(z // Z_PAS_MM) + 1) * Z_PAS_MM)
                        fin, contact = self._suivre(["G90", f"G1 Z{cible:.0f} F600", "M400", "M114"], op.COUNT_RE, 60)
                        lu = float(fin.group(3)) if fin else None
                        if contact or lu is None or abs(lu - cible) > 0.5:
                            op.commande("M410")  # arret des moteurs, au cas ou
                            self.methode = None
                            return dire("imprimante.net.buse_z_arret",
                                        mm=round(lu) if lu is not None else "?", cible=round(cible))
                        z = lu
                finally:
                    self.mouvement = None
                return dire("imprimante.net.buse_z_haut_fait", mm=round(haut)) + " " + self._suite()
            if quoi == "tete_droite":
                droite = op.profil()["largeur"] - X_BORD_MARGE
                self.mouvement = "doux"
                try:
                    self._suivre(["G90", f"G1 X{droite:.0f} F3000", "M400", "M114"], op.COUNT_RE, 60)
                finally:
                    self.mouvement = None
                return dire("imprimante.net.buse_tete_droite_faite") + " " + self._suite()
            if quoi == "chauffer_stable":
                self.chaud = True
                op.commande("M104 S%d" % etape[1])
                self._attendre_temp("buse", etape[1] - 2, "imprimante.net.chauffe", monter=True)
                time.sleep(15)  # le bloc de chauffe rattrape la buse : temperature stable
                self.chauffe_depuis = time.time()
                threading.Thread(target=self._garde_chauffe, daemon=True).start()
                return dire("imprimante.net.buse_stable", t=etape[1]) + " " + self._suite()
            if quoi == "z_repos":
                self.mouvement = "doux"
                try:
                    self._suivre(["G90", f"G1 Z{Z_REPOS} F600", "M400", "M114"], op.COUNT_RE, 180)
                finally:
                    self.mouvement = None
                return dire("imprimante.net.buse_repos_fait") + " " + self._suite()
            if quoi == "purger":
                self.mouvement = "doux"
                try:
                    self._suivre(["M83", f"G1 E{PURGE_MM} F100", "M400", "M114"], op.COUNT_RE, 120)
                finally:
                    self.mouvement = None
                return dire("imprimante.net.purge_faite") + " " + self._suite()
            if quoi == "pousser_jusqua":
                op.commande("M104 S0")
                self._attendre_temp("buse", etape[1], "imprimante.net.refroidit_pousse", monter=False)
                return dire("imprimante.net.lache") + " " + self._suite()
            if quoi == "attendre_froid":
                self._attendre_temp("buse", etape[1], "imprimante.net.refroidit", monter=False)
                # On attend Entree : la personne tire, la suite ne doit pas
                # parler pendant ce temps.
                self.attente_entree = True
                return dire("imprimante.net.tire_maintenant") + " " + dire("imprimante.net.entree_suite")
            if quoi == "refroidir":
                self.couper_chauffes()
                self._attendre_temp("buse", BUSE_TOUCHABLE, "imprimante.net.refroidit", monter=False)
                return dire("imprimante.net.touchable") + " " + self._suite()
            return ""
        self._fond(dire("imprimante.net.occupe"), travail)
        cles = {"plateau_froid": "imprimante.net.verifie_plateau", "plateau_devant": "imprimante.net.plateau_vient",
                "chauffer": "imprimante.net.chauffe_lancee", "purger": "imprimante.net.purge_lancee",
                "pousser_jusqua": "imprimante.net.coupe_et_pousse", "attendre_froid": "imprimante.net.attends_tirer",
                "refroidir": "imprimante.net.refroidir_lance", "centrer": "imprimante.net.centrage",
                "centrer_plateau": "imprimante.net.plateau_revient",
                "origine": "imprimante.net.buse_origine_lancee", "z_haut": "imprimante.net.buse_z_haut_lance",
                "tete_droite": "imprimante.net.buse_tete_droite_lance", "chauffer_stable": "imprimante.net.chauffe_lancee",
                "z_repos": "imprimante.net.buse_repos_lance"}
        return dire(cles[quoi], t=etape[1] if len(etape) > 1 else 0)

    def _suite(self):
        """Apres une action de fond : l'etape suivante est annoncee aussitot."""
        methode = self.methode

        def suite():
            if self.methode == methode and not self.fige:
                self.annoncer(self.avancer())
        threading.Timer(0.4, suite).start()
        return ""

    def _attendre_temp(self, cle, seuil, phrase, monter=True):
        """Attend que la buse (ou le plateau) passe le seuil, en annonçant la
        temperature tous les ANNONCE_DEGRES. Arrete sur ESPACE. Pendant un
        refroidissement, boucle "attente de temperature" (la chauffe, elle,
        est accompagnee par la veille de l'imprimante)."""
        if not monter:
            import sons_imprimante as sons
            with sons.boucle("printer_waiting_temperature_loop"):
                return self._attendre_temp_brut(cle, seuil, phrase, monter)
        return self._attendre_temp_brut(cle, seuil, phrase, monter)

    def _attendre_temp_brut(self, cle, seuil, phrase, monter=True):
        dernier = None
        while True:
            self._verifier_arret()
            reelle = (self.flux.temps.get(cle) or (None, None))[0]
            if reelle is not None:
                if (monter and reelle >= seuil) or (not monter and reelle <= seuil):
                    return reelle
                palier = int(reelle // ANNONCE_DEGRES)
                if dernier is not None and palier != dernier:
                    self.dire(dire(phrase, t=round(reelle)), garder=False)
                dernier = palier
            time.sleep(1.0)

    def _garde_chauffe(self):
        """Chauffe coupee seule apres CHAUFFE_MAX_SEC sans fin de methode."""
        debut = self.chauffe_depuis
        while self.chaud and self.chauffe_depuis == debut:
            if time.time() - debut > CHAUFFE_MAX_SEC:
                self.couper_chauffes()
                self.annoncer(dire("imprimante.net.chauffe_coupee_seule"))
                return
            time.sleep(5)

    def couper_chauffes(self):
        self.chaud = False
        try:
            op.commande("M104 S0", "M140 S0")
        except op.Erreur:
            pass

    def arreter(self):
        """ESPACE : chauffes coupees, puis l'arret doux du calibrage."""
        self.couper_chauffes()
        texte = super().arreter()
        self.methode, self.attente_entree, self.question = None, False, None
        return texte + " " + dire("imprimante.net.chauffes_coupees")

    # -- clavier ---------------------------------------------------------------
    def touche(self, key):
        if key == " " and (self.occupe or self.chaud):
            return self.arreter()
        if key in ("r", "R"):
            return self.repeter()
        self.rappel = 0
        if key in ("t", "T"):
            return self.lire_temps()
        if base.conseils.is_help(key):
            return dire("imprimante.net.aide") + " " + dire("imprimante.net.aide_touches")
        if self.fige:
            return dire("imprimante.cal.deja_fige")
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        if self.methode is None:
            if key in (FLECHE_HAUT, FLECHE_BAS):
                self.choix = (self.choix + (1 if key == FLECHE_BAS else -1)) % len(ORDRE)
                return f"{self.choix + 1}, {self._nom(ORDRE[self.choix])}. {self.presenter(ORDRE[self.choix])}"
            if key.isdigit() and 1 <= int(key) <= len(ORDRE):
                self.choix = int(key) - 1
                return self.details(ORDRE[self.choix])
            if key in ("i", "I"):
                return self.details(ORDRE[self.choix])
            if key in ENTER:
                return self.commencer(ORDRE[self.choix])
            if key in ("l", "L"):
                return self.liste()
            return dire("imprimante.net.touches_liste")
        if self.question:                     # buse_changer : sens, type de tete
            return self._reponse_buse(key)
        if self.tete_prete is None:           # question : place sous la buse
            if key in ENTER:
                self.tete_prete = True
                self.attente_entree = False
                return self.avancer()
            if key in ("h", "H"):
                return self.monter(30)
            return dire("imprimante.net.tete_place")
        if self.attente_entree and key in ENTER:
            self.attente_entree = False
            return self.avancer()
        return dire("imprimante.net.entree_suite")

    def quitter_methode(self):
        """Echap pendant une methode : chauffes coupees, retour a la liste."""
        self.couper_chauffes()
        self.journal(f"NETTOYAGE abandon {self.methode}")
        self.methode, self.attente_entree, self.question = None, False, None
        return dire("imprimante.net.methode_quittee") + " " + self.liste()

    def nettoyer(self):
        self.couper_chauffes()
        super().nettoyer()

    def boucle(self):
        if not self.flux.demarrer():
            print("[nettoyage] websocket OctoPrint indisponible", file=sys.stderr)
        intro = base.conseils.intro("imprimante_nettoyage", dire("imprimante.net.titre"),
                                    dire("imprimante.net.consigne"))
        self._out(intro + "\n")
        self.dire(f"{intro} {self.liste()}")
        try:
            for key in self.keys:
                if key == "\x1b":
                    if self.methode is not None and not self.occupe:
                        self.dire(self.quitter_methode())
                        continue
                    break
                if key.startswith("\x00"):
                    continue
                self.journal(f"TOUCHE {key!r} nettoyage={self.methode}")
                try:
                    texte = self.touche(key)
                except Exception as exc:  # une touche qui plante ne ferme pas le dialogue
                    print(f"[nettoyage] touche {key!r} : {exc!r}", file=sys.stderr)
                    texte = dire("imprimante.cal.erreur_interne")
                if texte is None:
                    break
                if texte:
                    self.dire(texte, garder=key not in ("r", "R"))
        finally:
            self.nettoyer()
        return dire("imprimante.net.ferme")


def nettoyage(key_source, say, out, say_wait=None):
    try:
        e = op.etat()
    except op.Erreur:
        e = {"connecte": False}
    if not e.get("connecte"):
        return dire("imprimante.cal.liaison_fermee")
    if any((e.get("flags") or {}).get(f) for f in ("printing", "paused", "pausing")):
        return dire("imprimante.impression_en_cours_refus")
    if say_wait is None:
        try:
            import voix
        except ImportError:
            voix = None
        if voix is not None:
            def say_wait(texte):
                courant = getattr(voix, "_proc", None)
                if courant is not None:
                    try:
                        courant.wait(timeout=40)
                    except Exception:
                        pass
                voix.say_and_wait(texte)
    marquer = getattr(base.audio_lock, "set_calibrage", None)
    if marquer:
        marquer(True)
    try:
        return Nettoyage(key_source, say, out, say_wait).boucle()
    finally:
        if marquer:
            marquer(False)
