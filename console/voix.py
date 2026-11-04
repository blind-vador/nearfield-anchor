"""Voix de la console : voix neuronale Microsoft (edge-tts, la meme que les
annonces) quand elle est choisie et disponible ; voix neuronale locale Piper
(choisie, ou en secours de Microsoft, 2026-10-18) ; espeak en dernier.

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
import chemins
import asyncio
import audioop
import hashlib
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

import espeak_out  # noqa: E402  (espeak -> aplay, jamais de verrou JACK)
import piper_out  # noqa: E402  (voix neuronales locales, 2026-10-18)
import phrases_fixes  # noqa: E402  (phrases fixes, cache phrase par phrase)
import voix_sources  # noqa: E402  (voix par source : console, touches, 2026-10-19)
import audio_lock  # noqa: E402  (priorites des haut-parleurs)
import cache_voix  # noqa: E402  (phrases en cache, validation)
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
EDGE_VOICES = ["fr-FR-DeniseNeural", "fr-FR-EloiseNeural", "fr-FR-HenriNeural", "fr-BE-CharlineNeural",
               "fr-BE-GerardNeural", "fr-CA-ThierryNeural", "fr-CA-AntoineNeural", "fr-CA-JeanNeural",
               "fr-CA-SylvieNeural", "fr-CH-ArianeNeural", "fr-CH-FabriceNeural"]  # comme speak.EDGE_TTS_VOICES
PIPER = "piper:"  # voix neuronales locales (lib/piper_out.py) : "piper:siwis"...
CACHE_DIR = chemins.base("voix_cache")
CACHE_MAX_BYTES = 1024 * 1024 * 1024  # toutes les phrases fixes en voix Piper (2026-10-18)
ECHO_MAX_CHARS = 30      # au-dela : pas un nom de touche, on ne fait pas attendre l'echo
CACHE_TEXT_MAX = 200     # phrases plus longues : jouees en direct, jamais gardees
FIRST_AUDIO_SEC = 2.5    # mesure 2026-10-22 : 0,7 a 1,6 s selon la voix (edge-tts 7.3)
LENTS_MAX = 3            # retards de suite avant de declarer Microsoft absent
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
_canal = "reponses"   # curseur de la commande "volume" de la phrase en cours (2026-10-22)


def _facteur():
    try:
        import volumes
        return volumes.facteur("console", _canal)
    except Exception:
        return 1.0


def _echelle():
    """Volume de la console pour les voix qui relaient le son (Piper, espeak),
    ou None s'il n'y a rien a changer."""
    f = _facteur()
    if abs(f - 1.0) < 0.005:
        return None

    def appliquer(data, ticket=None):
        try:
            return audioop.mul(data[: len(data) - len(data) % 2], 2, f)
        except audioop.error:
            return data
    return appliquer
_generation = 0
# Etat de Microsoft (2026-10-19) : une seule voix a la fois dans la console.
# Tant qu'une sonde n'a pas confirme que Microsoft repond, on le tient pour
# absent : toutes les phrases passent par la voix de secours, jamais un
# melange "Denise en cache / Piper pour le reste" d'une reponse a l'autre.
PROBE_SEC = 300
_cloud_down_until = time.time() + 3600
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


_lents = 0


def _cloud_lent(arrive):
    """Premier son Microsoft a temps (arrive) ou non. Un retard : cette phrase
    seule passe en secours ; Microsoft nest declare absent quapres LENTS_MAX
    retards de suite (2026-10-22 : des voix a 1,5 s pile, une hesitation
    basculait toute la console en secours pour 6 minutes)."""
    global _lents
    if arrive:
        _lents = 0
        return
    _lents += 1
    if _lents >= LENTS_MAX:
        _lents = 0
        _cloud_failed()


def _cloud_failed():
    """Microsoft n'a pas repondu : absent jusqu'a ce que la sonde le retrouve."""
    global _cloud_down_until
    _cloud_down_until = time.time() + PROBE_SEC + 60


def cloud_ok():
    return time.time() >= _cloud_down_until


def _probe():
    """Microsoft repond-il ? Seulement au demarrage et pendant une panne, toutes
    les PROBE_SEC (2026-10-22 : plus de sonde quand tout va bien, la parole
    elle-meme signale les refus -- voix_sources.erreur_microsoft)."""
    global _cloud_down_until
    while True:
        if edge_tts is not None and not cloud_ok():
            ok = bool(_synthesize("ok", "fr-FR-DeniseNeural", "+0%"))
            if ok and not cloud_ok():
                print("[voix] Microsoft repond : retour aux voix choisies", file=sys.stderr)
            _cloud_down_until = 0.0 if ok else time.time() + PROBE_SEC + 60
            voix_sources.noter_microsoft(ok)  # partage avec toutes les veilles
        time.sleep(PROBE_SEC)


# --- Cache -------------------------------------------------------------------

def _cache_path(text, voice, rate):
    digest = hashlib.md5(f"{rate}|{text}".encode("utf-8")).hexdigest()
    folder = os.path.join(CACHE_DIR, voice.replace(":", "_"))
    cache_voix.noter(folder, text, rate)  # texte retrouvable (commande "phrases", 2026-10-19)
    return os.path.join(folder, f"{digest}.wav")


def is_piper(voice):
    return bool(voice) and voice.startswith(PIPER)


def fallback_voice(source="console"):
    """Voix Piper qui prend le relais quand Microsoft ne repond pas."""
    sec = voix_sources.secours(source)
    return sec["voix"] if sec["moteur"] == "piper" else None


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
    """Menage : au-dela de CACHE_MAX_BYTES, les sons les moins utilises partent
    (jamais une phrase validee, commande "phrases")."""
    files = []
    valides = cache_voix.validees()
    for root, _, names in os.walk(CACHE_DIR):
        for name in names:
            path = os.path.join(root, name)
            if path in valides or not name.endswith(".wav"):
                continue
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


def _rk(choix):
    """Cle debit (+ hauteur) d'un choix de voix_sources : "+20%" ou "+20%~-10Hz"."""
    hauteur = choix.get("hauteur") or voix_sources.NEUTRE_HAUTEUR
    if choix.get("moteur") != "edge" or hauteur == voix_sources.NEUTRE_HAUTEUR:
        return choix.get("debit") or voix_sources.NEUTRE_DEBIT
    return f"{choix.get('debit') or voix_sources.NEUTRE_DEBIT}~{hauteur}"


async def _stream(text, voice, rate):
    rate, _, pitch = rate.partition("~")
    async for chunk in edge_tts.Communicate(text, voice, rate=rate, pitch=pitch or "+0Hz").stream():
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
        voix_sources.erreur_microsoft(exc)
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


# Preparation Piper : par lots, voix chargee une fois (lib/piper_out.batch).
_piper_jobs = queue.Queue()
PIPER_BATCH = 40


def _piper_worker():
    while True:
        jobs = [_piper_jobs.get()]
        time.sleep(0.5)
        while len(jobs) < PIPER_BATCH:
            try:
                jobs.append(_piper_jobs.get_nowait())
            except queue.Empty:
                break
        groups = {}
        for text, voice, rate in jobs:
            groups.setdefault((voice, rate), []).append(text)
        for (voice, rate), texts in groups.items():
            try:
                todo = [(t, _cache_path(t, voice, rate)) for t in dict.fromkeys(texts) if not _cached(t, voice, rate)]
                if todo and piper_out.available():
                    piper_out.batch(voice, todo, piper_out.length_scale(rate))
            finally:
                with _pending_guard:
                    for t in texts:
                        _pending.discard((t, voice, rate))


threading.Thread(target=_piper_worker, daemon=True).start()


def _prepare_piper(text, voice, rate):
    job = (text, voice, rate)
    with _pending_guard:
        if job in _pending:
            return
        _pending.add(job)
    _piper_jobs.put(job)


def preparer(text, echo=True):
    """Calcule d'avance, en fond, les phrases de text dans la voix ou il sera
    dit (echo : voix des touches, celle des dialogues ; sinon la console) :
    dit plus tard, il part tout de suite au lieu de laisser des blancs de
    calcul entre les phrases (touche O des plantes, 2026-10-20). Piper
    seulement ; rend le nombre de phrases mises en preparation."""
    ch = voix_sources.choisir("touches" if echo else "console", ms_ok=cloud_ok())
    if ch["moteur"] != "piper":
        return 0
    n = 0
    for part in phrases_fixes.split(text):
        if _cacheable(part) and not _cached(part, ch["voix"], ch["debit"]):
            _prepare_piper(part, ch["voix"], ch["debit"])
            n += 1
    return n


def prewarm(texts):
    """Prepare en fond les sons d'echo (voix et debit des touches du clavier).
    Rend le nombre de sons a preparer."""
    ch = voix_sources.choisir("touches", ms_ok=cloud_ok())
    todo = []
    if ch["moteur"] == "edge" and not ch["aleatoire"]:
        todo = [t for t in dict.fromkeys(texts) if t and not _cached(t, ch["voix"], _rk(ch))]
        for text in todo:
            _prepare(text, ch["voix"], _rk(ch))
        ch = voix_sources.secours("touches")
    if ch["moteur"] == "piper":
        missing = [t for t in dict.fromkeys(texts) if t and not _cached(t, ch["voix"], ch["debit"])]
        for text in missing:
            _prepare_piper(text, ch["voix"], ch["debit"])
        todo = todo or missing
    return len(todo)


def prechauffer(texts, echo=True):
    """Phrases du dialogue affiché, dans la voix qui va les dire.
    Une seule voix. Espeak n'a pas de cache à remplir."""
    propres = []
    for text in texts or ():
        if text and str(text).strip() and text not in propres:
            propres.append(text)
    if not propres:
        return 0
    threading.Thread(target=_prechauffer, args=(propres, echo), daemon=True).start()
    return len(propres)


def _prechauffer(texts, echo):
    try:
        source = "touches" if echo else "console"
        ch = voix_sources.choisir(source, ms_ok=cloud_ok())
        if voix_sources.en_direct(ch):
            return
        if ch["moteur"] not in ("edge", "piper") or (ch["moteur"] == "edge" and ch.get("aleatoire")):
            return
        import annonce_morceaux as morceaux
        import atomes_parole
        for text in texts:
            if not echo and morceaux.a_assembler(text) and not voix_sources.phrase_entiere(ch):
                morceaux.ensure(morceaux.expand_texte(text), ch)
                continue
            dit = text if echo else atomes_parole.indicatifs_en_clair(text)
            if not _cacheable(dit):
                continue
            if ch["moteur"] == "piper":
                if not _cached(dit, ch["voix"], ch["debit"]):
                    _prepare_piper(dit, ch["voix"], ch["debit"])
            else:
                rate = _rk(ch)
                if not _cached(dit, ch["voix"], rate):
                    _prepare(dit, ch["voix"], rate)
    except Exception as exc:
        print(f"[voix] prechauffe : {exc!r}", file=sys.stderr)


def pending():
    with _pending_guard:
        return len(_pending)


# --- Lecteurs ------------------------------------------------------------------

class Clip:
    """Son deja en cache. supprimer : efface le fichier a la fin (annonce
    collee dans un fichier temporaire ; les mots, eux, restent en cache)."""

    def __init__(self, path, supprimer=False):
        self._path, self._supprimer = path, supprimer
        f = _facteur()
        args = ["aplay", "-q", "-D", "default", path] if abs(f - 1.0) < 0.005 \
            else ["play", "-q", "-v", f"{f:.3f}", path]  # sox : le volume de la console
        self._p = subprocess.Popen(args, stderr=subprocess.DEVNULL)

    def _jeter(self):
        if self._supprimer:
            self._supprimer = False
            try:
                os.remove(self._path)
            except OSError:
                pass

    def wait(self, timeout=None):
        try:
            return self._p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            return None
        finally:
            self._jeter()

    def kill(self):
        if self._p.poll() is None:
            self._p.kill()
        try:
            self._p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        self._jeter()


class Cloud:
    """Son Microsoft joue au fil de l'eau (mpg123 lit le MP3 sur son entree),
    relais espeak si rien n'arrive dans FIRST_AUDIO_SEC. keep : chemin ou
    garder le son s'il arrive en entier."""

    def __init__(self, text, voice, rate, espeak_rate, keep=None, keep_backup=None, backup=None):
        self.text, self.voice, self.rate, self.espeak_rate, self.keep = text, voice, rate, espeak_rate, keep
        self.keep_backup = keep_backup
        self.backup = backup or (None, rate)   # (voix de secours, son debit)
        self._guard = threading.Lock()
        self._cancel = False
        self._audio = False
        self._fallback = None
        self._done = threading.Event()
        self._player = subprocess.Popen(["mpg123", "-q", "-o", "alsa", "-a", "default",
                                         "-f", str(int(32768 * _facteur())), "-"],
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
            complete = asyncio.run(asyncio.wait_for(pump(), timeout=max(STREAM_TIMEOUT_SEC,
                                                                        espeak_out.duree_max(self.text))))
        except Exception as exc:
            if not self._cancel:
                print(f"[voix] lecture Microsoft interrompue : {exc!r}", file=sys.stderr)
                voix_sources.erreur_microsoft(exc)
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
                if self._audio:
                    _cloud_lent(True)
                return
            # Rien a temps (reseau, service Microsoft) : la voix de secours dit cette phrase.
            _cloud_lent(False)
            self._cancel = True
            if self._player.poll() is None:
                self._player.kill()
            self._fallback = _local_speech(self.text, self.backup[1], self.espeak_rate, self.keep_backup,
                                           self.backup[0])

    def wait(self, timeout=None):
        # Sans timeout explicite : borne quand meme (handshake Microsoft peut
        # rester ouvert jusqu'a STREAM_TIMEOUT_SEC et figer l'appelant).
        if timeout is None:
            timeout = espeak_out.duree_max(self.text)
        end = time.time() + timeout

        def left():
            return max(0.1, end - time.time())
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
        if fallback is None:
            return 0
        try:
            return fallback.wait(left())
        except subprocess.TimeoutExpired:
            fallback.kill()
            return None

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


def _local_speech(text, rate, espeak_rate, keep=None, voice=None):
    """Voix locale : Piper (gardee dans keep si la phrase va au bout), espeak
    s'il est absent ou que le Pi est trop chaud."""
    voice = voice or fallback_voice()
    if voice and piper_out.available(voice):
        try:
            return piper_out.Speech(text, scale=_echelle(), voice_id=voice, scale_len=piper_out.length_scale(rate),
                                    keep_path=keep)
        except Exception as exc:
            print(f"[voix] piper : {exc!r}", file=sys.stderr)
    return espeak_out.Speech(text, "fr", espeak_rate, scale=_echelle())


def _cacheable(text):
    return len(text) <= CACHE_TEXT_MAX and not VOLATILE.search(text)


def _demande_cached(text, demande):
    """Ce que la voix demandee (Microsoft, absent en ce moment) a deja en cache."""
    if not demande:
        return None
    rate = _rk(demande)
    if not demande.get("aleatoire"):
        return _cached(text, demande["voix"], rate)
    import random
    for voice in random.sample(EDGE_VOICES, len(EDGE_VOICES)):
        path = _cached(text, voice, rate)
        if path:
            return path
    return None


def _piper_spawn(text, voice, rate, echo, espeak_rate, demande=None):
    """Voix Piper (choisie, ou en secours de Microsoft) : ce que la voix
    demandee a deja en cache d'abord (2026-10-19 : Denise reste la voix
    voulue), puis le cache Piper ; un echo pas encore pret = espeak tout de
    suite et Piper prepare pour la fois suivante ; une reponse = Piper en
    flux, gardee."""
    path = _demande_cached(text, demande) or _cached(text, voice, rate)
    if path:
        return _set_current(Clip(path))
    if echo and len(text) <= ECHO_MAX_CHARS:
        _prepare_piper(text, voice, rate)
        return _set_current(espeak_out.Speech(text, "fr", espeak_rate, scale=_echelle()))
    # Phrase par phrase (2026-10-18) : les phrases deja pretes (toutes les
    # phrases fixes de l'interface, preparees d'avance) sont jouees depuis le
    # cache, seules les autres (reponse de Gemini, valeurs) sont calculees.
    parts = phrases_fixes.split(text)
    if len(parts) > 1:
        return _set_current(Suite(parts, voice, rate, espeak_rate, demande))
    keep = _cache_path(text, voice, rate) if _cacheable(text) else None
    return _set_current(_local_speech(text, rate, espeak_rate, keep, voice))


class Suite:
    """Plusieurs phrases a la suite, une voix : cache quand la phrase est
    prete, Piper en flux sinon (gardee si la phrase est fixe ou courte)."""

    def __init__(self, parts, voice, rate, espeak_rate, demande=None):
        self.parts, self.voice, self.rate, self.espeak_rate = parts, voice, rate, espeak_rate
        self.demande = demande
        # Le cache Microsoft ne sert que s'il contient toute la suite.
        # Sinon tout est dit dans la voix choisie : pas Denise puis espeak.
        self._demande_entiere = demande if (
            demande and all(_demande_cached(p, demande) for p in parts)
        ) else None
        self._current = None
        self._killed = False
        self._guard = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        for part in self.parts:
            with self._guard:
                if self._killed:
                    return
                path = (_demande_cached(part, self._demande_entiere)
                if self._demande_entiere else _cached(part, self.voice, self.rate))
                if path:
                    self._current = Clip(path)
                else:
                    keep = _cache_path(part, self.voice, self.rate) if (
                        phrases_fixes.est_fixe(part) or _cacheable(part)) else None
                    self._current = _local_speech(part, self.rate, self.espeak_rate, keep, self.voice)
                current = self._current
            current.wait(espeak_out.duree_max(part, 60))

    def poll(self):
        return None if self._thread.is_alive() else 0

    def wait(self, timeout=None):
        self._thread.join(timeout if timeout is not None else espeak_out.duree_max(" ".join(self.parts), 120))
        if self._thread.is_alive():
            self.kill()
            return None
        return 0

    def kill(self):
        with self._guard:
            self._killed = True
            current = self._current
        if current is not None:
            current.kill()


def _spawn(text, echo):
    """Lance la phrase dans la voix de sa source ("touches" pour l'echo du
    clavier, "console" pour les reponses ; commande "voix") : Microsoft si
    choisi et present (cache d'abord), sinon la voix de secours pour TOUT
    (une seule voix a la fois), espeak en dernier. Jamais d'attente reseau
    pour un echo de touche."""
    global _canal
    _canal = "touches" if echo else "reponses"
    espeak_rate = RATE_ECHO if echo else RATE_REPONSE
    source = "touches" if echo else "console"
    ch = voix_sources.choisir(source, ms_ok=cloud_ok())
    if voix_sources.en_direct(ch):
        if not echo:
            try:
                import atomes_parole
                text = atomes_parole.indicatifs_en_clair(text)
            except Exception as exc:
                print(f"[voix] direct : {exc!r}", file=sys.stderr)
        voix_id = ch.get("voix") or ""
        if voix_id.startswith("piper:mbrola#") and piper_out.available(voix_id):
            return _set_current(piper_out.Speech(
                text, scale=_echelle(), voice_id=voix_id,
                scale_len=piper_out.length_scale(ch.get("debit"))))
        return _set_current(espeak_out.Speech(text, "fr", espeak_rate, scale=_echelle()))
    if not echo and ch["moteur"] in ("edge", "piper"):
        # « 410 » et un indicatif : mots deja en cache, pas la phrase entiere
        # (elle contient l'heure ou un nombre, le cache de la console la refuse).
        try:
            import annonce_morceaux as morceaux
            if morceaux.a_assembler(text) and not voix_sources.phrase_entiere(ch):
                choix = {"moteur": ch["moteur"], "voix": ch["voix"],
                         "debit": ch.get("debit") or voix_sources.NEUTRE_DEBIT,
                         "hauteur": ch.get("hauteur") or voix_sources.NEUTRE_HAUTEUR}
                # Toute la phrase dans une voix. Le secours reprend tout,
                # il ne bouche pas les trous.
                sec = voix_sources.secours(source)
                repli = sec if sec.get("moteur") in ("edge", "piper") else None
                return _set_current(morceaux.Lecture(
                    morceaux.expand_texte(text), choix, volume=_facteur(), repli=repli))
            import atomes_parole
            text = atomes_parole.indicatifs_en_clair(text)
        except Exception as exc:
            print(f"[voix] assemblage : {exc!r}", file=sys.stderr)
    if ch["moteur"] == voix_sources.ESPEAK:
        return _set_current(espeak_out.Speech(text, "fr", espeak_rate, scale=_echelle()))
    if ch["moteur"] == "piper":
        return _piper_spawn(text, ch["voix"], ch["debit"], echo, espeak_rate, ch.get("demande"))
    voice, rate = ch["voix"], _rk(ch)
    sec = voix_sources.secours(source)
    backup, brate = (sec["voix"], sec["debit"]) if sec["moteur"] == "piper" else (None, rate)
    path = _cached(text, voice, rate)
    if path:
        return _set_current(Clip(path))
    if echo and len(text) <= ECHO_MAX_CHARS:
        _prepare(text, voice, rate)  # Microsoft la prochaine fois
        path = _cached(text, backup, brate) if backup else None
        return _set_current(Clip(path) if path else espeak_out.Speech(text, "fr", espeak_rate, scale=_echelle()))
    keep = _cache_path(text, voice, rate) if _cacheable(text) else None
    keep_backup = _cache_path(text, backup, brate) if backup and _cacheable(text) else None
    return _set_current(Cloud(text, voice, rate, espeak_rate, keep, keep_backup, (backup, brate)))


def _speak_locked(text, generation):
    # Source "console" de l'arbitre (lib/audio_lock.py) ; on n'attend pas plus
    # de LOCK_WAIT_SEC : l'utilisateur attend sa reponse.
    lock = audio_lock.acquire("console", timeout=LOCK_WAIT_SEC)
    try:
        # Une touche pressee pendant l'attente annule la reponse ; abandon
        # (priorites) : on se tait.
        if generation != _generation or lock.dropped:
            return
        # Timeout dur : un aplay/mpg123 bloque (carte USB saturee) ne doit
        # jamais garder le verrou audio ni laisser croire que le clavier est mort.
        _spawn(text, echo=False).wait(timeout=espeak_out.duree_max(text))
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
        _tenir(_spawn(text, echo=True))


def _tenir(proc):
    """La console se declare a l'arbitre le temps de la phrase, sans attendre
    (rang 0) : les sources moins prioritaires passent dessous, attenuees, pendant qu'elle
    parle, au lieu de parler par-dessus (2026-10-16)."""
    try:
        ticket = audio_lock.prendre_sans_attendre("console")
    except Exception:
        return

    def fin():
        try:
            proc.wait(timeout=espeak_out.duree_max(getattr(proc, "text", "") or "", 120))
        except Exception:
            pass
        audio_lock.release(ticket)
    threading.Thread(target=fin, daemon=True).start()


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
        _spawn(text, echo=False).wait(timeout=espeak_out.duree_max(text))


def preview(text, voice, rate, pitch=None):
    """Fait entendre `text` avec une voix, un debit et une hauteur (choix de
    la voix) : son en cache s'il existe, sinon en direct, garde. Voix
    "aleatoire" : une voix tiree au hasard, comme a l'usage."""
    import random
    global _canal
    stop()
    _canal = "reponses"
    if voice in voix_sources.MODES_MELANGES or voice == voix_sources.ALEATOIRE_PIPER:
        voice = random.choice(voix_sources.tirage(voice, bool(cloud_ok() and edge_tts)) or [piper_out.DEFAULT_VOICE])
    elif voice == voix_sources.ALEATOIRE and not (cloud_ok() and edge_tts):
        voice = random.choice(piper_out.announce_voice_ids() or [piper_out.DEFAULT_VOICE])
    elif voice == voix_sources.ALEATOIRE:
        voice = random.choice(EDGE_VOICES)
    if is_piper(voice):
        voice = piper_out.canonical(voice)
        path = _cached(text, voice, rate)
        if path:
            return _set_current(Clip(path))
        return _set_current(_local_speech(text, rate, RATE_REPONSE, _cache_path(text, voice, rate), voice))
    if voice == ESPEAK or edge_tts is None or not cloud_ok():
        return _set_current(espeak_out.Speech(text, "fr", RATE_REPONSE, scale=_echelle()))
    rk = _rk({"moteur": "edge", "debit": rate, "hauteur": pitch or voix_sources.NEUTRE_HAUTEUR})
    path = _cached(text, voice, rk)
    if path:
        return _set_current(Clip(path))
    return _set_current(Cloud(text, voice, rk, RATE_REPONSE, _cache_path(text, voice, rk)))


threading.Thread(target=_probe, daemon=True).start()  # etat de Microsoft, partage (lib/voix_sources.py)
