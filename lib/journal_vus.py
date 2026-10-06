"""Enregistrements deja ecoutes dans les journaux (2026-10-06) : la commande
"notifications" compte ce qui n'a pas encore ete ecoute. Fichier sur la carte
SD, une entree par enregistrement (nom de fichier -> instant), oubliee apres
KEEP_SEC (les journaux eux-memes ne gardent que 48 heures)."""
import json
import os
import time

PATH = "/home/vador/autopilot/journal_ecoutes.json"
KEEP_SEC = 7 * 86400


def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def mark(path):
    """Enregistrement joue dans un journal : il n'est plus nouveau."""
    heard = _load()
    now = time.time()
    heard[os.path.basename(path)] = now
    heard = {k: t for k, t in heard.items() if now - t < KEEP_SEC}
    try:
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(heard, f)
        os.replace(tmp, PATH)
    except OSError as exc:
        print(f"[journal_vus] non ecrit : {exc}")


def heard_names():
    return set(_load())
