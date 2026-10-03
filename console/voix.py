"""Voix de la console : voix neuronale Microsoft (edge-tts, la meme que les
annonces) quand elle est choisie et disponible, espeak local sinon.

Deux usages distincts :
- echo de frappe et navigation (say(..., lock=False)) : immediat, sans
  verrou, coupe toute parole en cours -- taper une touche interrompt la
  voix, comme un lecteur d'ecran ;
- reponses aux commandes (say(..., lock=True)) : prennent le verrou audio
  partage avec les watchers (arbitre lib/audio_lock.py, source "console") pour ne pas
  parler par-dessus une annonce. Attente bornee a LOCK_WAIT_SEC : au-dela,
  on parle quand meme.

Voix Microsoft (reglages console.json, commande "voix") :
- cache sur la carte SD (CACHE_DIR) : noms de touches prepares en tache de
  fond (prewarm), et toute phrase courte deja dite -- rejoues sans reseau,
  silences de debut et de fin retires pour la fluidite ;
- le reste est joue au fil de l'eau (premier son en ~0,4 s mesure depuis
  le Pi, meme pour un long flash) ;
- un echo de touche n'attend jamais : pas encore en cache = espeak tout de
  suite, et le son Microsoft est prepare pour la fois suivante ;
- pas de son Microsoft dans FIRST_AUDIO_SEC, ou erreur : espeak prend le
  relais, et le service est considere en panne CLOUD_RETRY_SEC (on ne fait
  plus attendre personne pendant ce temps).
"""
import asyncio
import hashlib
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")
import espeak_out  # noqa: E402  (espeak -> aplay, jamais de verrou JACK)
import audio_lock  # noqa: E402  (priorites des haut-parleurs)
import reglages  # noqa: E402

try:
    import edge_tts
    import edge_langue
    edge_langue.force_language()  # requete en francais (edge-tts l'ecrit en anglais)
except ImportError:  # sans le module : espeak seul, la console reste utilisable
    edge_tts = None

LOCK_WAIT_SEC = 10
RATE_ECHO = 220
RATE_REPONSE = 170

ESPEAK = "espeak"
CACHE_DIR = "/home/vador/autopilot/voix_cache"
CACHE_MAX_BYTES = 100 * 1024 * 1024
ECHO_MAX_CHARS = 30      # au-dela : pas un nom de touche, on ne fait pas attendre l'echo
CACHE_TEXT_MAX = 200     # phrases plus longues : jouees en direct, jamais gardees
FIRST_AUDIO_SEC = 1.5
CLOUD_RETRY_SEC = 60
SYNTH_TIMEOUT_SEC = 10
STREAM_TIMEOUT_SEC = 120
# Phrases qui ne se rediront jamais a l'identique : heure et date (demande
# utilisateur 2026-09-30, "mardi 39 decembre a 23 h 56"). Jamais gardees.
VOLATILE = re.compile(
    r"\b\d{1,2} ?(heures?|h)\b|\b\d{1,2}:\d{2}\b"
    r"|\b(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b"
    r"|\b\d{1,2} (janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\b",
    re.IGNORECASE)

_proc = None
_proc_guard = threading.Lock()
_generation = 0
_cloud_down_until = 0.0
_settings = None


# --- Reglages ---------------------------------------------------------------

def settings():
    global _settings
    if _settings is None:
        _settings = reglages.get("console")
    return _settings


def reload():
    global _settings
    _settings = None
    return settings()


def _cloud_voice():
    voice = settings().get("voix") or ESPEAK
    if "Multilingual" in voice:  # bascule en anglais toute seule (2026-10-03) : voix francaise a la place
        voice = "fr-FR-DeniseNeural"
    if voice == ESPEAK or edge_tts is None or time.time() < _cloud_down_until:
        return None
    return voice


def _cloud_failed():
    global _cloud_down_until
    _cloud_down_until = time.time() + CLOUD_RETRY_SEC


# --- Cache -------------------------------------------------------------------

def _cache_path(text, voice, rate):
    digest = hashlib.md5(f"{rate}|{text}".encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, voice, f"{digest}.wav")


def _cached(text, voice, rate):
    path = _cache_path(text, voice, rate)
    if not os.path.exists(path):
        return None
    try:
        os.utime(path)  # plus recent = garde en priorite au menage
    except OSError:
        pass
    return path


def _store(mp3_bytes, path):
    """MP3 Microsoft -> WAV sans silence de debut ni de fin."""
    fd, tmp_mp3 = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    tmp_wav = path + ".tmp.wav"
    try:
        with open(tmp_mp3, "wb") as f:
            f.write(mp3_bytes)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        trim = "silenceremove=start_periods=1:start_threshold=-50dB"
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", tmp_mp3,
             "-af", f"{trim},areverse,{trim},areverse", "-ac", "1", tmp_wav],
            capture_output=True, timeout=30)
        if result.returncode == 0 and os.path.getsize(tmp_wav) > 0:
            os.replace(tmp_wav, path)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[voix] cache non ecrit : {exc}", file=sys.stderr)
    finally:
        for p in (tmp_mp3, tmp_wav):
            try:
                os.remove(p)
            except OSError:
                pass


def prune_cache():
    """Menage : au-dela de CACHE_MAX_BYTES, les sons les moins utilises partent."""
    files = []
    for root, _, names in os.walk(CACHE_DIR):
        for name in names:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
    total = sum(size for _, size, _ in files)
    for _, size, path in sorted(files):
        if total <= CACHE_MAX_BYTES:
            break
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass


async def _stream(text, voice, rate):
    async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
        if chunk["type"] == "audio":
            yield chunk["data"]


def _synthesize(text, voice, rate):
    """Son Microsoft complet (MP3), ou None."""
    async def run():
        data = bytearray()
        async for part in _stream(text, voice, rate):
            data += part
        return bytes(data)
    try:
        return asyncio.run(asyncio.wait_for(run(), timeout=SYNTH_TIMEOUT_SEC)) or None
    except Exception as exc:
        print(f"[voix] synthese Microsoft echouee : {exc!r}", file=sys.stderr)
        return None


# Preparation en tache de fond (noms de touches, echos courts).
_jobs = queue.Queue()
_pending = set()
_pending_guard = threading.Lock()


def _worker():
    while True:
        job = _jobs.get()
        text, voice, rate = job
        try:
            if time.time() >= _cloud_down_until and not _cached(text, voice, rate):
                mp3 = _synthesize(text, voice, rate)
                if mp3 is None:
                    _cloud_failed()
                else:
                    _store(mp3, _cache_path(text, voice, rate))
        finally:
            with _pending_guard:
                _pending.discard(job)


PREPARE_WORKERS = 3  # ~1 s par son : les 235 noms de touches en ~1 min 30
for _ in range(PREPARE_WORKERS):
    threading.Thread(target=_worker, daemon=True).start()


def _prepare(text, voice, rate):
    job = (text, voice, rate)
    with _pending_guard:
        if job in _pending:
            return
        _pending.add(job)
    _jobs.put(job)


def prewarm(texts):
    """Prepare en fond les sons d'echo (voix et debit d'echo actuels).
    Rend le nombre de sons a preparer."""
    voice = _cloud_voice()
    if voice is None:
        return 0
    rate = settings().get("debit_echo", "+50%")
    todo = [t for t in dict.fromkeys(texts) if t and not _cached(t, voice, rate)]
    for text in todo:
        _prepare(text, voice, rate)
    return len(todo)


def pending():
    with _pending_guard:
        return len(_pending)


# --- Lecteurs ------------------------------------------------------------------

class Clip:
    """Son deja en cache."""

    def __init__(self, path):
        self._p = subprocess.Popen(["aplay", "-q", "-D", "default", path], stderr=subprocess.DEVNULL)

    def wait(self, timeout=None):
        try:
            return self._p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            return None

    def kill(self):
        if self._p.poll() is None:
            self._p.kill()
        try:
            self._p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class Cloud:
    """Son Microsoft joue au fil de l'eau (mpg123 lit le MP3 sur son entree),
    relais espeak si rien n'arrive dans FIRST_AUDIO_SEC. keep : chemin ou
    garder le son s'il arrive en entier."""

    def __init__(self, text, voice, rate, espeak_rate, keep=None):
        self.text, self.voice, self.rate, self.espeak_rate, self.keep = text, voice, rate, espeak_rate, keep
        self._guard = threading.Lock()
        self._cancel = False
        self._audio = False
        self._fallback = None
        self._done = threading.Event()
        self._player = subprocess.Popen(["mpg123", "-q", "-o", "alsa", "-a", "default", "-"],
                                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._thread = threading.Thread(target=self._feed, daemon=True)
        self._thread.start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _feed(self):
        data = bytearray()
        complete = False

        async def pump():
            async for part in _stream(self.text, self.voice, self.rate):
                if self._cancel:
                    return False
                self._player.stdin.write(part)
                self._player.stdin.flush()
                data.extend(part)
                self._audio = True
            return True
        try:
            complete = asyncio.run(asyncio.wait_for(pump(), timeout=STREAM_TIMEOUT_SEC))
        except Exception as exc:
            if not self._cancel:
                print(f"[voix] lecture Microsoft interrompue : {exc!r}", file=sys.stderr)
        finally:
            try:
                self._player.stdin.close()
            except OSError:
                pass
            self._done.set()
        if complete and self.keep and data and not self._cancel:
            _store(bytes(data), self.keep)

    def _watchdog(self):
        deadline = time.time() + FIRST_AUDIO_SEC
        while time.time() < deadline and not self._audio and not self._done.is_set():
            time.sleep(0.05)
        with self._guard:
            if self._cancel or self._audio:
                return
            # Rien a temps (reseau, service Microsoft) : espeak prend le relais.
            _cloud_failed()
            self._cancel = True
            if self._player.poll() is None:
                self._player.kill()
            self._fallback = espeak_out.Speech(self.text, "fr", self.espeak_rate)

    def wait(self, timeout=None):
        end = None if timeout is None else time.time() + timeout

        def left():
            return None if end is None else max(0.1, end - time.time())
        self._thread.join(left())
        try:
            self._player.wait(left())
        except subprocess.TimeoutExpired:
            self.kill()
            return None
        # Laisse au relais espeak le temps de demarrer s'il doit prendre la main.
        deadline = time.time() + FIRST_AUDIO_SEC + 0.5
        while not self._audio and self._fallback is None and not self._cancel and time.time() < deadline:
            time.sleep(0.05)
        with self._guard:
            fallback = self._fallback
        return fallback.wait(left()) if fallback is not None else 0

    def kill(self):
        with self._guard:
            self._cancel = True
            fallback = self._fallback
        if self._player.poll() is None:
            self._player.kill()
        try:
            self._player.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if fallback is not None:
            fallback.kill()


# --- Interface ----------------------------------------------------------------

def stop():
    """Coupe immediatement toute parole en cours de la console."""
    global _proc, _generation
    with _proc_guard:
        _generation += 1
        if _proc is not None:
            _proc.kill()  # coupe si besoin, et ramasse toujours les processus
        _proc = None


def _set_current(p):
    global _proc
    with _proc_guard:
        _proc = p
    return p


def _spawn(text, echo):
    """Lance la phrase avec le meilleur moteur disponible, sans jamais
    attendre le reseau pour un echo de touche."""
    voice = _cloud_voice()
    espeak_rate = RATE_ECHO if echo else RATE_REPONSE
    if voice is None:
        return _set_current(espeak_out.Speech(text, "fr", espeak_rate))
    rate = settings().get("debit_echo" if echo else "debit", "+0%")
    path = _cached(text, voice, rate)
    if path:
        return _set_current(Clip(path))
    if echo and len(text) <= ECHO_MAX_CHARS:
        _prepare(text, voice, rate)  # Microsoft la prochaine fois
        return _set_current(espeak_out.Speech(text, "fr", espeak_rate))
    keep = None
    if len(text) <= CACHE_TEXT_MAX and not VOLATILE.search(text):
        keep = _cache_path(text, voice, rate)
    return _set_current(Cloud(text, voice, rate, espeak_rate, keep))


def _speak_locked(text, generation):
    # Source "console" de l'arbitre (lib/audio_lock.py) ; on n'attend pas plus
    # de LOCK_WAIT_SEC : l'utilisateur attend sa reponse.
    lock = audio_lock.acquire("console", timeout=LOCK_WAIT_SEC)
    try:
        # Une touche pressee pendant l'attente annule la reponse ; abandon
        # (priorites) : on se tait.
        if generation != _generation or lock.dropped:
            return
        _spawn(text, echo=False).wait()
    finally:
        audio_lock.release(lock)


def say(text, lock=False):
    """Parle sans bloquer l'appelant (la boucle clavier reste reactive)."""
    stop()
    if not text:
        return
    if lock:
        threading.Thread(target=_speak_locked, args=(text, _generation), daemon=True).start()
    else:
        _spawn(text, echo=True)


def say_and_wait(text):
    """Pour les rares cas ou la suite doit attendre la fin de la phrase
    (message d'accueil, annonce avant un scan radio qui monopolise le Pi)."""
    stop()
    _speak_locked(text, _generation)


def say_blocking(text):
    """Parle et attend la fin, SANS le verrou audio : pour les dialogues qui
    le tiennent deja (journal, archives) et doivent enchainer une phrase
    puis un enregistrement sans chevauchement."""
    stop()
    if text:
        _spawn(text, echo=False).wait(timeout=30)


def preview(text, voice, rate):
    """Fait entendre `text` avec une voix et un debit donnes (choix de la
    voix) : son en cache s'il existe, sinon Microsoft en direct, garde."""
    stop()
    if voice == ESPEAK or edge_tts is None:
        return _set_current(espeak_out.Speech(text, "fr", RATE_REPONSE))
    path = _cached(text, voice, rate)
    if path:
        return _set_current(Clip(path))
    return _set_current(Cloud(text, voice, rate, RATE_REPONSE, _cache_path(text, voice, rate)))
