import fcntl

# Verrou de fichier partage entre radio_watch.py et adsb_watch.py (processus
# systemd separes, aucun etat Python en commun) : garantit qu'une seule
# annonce/diffusion audio a la fois utilise les haut-parleurs. flock() est
# automatiquement libere par le noyau si le process qui le tient meurt/est
# tue, evitant les verrous "fantomes" qu'un simple fichier-drapeau
# provoquerait.
LOCK_PATH = "/tmp/radio_watch_audio.lock"


def acquire():
    """Bloque jusqu'a obtenir l'exclusivite audio. Retourne le descripteur
    de fichier a repasser a release()."""
    f = open(LOCK_PATH, "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def release(lock_file):
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()
