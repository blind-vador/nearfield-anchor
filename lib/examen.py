"""File d'examen a la demande (2026-10-10) : la touche "e" du journal met un
enregistrement en file pour que la passe d'analyse du Mac mini le reprenne (en
differe) -- elle recupere le BRUT, tente plusieurs nettoyages et depose une
proposition dans le "journal proposition". Permet d'examiner un clip precis
sans attendre la passe nocturne, et d'affiner les filtres du Pi.

Fichier partage sur le NAS (lu par mac/analyse.py --queue) :
radio_log/a_examiner.json = [{fichier, le}]. Ecriture atomique sous verrou.
"""
import fcntl
import json
import os
import time

from sound_paths import RADIO_LOG_DIR

QUEUE_PATH = os.path.join(RADIO_LOG_DIR, "a_examiner.json")
LOCK_PATH = "/tmp/examen.lock"


def _read():
    try:
        with open(QUEUE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def demander(fichier):
    """Met l'enregistrement `fichier` (nom de base) en file d'examen Mac.
    Retourne True si ajoute, False s'il y etait deja (ou NAS inaccessible)."""
    try:
        os.makedirs(RADIO_LOG_DIR, exist_ok=True)
        with open(LOCK_PATH, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = _read()
            if any(e.get("fichier") == fichier for e in data):
                return False
            data.append({"fichier": fichier, "le": time.time()})
            tmp = QUEUE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, QUEUE_PATH)
            return True
    except OSError:
        return False
