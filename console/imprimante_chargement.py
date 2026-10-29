"""Chargement guide du filament avant une impression (2026-10-16) : commande
« charge filament » (aussi « prépare l'imprimante », « change de filament »).

Deroule : choix du materiau (table imprimante_materiel), retrait de l'ancien
filament, preparation de la bobine, chargement a la main, verification de
l'ecoulement (oreille : claquements = extrudeur qui saute, crepitements =
filament humide ; toucher : fil lisse et regulier), verification facultative
du debit (repere a 120 mm, 100 mm pousses, reste mesure au metre), puis
materiau enregistre pour les impressions.

Sources : blog Creality (chauffer, levier, pousser jusqu'a la buse) ; All3DP
(bout coupe en biseau) ; guides de calibration de l'extrudeur (repere a
120 mm, G1 E100 F60, nouveau = actuel x 100 / pousse reel, M92 + M500) ;
guides sur le filament humide (crepitements, bulles). Tete Ender 3 d'origine
(tube PTFE jusqu'a la buse) : jamais plus de 240 degres.

Reprend du nettoyage la surveillance des temperatures, la coupure des
chauffes (ESPACE, Echap, fin, 10 min sans suite) et la voix.
"""
import re
import sys
import threading
import time

import imprimante_nettoyage as net
from imprimante_calibrage import ENTER, op, dire
from imprimante_nettoyage import FLECHE_BAS, FLECHE_HAUT

import imprimante_materiel as mat  # noqa: E402

TETE_MAX = 240                 # tete Ender 3 d'origine (PTFE)
RAMOLLIR_MM = 10               # poussee avant le recul : evite un bouchon au bout
RECUL_MM = 80                  # recul moteur : le bout sort de la tete et de l'extrudeur
PURGE_MM = 50
PURGE_PLAFOND_MM = 150         # au-dela, sans ecoulement confirme : on ne pousse plus
CENTRE = (117.5, 117.5)        # buse au-dessus du centre du plateau
DEBIT_DEMANDE = 100            # mm pousses pour la verification du debit
DEBIT_REPERE = 120             # repere pose a 120 mm de l'entree de l'extrudeur
DEBIT_TOLERANCE = 0.02         # a 2 % pres, on ne change rien
E_BORNES = (60.0, 200.0)       # pas par mm plausibles pour un extrudeur Ender
SECHAGE = {"PLA": 50, "PETG": 60, "ABS": 70, "TPU": 50}   # degres, guides (sources divergentes)
E_RE = re.compile(r"M92 X[\d.]+ Y[\d.]+ Z[\d.]+ E([\d.]+)")


class Chargement(net.Nettoyage):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.ordre = tuple(mat.MATERIAUX)
        self.deroule = []
        self.question = None          # "ancien", "ecoulement", "debit", "chaleur" ou None
        self.saisie = None            # chiffres du reste mesure (debit)
        self.e_actuel = None
        self.pousse_total = 0          # mm pousses depuis le chargement, sans ecoulement confirme

    # -- catalogue -------------------------------------------------------------
    def _nom(self, code):
        return mat.MATERIAUX[code]["nom"]

    def presenter(self, code):
        m = mat.MATERIAUX[code]
        return dire("imprimante.charge.resume", buse=m["buse"], plateau=m["plateau"]) + " " + \
            dire(f"imprimante.charge.toucher.{code}")

    def details(self, code):
        return " ".join([dire("imprimante.charge.materiau", n=self.ordre.index(code) + 1, nom=self._nom(code)),
                         self.presenter(code), dire(f"imprimante.charge.prudence.{code}"),
                         dire("imprimante.charge.materiel"), dire("imprimante.net.choisir")])

    def liste(self):
        actuel, _ = mat.materiau_courant()
        noms = ", ".join(f"{i + 1}, {self._nom(c)}" for i, c in enumerate(self.ordre))
        return dire("imprimante.charge.liste", noms=noms, actuel=self._nom(actuel))

    # -- deroule -------------------------------------------------------------
    def commencer(self, code):
        self.methode, self.pas = code, 0
        self.journal(f"CHARGEMENT debut {code}")
        self.deroule = [("tete_haute",), ("question", "ancien")]
        return self.avancer()

    def _suite_chargement(self, ancien):
        """Construit la suite selon la reponse sur l'ancien filament."""
        code = self.methode
        neuf = min(TETE_MAX, mat.MATERIAUX[code]["buse"])
        suite = []
        if ancien:
            vieux, preset = mat.materiau_courant()
            suite += [("centrer",), ("chauffer", min(TETE_MAX, preset["buse"])), ("dire", "retrait_avant"),
                      ("reculer",),
                      ("dire", "retrait_main"), ("dire", "ancien_range")]
        suite += [("dire", "bobine_place"), ("dire", "bout_coupe"), ("chauffer", neuf), ("centrer",),
                  ("dire", "inserer_1"), ("dire", "inserer_2"), ("purger", PURGE_MM), ("question", "ecoulement"),
                  ("question", "debit"), ("declarer",), ("question", "chaleur")]
        self.deroule = self.deroule[:self.pas] + suite

    def avancer(self):
        if self.methode is None:
            return ""
        if self.pas >= len(self.deroule):
            self.journal(f"CHARGEMENT fin {self.methode}")
            self.methode = None
            import sons_imprimante
            sons_imprimante.jouer("printer_filament_in", attendre=True)
            return dire("imprimante.charge.fini")
        etape = self.deroule[self.pas]
        self.pas += 1
        quoi = etape[0]
        if quoi == "dire":
            self.attente_entree = True
            return dire(f"imprimante.charge.{etape[1]}") + " " + dire("imprimante.net.entree_suite")
        if quoi == "tete_haute":
            if not self.tete_prete:
                self.pas -= 1
                self.attente_entree = True
                self.tete_prete = None
                return dire("imprimante.net.tete_place")
            return self.avancer()
        if quoi == "question":
            self.question = etape[1]
            self.attente_entree = False
            return dire(f"imprimante.charge.q_{etape[1]}")
        if quoi == "declarer":
            got = mat.set_materiau(self.methode)
            self.journal(f"CHARGEMENT materiau enregistre : {got[0] if got else '?'}")
            return dire("imprimante.charge.enregistre", nom=self._nom(self.methode)) + " " + self.avancer()
        self.attente_entree = False
        return self._action_charge(etape)

    def _action_charge(self, etape):
        quoi = etape[0]

        def travail():
            if quoi == "chauffer":
                cible = etape[1]
                self.chaud = True
                op.commande(f"M104 S{cible}")
                reelle = (self.flux.temps.get("buse") or (0, 0))[0] or 0
                if reelle < cible - 2:
                    self._attendre_temp("buse", cible - 2, "imprimante.net.chauffe", monter=True)
                self.chauffe_depuis = time.time()
                threading.Thread(target=self._garde_chauffe, daemon=True).start()
                return dire("imprimante.net.buse_chaude", t=cible) + " " + self._suite()
            if quoi == "centrer":
                # Jamais d'extrusion hors du plateau (16/10 : buse au bord, X 235
                # Y 235, au-dessus des courroies). R0 : pas de remontee avant les
                # butees (elle partirait du zero interne, voir le calibrage) ; la
                # place sous la buse a ete verifiee a l'etape « tete_haute ».
                self.mouvement = "doux"
                try:
                    self._suivre(["G28 X Y R0", "G90", f"G1 X{CENTRE[0]} Y{CENTRE[1]} F3000", "M400", "M114"],
                                 op.COUNT_RE, 90)
                finally:
                    self.mouvement = None
                return dire("imprimante.charge.centre") + " " + self._suite()
            if quoi == "reculer":
                self._pousser([f"G1 E{RAMOLLIR_MM} F100", f"G1 E-{RECUL_MM} F1200"], 60)
                return dire("imprimante.charge.recul_fait") + " " + self._suite()
            if quoi == "purger":
                self._pousser([f"G1 E{etape[1]} F150"], 90)
                self.pousse_total += etape[1]
                self.chauffe_depuis = time.time()
                return dire("imprimante.charge.purge_faite", mm=etape[1]) + " " + self._suite()
            if quoi == "debit":
                self._pousser([f"G1 E{DEBIT_DEMANDE} F60"], DEBIT_DEMANDE + 60,
                              annonce=lambda s: dire("imprimante.charge.debit_progres", s=round(s)))
                self.chauffe_depuis = time.time()
                self.saisie = ""
                return dire("imprimante.charge.debit_mesure")
            return ""
        self._fond(dire("imprimante.net.occupe"), travail)
        cles = {"chauffer": "imprimante.net.chauffe_lancee", "reculer": "imprimante.charge.recul_lance",
                "centrer": "imprimante.charge.centrage",
                "purger": "imprimante.charge.purge_lancee", "debit": "imprimante.charge.debit_lance"}
        return dire(cles[quoi], t=etape[1] if len(etape) > 1 else 0, mm=etape[1] if len(etape) > 1 else 0)

    def _pousser(self, gcodes, limite, annonce=None):
        """Extrusion relative (M83), arretable par ESPACE (M410)."""
        self.mouvement = "doux"
        try:
            self._suivre(["M83"] + gcodes + ["M400", "M114"], op.COUNT_RE, limite, annonce=annonce,
                         annonce_sec=20)
        finally:
            self.mouvement = None

    def _garde_chauffe(self):
        """Comme le nettoyage, mais jamais pendant une impression : « garder
        chaud pour imprimer » ne doit pas couper un job qui vient de partir."""
        debut = self.chauffe_depuis
        while self.chaud and self.chauffe_depuis == debut:
            try:
                if any((op.etat().get("flags") or {}).get(f) for f in ("printing", "paused", "pausing")):
                    return
            except op.Erreur:
                pass
            if time.time() - debut > net.CHAUFFE_MAX_SEC:
                self.couper_chauffes()
                self.annoncer(dire("imprimante.net.chauffe_coupee_seule"))
                return
            time.sleep(5)

    # -- questions -------------------------------------------------------------
    def repondre(self, key):
        q = self.question
        if q == "ancien":
            if key in ENTER or key in ("n", "N"):
                self.question = None
                self._suite_chargement(ancien=key in ENTER)
                return self.avancer()
            return dire("imprimante.charge.q_ancien")
        if q == "ecoulement":
            if key in ENTER:
                self.question = None
                self.pousse_total = 0
                return self.avancer()
            if key in ("p", "P"):
                if self.pousse_total >= PURGE_PLAFOND_MM:
                    return dire("imprimante.charge.plafond", mm=self.pousse_total)
                self.question = None
                self.pas -= 1             # on repose la question apres la purge
                return self._action_charge(("purger", 30))
            if key in ("z", "Z"):
                self.journal(f"CHARGEMENT rien ne sort apres {self.pousse_total} mm")
                self.couper_chauffes()
                self.methode, self.question = None, None

                def refroidir():
                    self._attendre_temp("buse", net.BUSE_TOUCHABLE, "imprimante.net.refroidit", monter=False)
                    return dire("imprimante.net.touchable")
                self._fond(dire("imprimante.net.occupe"), refroidir)
                return dire("imprimante.charge.rien_ne_sort", mm=self.pousse_total)
            if key in ("c", "C"):
                return dire("imprimante.charge.claquements")
            if key in ("b", "B"):
                code = self.methode
                return dire("imprimante.charge.crepitements", t=SECHAGE.get(code, 50))
            return dire("imprimante.charge.q_ecoulement")
        if q == "debit":
            if key in ENTER:
                self.question = None
                return self.avancer()
            if key in ("v", "V"):
                self.question = "debit_repere"
                return dire("imprimante.charge.debit_repere")
            return dire("imprimante.charge.q_debit")
        if q == "debit_repere":
            if key in ENTER:
                self.question = "debit_saisie"
                return self._action_charge(("debit",))
            return dire("imprimante.charge.debit_repere")
        if q == "debit_saisie":
            if self.saisie is None:
                return dire("imprimante.patiente", quoi=dire("imprimante.net.occupe"))
            return self.saisir_reste(key)
        if q == "debit_propose":
            if key in ENTER:
                self.question = None
                op.commande(f"M92 E{self.e_propose:.2f}", "M500")
                self.journal(f"CHARGEMENT debit : M92 E{self.e_propose:.2f} enregistre")
                return dire("imprimante.charge.debit_enregistre", e=net.base._fr(self.e_propose)) + " " + \
                    self.avancer()
            if key in ("n", "N"):
                self.question = None
                return dire("imprimante.charge.debit_garde") + " " + self.avancer()
            return dire("imprimante.charge.debit_propose", e=net.base._fr(self.e_propose))
        if q == "chaleur":
            if key in ("g", "G"):
                self.question = None
                self.chaud = True
                return dire("imprimante.charge.garde_chaud") + " " + self.avancer()
            if key in ENTER:
                self.question = None
                self.couper_chauffes()
                return dire("imprimante.charge.chauffe_coupee") + " " + self.avancer()
            return dire("imprimante.charge.q_chaleur")
        return ""

    def saisir_reste(self, key):
        if key.isdigit() and len(self.saisie) < 3:
            self.saisie += key
            return key
        if key in ("\x7f", "\x08"):
            self.saisie = self.saisie[:-1]
            return dire("imprimante.cal.saisie_efface", reste=self.saisie or dire("imprimante.cal.saisie_vide"))
        if key in ENTER:
            if not self.saisie:
                return dire("imprimante.charge.debit_mesure")
            reste = int(self.saisie)
            self.saisie = None
            reel = DEBIT_REPERE - reste
            if not DEBIT_DEMANDE * 0.7 <= reel <= DEBIT_DEMANDE * 1.3:
                self.question = None
                return dire("imprimante.charge.debit_aberrant", reel=reel) + " " + self.avancer()
            if self.e_actuel is None:
                found, _, _ = self.flux.envoyer(["M503"], E_RE, 10, echec=False)
                self.e_actuel = float(found.group(1)) if found else None
            if self.e_actuel is None:
                self.question = None
                return dire("imprimante.charge.debit_sans_e") + " " + self.avancer()
            if abs(reel - DEBIT_DEMANDE) <= DEBIT_DEMANDE * DEBIT_TOLERANCE:
                self.question = None
                return dire("imprimante.charge.debit_bon", reel=reel) + " " + self.avancer()
            self.e_propose = round(self.e_actuel * DEBIT_DEMANDE / reel, 2)
            if not E_BORNES[0] <= self.e_propose <= E_BORNES[1]:
                self.question = None
                return dire("imprimante.charge.debit_aberrant", reel=reel) + " " + self.avancer()
            self.question = "debit_propose"
            self.journal(f"CHARGEMENT debit : reel={reel} mm, E {self.e_actuel} -> {self.e_propose}")
            return dire("imprimante.charge.debit_resultat", reel=reel, ancien=net.base._fr(self.e_actuel),
                        e=net.base._fr(self.e_propose))
        return dire("imprimante.charge.debit_mesure")

    # -- clavier ---------------------------------------------------------------
    def touche(self, key):
        if key == " " and (self.occupe or self.chaud):
            self.question = None
            return self.arreter()
        if key in ("r", "R"):
            return self.repeter()
        self.rappel = 0
        if key in ("t", "T") and self.question != "debit_saisie":
            return self.lire_temps()
        if net.base.conseils.is_help(key):
            return dire("imprimante.charge.aide") + " " + dire("imprimante.net.aide_touches")
        if self.fige:
            return dire("imprimante.cal.deja_fige")
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        if self.methode is None:
            if key in (FLECHE_HAUT, FLECHE_BAS):
                self.choix = (self.choix + (1 if key == FLECHE_BAS else -1)) % len(self.ordre)
                code = self.ordre[self.choix]
                return f"{self.choix + 1}, {self._nom(code)}. {self.presenter(code)}"
            if key.isdigit() and 1 <= int(key) <= len(self.ordre):
                self.choix = int(key) - 1
                return self.details(self.ordre[self.choix])
            if key in ("i", "I"):
                return self.details(self.ordre[self.choix])
            if key in ENTER:
                return self.commencer(self.ordre[self.choix])
            if key in ("l", "L"):
                return self.liste()
            return dire("imprimante.net.touches_liste")
        if self.tete_prete is None:
            if key in ENTER:
                self.tete_prete = True
                self.attente_entree = False
                return self.avancer()
            if key in ("h", "H"):
                return self.monter(30)
            return dire("imprimante.net.tete_place")
        if self.question:
            return self.repondre(key)
        if self.attente_entree and key in ENTER:
            self.attente_entree = False
            return self.avancer()
        return dire("imprimante.net.entree_suite")

    def quitter_methode(self):
        self.couper_chauffes()
        self.journal(f"CHARGEMENT abandon {self.methode}")
        self.methode, self.attente_entree, self.question = None, False, None
        return dire("imprimante.charge.quitte") + " " + self.liste()

    def boucle(self):
        if not self.flux.demarrer():
            print("[chargement] websocket OctoPrint indisponible", file=sys.stderr)
        intro = net.base.conseils.intro("imprimante_chargement", dire("imprimante.charge.titre"),
                                        dire("imprimante.charge.consigne"))
        self._out(intro + "\n")
        self.dire(f"{intro} {self.liste()}")
        garder = False
        try:
            for key in self.keys:
                if key == "\x1b":
                    if self.methode is not None and not self.occupe:
                        self.dire(self.quitter_methode())
                        continue
                    break
                if key.startswith("\x00"):
                    continue
                self.journal(f"TOUCHE {key!r} chargement={self.methode} question={self.question}")
                try:
                    texte = self.touche(key)
                except Exception as exc:  # une touche qui plante ne ferme pas le dialogue
                    print(f"[chargement] touche {key!r} : {exc!r}", file=sys.stderr)
                    texte = dire("imprimante.cal.erreur_interne")
                if texte:
                    self.dire(texte, garder=key not in ("r", "R"))
            garder = self.chaud and self.methode is None
        finally:
            if garder:
                # « garder chaud pour imprimer » : seule la tete reste chaude ;
                # le reste du nettoyage de fin se fait sans couper la chauffe.
                net.base.Calibrage.nettoyer(self)
            else:
                self.nettoyer()
        return dire("imprimante.charge.ferme_chaud" if garder else "imprimante.charge.ferme")


def chargement(key_source, say, out, say_wait=None):
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
    marquer = getattr(net.base.audio_lock, "set_calibrage", None)
    if marquer:
        marquer(True)
    try:
        return Chargement(key_source, say, out, say_wait).boucle()
    finally:
        if marquer:
            marquer(False)
