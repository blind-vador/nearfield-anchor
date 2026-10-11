"""Pilotage vocal de l'imprimante 3D par OctoPrint (2026-10-11).

Dialogue "pilote imprimante" (touches, tout est dit) :
  fleches gauche / droite      axe X (gauche / droite)
  fleches haut / bas           axe Y (arriere / avant)
  page precedente / suivante   axe Z (monte / descend)
  + et -                       pas du deplacement : 0,1 / 1 / 10 / 50 mm
                               (Z : 10 mm au plus)
  Ctrl + X                     origine de X et Y
  Ctrl + Z                     origine de Z
  Debut                        origine de tous les axes
  x, y, z                      position de cet axe ; p ou espace : les trois
  t                            temperatures
  e / r                        pousse / retire 5 mm de filament (buse chaude)
  v                            ventilateur marche / arret
  m                            coupe les moteurs (axes libres a la main)
  n                            reglage du plateau : point suivant (4 coins
                               puis centre, buse au contact pour la feuille)
  i                            etat de l'imprimante
  F1 aide ; Echap ferme

Commandes tapees : chauffe (pla, abs, buse 200, plateau 60), refroidis,
pause / reprends / annule l'impression, imprime (liste des fichiers),
ventilateur, moteurs, connecte / deconnecte l'imprimante, vitesse et
debit d'impression, arret d'urgence.

Garde-fous : rien ne bouge pendant une impression ni une pause (les
gcodes iraient dans le flux de la piece) ; Z ne descend que de 1 mm par
appui tant que Z n'a pas ete mis a l'origine dans cette console (pas de
butee logicielle connue) ; filament seulement buse chaude ; temperatures
bornees. Les evenements (chauffe atteinte, fin d'impression...) sont dits
par watchers/imprimante_watch.py, meme console fermee.
"""
import re
import sys
import threading

sys.path.insert(0, "/home/vador/autopilot/pi_console")
import conseils  # noqa: E402
import matcher  # noqa: E402
import octoprint as op  # noqa: E402
from phrases import Etiquettes, dire, nombre  # noqa: E402

UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
PAGE_UP, PAGE_DOWN = "\x1b[5~", "\x1b[6~"
HOME_KEYS = ("\x1b[1~", "\x1b[H")
CTRL_X, CTRL_Z = "\x18", "\x1a"

STEPS = [0.1, 1, 10, 50]
DEFAULT_STEP = 2            # 10 mm
Z_STEP_MAX = 10
Z_STEP_UNHOMED = 1
EXTRUDE_MM = 5
EXTRUDE_MIN_C = 170         # Marlin refuse d'extruder en dessous (EXTRUDE_MINTEMP)
NOZZLE_MAX_C, BED_MAX_C = 260, 110
SPEED_RANGE, FLOW_RANGE = (25, 200), (75, 125)
POSITION_TIMEOUT_SEC = 6
HOMING_TIMEOUT_SEC = 150
LEVEL_MARGIN_MM = 30

MOVES = {RIGHT: ("x", 1, "droite"), LEFT: ("x", -1, "gauche"), UP: ("y", 1, "arriere"),
         DOWN: ("y", -1, "avant"), PAGE_UP: ("z", 1, "monte"), PAGE_DOWN: ("z", -1, "descend")}
SENS = Etiquettes("imprimante.sens", {"droite": "Droite", "gauche": "Gauche", "arriere": "Arrière",
                                      "avant": "Avant", "monte": "Monte", "descend": "Descend"},
                  "sens d'un déplacement de la tête ou du plateau, début de phrase")
LEVEL_POINTS = Etiquettes("imprimante.point", {"avant_gauche": "Coin avant gauche", "avant_droit": "Coin avant droit",
                                               "arriere_droit": "Coin arrière droit",
                                               "arriere_gauche": "Coin arrière gauche", "centre": "Centre"},
                          "point du réglage du plateau")

# Etat de la console (le Pi ne sait pas ce qu'OctoPrint ou le panneau de
# l'imprimante ont fait entre-temps : prudence par defaut).
_session = {"origine": set(), "ventilateur": False, "niveau": -1, "occupe": None, "averti_z": False}


def _fr(value):
    """10.0 -> "10" ; 0.1 -> "0,1" ; 110.55 -> "110,6"."""
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") or "0"


def _erreur(exc):
    if exc.code == "occupee":
        try:
            e = op.etat()
        except op.Erreur:
            e = {"connecte": False, "texte": ""}
        if not e["connecte"]:
            return dire("imprimante.pas_connectee")
        if e["flags"].get("printing") or e["flags"].get("paused"):
            return dire("imprimante.impression_en_cours_refus")
    return dire(f"imprimante.erreur.{exc.code}") if exc.code in ("cle", "refus", "injoignable", "introuvable") \
        else dire("imprimante.erreur.injoignable")


def _libre():
    """None si l'imprimante est connectee et libre (ni impression ni pause),
    sinon la phrase qui explique pourquoi on ne bouge rien."""
    try:
        e = op.etat()
    except op.Erreur as exc:
        return _erreur(exc)
    if not e["connecte"]:
        return dire("imprimante.pas_connectee")
    flags = e["flags"]
    if flags.get("printing") or flags.get("paused") or flags.get("pausing") or flags.get("cancelling"):
        return dire("imprimante.impression_en_cours_refus")
    return None


def _temps_text(e):
    parts = []
    for key, label in (("buse", "imprimante.buse"), ("plateau", "imprimante.plateau")):
        actual, target = e[key]
        if actual is None:
            continue
        if target:
            parts.append(dire("imprimante.temp_cible", nom=dire(label), reelle=round(actual), cible=round(target)))
        else:
            parts.append(dire("imprimante.temp_froide", nom=dire(label), reelle=round(actual)))
    return " ".join(parts)


def _duree(seconds):
    h, m = divmod(int(seconds or 0) // 60, 60)
    if h:
        return dire("imprimante.duree_h", heures=nombre(h, "heure"), m=m)
    return dire("imprimante.duree_m", minutes=nombre(max(m, 1), "minute"))


def etat_texte():
    """Etat complet : liaison, travail en cours, progression, temperatures."""
    try:
        e = op.etat()
    except op.Erreur as exc:
        return _erreur(exc)
    if not e["connecte"]:
        return dire("imprimante.pas_connectee_etat", etat=e["texte"] or "?")
    flags, job = e["flags"], e["job"]
    parts = []
    name = ((job.get("job") or {}).get("file") or {}).get("display") or \
        ((job.get("job") or {}).get("file") or {}).get("name")
    progress = job.get("progress") or {}
    if flags.get("printing") or flags.get("paused") or flags.get("pausing"):
        key = "imprimante.etat_pause" if flags.get("paused") or flags.get("pausing") else "imprimante.etat_impression"
        parts.append(dire(key, fichier=name or "?", pourcent=round(progress.get("completion") or 0)))
        if progress.get("printTimeLeft"):
            parts.append(dire("imprimante.reste", duree=_duree(progress["printTimeLeft"])))
    elif e["erreur"]:
        parts.append(dire("imprimante.etat_erreur", erreur=e["erreur"]))
    else:
        parts.append(dire("imprimante.etat_pret"))
    parts.append(_temps_text(e))
    return " ".join(p for p in parts if p)


# --- dialogue de pilotage -------------------------------------------------------

def _background(say, work):
    """Mouvement long (origine, reglage du plateau) : le clavier reste libre,
    la fin est dite quand le firmware a fini (M400 puis M114)."""
    def run():
        try:
            say(work())
        except op.Erreur as exc:
            say(_erreur(exc))
        except Exception as exc:  # ne jamais tuer la console
            print(f"[imprimante] erreur en tache de fond : {exc!r}", file=sys.stderr)
            say(dire("imprimante.erreur.injoignable"))
        finally:
            _session["occupe"] = None
    threading.Thread(target=run, daemon=True).start()


def _homing(axes, say):
    refused = _libre()
    if refused:
        return refused
    if _session["occupe"]:
        return dire("imprimante.patiente", quoi=_session["occupe"])
    label = {"xy": "imprimante.axes_xy", "z": "imprimante.axe_z", "xyz": "imprimante.axes_tous"}[axes]
    gcode = {"xy": "G28 X Y", "z": "G28 Z", "xyz": "G28"}[axes]
    _session["occupe"] = dire("imprimante.occupe_origine")

    def work():
        found, lines = op.interroger([gcode, "M400", "M114"], op.POSITION_RE, HOMING_TIMEOUT_SEC)
        problem = op.probleme_firmware(lines)
        if problem or not found:
            return dire("imprimante.origine_echec", axes=dire(label), detail=problem or dire("imprimante.sans_reponse"))
        _session["origine"].update(axes)
        x, y, z = (float(v) for v in found.groups())
        return dire("imprimante.origine_faite", axes=dire(label), x=_fr(x), y=_fr(y), z=_fr(z))

    _background(say, work)
    return dire("imprimante.origine_lancee", axes=dire(label))


def _level_next(say):
    refused = _libre()
    if refused:
        return refused
    if _session["occupe"]:
        return dire("imprimante.patiente", quoi=_session["occupe"])
    prof = op.profil()
    m = LEVEL_MARGIN_MM
    points = [("avant_gauche", m, m), ("avant_droit", prof["largeur"] - m, m),
              ("arriere_droit", prof["largeur"] - m, prof["profondeur"] - m),
              ("arriere_gauche", m, prof["profondeur"] - m),
              ("centre", prof["largeur"] / 2, prof["profondeur"] / 2)]
    _session["niveau"] = (_session["niveau"] + 1) % len(points)
    code, x, y = points[_session["niveau"]]
    gcodes = [] if _session["origine"] >= set("xyz") else ["G28"]
    gcodes += ["G90", "G1 Z5 F600", f"G1 X{x:.1f} Y{y:.1f} F3000", "G1 Z0 F300", "M400", "M114"]
    _session["occupe"] = dire("imprimante.occupe_niveau")

    def work():
        found, lines = op.interroger(gcodes, op.POSITION_RE, HOMING_TIMEOUT_SEC)
        problem = op.probleme_firmware(lines)
        if problem or not found:
            return dire("imprimante.niveau_echec", detail=problem or dire("imprimante.sans_reponse"))
        _session["origine"].update("xyz")
        return dire("imprimante.niveau_point", point=LEVEL_POINTS[code])

    _background(say, work)
    return dire("imprimante.niveau_lance", point=LEVEL_POINTS[code],
                origine=dire("imprimante.niveau_avec_origine") + " " if gcodes[0] == "G28" else "")


def _move(key, step):
    axis, sign, sens = MOVES[key]
    if _session["occupe"]:
        return dire("imprimante.patiente", quoi=_session["occupe"])
    refused = _libre()
    if refused:
        return refused
    warning = ""
    if axis == "z":
        step = min(step, Z_STEP_MAX)
        if sign < 0 and "z" not in _session["origine"]:
            step = min(step, Z_STEP_UNHOMED)
            if not _session["averti_z"]:
                _session["averti_z"] = True
                warning = " " + dire("imprimante.z_sans_origine")
    try:
        op.deplacer(**{axis: sign * step})
    except op.Erreur as exc:
        return _erreur(exc)
    return dire("imprimante.mouvement", sens=SENS[sens], pas=_fr(step)) + warning


def _position(axes):
    try:
        e = op.etat()
        if not e["connecte"]:
            return dire("imprimante.pas_connectee")
        busy = any(e["flags"].get(f) for f in ("printing", "pausing", "cancelling"))
        pos = op.position(POSITION_TIMEOUT_SEC, attendre=not busy)
    except op.Erreur as exc:
        return _erreur(exc)
    if pos is None:
        return dire("imprimante.position_sans_reponse")
    if len(axes) == 1:
        return dire("imprimante.position_axe", axe=axes.upper(), valeur=_fr(pos[axes]))
    text = dire("imprimante.position_tous", x=_fr(pos["x"]), y=_fr(pos["y"]), z=_fr(pos["z"]))
    if not _session["origine"] >= set("xyz"):
        text += " " + dire("imprimante.position_sans_origine")
    return text


def _extrude(sign):
    try:
        e = op.etat()
    except op.Erreur as exc:
        return _erreur(exc)
    if not e["connecte"]:
        return dire("imprimante.pas_connectee")
    if e["flags"].get("printing"):
        return dire("imprimante.impression_en_cours_refus")
    actual = e["buse"][0] or 0
    if actual < EXTRUDE_MIN_C:
        return dire("imprimante.buse_trop_froide", reelle=round(actual), minimum=EXTRUDE_MIN_C)
    try:
        op.extruder(sign * EXTRUDE_MM)
    except op.Erreur as exc:
        return _erreur(exc)
    return dire("imprimante.filament_pousse" if sign > 0 else "imprimante.filament_retire", mm=EXTRUDE_MM)


def _gcode_simple(gcodes):
    """None si envoye, sinon la phrase du refus."""
    refused = _libre()
    if refused:
        return refused
    try:
        op.commande(*gcodes)
    except op.Erreur as exc:
        return _erreur(exc)
    return None


def _fan_toggle():
    on = not _session["ventilateur"]
    refused = _gcode_simple(["M106 S255"] if on else ["M107"])
    if refused:
        return refused
    _session["ventilateur"] = on
    return dire("imprimante.ventilateur_marche" if on else "imprimante.ventilateur_arret")


def _motors_off():
    refused = _gcode_simple(["M84"])
    if refused:
        return refused
    _session["origine"].clear()  # les axes peuvent bouger a la main : positions perdues
    _session["averti_z"] = False
    return dire("imprimante.moteurs_coupes")


def _key(key, state, say):
    if key in MOVES:
        return _move(key, STEPS[state["pas"]])
    if key in ("+", "=", "-"):
        state["pas"] = max(0, min(len(STEPS) - 1, state["pas"] + (1 if key != "-" else -1)))
        return dire("imprimante.pas", pas=_fr(STEPS[state["pas"]]))
    if key == CTRL_X:
        return _homing("xy", say)
    if key == CTRL_Z:
        return _homing("z", say)
    if key in HOME_KEYS:
        return _homing("xyz", say)
    if key in ("x", "y", "z", "X", "Y", "Z"):
        return _position(key.lower())
    if key in ("p", "P", " "):
        return _position("xyz")
    if key in ("t", "T"):
        try:
            e = op.etat()
        except op.Erreur as exc:
            return _erreur(exc)
        return _temps_text(e) if e["connecte"] else dire("imprimante.pas_connectee")
    if key in ("e", "E"):
        return _extrude(1)
    if key in ("r", "R"):
        return _extrude(-1)
    if key in ("v", "V"):
        return _fan_toggle()
    if key in ("m", "M"):
        return _motors_off()
    if key in ("n", "N"):
        return _level_next(say)
    if key in ("i", "I"):
        return etat_texte()
    if conseils.is_help(key):
        return dire("imprimante.pilote_consigne")
    return dire("imprimante.touche_sans_action")


def pilote(key_source, say, out):
    try:
        e = op.etat()
    except op.Erreur as exc:
        return _erreur(exc)
    if not e["connecte"]:
        return dire("imprimante.pas_connectee")
    state = {"pas": DEFAULT_STEP}
    intro = conseils.intro("imprimante_pilote", dire("imprimante.pilote_titre"), dire("imprimante.pilote_consigne"))
    out(intro + "\n")
    say(f"{intro} {dire('imprimante.pas', pas=_fr(STEPS[state['pas']]))}")
    for key in key_source:
        if key == "\x1b":
            return dire("imprimante.pilote_ferme")
        if key.startswith("\x00"):  # reveil de la lecture (keys.WAKE), pas une touche
            continue
        try:
            text = _key(key, state, say)
        except Exception as exc:  # une touche qui plante ne doit pas fermer le dialogue
            print(f"[imprimante] touche {key!r} : {exc!r}", file=sys.stderr)
            text = dire("imprimante.erreur.injoignable")
        out(f"[{text}]\n")
        say(text)
    return dire("imprimante.pilote_ferme")


# --- commandes tapees ------------------------------------------------------------

def _numbers(line):
    return [int(n) for n in re.findall(r"\d+", line)]


def chauffe(line):
    words = matcher.normalize(line).split()
    nozzle = bed = None
    presets = {name.lower(): (n, b) for name, n, b in op.prechauffages()}
    for word in words:
        if word in presets:
            nozzle, bed = presets[word]
            label = word.upper()
            break
    else:
        label = ""
        numbers = _numbers(line)
        # "chauffe buse 200 plateau 60", "chauffe plateau 60", "chauffe 200"
        for i, word in enumerate(words):
            follow = next((int(w) for w in words[i + 1:] if w.isdigit()), None)
            if word == "buse" and follow is not None:
                nozzle = follow
            elif word == "plateau" and follow is not None:
                bed = follow
        if nozzle is None and bed is None and numbers:
            nozzle = numbers[0]
    if nozzle is None and bed is None:
        names = ", ".join(n for n, _, _ in op.prechauffages())
        return dire("imprimante.chauffe_aide", prechauffages=names)
    if (nozzle or 0) > NOZZLE_MAX_C or (bed or 0) > BED_MAX_C:
        return dire("imprimante.chauffe_trop", buse_max=NOZZLE_MAX_C, plateau_max=BED_MAX_C)
    try:
        if not op.etat()["connecte"]:
            return dire("imprimante.pas_connectee")
        if nozzle is not None:
            op.cible_buse(nozzle)
        if bed is not None:
            op.cible_plateau(bed)
    except op.Erreur as exc:
        return _erreur(exc)
    parts = []
    if nozzle is not None:
        parts.append(dire("imprimante.chauffe_buse", cible=nozzle))
    if bed is not None:
        parts.append(dire("imprimante.chauffe_plateau", cible=bed))
    return dire("imprimante.chauffe_lancee", prechauffage=f"{label} " if label else "", cibles=", ".join(parts))


def refroidis():
    try:
        if not op.etat()["connecte"]:
            return dire("imprimante.pas_connectee")
        op.cible_buse(0)
        op.cible_plateau(0)
    except op.Erreur as exc:
        return _erreur(exc)
    return dire("imprimante.refroidissement")


def _job(action, needs, done_key):
    try:
        e = op.etat()
    except op.Erreur as exc:
        return _erreur(exc)
    if not e["connecte"]:
        return dire("imprimante.pas_connectee")
    if not needs(e["flags"]):
        return dire({"pause": "imprimante.rien_a_suspendre", "resume": "imprimante.rien_a_reprendre",
                     "cancel": "imprimante.rien_a_annuler"}[action])
    try:
        op.travail(action)
    except op.Erreur as exc:
        return _erreur(exc)
    return dire(done_key)


def pause():
    return _job("pause", lambda f: f.get("printing"), "imprimante.pause_demandee")


def reprendre():
    return _job("resume", lambda f: f.get("paused"), "imprimante.reprise_demandee")


def annuler(announce=None):
    return _job("cancel", lambda f: f.get("printing") or f.get("paused") or f.get("pausing"),
                "imprimante.annulation_demandee")


def ventilateur(line):
    words = set(matcher.normalize(line).split())
    numbers = _numbers(line)
    if words & {"off", "arret", "arrete", "coupe", "eteins", "stop", "non", "zero"} or numbers[:1] == [0]:
        percent = 0
    else:
        percent = max(1, min(100, numbers[0])) if numbers else 100
    gcode = f"M106 S{round(percent * 255 / 100)}" if percent else "M107"
    try:
        if not op.etat()["connecte"]:
            return dire("imprimante.pas_connectee")
        op.commande(gcode)  # permis pendant une impression : c'est un reglage courant
    except op.Erreur as exc:
        return _erreur(exc)
    _session["ventilateur"] = bool(percent)
    return dire("imprimante.ventilateur_pourcent", pourcent=percent) if percent else dire("imprimante.ventilateur_arret")


def moteurs():
    return _motors_off()


def connexion(line):
    words = set(matcher.normalize(line).split())
    off = bool(words & {"deconnecte", "deconnecter", "deconnexion"})
    try:
        e = op.etat()
        if off and e["connecte"] and (e["flags"].get("printing") or e["flags"].get("paused")):
            return dire("imprimante.impression_en_cours_refus")
        if not off and e["connecte"]:
            return dire("imprimante.deja_connectee")
        op.connecter(not off)
    except op.Erreur as exc:
        return _erreur(exc)
    if off:
        _session["origine"].clear()
    return dire("imprimante.deconnexion_demandee" if off else "imprimante.connexion_demandee")


def _factor(line, setter, bounds, done_key):
    numbers = _numbers(line)
    if not numbers:
        return dire("imprimante.facteur_aide", mini=bounds[0], maxi=bounds[1])
    value = numbers[0]
    if not bounds[0] <= value <= bounds[1]:
        return dire("imprimante.facteur_hors_bornes", mini=bounds[0], maxi=bounds[1])
    try:
        if not op.etat()["connecte"]:
            return dire("imprimante.pas_connectee")
        setter(value)
    except op.Erreur as exc:
        return _erreur(exc)
    return dire(done_key, pourcent=value)


def vitesse(line):
    return _factor(line, op.vitesse, SPEED_RANGE, "imprimante.vitesse_reglee")


def debit(line):
    return _factor(line, op.debit, FLOW_RANGE, "imprimante.debit_regle")


def urgence():
    """M112 : OctoPrint l'envoie en priorite, hors file d'attente. Le
    firmware se fige : il faudra redemarrer l'imprimante."""
    try:
        op.commande("M112")
    except op.Erreur as exc:
        return _erreur(exc)
    _session["origine"].clear()
    return dire("imprimante.urgence_envoyee")


def fichiers(key_source, say, out, read_line):
    """Liste des fichiers d'impression : fleches pour choisir, Entree pour
    lancer (apres confirmation), Echap pour fermer."""
    refused = _libre()
    if refused:
        return refused
    try:
        files = op.fichiers()
    except op.Erreur as exc:
        return _erreur(exc)
    if not files:
        return dire("imprimante.aucun_fichier")

    def label(i):
        f = files[i]
        text = dire("imprimante.fichier", nom=f["nom"], rang=i + 1, total=len(files))
        if f["duree"]:
            text += " " + dire("imprimante.fichier_duree", duree=_duree(f["duree"]))
        if f["origine"] == "sdcard":
            text += " " + dire("imprimante.fichier_sd")
        return text

    i = 0
    intro = conseils.intro("imprimante_fichiers", dire("imprimante.fichiers_titre", n=len(files)),
                           dire("imprimante.fichiers_consigne"))
    out(intro + "\n")
    say(f"{intro} {label(i)}")
    for key in key_source:
        if key == "\x1b":
            return dire("imprimante.fichiers_ferme")
        if key.startswith("\x00"):
            continue
        if key in (UP, DOWN):
            j = i + (1 if key == DOWN else -1)
            edge = "" if 0 <= j < len(files) else dire("imprimante.debut_liste" if j < 0 else "commun.fin_liste") + " "
            text = edge + label(j if not edge else i)
            i = j if 0 <= j < len(files) else i
        elif key in ("\r", "\n"):
            question = dire("imprimante.lancer_confirmation", nom=files[i]["nom"])
            out(f"{question}\n? ")
            say(question)
            if matcher.normalize(read_line(key_source)) != "oui":
                return dire("commun.annule")
            refused = _libre()
            if refused:
                return refused
            try:
                op.imprimer(files[i])
            except op.Erreur as exc:
                return _erreur(exc)
            return dire("imprimante.impression_lancee", nom=files[i]["nom"])
        elif conseils.is_help(key):
            text = dire("imprimante.fichiers_consigne")
        else:
            text = dire("imprimante.fichiers_touches")
        out(f"[{text}]\n")
        say(text)
    return dire("imprimante.fichiers_ferme")


# Noms des commandes (console/commands.py) traitees ici, avec la ligne tapee.
LINE_HANDLERS = {"chauffe": chauffe, "ventilateur": ventilateur, "connexion imprimante": connexion,
                 "vitesse d'impression": vitesse, "débit": debit}
SIMPLE_HANDLERS = {"refroidis": refroidis, "pause impression": pause, "reprends impression": reprendre,
                   "moteurs": moteurs, "arrêt d'urgence": urgence}
DIALOGS = {"pilote imprimante", "imprime"}


def handles(name):
    return name in LINE_HANDLERS or name in SIMPLE_HANDLERS or name in DIALOGS


def run(name, line, key_source, say, out, read_line):
    if name == "pilote imprimante":
        return pilote(key_source, say, out)
    if name == "imprime":
        return fichiers(key_source, say, out, read_line)
    if name in LINE_HANDLERS:
        return LINE_HANDLERS[name](line)
    return SIMPLE_HANDLERS[name]()
