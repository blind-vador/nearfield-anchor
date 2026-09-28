"""Voix de la console (espeak local, jamais de cloud : la console doit
repondre instantanement, meme sans reseau).

Deux usages distincts :
- echo de frappe (say(..., lock=False)) : immediat, sans verrou, coupe
  toute parole en cours -- taper une touche interrompt la voix, comme un
  lecteur d'ecran ;
- reponses aux commandes (say(..., lock=True)) : prennent le verrou audio
  partage avec les watchers (/tmp/radio_watch_audio.lock) pour ne pas
  parler par-dessus une annonce. Attente bornee a LOCK_WAIT_SEC : au-dela,
  on parle quand meme -- l'utilisateur attend une reponse, il ne doit pas
  rester des minutes dans le silence derriere une longue annonce de gare.
"""
import fcntl
import subprocess
import threading
import time

LOCK_PATH = "/tmp/radio_watch_audio.lock"
LOCK_WAIT_SEC = 10
RATE_ECHO = 220
RATE_REPONSE = 170

_proc = None
_proc_guard = threading.Lock()
_generation = 0


def stop():
    """Coupe immediatement toute parole en cours de la console."""
    global _proc, _generation
    with _proc_guard:
        _generation += 1
        if _proc is not None and _proc.poll() is None:
            _proc.kill()
        _proc = None


def _spawn(text, rate):
    global _proc
    p = subprocess.Popen(
        ["espeak", "-v", "fr", "-b", "1", "-s", str(rate), text],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with _proc_guard:
        _proc = p
    return p


def _acquire_lock():
    f = open(LOCK_PATH, "w")
    deadline = time.time() + LOCK_WAIT_SEC
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.time() >= deadline:
                return f
            time.sleep(0.2)


def _speak_locked(text, generation):
    lock = _acquire_lock()
    try:
        # Une touche pressee pendant l'attente du verrou annule la reponse.
        if generation != _generation:
            return
        _spawn(text, RATE_REPONSE).wait()
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


def say(text, lock=False):
    """Parle sans bloquer l'appelant (la boucle clavier reste reactive)."""
    stop()
    if not text:
        return
    if lock:
        threading.Thread(target=_speak_locked, args=(text, _generation), daemon=True).start()
    else:
        _spawn(text, RATE_ECHO)


def say_and_wait(text):
    """Pour les rares cas ou la suite doit attendre la fin de la phrase
    (message d'accueil, annonce avant un scan radio qui monopolise le Pi)."""
    stop()
    _speak_locked(text, _generation)
