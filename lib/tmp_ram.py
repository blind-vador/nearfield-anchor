"""Dossier temporaire en memoire vive pour les sons de passage : rien n'use la carte
SD (2026-11-06). Ce qu'un programme oublie est efface au bout de 6 heures."""
import os
import shutil
import time

DOSSIER = "/dev/shm/nearfield_tmp"
LIBRE_MIN = 64 * 1024 * 1024
AGE_MAX_SEC = 6 * 3600
PURGE_SEC = 600
_purge_a = 0.0


def _purger():
    global _purge_a
    if time.time() - _purge_a < PURGE_SEC:
        return
    _purge_a = time.time()
    with os.scandir(DOSSIER) as entrees:
        for e in entrees:
            try:
                if time.time() - e.stat(follow_symlinks=False).st_mtime > AGE_MAX_SEC:
                    if e.is_dir(follow_symlinks=False):
                        shutil.rmtree(e.path, ignore_errors=True)
                    else:
                        os.remove(e.path)
            except OSError:
                pass


def dossier():
    """Le dossier en memoire, ou None (dossier temporaire habituel) s'il n'y en a pas."""
    try:
        os.makedirs(DOSSIER, mode=0o700, exist_ok=True)
        if shutil.disk_usage(DOSSIER).free < LIBRE_MIN:
            return None
        _purger()
        return DOSSIER
    except OSError:
        return None
