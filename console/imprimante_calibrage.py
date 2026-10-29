"""Calibrage guide de l'imprimante 3D, version 2 (2026-10-16) : commande
« calibrage imprimante » (aussi « calibre plateau », « nivellement »).

Procedure de reference (docs Marlin « Auto Bed Leveling » et Klipper
« Probe calibration ») : la sonde BLTouch fait l'origine Z (Z_SAFE_HOMING),
on la valide avant de lui confier la machine, puis on regle molettes,
hauteur de buse et carte du plateau. Etapes, dites une par une :
  1. decouverte : visite de la machine au toucher (Gemini, une fois) ;
  2. verifications : OctoPrint, liaison, temperatures, firmware (arret doux) ;
  3. test de la sonde : M43 S, sans mouvement, on pousse la tige a l'ongle ;
  4. hauteur de la sonde : tige rangee, environ 2 mm au-dessus de la buse ;
  5. test a l'ongle : la tete descend lentement, l'ongle arrete la tige ;
  6. origine : X et Y, approche lente jusqu'au contact de la sonde, puis
     G28 Z court (quelques secondes, le seul mouvement qu'on ne peut pas
     arreter en douceur) ;
  7. repetabilite : M48, dix mesures au meme point ;
  8. molettes : la sonde reste au-dessus d'un coin et remesure en boucle
     pendant qu'on tourne la molette (« continue », « stop ») ;
  9. hauteur de la buse : feuille de papier, M851 + M500 ;
 10. carte du plateau : G29 (5 x 5 points), M500, M420 S1.

Lecture des capteurs (corrige le 2026-10-16) : le contact se lit dans la
ligne « endstops hit » de Marlin, jamais par M119 en boucle (chaque M119
recommande la BLTouch) ni par la duree d'un mouvement.

Arret : ESPACE pendant un mouvement ordinaire = M410 (OctoPrint l'envoie
hors file grace a EMERGENCY_PARSER) puis la tete remonte de 10 mm, sans
redemarrer l'imprimante. Pendant le G28 Z court, Marlin ne sait pas
s'arreter sans echouer : ESPACE = M112, comme avant.

Touches : Entree, S passer l'etape, R repeter, C capteurs, T temperatures,
F1 aide de l'etape, Echap quitter, ESPACE arret.
"""
import chemins
import collections
import json
import os
import queue
import re
import sys
import threading
import time
import uuid

import audio_lock  # noqa: E402
import conseils  # noqa: E402
import imprimante_decouverte as decouverte  # noqa: E402
import octoprint as op  # noqa: E402
from imprimante_cmd import LEVEL_POINTS as NOMS_POINTS  # noqa: E402
from phrases import Etiquettes, dire  # noqa: E402

PAGE_UP, PAGE_DOWN = "\x1b[5~", "\x1b[6~"
ENTER = ("\r", "\n")
JOURNAL = chemins.base("calibrage_journal.log")   # phrases dites, touches, mesures
MOLETTES = chemins.base("reglages_cache/imprimante_molettes.json")
HISTORIQUE = 12
CARTE_ANNONCE_SEC = 30          # progression de la carte (3 min) : pas toutes les 10 s

HIT_RE = re.compile(r"Recv:.*endstops hit", re.I)
STOP_RE = re.compile(r"Recv:.*(STOP called|restart with M999|Printer stopped)", re.I)
M43S_FIN_RE = re.compile(r"= BLTouch V3\.1 detected|= BLTouch pre V3\.1|= Z Servo Probe|FAIL|Noise detected")
M48_RE = re.compile(r"Recv:.*Range:\s*(-?[\d.]+)")
SIGMA_RE = re.compile(r"Recv:.*Standard Deviation:\s*(-?[\d.]+)")
GRILLE_RE = re.compile(r"Recv:\s*(\d+)\s+((?:[+-]?\d+\.\d+\s*)+)$")
OK_RE = re.compile(r"Recv:\s*ok\b")

# Approche (etapes 5 et 6) : mouvement ordinaire, donc arretable par M410.
APPROCHE_MAX_MM = 260          # plafond de la hauteur tapee par la personne
APPROCHE_MARGE_MM = 2          # au-dela de l'ecart mesure buse-verre : erreur de mesure au metre
# Contact = descente plus courte que prevu d'au moins ECOURTE_SEC. Sans
# contact l'ecart mesure reste sous 0,15 s (reperes M118 lus au dixieme) ;
# avec contact la tige touche au moins APPROCHE_MARGE_MM avant la fin de
# course, soit 0,7 s a 3 mm/s. Seuil ABSOLU : une regle en pourcentage
# raterait le contact d'une longue descente (4 mm sur 200).
ECOURTE_SEC = 0.5
APPROCHE_VITESSE = 180         # mm/min (3 mm/s)
APPROCHE_ANNONCE_SEC = 10
ONGLE_MM = 15
ONGLE_VITESSE = 120            # 2 mm/s : 7 s pour arreter la tige
RECUL_MM = 10                  # remontee apres un arret
G28Z_ALERTE_SEC = 30           # G28 Z court : ~10 s attendues
# Garde-fou seulement : le temps de descendre de tout en haut a la vitesse
# d'origine (250 mm a 4 mm/s) plus la double touche. 45 s coupaient une
# origine normale partie de haut (16/10, 21:04).
G28Z_LIMITE_SEC = 90
HOMING_XY_SEC = 90
SYNCHRO_SEC = 120              # fin des commandes deja en file avant une action
G30_SEC = 60
M48_SEC = 240
G29_SEC = 600
# Repetabilite (Klipper « Probe calibration » : plus de 25 microns d'ecart
# total, la sonde ne convient pas au nivellement).
M48_BON_MM = 0.025
M48_MOYEN_MM = 0.05

TOLERANCE_MM = 0.05
PAS_VIS_MM = 0.7               # vis M4 des molettes Ender 3 : 0,7 mm par tour
APPRENTISSAGE_MIN_MM = 0.03    # un quart de tour doit bouger le coin d'au moins ca
MOLETTE_PASSES = 3
MOLETTE_PAUSE_SEC = 1.0        # entre deux mesures : le temps de tourner
# Points de la SONDE (G30 X Y) au plus pres des molettes et a sa portee
# (elle est 50 mm a gauche de la buse) : memes points que G35 du firmware v2.
POINTS_MOLETTES = {"avant_gauche": (32, 32), "avant_droit": (185, 32), "arriere_droit": (185, 203),
                   "arriere_gauche": (32, 203)}
COINS = tuple(POINTS_MOLETTES)

PAS_HAUTEUR = [0.02, 0.05, 0.1, 0.5]
PAS_HAUTEUR_DEFAUT = 2          # 0,1 mm : apres un reglage de la sonde, la buse peut avoir 2 mm a descendre
DECALAGE_BORNES = (-4.0, 1.0)

ETAPES = ("decouverte", "verifs", "sonde", "hauteur_sonde", "sens_z", "ongle", "origine", "repetabilite", "molettes",
          "hauteur", "carte")
NOMS_ETAPES = Etiquettes("imprimante.cal.nom_etape", {
    "decouverte": "Découverte de la machine", "verifs": "Vérifications", "sonde": "Test de la sonde",
    "hauteur_sonde": "Hauteur de la sonde", "sens_z": "Sens du moteur Z", "ongle": "Test à l'ongle", "origine": "Mise à l'origine",
    "repetabilite": "Fidélité de la sonde", "molettes": "Réglage des molettes", "hauteur": "Hauteur de la buse",
    "carte": "Carte du plateau"}, "nom d'une étape du calibrage, début de phrase")


def _fr(value, decimales=2):
    """0.15 -> "0,15" ; 1.0 -> "1" ; -0.4 -> "moins 0,4"."""
    text = f"{abs(value):.{decimales}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    text = text.replace(".", ",")
    return ("moins " + text) if value < 0 and text != "0" else text


def _tours(mm, mm_par_tour=PAS_VIS_MM):
    """Fraction de tour de molette parlee pour corriger mm."""
    t = abs(mm) / mm_par_tour
    if t < 0.09:
        return dire("imprimante.cal.tour_tres_peu")
    if t < 0.19:
        return dire("imprimante.cal.tour_huitieme")
    if t < 0.32:
        return dire("imprimante.cal.tour_quart")
    if t < 0.62:
        return dire("imprimante.cal.tour_demi")
    if t < 0.87:
        return dire("imprimante.cal.tour_trois_quarts")
    return dire("imprimante.cal.tour_n", n=_fr(round(t * 2) / 2, 1))


def _lire_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _ecrire_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


class Arrete(Exception):
    """ESPACE pendant un travail de fond : on sort proprement."""


class Calibrage:
    def __init__(self, key_source, say, out, say_wait=None):
        self.keys, self._say, self._out = key_source, say, out
        self._say_wait = say_wait or say
        self._file = queue.Queue()
        threading.Thread(target=self._parleur, daemon=True).start()
        self.historique = collections.deque(maxlen=HISTORIQUE)
        self.rappel = 0
        self.flux = op.Flux()
        self.etape = 0
        self.occupe = None            # phrase de ce qui tourne en tache de fond
        self.mouvement = None         # None, "doux" (M410 possible) ou "g28" (seul M112 arrete)
        self.arret = threading.Event()
        self.fige = False             # M112 envoye : plus rien ne bouge
        self.bloque = False
        self.machine = {}
        self.caps = {}
        self.version = ""
        self.volume = {"x": 235.0, "y": 235.0, "z": 250.0}
        self.sonde_ok = False         # M43 S a vu la BLTouch et l'appui
        self.ongle_ok = False         # la tete s'est arretee sur la tige
        # Sens du moteur Z confirme au toucher (2026-10-16 : firmware compile
        # avec INVERT_Z_DIR a l'envers, les « descentes » montaient en butee
        # haute ; le test a l'ongle ne pouvait pas le voir).
        self.sens_ok = False
        self.sens_attente = False
        # etape origine : ecart buse-verre mesure au metre et tape (mm)
        self.saisie = None
        # Position interne de Marlin juste (apres un G28 Z reussi) : une
        # origine directe suffit alors pour la refaire (apres M851).
        self.natif_valide = False
        # etapes reussies : Entree passe a la suite au lieu de refaire
        # (16/10 : Entree relancait les dix mesures en boucle)
        self.m48_ok = self.molettes_ok = self.carte_ok = False
        self.xy_origine = self.z_origine = False
        # etape 1
        self.visite = None
        self.element = -1
        # etape 4
        self.tige_sortie = False
        self.ongle_pret = False
        # etape 8
        self.molette = None           # etat partage avec le fil de mesure
        self.mesurer_maintenant = threading.Event()
        self.passer_coin = threading.Event()
        # etape 9
        self.hauteur = None
        self.pas_h = PAS_HAUTEUR_DEFAUT
        self.decalage_fait = False
        self.butees_coupees = False
        self.propose = None

    # -- parole --------------------------------------------------------------
    def journal(self, texte):
        try:
            with open(JOURNAL, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {texte}\n")
        except OSError:
            pass

    def dire(self, texte, garder=True):
        """Tout de suite, en coupant ce qui se dit (touches, guidage des molettes)."""
        if texte:
            self.journal(f"DIT {texte}")
            if garder:
                self.historique.append(texte)
            self._out(f"[{texte}]\n")
            self._say(texte)

    def annoncer(self, texte):
        """A la suite des annonces precedentes (resultats des taches de fond)."""
        if texte:
            self.journal(f"ANNONCE {texte}")
            self.historique.append(texte)
            self._out(f"[{texte}]\n")
            self._file.put(texte)

    def repeter(self):
        if not self.historique:
            return dire("imprimante.cal.rien_a_repeter")
        self.rappel = min(self.rappel + 1, len(self.historique))
        texte = self.historique[-self.rappel]
        if self.rappel == len(self.historique) and self.rappel > 1:
            texte = dire("imprimante.cal.plus_ancien") + " " + texte
        return texte

    def _parleur(self):
        while True:
            texte = self._file.get()
            try:
                self._say_wait(texte)
            except Exception as exc:
                print(f"[calibrage] voix : {exc!r}", file=sys.stderr)

    def _vider_annonces(self):
        while not self._file.empty():
            try:
                self._file.get_nowait()
            except queue.Empty:
                break

    # -- taches de fond et arret ---------------------------------------------
    def _fond(self, quoi, travail):
        self.occupe = quoi
        self.arret.clear()

        def run():
            try:
                if self.flux.vivant:
                    self._synchro()
                texte = travail()
            except Arrete:
                texte = ""
            except RuntimeError as exc:   # phrase deja prete (echec du firmware, tige, file)
                texte = str(exc)
            except op.Erreur as exc:
                texte = dire("imprimante.cal.octoprint_muet") if exc.code == "injoignable" \
                    else dire("imprimante.cal.refus_octoprint")
            except Exception as exc:  # ne jamais tuer la console
                print(f"[calibrage] erreur en tache de fond : {exc!r}", file=sys.stderr)
                texte = dire("imprimante.cal.erreur_interne")
            finally:
                self.occupe, self.mouvement = None, None
            if not self.fige:
                self.annoncer(texte)
        threading.Thread(target=run, daemon=True).start()

    def _verifier_arret(self):
        if self.arret.is_set() or self.fige:
            raise Arrete()

    def arreter(self):
        """ESPACE. Mouvement ordinaire : M410 puis remontee, l'imprimante reste
        allumee. Pendant le G28 Z court : M112, seul arret possible."""
        if self.mouvement == "g28":
            return self.urgence()
        self.arret.set()
        self.mesurer_maintenant.set()
        try:
            op.commande("M410")
        except op.Erreur:
            return dire("imprimante.cal.octoprint_muet")
        self._vider_annonces()
        threading.Thread(target=self._apres_arret, daemon=True).start()
        return dire("imprimante.cal.arret_doux")

    def _ranger(self):
        """Gcodes pour ranger la tige. M402 remonte d'abord la tete a 5,5 mm
        au-dessus du zero INTERNE de Marlin (probe.cpp do_z_raise, position
        native) ; G92 ne change pas ce zero interne (decalage d'espace de
        travail, G92.cpp). Apres nos descentes sans origine, Marlin se croit
        jusqu'a 24 cm sous le plateau : M402 remontait d'autant (16/10 18:48,
        20:35, 21:02). Sans origine valable : commande directe du servo, qui
        ne bouge rien (Marlin croit la sonde encore sortie ; M401 ne bougera
        donc rien non plus, et le G28 Z la rangera proprement)."""
        return ["M402"] if self.z_origine else ["M280 P0 S90"]

    def _apres_arret(self):
        """M410 fait : sortie de l'etat « stoppe » si une mesure a echoue,
        puis la tete remonte et tout revient a la normale."""
        time.sleep(0.8)
        try:
            gcodes = []
            if any(STOP_RE.search(ligne) for _, ligne in list(self.flux._lignes)[-40:]):
                gcodes.append("M999")
            gcodes += ["M121", "G91", f"G1 Z{RECUL_MM} F300", "G90", "M211 S1"] + self._ranger()
            op.commande(*gcodes)
        except op.Erreur:
            return
        self.butees_coupees = False
        self.hauteur = None
        self.annoncer(dire("imprimante.cal.arret_remonte", mm=RECUL_MM))

    def urgence(self):
        try:
            op.commande("M112")
        except op.Erreur:
            return dire("imprimante.cal.octoprint_muet")
        self.fige = True
        self.natif_valide = False
        self.arret.set()
        self.flux.sonder(0)
        self._vider_annonces()
        return dire("imprimante.cal.urgence")

    def _echec(self, ligne):
        """Message d'echec du firmware, en clair."""
        low = ligne.lower()
        if "past bed" in low:
            return dire("imprimante.cal.echec_sonde_hors_plateau")
        if "origine" in low or "homing" in low:
            self.fige = True  # Marlin s'arrete (kill) apres un echec d'origine
            return dire("imprimante.cal.echec_origine")
        if "sonde" in low or "probing" in low or "palpage" in low:
            return dire("imprimante.cal.echec_mesure")
        if "stow" in low or "deploy" in low or "bltouch" in low:
            return dire("imprimante.cal.echec_tige")
        if "halted" in low or "kill" in low:
            self.fige = True
            return dire("imprimante.cal.echec_fige")
        return dire("imprimante.cal.echec_autre", texte=ligne)

    def _executer(self, gcodes, limite, annonce=None, alerte=None, pas=1.0, reperes=None,
                  annonce_sec=APPROCHE_ANNONCE_SEC):
        """Envoie gcodes suivis de M118 <jeton> : Marlin repete le jeton une
        fois tout le reste traite. Seules les lignes recues AVANT cet echo
        repondent a ces gcodes (16/10, 20:10 : la reponse d'un essai
        precedent, encore en file, avait ete prise pour celle de la descente).
        Rend (lignes, contact_vu, termine). Leve Arrete sur ESPACE,
        RuntimeError (phrase) sur echec du firmware. reperes : {texte: None}
        rempli avec l'heure d'arrivee de chaque « echo:texte » (a pas pres)."""
        jeton = "cal" + uuid.uuid4().hex[:10]
        jeton_re = re.compile(r"Recv:\s*echo:\s*" + jeton)
        depuis = self.flux.numero
        op.commande(*gcodes, f"M118 E1 {jeton}")
        debut = derniere = time.time()
        lignes, vu_contact, alerte_dite = [], False, False
        while True:
            found, vues, ligne, n = self.flux._attendre(jeton_re, depuis, pas)
            lignes += vues
            if reperes:
                for v in vues:
                    for cle in reperes:
                        if reperes[cle] is None and f"echo:{cle}" in v:
                            reperes[cle] = time.time()
            if any(HIT_RE.search(v) for v in vues):
                vu_contact = True
            if self.arret.is_set() or self.fige:
                raise Arrete()   # ESPACE : la fin du mouvement ne veut rien dire
            if ligne:
                raise RuntimeError(self._echec(ligne))
            if found:
                return lignes, vu_contact, True
            depuis = n
            self._verifier_arret()
            ecoule = time.time() - debut
            if alerte and not alerte_dite and ecoule > alerte[0]:
                alerte_dite = True
                self.dire(alerte[1])
            if ecoule > limite:
                return lignes, vu_contact, False
            if annonce and time.time() - derniere >= annonce_sec:
                derniere = time.time()
                self.dire(annonce(ecoule), garder=False)

    def _suivre(self, gcodes, fin_re, limite, annonce=None, alerte=None, contact=False,
                annonce_sec=APPROCHE_ANNONCE_SEC):
        """Comme _executer ; rend (derniere ligne fin_re de CES gcodes, ou None
        si la limite est depassee ; contact_vu)."""
        lignes, vu_contact, termine = self._executer(gcodes, limite, annonce, alerte, annonce_sec=annonce_sec)
        if not termine:
            return None, vu_contact
        fin = None
        for ligne in lignes:
            fin = fin_re.search(ligne) or fin
        return fin, vu_contact

    def _synchro(self):
        """Attend la fin des commandes encore en file (un essai precedent
        abandonne, un mouvement long) avant d'en envoyer de nouvelles."""
        _, _, termine = self._executer([], SYNCHRO_SEC)
        if not termine:
            raise RuntimeError(dire("imprimante.cal.file_occupee"))

    def _attendre_capteur(self, gcodes):
        """z_min lu une fois (M119 ponctuel, jamais en boucle)."""
        lignes, _, termine = self._executer(gcodes + ["M119"], 60)
        if not termine:
            raise RuntimeError(dire("imprimante.sans_reponse"))
        return next((m.group(2).lower() for m in map(op.ENDSTOP_RE.search, lignes)
                     if m and m.group(1).lower() == "z_min"), None)

    # -- etape 1 : decouverte -------------------------------------------------
    def entrer_decouverte(self):
        data = decouverte.charger()
        if data and data.get("confirmations"):
            return dire("imprimante.cal.decouverte_deja")
        return dire("imprimante.cal.decouverte_consigne")

    def lancer_decouverte(self, force=False):
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)

        def travail():
            data, origine = decouverte.visite(self.machine, self.volume, self.version, force=force)
            self.visite, self.element = data, -1
            debut = dire("imprimante.cal.decouverte_secours") if origine == "secours" else ""
            return " ".join(t for t in (debut, data.get("presentation", ""),
                                        dire("imprimante.cal.decouverte_suite",
                                             n=len(data["elements"]))) if t)
        self._fond(dire("imprimante.cal.occupe_decouverte"), travail)
        return dire("imprimante.cal.decouverte_lancee")

    @staticmethod
    def _phrase(texte):
        """Morceau de description en phrase : majuscule et point final."""
        texte = (texte or "").strip()
        if not texte:
            return ""
        texte = texte[0].upper() + texte[1:]
        return texte if texte[-1] in ".!?" else texte + "."

    def _dire_element(self):
        e = self.visite["elements"][self.element]
        parts = [dire("imprimante.cal.element", n=self.element + 1, total=len(self.visite["elements"]), nom=e["nom"]),
                 self._phrase(e["ou"]), self._phrase(e["au_toucher"])]
        if e.get("role"):
            parts.append(dire("imprimante.cal.element_role", role=e["role"]))
        if e.get("prudence"):
            parts.append(dire("imprimante.cal.element_prudence", prudence=e["prudence"]))
        parts.append(dire("imprimante.cal.element_touches"))
        return " ".join(p for p in parts if p)

    def element_suivant(self, trouve=None):
        if trouve is not None and 0 <= self.element < len(self.visite["elements"]):
            nom = self.visite["elements"][self.element]["nom"]
            decouverte.confirmer(self.visite, nom, trouve)
            self.journal(f"DECOUVERTE {nom} : {'trouve' if trouve else 'pas trouve'}")
        self.element += 1
        if self.element >= len(self.visite["elements"]):
            manques = [n for n, c in self.visite.get("confirmations", {}).items() if not c["trouve"]]
            fin = dire("imprimante.cal.decouverte_finie")
            if manques:
                fin += " " + dire("imprimante.cal.decouverte_manques", liste=", ".join(manques))
            if self.visite.get("variantes"):
                fin += " " + self.visite["variantes"]
            return fin + " " + dire("imprimante.cal.suite_entree")
        return self._dire_element()

    # -- etape 2 : verifications --------------------------------------------
    def verifs(self):
        """Rend (bloquant, phrase)."""
        try:
            e = op.etat()
        except op.Erreur as exc:
            return True, dire({"cle": "imprimante.erreur.cle", "refus": "imprimante.erreur.refus"}.get(
                exc.code, "imprimante.cal.octoprint_muet"))
        if not e["connecte"]:
            if e["texte"].startswith(("Offline after error", "Error")):
                return True, dire("imprimante.cal.liaison_apres_erreur")
            return True, dire("imprimante.cal.liaison_fermee")
        if any(e["flags"].get(f) for f in ("printing", "paused", "pausing", "cancelling")):
            return True, dire("imprimante.impression_en_cours_refus")
        problemes, infos = [], []
        for cle, nom in (("buse", "imprimante.buse"), ("plateau", "imprimante.plateau")):
            reelle, cible = e[cle]
            if reelle is None or reelle < 5 or reelle > 290:
                problemes.append(dire("imprimante.cal.thermistance", nom=dire(nom),
                                      valeur=round(reelle) if reelle is not None else "?"))
            elif cible:
                infos.append(dire("imprimante.cal.chauffe_allumee", nom=dire(nom), reelle=round(reelle),
                                  cible=round(cible)))
            elif cle == "buse" and reelle > 45:
                problemes.append(dire("imprimante.cal.buse_chaude", valeur=round(reelle)))
        if not self.flux.vivant:
            problemes.append(dire("imprimante.cal.flux_absent"))
        try:
            self.machine = op.detecter_machine(force=True)
        except Exception:
            self.machine = {}
        self.caps = self.machine.get("caps") or {}
        if self.flux.vivant:
            found, lues, _ = self.flux.envoyer(["M115"], re.compile(r"Cap:EMERGENCY_PARSER"), 8, echec=False)
            for ligne in lues:
                m = re.search(r"Cap:([A-Z_]+):([01])", ligne)
                if m:
                    self.caps[m.group(1)] = m.group(2)
                m = re.search(r"FIRMWARE_NAME:Marlin ([\w.]+)", ligne)
                if m:
                    self.version = m.group(1)
                m = re.search(r"MACHINE_TYPE:([^:]+?) EXTRUDER", ligne)
                if m:
                    self.machine["modele"] = m.group(1).strip()
            volume = op.limites_firmware(self.flux)
            if volume:
                self.volume = volume
        if self.caps.get("EMERGENCY_PARSER") != "1":
            problemes.append(dire("imprimante.cal.sans_arret_doux"))
        if not self.machine.get("z_probe"):
            problemes.append(dire("imprimante.cal.sonde_absente"))
        if self.caps.get("AUTOLEVEL") != "1":
            infos.append(dire("imprimante.cal.sans_carte"))
        off = self.machine.get("probe_offset") or {}
        infos.insert(0, dire("imprimante.cal.sonde_presente", gauche=_fr(-off.get("x", 0), 0),
                             avant=_fr(-off.get("y", 0), 0), hauteur=_fr(off.get("z", 0))))
        if problemes:
            return True, " ".join([dire("imprimante.cal.verifs_problemes")] + problemes + infos)
        return False, " ".join([dire("imprimante.cal.verifs_ok")] + infos + [dire("imprimante.cal.suite_entree")])

    # -- etape 3 : test de la sonde (M43 S) ----------------------------------
    def test_sonde(self):
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        self.flux.sonder(0)

        def travail():
            self.mouvement = None   # rien ne bouge : la tige seulement
            depuis = self.flux.numero
            op.commande("M43 S")
            fin = time.time() + 60
            appui_demande = False
            resultat = []
            while time.time() < fin:
                found, vues, _, n = self.flux._attendre(M43S_FIN_RE, depuis, 1, echec=False)
                for ligne in vues:
                    if "Please trigger probe" in ligne and not appui_demande:
                        appui_demande = True
                        self.dire(dire("imprimante.cal.sonde_pousse"))
                    if "BLTouch Classic" in ligne:
                        self.dire(dire("imprimante.cal.sonde_reconnue"))
                    m = re.search(r"Pulse width \(\+/- 4ms\): (\d+)|Pulse width: 30ms", ligne)
                    if m:
                        resultat.append(dire("imprimante.cal.sonde_impulsion", ms=m.group(1) or "30"))
                depuis = n
                if found:
                    ligne = found.string
                    self.journal(f"M43 S : {ligne}")
                    if "V3.1" in ligne or "pre V3.1" in ligne:
                        self.sonde_ok = True
                        modele = dire("imprimante.cal.sonde_v31" if "= BLTouch V3.1" in ligne
                                      else "imprimante.cal.sonde_ancienne")
                        return " ".join(resultat + [modele, dire("imprimante.cal.sonde_ok"),
                                                    dire("imprimante.cal.suite_entree")])
                    if "No trigger" in ligne:
                        return dire("imprimante.cal.sonde_rien")
                    if "Noise" in ligne:
                        return dire("imprimante.cal.sonde_parasite")
                    return dire("imprimante.cal.sonde_echec")
            return dire("imprimante.sans_reponse")
        self._fond(dire("imprimante.cal.occupe_sonde"), travail)
        return dire("imprimante.cal.sonde_lance")

    # -- etape 4 : hauteur de la sonde (au toucher) ----------------------------
    def tige(self, sortir):
        try:
            op.commande("M280 P0 S10" if sortir else "M280 P0 S90")
        except op.Erreur:
            return dire("imprimante.cal.octoprint_muet")
        self.tige_sortie = sortir
        return dire("imprimante.cal.tige_sortie_toucher" if sortir else "imprimante.cal.tige_rangee_toucher")

    # -- etape 5 : sens du moteur Z ---------------------------------------------
    def test_sens(self):
        """Monte de 10 mm puis redescend ; la personne dit si la tete a monte."""
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)

        def travail():
            self.mouvement = "doux"
            try:
                self._suivre(["M211 S0", "G91", "G1 Z10 F300", "G4 P1500", "G1 Z-10 F300", "G90", "M211 S1", "M400",
                              "M114"], op.COUNT_RE, 30)
            finally:
                self.mouvement = None
            self.sens_attente = True
            return dire("imprimante.cal.sens_question")
        self._fond(dire("imprimante.cal.occupe_sens"), travail)
        return dire("imprimante.cal.sens_lance")

    # -- etape 6 : test a l'ongle -----------------------------------------------
    def monter(self, mm):
        """Remonte la tete (hauteur inconnue avant l'origine : la personne
        a dit qu'elle est basse, il y a de la place au-dessus)."""
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        try:
            op.commande("M211 S0", "G91", f"G1 Z{mm} F300", "G90", "M211 S1")
        except op.Erreur:
            return dire("imprimante.cal.octoprint_muet")
        return dire("imprimante.cal.tete_monte", mm=mm)

    def _descente(self, mm, vitesse, annonce=None, avant=None):
        """Tige sortie, butees actives, la tete descend d'au plus mm. Rend
        True si la sonde a arrete la tete (ligne « endstops hit »), False si
        elle est allee au bout. Butees logicielles coupees : avant l'origine
        Z, Marlin ne sait pas ou est le plateau.

        La tige sort par M401, JAMAIS par M280 P0 S10 : avec la sonde sur la
        prise Z, Marlin ne surveille la prise que si SA sonde est deployee
        (endstops.cpp 2.1.2.5, l. 1190 : z_probe_enabled). Le 16/10, tige
        sortie par M280, l'ongle n'a pas arrete la tete. M401 remonte la
        tete de 5 mm avant de sortir la tige (Z_CLEARANCE_DEPLOY_PROBE)."""
        # M401 met Marlin en surveillance de la sonde ; il ne bouge rien si
        # Marlin la croit deja sortie (rangements par M280, voir _ranger).
        # Le M280 S10 qui suit sort la tige pour de bon dans tous les cas.
        etat = self._attendre_capteur(["M280 P0 S160", "G4 P300", "M401", "M280 P0 S10", "G4 P600"])
        self.journal(f"DESCENTE tige sortie : z_min={etat}")
        if etat != "open":
            op.commande(*self._ranger())
            raise RuntimeError(dire("imprimante.cal.tige_bloquee"))
        if avant:
            self.dire(avant)
        self.mouvement = "doux"
        try:
            # M119 juste apres la descente : la BLTouch range d'elle-meme sa tige
            # quand elle touche. Tige rangee (z_min TRIGGERED) = contact. On ne
            # se fie pas au seul « endstops hit » : Marlin ne l'ecrit que si le
            # contact differe du precedent (endstops.cpp, event_handler) ; le
            # 16/10 a 20:16 l'ongle avait arrete la tete, sans ce message.
            # Contact = mouvement ecourte. Deux reperes M118 encadrent la
            # descente ; butees logicielles coupees, seules la sonde ou ESPACE
            # (Arrete) peuvent la raccourcir. Ni M119 (il ressort la tige
            # avant de la lire), ni le seul « endstops hit » (Marlin ne
            # l'ecrit que si le contact differe du precedent, 16/10 20:16).
            top = "d" + uuid.uuid4().hex[:8]
            fin_mvt = "f" + uuid.uuid4().hex[:8]
            reperes = {top: None, fin_mvt: None}
            lignes, hit, termine = self._executer(
                ["M211 S0", "M120", "G91", f"M118 E1 {top}", f"G1 Z-{mm} F{vitesse}", "G90", "M400",
                 f"M118 E1 {fin_mvt}", "M121", "M114"], mm * 60 / vitesse + 30, annonce=annonce, pas=0.1,
                reperes=reperes)
        finally:
            self.mouvement = None
        fin = termine
        prevue = mm * 60 / vitesse
        duree = (reperes[fin_mvt] - reperes[top]) if reperes[top] and reperes[fin_mvt] else None
        ecourte = duree is not None and prevue - duree > ECOURTE_SEC
        contact = hit or ecourte
        self.journal(f"DESCENTE endstops_hit={hit} duree={duree if duree is None else round(duree, 2)} s "
                     f"prevue={prevue:.1f} s")
        # Apres un arret sur la sonde, Marlin garde la position VISEE (vu le
        # 14/10 : Z:139,5 annonce, Count Z:0) ; sans contact, il se croit
        # sous le plateau. Dans les deux cas on declare ici une hauteur
        # nulle (contact de la tige, ou buse au verre) puis on remonte de
        # 10 mm : le rangement de la tige ne bouge alors plus la tete.
        self.z_origine = False
        op.commande("M121", "G92 Z0", "G91", "G1 Z10 F300", "G90", "M211 S1", *self._ranger())
        self.journal(f"DESCENTE fin={bool(fin)} contact={contact}")
        if not contact:
            self.natif_valide = False   # la buse a pu appuyer et le moteur sauter des pas
        if not fin:
            raise RuntimeError(dire("imprimante.sans_reponse"))
        return contact

    def test_ongle(self):
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        if not self.sens_ok:
            return dire("imprimante.cal.sens_dabord")
        if not self.ongle_pret:
            self.ongle_pret = True
            return dire("imprimante.cal.ongle_hauteur")
        self.flux.sonder(0)

        def travail():
            if self._descente(ONGLE_MM, ONGLE_VITESSE, avant=dire("imprimante.cal.ongle_go")):
                self.ongle_ok = True
                return dire("imprimante.cal.ongle_ok") + " " + dire("imprimante.cal.suite_entree")
            return dire("imprimante.cal.ongle_rien")
        self._fond(dire("imprimante.cal.occupe_ongle"), travail)
        return dire("imprimante.cal.ongle_lance")

    # -- etape 6 : origine -----------------------------------------------------
    def _point_sonde_centre(self):
        off = self.machine.get("probe_offset") or {}
        x = self.volume["x"] / 2 - off.get("x", 0)
        y = self.volume["y"] / 2 - off.get("y", 0)
        return max(5.0, min(self.volume["x"] - 5, x)), max(5.0, min(self.volume["y"] - 5, y))

    def origines(self):
        """Premier temps : gauche et fond, sonde au-dessus du centre, puis la
        personne mesure l'ecart buse-verre et le tape (saisie)."""
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        if not (self.sonde_ok and self.ongle_ok and self.sens_ok):
            return dire("imprimante.cal.origine_refusee")
        self.flux.sonder(0)

        def travail():
            self.mouvement = "doux"
            try:
                fin, _ = self._suivre(["G28 X Y", "M400", "M114"], op.COUNT_RE, HOMING_XY_SEC)
                if not fin:
                    return dire("imprimante.cal.sans_reponse_xy")
                self.xy_origine = True
                x, y = self._point_sonde_centre()
                self._suivre(["G90", f"G1 X{x:.1f} Y{y:.1f} F3000", "M400", "M114"], op.COUNT_RE, 60)
            finally:
                self.mouvement = None
            self.saisie = ""
            return dire("imprimante.cal.origine_mesure")
        self._fond(dire("imprimante.occupe_origine"), travail)
        return dire("imprimante.cal.origines_lancees")

    def saisir(self, key):
        """Chiffres de l'ecart buse-verre, en millimetres."""
        if key.isdigit() and len(self.saisie) < 3:
            self.saisie += key
            return key
        if key in ("\x7f", "\x08"):
            self.saisie = self.saisie[:-1]
            return dire("imprimante.cal.saisie_efface", reste=self.saisie or dire("imprimante.cal.saisie_vide"))
        if key in ENTER:
            if not self.saisie or not 3 <= int(self.saisie) <= APPROCHE_MAX_MM:
                self.saisie = ""
                return dire("imprimante.cal.saisie_invalide", maxi=APPROCHE_MAX_MM)
            mm, self.saisie = int(self.saisie), None
            return self.approche(mm)
        return dire("imprimante.cal.origine_mesure")

    def approche(self, ecart):
        """Second temps : descente lente d'au plus l'ecart mesure + 2 mm,
        jusqu'au contact de la tige, puis G28 Z court."""
        course = min(APPROCHE_MAX_MM, ecart + APPROCHE_MARGE_MM)
        self.journal(f"APPROCHE ecart tape={ecart} mm, course={course} mm")

        def travail():
            debut = time.time()
            if not self._descente(course, APPROCHE_VITESSE,
                                  annonce=lambda s: dire("imprimante.cal.approche_progres",
                                                         mm=round(s * APPROCHE_VITESSE / 60))):
                return dire("imprimante.cal.approche_rien", mm=course)
            self.journal(f"APPROCHE contact apres {time.time() - debut:.1f} s")
            # Contact : la buse est a quelques mm du plateau. G28 Z ne
            # parcourt plus que ~15 mm (remontee de 10 mm, puis descente).
            # La fin de l'approche (rangement de la tige, remontee de 10 mm)
            # doit etre finie AVANT de lancer le chronometre du G28 Z (16/10,
            # 20:35 : le M402 encore en cours avait mange tout le delai).
            self._synchro()
            self.annoncer(dire("imprimante.cal.z_court_lance"))
            self.mouvement = "g28"
            try:
                # Tige ressortie pour de bon (Marlin la croit deja sortie et ne
                # la sortirait pas lui-meme) ; R0 : pas de remontee avant
                # l'origine (elle se ferait depuis le zero interne faux).
                fin, _ = self._suivre(["M280 P0 S160", "G4 P300", "M280 P0 S10", "G4 P600", "G28 Z R0", "M400",
                                       "M114"], op.COUNT_RE, G28Z_LIMITE_SEC,
                                      alerte=(G28Z_ALERTE_SEC, dire("imprimante.cal.z_long")))
            finally:
                self.mouvement = None
            if not fin:
                self.urgence()
                return dire("imprimante.cal.z_urgence_auto")
            self.z_origine = self.natif_valide = True
            op.commande("G90", "G1 Z10 F600")
            z = float(fin.group(3))
            self.journal(f"ORIGINE Z faite : Z{z}")
            return dire("imprimante.cal.origines_faites", z=_fr(z, 1)) + " " + dire("imprimante.cal.suite_entree")
        self._fond(dire("imprimante.occupe_origine"), travail)
        return dire("imprimante.cal.approche_lancee", mm=course)

    def reorigine(self):
        """Origine a refaire (reglage de la sonde change, arret) : si Marlin
        connait encore sa position, G28 Z direct, quelques secondes ; sinon
        la mise a l'origine complete (mesure au metre)."""
        if not self.natif_valide:
            return self.origines()
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)

        def travail():
            self.mouvement = "g28"
            try:
                fin, _ = self._suivre(["G28 Z", "M400", "M114"], op.COUNT_RE, G28Z_LIMITE_SEC,
                                      alerte=(G28Z_ALERTE_SEC, dire("imprimante.cal.z_long")))
            finally:
                self.mouvement = None
            if not fin:
                self.urgence()
                return dire("imprimante.cal.z_urgence_auto")
            self.z_origine = True
            op.commande("G90", "G1 Z10 F600")
            self.journal(f"ORIGINE Z refaite directement : Z{fin.group(3)}")
            return dire("imprimante.cal.reorigine_faite") + " " + dire("imprimante.cal.suite_entree")
        self._fond(dire("imprimante.occupe_origine"), travail)
        return dire("imprimante.cal.reorigine_lancee")

    # -- etape 7 : repetabilite (M48) -------------------------------------------
    def repetabilite(self):
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        if not self.z_origine:
            return dire("imprimante.cal.sans_origine")
        x, y = self.volume["x"] / 2, self.volume["y"] / 2

        def travail():
            self.mouvement = "doux"
            try:
                fin, _ = self._suivre([f"M48 P10 X{x:.1f} Y{y:.1f} V2"], SIGMA_RE, M48_SEC)
            finally:
                self.mouvement = None
            if not fin:
                return dire("imprimante.sans_reponse")
            sigma = float(fin.group(1))
            ecart = next((float(m.group(1)) for m in map(M48_RE.search, reversed([l for _, l in self.flux._lignes]))
                          if m), sigma * 4)
            self.journal(f"M48 ecart={ecart:.4f} sigma={sigma:.5f}")
            op.commande("G90", "G1 Z10 F600")
            microns = round(ecart * 1000)
            if ecart <= M48_BON_MM:
                self.m48_ok = True
                return dire("imprimante.cal.m48_bon", microns=microns) + " " + dire("imprimante.cal.suite_entree")
            if ecart <= M48_MOYEN_MM:
                return dire("imprimante.cal.m48_moyen", microns=microns)
            return dire("imprimante.cal.m48_mauvais", microns=microns)
        self._fond(dire("imprimante.cal.occupe_m48"), travail)
        return dire("imprimante.cal.m48_lance")

    # -- etape 8 : molettes, guidage continu -------------------------------------
    def _mesurer(self, x, y):
        self.mouvement = "doux"
        try:
            fin, _ = self._suivre([f"G30 X{x:.1f} Y{y:.1f}"], op.BED_PROBE_RE, G30_SEC)
        finally:
            self.mouvement = None
        if not fin:
            raise RuntimeError(dire("imprimante.cal.mesure_muette", point=dire("imprimante.cal.ce_point")))
        return float(fin.group(3))

    def _pause_reglage(self):
        """Laisse le temps de tourner ; Entree remesure tout de suite."""
        self.mesurer_maintenant.wait(MOLETTE_PAUSE_SEC)
        self.mesurer_maintenant.clear()
        self._verifier_arret()

    def _consigne(self, ecart, conv):
        """Phrase : quel sens tourner et de combien, pour un ecart (mm,
        positif = coin trop haut)."""
        descendre = ecart > 0
        serrer = (conv["serrer"] == "descend") == descendre
        return dire("imprimante.cal.molette_serre" if serrer else "imprimante.cal.molette_desserre",
                    tours=_tours(ecart, conv["mm_par_tour"]))

    def _apprendre_sens(self, nom, x, y, avant):
        """Premier coin : un quart de tour « en serrant », on mesure l'effet."""
        for _ in range(3):
            self.dire(dire("imprimante.cal.apprendre_sens"))
            self.mesurer_maintenant.clear()
            while not self.mesurer_maintenant.wait(0.5):
                self._verifier_arret()
            self.mesurer_maintenant.clear()
            apres = self._mesurer(x, y)
            bouge = apres - avant
            self.journal(f"APPRENTISSAGE {nom} : {avant:.3f} -> {apres:.3f}")
            if abs(bouge) >= APPRENTISSAGE_MIN_MM:
                conv = {"serrer": "descend" if bouge < 0 else "monte",
                        "mm_par_tour": max(0.3, min(1.5, abs(bouge) / 0.25)),
                        "date": time.strftime("%Y-%m-%d %H:%M")}
                _ecrire_json(MOLETTES, conv)
                self.dire(dire("imprimante.cal.sens_appris_descend" if bouge < 0 else "imprimante.cal.sens_appris_monte",
                               mm=_fr(abs(bouge))))
                return conv, apres
            self.dire(dire("imprimante.cal.sens_rien"))
            avant = apres
        raise RuntimeError(dire("imprimante.cal.sens_echec"))

    def _regler_coin(self, coin, centre, conv):
        """Guidage d'un coin jusqu'a deux mesures de suite dans la tolerance.
        Rend la convention (apprise au premier coin)."""
        nom = NOMS_POINTS[coin]
        x, y = POINTS_MOLETTES[coin]
        self.molette = coin
        self.passer_coin.clear()
        ecart = round(self._mesurer(x, y) - centre, 3)
        self.journal(f"MOLETTE {coin} ecart={ecart:+.3f}")
        if abs(ecart) <= TOLERANCE_MM:
            self.dire(dire("imprimante.cal.coin_deja_bon", point=nom))
            return conv
        self.dire(dire("imprimante.cal.coin_ecart_haut" if ecart > 0 else "imprimante.cal.coin_ecart_bas",
                       point=nom, mm=_fr(abs(ecart))))
        if not conv:
            conv, z = self._apprendre_sens(nom, x, y, ecart + centre)
            ecart = round(z - centre, 3)
        self.dire(self._consigne(ecart, conv))
        bons, precedent, immobile = 0, ecart, 0
        while not self.passer_coin.is_set():
            self._pause_reglage()
            ecart = round(self._mesurer(x, y) - centre, 3)
            self.journal(f"MOLETTE {coin} ecart={ecart:+.3f}")
            if abs(ecart) <= TOLERANCE_MM:
                bons += 1
                if bons >= 2:
                    self.dire(dire("imprimante.cal.coin_regle", point=nom))
                    return conv
                self.dire(dire("imprimante.cal.stop_verifie"))
                continue
            bons = 0
            if (ecart > 0) != (precedent > 0) and abs(precedent) > TOLERANCE_MM:
                self.dire(dire("imprimante.cal.depasse") + " " + self._consigne(ecart, conv))
                immobile = 0
            elif abs(ecart) < abs(precedent) - 0.015:
                cle = "imprimante.cal.presque" if abs(ecart) < 0.15 else "imprimante.cal.continue"
                self.dire(dire(cle, mm=_fr(abs(ecart))))
                immobile = 0
            elif abs(ecart) > abs(precedent) + 0.015:
                self.dire(dire("imprimante.cal.mauvais_sens") + " " + self._consigne(ecart, conv))
                immobile = 0
            else:
                immobile += 1
                if immobile >= 2:
                    self.dire(self._consigne(ecart, conv))
                    immobile = 0
            precedent = ecart
        self.dire(dire("imprimante.cal.coin_passe", point=nom))
        return conv

    def molettes(self):
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        if not self.z_origine:
            return dire("imprimante.cal.sans_origine")
        cx, cy = self.volume["x"] / 2, self.volume["y"] / 2

        def travail():
            conv = _lire_json(MOLETTES) or None
            try:
                for passe in range(MOLETTE_PASSES):
                    centre = self._mesurer(cx, cy)
                    self.journal(f"MOLETTES passe {passe + 1}, centre={centre:.3f}")
                    if passe:
                        ecarts = {c: round(self._mesurer(*POINTS_MOLETTES[c]) - centre, 3) for c in COINS}
                        self.journal(f"VERIFICATION {ecarts}")
                        a_reprendre = [c for c in COINS if abs(ecarts[c]) > TOLERANCE_MM]
                        if not a_reprendre:
                            self.molettes_ok = True
                            return dire("imprimante.cal.plateau_ok") + " " + dire("imprimante.cal.suite_entree")
                        self.dire(dire("imprimante.cal.reprise",
                                       liste=", ".join(NOMS_POINTS[c] for c in a_reprendre)))
                    else:
                        a_reprendre = COINS
                    for coin in a_reprendre:
                        conv = self._regler_coin(coin, centre, conv)
                    self.dire(dire("imprimante.cal.verification_tour"))
                return dire("imprimante.cal.plateau_pas_fini")
            except RuntimeError as exc:
                return str(exc)
            finally:
                self.molette = None
                try:
                    op.commande("G90", "G1 Z10 F600")
                except op.Erreur:
                    pass
        self._fond(dire("imprimante.cal.occupe_molettes"), travail)
        return dire("imprimante.cal.molettes_lance")

    # -- etape 9 : hauteur de la buse (feuille) ----------------------------------
    def entrer_hauteur(self):
        if not self.z_origine:
            return dire("imprimante.cal.sans_origine")
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        x, y = self.volume["x"] / 2, self.volume["y"] / 2

        def travail():
            self.butees_coupees = True
            self.mouvement = "doux"
            try:
                fin, _ = self._suivre(["M211 S0", "G90", "G1 Z5 F600", f"G1 X{x:.1f} Y{y:.1f} F3000",
                                       "G1 Z0 F300", "M400", "M114"], op.COUNT_RE, 60)
            finally:
                self.mouvement = None
            if not fin:
                return dire("imprimante.sans_reponse")
            self.hauteur = 0.0
            return dire("imprimante.cal.hauteur_prete", pas=_fr(PAS_HAUTEUR[self.pas_h]))
        self._fond(dire("imprimante.cal.occupe_hauteur"), travail)
        return dire("imprimante.cal.hauteur_lancee")

    def bouger_z(self, sens, pas):
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        cible = round(self.hauteur + sens * pas, 3)
        # Jamais plus bas que le decalage minimal admis (-4 mm) : borne
        # calculee depuis le decalage actuel (16/10 : sonde descendue de
        # 2,5 mm, une borne fixe a -2 mm empechait d'atteindre la feuille).
        ancien = (self.machine.get("probe_offset") or {}).get("z", 0.0)
        if ancien + cible < DECALAGE_BORNES[0]:
            return dire("imprimante.cal.hauteur_limite")
        self.hauteur = cible
        self.propose = None
        try:
            op.commande("G91", f"G1 Z{sens * pas:.3f} F300", "G90")
        except op.Erreur:
            return dire("imprimante.cal.octoprint_muet")
        return dire("imprimante.cal.z_monte" if sens > 0 else "imprimante.cal.z_descend", pas=_fr(pas)) + " " + \
            dire("imprimante.cal.hauteur_actuelle", z=_fr(self.hauteur))

    def valider_hauteur(self):
        """1er Entree : nouveau decalage propose ; 2e Entree : enregistre."""
        ancien = (self.machine.get("probe_offset") or {}).get("z", 0.0)
        nouveau = round(ancien + self.hauteur, 2)
        if not DECALAGE_BORNES[0] <= nouveau <= DECALAGE_BORNES[1]:
            return dire("imprimante.cal.decalage_aberrant", valeur=_fr(nouveau))
        if self.propose != nouveau:
            self.propose = nouveau
            if abs(nouveau - ancien) < 0.015:
                op.commande("G91", "G1 Z10 F600", "G90", "M211 S1")
                self.butees_coupees, self.hauteur, self.decalage_fait, self.propose = False, None, True, None
                return dire("imprimante.cal.decalage_inchange", valeur=_fr(ancien))
            return dire("imprimante.cal.decalage_propose", nouveau=_fr(nouveau), ancien=_fr(ancien))
        op.commande(f"M851 Z{nouveau:.2f}", "M500", "G91", "G1 Z10 F600", "G90", "M211 S1")
        self.butees_coupees, self.hauteur, self.decalage_fait, self.propose = False, None, True, None
        (self.machine.setdefault("probe_offset", {}))["z"] = nouveau
        self.z_origine = False  # le zero a change : refaire l'origine avant la carte
        return dire("imprimante.cal.decalage_enregistre", valeur=_fr(nouveau))

    # -- etape 10 : carte du plateau (G29) -------------------------------------
    def carte(self):
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        if self.caps.get("AUTOLEVEL") != "1":
            return dire("imprimante.cal.sans_carte")
        if not self.z_origine:
            return dire("imprimante.cal.carte_origine")
        if self.carte_ok:
            return dire("imprimante.cal.fini")

        def travail():
            self.mouvement = "doux"
            try:
                fin, _ = self._suivre(["G29", "M400", "M500", "M420 S1 V"], re.compile(r"Recv:.*Bilinear Leveling Grid",
                                                                                       re.I), G29_SEC,
                                      annonce=lambda s: dire("imprimante.cal.carte_progres", s=round(s)),
                                      annonce_sec=CARTE_ANNONCE_SEC)
            finally:
                self.mouvement = None
            if not fin:
                return dire("imprimante.sans_reponse")
            time.sleep(1.5)
            valeurs = []
            for _, ligne in list(self.flux._lignes):
                m = GRILLE_RE.search(ligne)
                if m:
                    valeurs += [float(v) for v in m.group(2).split()]
            valeurs = valeurs[-25:]
            op.commande("G90", "G1 Z10 F600")
            self.carte_ok = True
            if not valeurs:
                return dire("imprimante.cal.carte_faite") + " " + dire("imprimante.cal.fini")
            ecart = max(valeurs) - min(valeurs)
            self.journal(f"CARTE {valeurs}")
            return dire("imprimante.cal.carte_resume", mm=_fr(ecart), haut=_fr(max(valeurs)), bas=_fr(min(valeurs))) \
                + " " + dire("imprimante.cal.fini")
        self._fond(dire("imprimante.cal.occupe_carte"), travail)
        return dire("imprimante.cal.carte_lancee")

    # -- nettoyage, navigation ------------------------------------------------
    def nettoyer(self):
        self.flux.sonder(0)
        self.passer_coin.set()
        if not self.fige:
            try:
                gcodes = ["M121"] + self._ranger()
                if self.butees_coupees:
                    gcodes = ["G91", "G1 Z5 F600", "G90", "M211 S1"] + gcodes
                op.commande(*gcodes)
            except op.Erreur:
                pass
        self.flux.arreter()

    def entrer(self, index):
        self.etape = index
        nom = ETAPES[index]
        tete = dire("imprimante.cal.etape", n=index + 1, total=len(ETAPES), nom=NOMS_ETAPES[nom])
        if nom == "decouverte":
            return f"{tete} {self.entrer_decouverte()}"
        if nom == "verifs":
            self.bloque, texte = self.verifs()
            return f"{tete} {texte}"
        cles = {"sonde": "imprimante.cal.sonde_consigne", "hauteur_sonde": "imprimante.cal.hauteur_sonde_consigne",
                "sens_z": "imprimante.cal.sens_consigne",
                "ongle": "imprimante.cal.ongle_consigne", "origine": "imprimante.cal.origine_consigne",
                "repetabilite": "imprimante.cal.m48_consigne", "molettes": "imprimante.cal.molettes_consigne",
                "hauteur": "imprimante.cal.hauteur_consigne", "carte": "imprimante.cal.carte_consigne"}
        consigne = dire(cles[nom])
        if nom == "origine" and not (self.sonde_ok and self.ongle_ok and self.sens_ok):
            consigne = dire("imprimante.cal.origine_refusee")
        if nom in ("repetabilite", "molettes", "hauteur", "carte") and not self.z_origine:
            consigne = dire("imprimante.cal.sans_origine")
        return f"{tete} {consigne}"

    def aide(self):
        return dire(f"imprimante.cal.aide_{ETAPES[self.etape]}") + " " + dire("imprimante.cal.aide_commune")

    def lire_capteurs(self):
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        etat = self._attendre_capteur([])
        c = self.flux.capteurs
        parts = [dire("imprimante.cal.butee_appuyee" if c.get(n) == "triggered" else "imprimante.cal.butee_relachee",
                      axe=n[0].upper()) for n in ("x_min", "y_min") if n in c]
        if etat:
            parts.append(dire("imprimante.cal.sonde_rangee_lue" if etat == "triggered" else "imprimante.cal.sonde_sortie_lue"))
        return " ".join(parts) or dire("imprimante.cal.capteurs_inconnus")

    def lire_temps(self):
        t = self.flux.temps
        parts = []
        for cle, nom in (("buse", "imprimante.buse"), ("plateau", "imprimante.plateau")):
            if cle in t:
                reelle, cible = t[cle]
                parts.append(dire("imprimante.temp_cible", nom=dire(nom), reelle=round(reelle), cible=round(cible))
                             if cible else dire("imprimante.temp_froide", nom=dire(nom), reelle=round(reelle)))
        return " ".join(parts) or dire("imprimante.cal.capteurs_inconnus")

    def suivante(self):
        if ETAPES[self.etape] == "verifs" and self.bloque:
            return dire("imprimante.cal.verifs_bloquent")
        if self.occupe:
            return dire("imprimante.patiente", quoi=self.occupe)
        if self.etape + 1 >= len(ETAPES):
            return None
        return self.entrer(self.etape + 1)

    def touche(self, key):
        nom = ETAPES[self.etape]
        if key == " " and self.occupe:
            return self.arreter()
        if key in ("r", "R"):
            return self.repeter()
        self.rappel = 0
        if key in ("t", "T"):
            return self.lire_temps()
        if conseils.is_help(key):
            return self.aide()
        if self.fige:
            return dire("imprimante.cal.deja_fige")
        if key in ("c", "C"):
            return self.lire_capteurs()
        if nom == "molettes" and self.molette:
            if key in ENTER:
                self.mesurer_maintenant.set()
                return dire("imprimante.cal.mesure_tout_de_suite")
            if key in ("s", "S"):
                self.passer_coin.set()
                self.mesurer_maintenant.set()
                return dire("imprimante.cal.coin_on_passe")
            return dire("imprimante.patiente", quoi=self.occupe)
        if key in ("s", "S"):
            return self.suivante()
        if nom == "decouverte":
            if self.visite is None:
                if key in ENTER:
                    return self.lancer_decouverte()
                if key in ("g", "G"):
                    return self.lancer_decouverte(force=True)
            elif key in ENTER:
                if self.element >= len(self.visite["elements"]):
                    return self.suivante()
                return self.element_suivant(trouve=True if self.element >= 0 else None)
            elif key in ("n", "N") and self.element >= 0:
                return self.element_suivant(trouve=False)
            elif key == PAGE_UP and self.element > 0:
                self.element -= 1
                return self._dire_element()
        if nom == "verifs" and key in ENTER:
            if self.bloque:
                self.bloque, texte = self.verifs()
                return texte if self.bloque else self.entrer(self.etape + 1)
            return self.suivante()
        if nom == "sonde" and key in ENTER:
            return self.suivante() if self.sonde_ok else self.test_sonde()
        if nom == "hauteur_sonde":
            if key in ("d", "D"):
                return self.tige(True)
            if key in ("u", "U"):
                return self.tige(False)
            if key in ENTER:
                self.journal("HAUTEUR SONDE : confirmee au toucher")
                if self.tige_sortie:
                    self.tige(False)
                return self.suivante()
            if key in ("n", "N"):
                self.journal("HAUTEUR SONDE : douteuse")
                return dire("imprimante.cal.hauteur_sonde_doute")
        if nom == "sens_z":
            if self.sens_attente:
                if key in ENTER:
                    self.sens_attente, self.sens_ok = False, True
                    self.journal("SENS Z : la tete a monte, confirme au toucher")
                    return dire("imprimante.cal.sens_ok") + " " + dire("imprimante.cal.suite_entree")
                if key in ("n", "N"):
                    self.sens_attente = False
                    self.journal("SENS Z : INVERSE selon la personne")
                    return dire("imprimante.cal.sens_inverse")
                if key in ("t", "T"):
                    self.sens_attente = False
                    return self.test_sens()
                return dire("imprimante.cal.sens_question")
            if key in ENTER:
                return self.suivante() if self.sens_ok else self.test_sens()
        if nom == "ongle":
            if key in ("h", "H"):
                return self.monter(30)
            if key in ENTER:
                return self.suivante() if self.ongle_ok else self.test_ongle()
        if nom == "origine" and self.saisie is not None:
            return self.saisir(key)
        if nom == "origine" and key in ENTER:
            return self.suivante() if self.z_origine else self.origines()
        if nom in ("repetabilite", "molettes", "hauteur", "carte") and self.saisie is not None:
            return self.saisir(key)
        if nom in ("repetabilite", "molettes", "hauteur", "carte") and key in ENTER and not self.z_origine \
                and self.hauteur is None and not (nom == "hauteur" and self.decalage_fait):
            return self.reorigine()   # origine perdue (decalage change, arret) : on la refait d'abord
        if nom == "repetabilite" and key in ENTER:
            return self.suivante() if self.m48_ok else self.repetabilite()
        if nom == "molettes" and key in ENTER:
            return self.suivante() if self.molettes_ok else self.molettes()
        if nom == "hauteur":
            if self.hauteur is None:
                if key in ENTER:
                    return self.suivante() if self.decalage_fait and not self.propose else self.entrer_hauteur()
            else:
                if key in (PAGE_UP, PAGE_DOWN):
                    return self.bouger_z(1 if key == PAGE_UP else -1, PAS_HAUTEUR[self.pas_h])
                if key in ("+", "=", "-"):
                    self.pas_h = max(0, min(len(PAS_HAUTEUR) - 1, self.pas_h + (1 if key != "-" else -1)))
                    return dire("imprimante.cal.pas", pas=_fr(PAS_HAUTEUR[self.pas_h]))
                if key in ENTER:
                    return self.valider_hauteur()
        if nom == "carte" and key in ENTER:
            return self.carte()
        return dire("imprimante.cal.touche_sans_action")

    def boucle(self):
        if not self.flux.demarrer():
            print("[calibrage] websocket OctoPrint indisponible", file=sys.stderr)
        intro = conseils.intro("imprimante_calibrage", dire("imprimante.cal.titre"), dire("imprimante.cal.consigne"))
        self._out(intro + "\n")
        # Etape 2 d'abord en silence : il faut le modele et la sonde pour la
        # decouverte ; on ouvre ensuite sur la decouverte.
        self.bloque, _ = self.verifs()
        self.dire(f"{intro} {self.entrer(0)}")
        try:
            for key in self.keys:
                if key == "\x1b":
                    break
                if key.startswith("\x00"):
                    continue
                self.journal(f"TOUCHE {key!r} etape={ETAPES[self.etape]}")
                try:
                    texte = self.touche(key)
                except Exception as exc:  # une touche qui plante ne ferme pas le dialogue
                    print(f"[calibrage] touche {key!r} : {exc!r}", file=sys.stderr)
                    texte = dire("imprimante.cal.erreur_interne")
                if texte is None:
                    break
                self.dire(texte, garder=key not in ("r", "R"))
        finally:
            self.nettoyer()
        if self.occupe and not self.fige:
            return dire("imprimante.cal.ferme_mouvement")
        return dire("imprimante.cal.ferme")


def calibrage(key_source, say, out, say_wait=None):
    try:
        e = op.etat()
    except op.Erreur:
        e = {"connecte": False}
    if e.get("connecte") and any((e.get("flags") or {}).get(f) for f in ("printing", "paused", "pausing")):
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
    # Mode calibrage de l'arbitre des haut-parleurs (audio_lock, 2026-10-16) :
    # pendant le dialogue, aucune autre annonce ne couvre les consignes.
    marquer = getattr(audio_lock, "set_calibrage", None)
    if marquer:
        marquer(True)
    try:
        return Calibrage(key_source, say, out, say_wait).boucle()
    finally:
        if marquer:
            marquer(False)
