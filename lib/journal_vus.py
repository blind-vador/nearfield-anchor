"""Enregistrements deja ecoutes dans les journaux (2026-10-06) : la commande
"notifications" compte ce qui n'a pas encore ete ecoute. Fichier sur la carte
SD : {"depuis": instant du premier usage, "ecoutes": {nom de fichier:
instant}} ; ce qui existait avant le premier usage n'est pas "nouveau" ; une
ecoute est oubliee apres KEEP_SEC (les journaux ne gardent que 48 heures)."""
import chemins
import json
import os
import time

PATH = chemins.base("journal_ecoutes.json")
KEEP_SEC = 7 * 86400


def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if "ecoutes" not in data:
        data = {"depuis": time.time(), "ecoutes": {}}
        _save(data)
    return data


def _save(data):
    try:
        tmp = PATH + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, PATH)
    except OSError as exc:
        print(f"[journal_vus] non ecrit : {exc}")


def mark(path):
    """Enregistrement joue dans un journal, ou dit a voix haute par une veille
    (lib/prise_vocale.py) : il n'est plus nouveau. Plusieurs processus marquent
    (console, veilles) : lire-modifier-ecrire sous verrou (2026-10-23)."""
    mark_many([path])


def mark_many(paths):
    """Tout un journal declare lu (commande "journal messages lu", 2026-11-07) :
    une seule ecriture pour toutes les entrees."""
    import fcntl
    with open(PATH + ".verrou", "a") as verrou:
        fcntl.flock(verrou, fcntl.LOCK_EX)
        data = _load()
        now = time.time()
        for path in paths:
            data["ecoutes"][os.path.basename(path)] = now
        data["ecoutes"] = {k: t for k, t in data["ecoutes"].items() if now - t < KEEP_SEC}
        _save(data)


def is_new(path):
    data = _load()
    if os.path.basename(path) in data["ecoutes"]:
        return False
    try:
        return os.path.getmtime(path) >= data["depuis"]
    except OSError:
        return False
