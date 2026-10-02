"""Notation d'un enregistrement radio (touche N du journal).

Grille construite avec l'utilisateur le 2026-09-30 a partir de references
eprouvees : code SINPO (UIT-R SM.1135 : signal, interference, bruit,
propagation, impression generale), ITU-T P.835 (deformation de la voix,
bruit de fond, qualite globale), dimensions NISQA (bruit, coloration,
discontinuites, volume), erreurs classiques des detecteurs de voix (debut
coupe, coupure pendant une pause, traine, bruit pris pour de la voix) et
familles de sons d'AudioSet. Chaque note garde les MESURES du systeme pour
cette capture : la commande "bilan" confronte les deux, bande par bande.

  fleches haut / bas       champ precedent / suivant
  fleches gauche / droite  valeur precedente / suivante
  E ou F2                  ecoute l'exemple de reference de la valeur choisie
                           (referentiel fixe, tools/build_exemples_notation.py)
  Entree sur OK            enregistre       Echap : annule
"""
import json
import os
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import noise_signatures as ns  # noqa: E402
import conseils  # noqa: E402

NOTES_PATH = "/media/nas/nd1/server/radio_notes.json"  # jamais purge (le journal, lui, l'est a 48 h)

# (code, question parlee, [(code, valeur parlee), ...], valeur par defaut)
INTERFERENCES = [("sifflement", "Sifflement"), ("bourdonnement", "Bourdonnement"),
                 ("autre_station", "Autre station"), ("crachotements", "Crachotements")]

FIELDS = [
    ("contenu", "Contenu", [("voix", "voix"), ("musique", "musique"), ("tonalites", "tonalités, bips ou DTMF"),
                            ("donnees", "données numériques"), ("rien", "rien d'identifiable")], 0),
    ("lisibilite", "Lisibilité", [(n, f"{n} sur 5") for n in range(1, 6)], 2),
    ("souffle", "Souffle", [("aucun", "aucun"), ("discret", "discret"), ("moyen", "moyen"),
                            ("fort", "fort"), ("envahissant", "envahissant")], 1),
    # Un champ par brouillage (2026-10-02) : on peut avoir sifflement ET
    # bourdonnement, comme les corpus (P.835, NISQA) notent chaque dimension
    # sur sa propre echelle. Les anciennes notes gardent leur champ
    # "brouillage" unique (lu par interferences()).
    *[(code, name, [("aucun", "aucun"), ("un_peu", "un peu"), ("beaucoup", "beaucoup")], 0)
      for code, name in INTERFERENCES],
    ("son", "Son déformé", [("naturel", "naturel"), ("etouffe", "étouffé"), ("metallique", "métallique"),
                            ("sature", "saturé")], 0),
    ("volume", "Volume", [("faible", "trop faible"), ("correct", "correct"), ("fort", "trop fort")], 1),
    ("stabilite", "Stabilité", [("stable", "stable"), ("coupures", "quelques coupures"), ("hache", "haché")], 0),
    ("debut", "Début", [("complet", "complet"), ("coupe", "coupé")], 0),
    ("fin", "Fin", [("coupee", "coupée trop tôt"), ("correcte", "correcte"), ("longue", "trop longue")], 1),
    ("merite", "Méritait d'être annoncé", [("oui", "oui"), ("bof", "bof"), ("non", "non")], 0),
    ("certitude", "Certitude", [("sur", "sûr"), ("hesitant", "hésitant")], 0),
]
OK_INDEX = len(FIELDS)
TITLE = "Notation."
CONSIGNE = ("Flèches haut et bas : champ. Gauche et droite : valeur. E ou F2 : écouter l'exemple de "
            "référence de la valeur choisie. Entrée sur OK pour enregistrer. Échap : annuler. F1 redit ces touches.")
EXAMPLES_DIR = "/home/vador/autopilot/exemples_notation"
F2 = ("\x1b[[B", "\x1bOQ", "\x1b[12~")
NO_EXAMPLE = ("merite", "certitude")  # c'est l'avis de la personne, pas un son


def load_notes():
    try:
        with open(NOTES_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_note(note):
    notes = [n for n in load_notes() if n.get("fichier") != note["fichier"]]  # re-noter remplace
    notes.append(note)
    tmp = NOTES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(notes, f, ensure_ascii=False)
    os.replace(tmp, NOTES_PATH)
    return len(notes)


def interferences(reponses):
    """{type: aucun / un_peu / beaucoup} pour une note, nouvelle ou ancienne
    (ancien champ unique "brouillage" : le type nomme compte pour present)."""
    if "brouillage" in reponses and not any(code in reponses for code, _ in INTERFERENCES):
        old = reponses["brouillage"]
        return {code: ("un_peu" if code == old else "aucun") for code, _ in INTERFERENCES}
    return {code: reponses.get(code, "aucun") for code, _ in INTERFERENCES}


def play_example(field, values):
    """Touche E / F2 : exemple sonore fixe de la valeur sous le curseur ; rend
    le texte a dire ensuite, ou None."""
    if field >= OK_INDEX:
        return "Choisis d'abord un champ."
    code, question, options, _ = FIELDS[field]
    value_code, value_spoken = options[values[field]]
    if code in NO_EXAMPLE:
        return f"Pas d'exemple pour {question.lower()} : c'est ton avis."
    # Brouillages : l'exemple montre le type, quel que soit le niveau choisi.
    name = f"brouillage_{code}" if code in dict(INTERFERENCES) else f"{code}_{value_code}"
    path = os.path.join(EXAMPLES_DIR, name + ".mp3")
    if not os.path.exists(path):
        return f"Pas d'exemple pour {question.lower()} {value_spoken}."
    import audio_lock
    import sound_player
    import voix
    voix.say_blocking(f"Exemple : {question.lower()}." if code in dict(INTERFERENCES)
                      else f"Exemple : {question.lower()}, {value_spoken}.")
    ticket = audio_lock.acquire("console", timeout=10)
    try:
        sound_player.play_sound_effect(path, timeout_sec=20, label="notation")
    finally:
        audio_lock.release(ticket)
    return f"{question} : {value_spoken}."


def _field_text(i, values):
    if i == OK_INDEX:
        return "OK, entrée pour enregistrer."
    _, question, options, _ = FIELDS[i]
    return f"{question} : {options[values[i]][1]}."


# Validation rapide (touche V du journal) : "bonne prise", sans dialogue.
# Le contenu n'est pas precise (voix, tonalites...) : "non_precise", que le
# bilan ignore dans ses parts par type de contenu.
GOOD_TAKE = {"contenu": "non_precise", "lisibilite": 5, "souffle": "aucun",
             **{code: "aucun" for code, _ in INTERFERENCES}, "son": "naturel", "volume": "correct", "stabilite": "stable", "debut": "complet",
             "fin": "correcte", "merite": "oui", "certitude": "sur"}


def _record(info, path, stamp, reponses, rapide=False):
    note = {
        "fichier": os.path.basename(path),
        "capture_le": stamp.isoformat(),
        "note_le": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "freq_hz": info.get("freq_hz"), "label": info.get("label"),
        "bande": info.get("target"), "source": info.get("source"), "mode": info.get("mode"),
        "mesures": info.get("metrics") or {},
        "empreinte": info.get("fingerprint"),
        "reponses": reponses,
    }
    if rapide:
        note["validation_rapide"] = True
    total = _save_note(note)
    try:  # "meritait : oui" pique la bande, "non" l'apaise
        import attention
        attention.stimulate(note["bande"], attention.NOTE_POINTS.get(reponses.get("merite"), 0))
    except Exception:
        pass
    station = False
    try:  # prise parfaite : station probable, recentree et revisitee (lib/stations.py)
        import stations
        station = stations.register_from_note(note)
    except Exception as exc:
        print(f"[notation] station probable : {exc!r}")
    return total, station


def _station_text(station):
    return " Station probable notée : le scanner s'y recentrera et y reviendra." if station else ""


def validate(path, stamp):
    """Bonne prise, en une touche : note maximale sur tous les criteres."""
    if not os.path.ismount("/media/nas/nd1"):
        return "Le disque server n'est pas accessible, validation impossible."
    previous = next((n for n in load_notes() if n.get("fichier") == os.path.basename(path)), None)
    if previous and previous.get("validation_rapide"):
        return "Déjà validé."
    if previous:
        return "Déjà noté en détail : N pour modifier la note."
    total, station = _record(ns.capture_info(path) or {}, path, stamp, dict(GOOD_TAKE), rapide=True)
    return f"Validé, bonne prise. {total} note{'s' if total > 1 else ''} au total." + _station_text(station)


def rate(key_source, say, out, path, stamp):
    """Dialogue de notation ; rend la phrase de sortie."""
    if not os.path.ismount("/media/nas/nd1"):
        return "Le disque server n'est pas accessible, notation impossible."
    info = ns.capture_info(path) or {}
    previous = next((n for n in load_notes() if n.get("fichier") == os.path.basename(path)), None)
    values = [f[3] for f in FIELDS]
    if previous:  # reprendre la note deja donnee (ancien champ brouillage compris)
        known = dict(previous["reponses"], **interferences(previous["reponses"]))
        for i, (code, _, options, _) in enumerate(FIELDS):
            codes = [c for c, _ in options]
            if known.get(code) in codes:
                values[i] = codes.index(known[code])
    field = 0
    intro = conseils.intro("notation", TITLE, CONSIGNE)
    out(intro + "\n")
    say(intro + (" Déjà noté, tu modifies la note. " if previous else " ") + _field_text(field, values))
    for key in key_source:
        if key == "\x1b":
            return "Notation annulée."
        if key in ("\x1b[A", "\x1b[B"):
            field = (field + (1 if key == "\x1b[B" else -1)) % (OK_INDEX + 1)
            text = _field_text(field, values)
        elif key in ("\x1b[C", "\x1b[D") and field < OK_INDEX:
            n = len(FIELDS[field][2])
            values[field] = (values[field] + (1 if key == "\x1b[C" else -1)) % n
            text = FIELDS[field][2][values[field]][1]
        elif key in ("\r", "\n") and field == OK_INDEX:
            reponses = {code: FIELDS[i][2][values[i]][0] for i, (code, *_) in enumerate(FIELDS)}
            total, station = _record(info, path, stamp, reponses)
            return f"Note enregistrée. {total} note{'s' if total > 1 else ''} au total." + _station_text(station)
        elif key in ("\r", "\n"):
            text = "Descends jusqu'à OK pour enregistrer."
        elif key in ("e", "E") or key in F2:
            text = play_example(field, values)
            if text is None:
                continue
        elif conseils.is_help(key):
            text = CONSIGNE
        else:
            text = "Flèches, E pour l'exemple, entrée sur OK, ou échap. F1 pour l'aide."
        out(f"[{text}]\n")
        say(text)
    return "Notation annulée."
