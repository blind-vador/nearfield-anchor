"""Capteurs d'interieur probables (2026-10-03, observation utilisateur : la
nuit, dehors 17 degres, quelques capteurs restent a 22,4 -- temperature
"trop stable"). Un thermometre dans une maison suit le chauffage, pas le
cycle jour-nuit : sur les dernieres 24 heures (releves bruts archives),
son amplitude est bien plus faible que celle des autres capteurs.

Regles (thermometres meteo seulement : ni pneus, ni cuisson) :
- suivi sur au moins MIN_SPAN_SEC : amplitude faible (< INDOOR_RATIO x
  l'amplitude mediane, ou < MIN_RANGE_C) ET moyenne ecartee d'au moins
  LEVEL_GAP_C de celle des autres capteurs ;
- la nuit (soleil exclu), un capteur vu peu de fois entre INDOOR_MIN_C et
  INDOOR_MAX_C et au moins NIGHT_GAP_C au-dessus des autres.
Resultat mis en cache REFRESH_SEC. Les capteurs d'interieur sont ecartes
de la meteo locale (lib/meteo_locale.py) et annonces comme tels.
"""
import glob
import json
import os
import time

DATA_DIR = "/media/nas/nd1/server/radio_log/donnees"
WINDOW_SEC = 24 * 3600
MIN_SPAN_SEC = 6 * 3600
INDOOR_RATIO = 0.6
MIN_RANGE_C = 1.5
LEVEL_GAP_C = 3.0
NIGHT_GAP_C = 4.0
INDOOR_MIN_C, INDOOR_MAX_C = 18.0, 26.0
NIGHT_HOURS = set(range(21, 24)) | set(range(0, 7))
REFRESH_SEC = 3600
_cache = {"t": 0.0, "interieurs": set()}


def key_of(event):
    """Meme cle que capteurs_watch._key."""
    return "|".join(str(event.get(k, "")) for k in ("model", "id", "channel"))


def _series():
    now = time.time()
    series = {}
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "capteurs_*.jsonl")))[-2:]:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("doublon") or e.get("temperature_C") is None or now - e.get("t", 0) > WINDOW_SEC:
                        continue
                    if any(k.startswith("pressure") for k in e) or not -40 <= e["temperature_C"] <= 45:
                        continue  # pneus, cuisson, congelateur : pas des thermometres d'air
                    series.setdefault(key_of(e), []).append((e["t"], e["temperature_C"]))
        except OSError:
            continue
    return series


def _median(values):
    values = sorted(values)
    return values[len(values) // 2] if values else None


def indoor_keys(fresh=False):
    """Cles des capteurs d'interieur probables."""
    if not fresh and time.time() - _cache["t"] < REFRESH_SEC:
        return _cache["interieurs"]
    series = _series()
    stats = {}
    for key, rows in series.items():
        temps = [t for _, t in rows]
        stats[key] = {"moy": sum(temps) / len(temps), "amp": max(temps) - min(temps), "dernier": temps[-1],
                      "suivi": len(rows) >= 6 and rows[-1][0] - rows[0][0] >= MIN_SPAN_SEC}
    followed = [s_["amp"] for s_ in stats.values() if s_["suivi"]]
    reference_range = _median(followed) or 0
    night = time.localtime().tm_hour in NIGHT_HOURS
    found = set()
    for key, st in stats.items():
        others = [o for k, o in stats.items() if k != key]
        if len(others) < 2:
            continue
        if st["suivi"]:
            others_mean = _median([o["moy"] for o in others])
            flat = st["amp"] < max(MIN_RANGE_C, INDOOR_RATIO * reference_range)
            if flat and abs(st["moy"] - others_mean) >= LEVEL_GAP_C:
                found.add(key)
        elif night:
            others_last = _median([o["dernier"] for o in others])
            if INDOOR_MIN_C <= st["dernier"] <= INDOOR_MAX_C and st["dernier"] - others_last >= NIGHT_GAP_C:
                found.add(key)
    _cache.update(t=time.time(), interieurs=found)
    return found


def is_indoor(key):
    try:
        return key in indoor_keys()
    except Exception:
        return False
