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
  Entree sur OK            enregistre       Echap : annule
"""
import json
import os
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import noise_signatures as ns  # noqa: E402

NOTES_PATH = "/media/nas/nd1/server/radio_notes.json"  # jamais purge (le journal, lui, l'est a 48 h)

# (code, question parlee, [(code, valeur parlee), ...], valeur par defaut)
FIELDS = [
    ("contenu", "Contenu", [("voix", "voix"), ("musique", "musique"), ("tonalites", "tonalités, bips ou DTMF"),
                            ("donnees", "données numériques"), ("rien", "rien d'identifiable")], 0),
    ("lisibilite", "Lisibilité", [(n, f"{n} sur 5") for n in range(1, 6)], 2),
    ("souffle", "Souffle", [("aucun", "aucun"), ("discret", "discret"), ("moyen", "moyen"),
                            ("fort", "fort"), ("envahissant", "envahissant")], 1),
    ("brouillage", "Brouillage", [("aucun", "aucun"), ("sifflement", "sifflement"),
                                  ("bourdonnement", "bourdonnement"), ("autre_station", "autre station"),
                                  ("crachotements", "crachotements")], 0),
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
INTRO = ("Notation. Flèches haut et bas : champ. Gauche et droite : valeur. "
         "Entrée sur OK pour enregistrer. Échap : annuler.")


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


def _field_text(i, values):
    if i == OK_INDEX:
        return "OK, entrée pour enregistrer."
    _, question, options, _ = FIELDS[i]
    return f"{question} : {options[values[i]][1]}."


def rate(key_source, say, out, path, stamp):
    """Dialogue de notation ; rend la phrase de sortie."""
    if not os.path.ismount("/media/nas/nd1"):
        return "Le disque server n'est pas accessible, notation impossible."
    info = ns.capture_info(path) or {}
    previous = next((n for n in load_notes() if n.get("fichier") == os.path.basename(path)), None)
    values = [f[3] for f in FIELDS]
    if previous:  # reprendre la note deja donnee
        for i, (code, _, options, _) in enumerate(FIELDS):
            codes = [c for c, _ in options]
            if previous["reponses"].get(code) in codes:
                values[i] = codes.index(previous["reponses"][code])
    field = 0
    out(INTRO + "\n")
    say(INTRO + (" Déjà noté, tu modifies la note. " if previous else " ") + _field_text(field, values))
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
            note = {
                "fichier": os.path.basename(path),
                "capture_le": stamp.isoformat(),
                "note_le": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "freq_hz": info.get("freq_hz"), "label": info.get("label"),
                "bande": info.get("target"), "source": info.get("source"), "mode": info.get("mode"),
                "mesures": info.get("metrics") or {},
                "empreinte": info.get("fingerprint"),
                "reponses": {code: FIELDS[i][2][values[i]][0] for i, (code, *_) in enumerate(FIELDS)},
            }
            total = _save_note(note)
            return f"Note enregistrée. {total} note{'s' if total > 1 else ''} au total."
        elif key in ("\r", "\n"):
            text = "Descends jusqu'à OK pour enregistrer."
        else:
            text = "Flèches, entrée sur OK, ou échap."
        out(f"[{text}]\n")
        say(text)
    return "Notation annulée."
