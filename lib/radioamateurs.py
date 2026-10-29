"""Indicatif d'un emetteur radioamateur d'apres son identifiant numerique
(DMR), base publique radioid.net (gratuite, sans cle). Cache sur la carte SD
(30 jours). On ne garde que l'indicatif, la ville et le pays : jamais le nom
de la personne (2026-10-03)."""
import chemins
import json
import os
import time

import requests

CACHE_PATH = chemins.base("radioamateurs_cache.json")
API = "https://radioid.net/api/dmr/user/"
TTL = 30 * 86400
MISS_TTL = 3 * 86400
TIMEOUT = 8
_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save():
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_load(), f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        print(f"[radioamateurs] cache non ecrit : {exc}")


def dmr_id(number):
    """{indicatif, ville, pays} ou None."""
    key = str(number)
    if not key.isdigit() or len(key) < 6:  # groupes et petits numeros : pas des personnes
        return None
    entry = _load().get(key)
    if entry and time.time() - entry["t"] < (TTL if entry["v"] else MISS_TTL):
        return entry["v"]
    try:
        found = (requests.get(API, params={"id": key}, timeout=TIMEOUT).json().get("results") or [None])[0]
    except (requests.RequestException, ValueError) as exc:
        print(f"[radioamateurs] {key} : {exc!r}")
        return entry["v"] if entry else None
    value = ({"indicatif": found.get("callsign"), "ville": found.get("city"), "pays": found.get("country")}
             if found else None)
    _load()[key] = {"t": time.time(), "v": value}
    _save()
    return value
