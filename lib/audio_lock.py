import fcntl
import time

# Verrou de fichier partage entre tous les watchers qui parlent (processus
# systemd separes, aucun etat Python en commun) : garantit qu'une seule
# annonce/diffusion audio a la fois utilise les haut-parleurs. flock() est
# automatiquement libere par le noyau si le process qui le tient meurt/est
# tue, evitant les verrous "fantomes" qu'un simple fichier-drapeau
# provoquerait.
LOCK_PATH = "/tmp/radio_watch_audio.lock"

# Delai maximum d'attente avant d'abandonner et de continuer SANS le
# verrou (2026-08-31, ajoute suite a un vrai incident constate en
# session : un ffmpeg bloque dans sncf_watch.py -- probablement un alea
# materiel/pilote audio, pas un bug de sncf_watch.py lui-meme -- a fige
# TOUT le systeme de voix du projet pendant plusieurs heures, radio_watch
# inclus (lui aussi bloque en attente du meme verrou), faute de timeout
# ici : fcntl.flock() bloque par defaut indefiniment, sans aucune limite.
# Remonte a 300s (2026-08-31, apres coup) : l'incident qui a motive ce
# correctif n'etait PAS un vrai blocage logiciel -- train_scaner/
# ter_arriving/1.mp3 dure en realite 3min20 (fichier bien plus long
# qu'un son d'accompagnement, a verifier/recouper cote contenu), et
# jouait donc legitimement tout ce temps en tenant le verrou. 180s
# passait trop pres de cette duree reelle ; 300s laisse une vraie marge
# au-dessus du pire cas connu du projet tout en bornant quand meme
# l'attente (au lieu d'indefiniment) si un jour un detenteur est
# reellement bloque de facon anormale.
ACQUIRE_TIMEOUT_SEC = 300
POLL_INTERVAL_SEC = 0.5


def acquire():
    """Tente d'obtenir l'exclusivite audio pendant jusqu'a
    ACQUIRE_TIMEOUT_SEC. Retourne le descripteur de fichier a repasser a
    release() dans TOUS les cas -- meme si le delai expire, pour ne pas
    changer le contrat (lock = acquire(); ... ; release(lock)) utilise
    partout dans le projet. Si le delai expire (verrou bloque ailleurs de
    facon anormale), continue sans l'exclusivite plutot que de rester
    bloque indefiniment."""
    f = open(LOCK_PATH, "w")
    deadline = time.time() + ACQUIRE_TIMEOUT_SEC
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.time() >= deadline:
                print(f"[audio_lock] verrou audio indisponible depuis {ACQUIRE_TIMEOUT_SEC}s "
                      f"(un autre watcher semble bloque de facon anormale) -- "
                      f"on continue SANS l'exclusivite plutot que de figer tout le systeme de voix.")
                return f
            time.sleep(POLL_INTERVAL_SEC)


def release(lock_file):
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()
