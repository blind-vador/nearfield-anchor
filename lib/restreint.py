"""Mode restreint du scanner (demande utilisateur 2026-10-17) : on coche les
bandes, fenetres et veilles a ecouter (dialogue console "restreint", barre
d'espace) ; tant que le mode est allume, le scanner ne visite QUE celles-la.
Different des interets (lib/interets.py), qui ponderent tout le reste.

Cibles proposees : ecrites par le scanner au demarrage (CIBLES_PATH, voir
radio_watch._publish_plages) : bandes balayees, canaux, fenetres
RTLSDR-Airband, veilles de donnees, plus "stations" (stations reperees) et
"relais" (relais appris par l'APRS). Le mode manuel et le verrouillage ne
sont pas concernes.
"""
import json
import os
import time

REGLAGES = "/home/vador/autopilot/restreint.json"
CIBLES_PATH = "/tmp/radio_watch_cibles.json"
CACHE_SEC = 5
_cache = {"t": 0.0, "data": None}


def load(fresh=False):
    if fresh or _cache["data"] is None or time.time() - _cache["t"] > CACHE_SEC:
        data = {"actif": False, "cibles": []}
        try:
            with open(REGLAGES, encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                data.update(saved)
        except (OSError, ValueError):
            pass
        _cache.update(t=time.time(), data=data)
    return _cache["data"]


def save(actif, cibles):
    data = {"actif": bool(actif), "cibles": sorted(set(cibles))}
    tmp = REGLAGES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, REGLAGES)
    _cache.update(t=time.time(), data=data)


def canonical(target):
    """Nom de cible tel que coche : "stations_nfm" -> "stations", "relais_2m" -> "relais"."""
    target = target or ""
    for prefix in ("stations", "relais"):
        if target.startswith(prefix + "_"):
            return prefix
    return target


def actif():
    data = load()
    return bool(data.get("actif") and data.get("cibles"))


def exclu(target):
    """Vrai si le mode restreint est allume et que cette cible n'est pas cochee."""
    data = load()
    if not (data.get("actif") and data.get("cibles")):
        return False
    return canonical(target) not in data["cibles"]


def publish(cibles):
    """[(nom, libelle, genre)] : ecrit par le scanner, lu par le dialogue."""
    tmp = CIBLES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([{"nom": n, "libelle": l, "genre": g} for n, l, g in cibles], f, ensure_ascii=False)
    os.replace(tmp, CIBLES_PATH)


def cibles():
    try:
        with open(CIBLES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []
