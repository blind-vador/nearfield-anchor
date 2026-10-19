"""Contenu appris par station (2026-10-14, demande utilisateur) : les
pourcentages notes (voix, musique, tonalites, donnees...) s'additionnent par
frequence ; une station surtout musicale recoit alors les reglages
"musique" (lib/reglages_audio.py, CONTENTS) au lieu du traitement de la voix.
Jamais au-dessus d'un reglage fait a la main : reglages_audio.get() ne s'en
sert que si le type de contenu de la bande est "automatique".

Fichier sur la carte SD (lu a chaque capture, donc petit) :
  {cle de frequence (pas de 12,5 kHz): {"n": notes, "parts": {code: somme des parts}}}
Une note donne des parts de 0 a 1 qui somment a 1 (pourcentages normalises ;
ancienne note a choix unique : 100 % du contenu choisi).
"""
import json
import os

PATH = "/home/vador/autopilot/contenus_stations.json"
STEP_HZ = 12_500
CODES = ["voix", "musique", "tonalites", "donnees", "rien"]
MIN_NOTES = 2
DOMINANT = 0.6
_cache = {"mtime": None, "data": {}}


def key(freq_hz):
    return str(int(round(freq_hz / STEP_HZ) * STEP_HZ))


def _load():
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        try:
            with open(PATH, encoding="utf-8") as f:
                _cache["data"] = json.load(f)
            _cache["mtime"] = mtime
        except (OSError, ValueError):
            return {}
    return _cache["data"]


def _save(data):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, PATH)


def parts_of(note):
    """{code: part} d'une note (nouvelle en pourcentages, ou ancienne a choix
    unique), ou None si le contenu n'a pas ete precise."""
    pct = (note.get("degres") or {}).get("contenu")
    if isinstance(pct, dict) and sum(pct.values()) > 0:
        total = float(sum(pct.values()))
        return {c: v / total for c, v in pct.items() if v}
    code = (note.get("reponses") or {}).get("contenu")
    return {code: 1.0} if code in CODES else None


def add(note):
    """Ajoute une note a sa station (appele a chaque note enregistree)."""
    parts = parts_of(note)
    if not parts or not note.get("freq_hz"):
        return
    data = dict(_load())
    e = data.setdefault(key(note["freq_hz"]), {"n": 0, "parts": {}})
    e["n"] += 1
    for c, v in parts.items():
        e["parts"][c] = round(e["parts"].get(c, 0.0) + v, 4)
    _save(data)


def rebuild(notes):
    """Recalcule tout depuis les notes (re-noter remplace une note)."""
    data = {}
    for note in notes:
        parts = parts_of(note)
        if not parts or not note.get("freq_hz"):
            continue
        e = data.setdefault(key(note["freq_hz"]), {"n": 0, "parts": {}})
        e["n"] += 1
        for c, v in parts.items():
            e["parts"][c] = round(e["parts"].get(c, 0.0) + v, 4)
    _save(data)
    return len(data)


def mix(freq_hz):
    """{code: part moyenne} pour cette station, ou None (pas assez de notes)."""
    if not freq_hz:
        return None
    e = _load().get(key(freq_hz))
    if not e or e["n"] < MIN_NOTES:
        return None
    return {c: v / e["n"] for c, v in e["parts"].items()}


def dominant(freq_hz):
    """Contenu largement majoritaire de la station ("musique", "voix"...) ou None."""
    m = mix(freq_hz)
    if not m:
        return None
    code, share = max(m.items(), key=lambda kv: kv[1])
    return code if share >= DOMINANT else None
