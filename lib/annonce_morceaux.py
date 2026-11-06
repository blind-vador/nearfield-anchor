"""Annonces assemblees par morceaux (demande utilisateur 2026-10-18), comme
les vraies annonces de gare : chaque morceau ("va etre mis en place",
"entrera en gare a", "Lyon Part-Dieu", "17 heure", "42"...) est synthetise
UNE fois par voix et garde dans le cache des phrases repetitives (meme
dossier que les echantillons Microsoft : <voix>.mp3, piper-<voix>.wav) ;
seuls les morceaux jamais entendus sont calcules, et une annonce prevue
d'avance l'est des qu'elle est previsible (prepare()).

Une annonce = UNE voix du debut a la fin : Microsoft s'il repond (il remplit
alors le meme cache), sinon une voix Piper francaise tiree au hasard.
L'assemblage passe par play_sound_effect : effets (gare) et journal radio
comme toute annonce.

Morceaux :
  str          garde en cache (fixe, ou valeur qui revient : gare, heure...)
  Live(str)    calcule a chaque fois, jamais garde (commentaire unique...)

Nombres et indicatifs (lib/atomes_parole.py, 2026-11-01) : « 410 » n'est plus
une phrase nouvelle. Il est colle depuis les mots deja en cache (« quatre »,
« cent », « dix »). Une lettre seule ou un indicatif (AFR410, F-HBOD, F4ABC)
se dit en alphabet radio international, lettre par lettre, depuis les memes
mots. Le reste de la phrase n'est pas decoupe.
"""
import os
import queue
import random
import re
import shutil
import subprocess
import tempfile
import tmp_ram
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import atomes_parole
import piper_out
import speak
from phrases import texte

RATE = 22050
EDGE_RETRY_SEC = 1800
SILENCE_DB = -42
PAUSES = {",": 0.16, ":": 0.22, ";": 0.22, ".": 0.32, "!": 0.32, "?": 0.32,
          atomes_parole.COLLE_NOMBRE: 0.02,   # l'ecart de « cinquante » + « et un »
          atomes_parole.COLLE_SUITE: 0.02,
          atomes_parole.COLLE_LETTRE: 0.02}
_SERRE = {atomes_parole.COLLE_NOMBRE, atomes_parole.COLLE_SUITE, atomes_parole.COLLE_LETTRE}
PAUSE_MOT = 0.02
MOT_COURT = 3   # "Le", "et", "de" : jamais seuls
PROSE_CACHE_MAX = 48   # au-dela, un bout de phrase unique n'est pas garde
_edge_down_until = [0.0]
_lock = threading.Lock()


class Live(str):
    """Morceau calcule a chaque annonce, jamais garde."""


# --- decoupage ------------------------------------------------------------------------

def _split_literal(lit):
    """", a destination de " -> [(", ", ""), ("a destination de", "")] : texte et ponctuation."""
    out = []
    for piece in re.split(r"([,:;.!?])", lit):
        if piece in PAUSES:
            if out:
                out[-1] = (out[-1][0], piece)
            else:
                out.append(("", piece))
        elif piece.strip():
            out.append((piece.strip(), ""))
    return out


def _split_value(value):
    """Une heure "17 heure 42" -> "17 heure" + "42" (nombre de morceaux borne)."""
    if isinstance(value, Live):
        return [(value, "")]
    m = re.fullmatch(r"(\d{1,2} heures?)(?: (\d{1,2}))?", value.strip())
    if m:
        return [(m.group(1), "")] + ([(m.group(2), "")] if m.group(2) else [])
    return [(value.strip(), "")]


def fragments(key, **values):
    """Morceaux d'une phrase du projet (cle de phrases/fr.ini) : les parties
    fixes du modele et les valeurs. Une valeur peut etre une liste de
    morceaux deja decoupes (annonces groupees)."""
    template = texte(key) or ""
    parts = []
    pos = 0
    for m in re.finditer(r"\{(\w+)\}", template):
        parts += _split_literal(template[pos:m.start()])
        value = values.get(m.group(1), "")
        if isinstance(value, list):
            parts += value
        elif str(value).strip():
            parts += _split_value(value if isinstance(value, Live) else str(value))
        pos = m.end()
    parts += _split_literal(template[pos:])
    merged = []  # ponctuation seule : rattachee au morceau d'avant
    for text, punct in parts:
        if not text and merged:
            merged[-1] = (merged[-1][0], punct or merged[-1][1])
        elif text:
            merged.append((text, punct))
    return _expand_parts(_glue(merged))


def _glue(parts):
    """Petits mots ("Le", "et", "de") colles au morceau suivant quand rien ne
    les separe : un morceau d'une syllabe sonne hache."""
    out, carry = [], ""
    for text, punct in parts:
        if carry:
            joined = f"{carry} {text}"
            text = Live(joined) if isinstance(text, Live) else joined
            carry = ""
        if len(text) <= MOT_COURT and not punct and not isinstance(text, Live):
            carry = text
            continue
        out.append((text, punct))
    if carry:
        out.append((carry, ""))
    return out


def _expand_parts(parts):
    """Nombres et indicatifs -> mots deja en cache. Une phrase sans chiffre
    ni lettre seule reste un seul morceau (le cache des phrases ne change pas)."""
    out = []
    for text, punct in parts:
        pieces = atomes_parole.decouper(str(text))
        if not pieces or (len(pieces) == 1 and pieces[0][1] == "prose"):
            out.append((text, punct))
            continue
        built, finale = [], ""
        for mot, genre in pieces:
            nu = mot.strip()
            if nu and all(c in ".,:;!?… " for c in nu):
                finale = nu.strip()[:1] or finale
                continue
            if nu:
                built.append((nu, genre))
        if not built:
            out.append((text, punct))
            continue
        if not punct and finale in PAUSES:
            punct = finale
        for i, (mot, genre) in enumerate(built):
            last = i == len(built) - 1
            nxt = built[i + 1][1] if not last else None
            if last:
                # Pas de point ni de virgule sur un chiffre : le silence hache le nombre.
                p = "" if genre == "nombre" else punct
            elif genre == nxt == "lettre" or (genre == "lettre" and nxt == "nombre"):
                p = atomes_parole.COLLE_LETTRE
            elif genre == nxt == "nombre":
                p = atomes_parole.COLLE_NOMBRE
            elif genre == "nombre" and nxt == "prose" and atomes_parole.liaison(built[i + 1][0]):
                p = atomes_parole.COLLE_SUITE
            else:
                p = ""
            if genre == "prose" and len(mot) > PROSE_CACHE_MAX:
                out.append((Live(mot), p))
            else:
                out.append((mot, p))
    return out


def whole(text, live=False):
    """Une phrase entiere comme un seul morceau ("3 avions detectes").
    Un nombre ou un indicatif a l'interieur est quand meme colle depuis le cache."""
    text = (text or "").strip()
    if not text:
        return []
    punct = text[-1] if text[-1:] in PAUSES else ""
    body = text[:-1].strip() if punct else text
    piece = Live(body) if live else body
    return _expand_parts([(piece, punct)])


def expand_texte(text):
    """Phrase dite telle quelle -> morceaux. Une phrase fixe du projet reste
    un seul morceau (son cache existe deja)."""
    import phrases_fixes
    parts = []
    sentences = phrases_fixes.split(text) or ([text.strip()] if text and text.strip() else [])
    for sentence in sentences:
        punct = sentence[-1] if sentence[-1:] in PAUSES else ""
        body = sentence[:-1].strip() if punct else sentence.strip()
        if not body:
            continue
        fixe = (phrases_fixes.est_fixe(sentence) or phrases_fixes.est_fixe(body)
                 or phrases_fixes.est_fixe((body + punct).strip()) or phrases_fixes.est_fixe(body + "."))
        if fixe or not atomes_parole.a_decouper(body):
            parts.append((body, punct))
        else:
            parts.extend(_expand_parts([(body, punct)]))
    return parts


def a_assembler(text):
    """Vrai s'il y a un nombre ou un indicatif a coller, et que la phrase
    n'est pas deja une phrase fixe entiere. Un flash (plusieurs phrases,
    ou un long texte) n'est pas decoupe : il est genere d'un bloc."""
    if not atomes_parole.annonce_courte(text):
        return False
    try:
        parts = expand_texte(text)
    except Exception:
        return False
    if len(parts) > 1:
        return True
    if len(parts) == 1 and atomes_parole.a_decouper(str(parts[0][0])):
        return atomes_parole.a_decouper(text)
    return False


def spoken(parts):
    """Ce qui est dit, sans les marques de collage (elles ne se prononcent pas)."""
    mots = []
    for t, p in parts:
        mot = str(t).strip()
        if not mot:
            continue
        piece = mot + (p if p in ".,:;!?" else "")
        if mots and piece[:1] in ".,:;!?…":
            mots[-1] += piece
        else:
            mots.append(piece)
    return " ".join(mots)


# --- voix et cache --------------------------------------------------------------------

def choose_voice(source=None):
    """Voix de la source (commande "voix", lib/voix_sources.py) : un dict
    {moteur, voix, debit, hauteur, aleatoire}."""
    import voix_sources
    return voix_sources.choisir(source or voix_sources.source_pour())


def _cache_file(text, choix):
    import voix_sources
    return voix_sources.fichier_cache(speak._repetitive_cache_dir(text), choix)


def _synth_edge(text, choix, dest):
    holder = [None]
    speak._synthesize_edge_tts(text, holder, [choix["voix"]], choix["debit"], choix["hauteur"])
    if not holder[0]:
        return False
    path, _ = holder[0]
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(path, dest)  # /tmp -> NAS : autre disque
        rogner_fichier(dest)
    except OSError:
        return False
    return True


def _espeak_mot(text, path):
    """Dernier recours, phrase entière. Jamais un trou dans une autre voix,
    et jamais un fichier rangé dans le cache de cette voix."""
    try:
        subprocess.run(["espeak", "-v", "fr", "-b", "1", "-w", path, text],
                       capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return os.path.exists(path) and os.path.getsize(path) > 100


class Chantier:
    """Calcule les morceaux manquants en parallele. Ce qui est deja en cache
    est pret tout de suite ; la lecture peut commencer pendant que le reste
    se fabrique."""

    def __init__(self, parts, voice):
        self.voice = voice
        self.tmpdir = tempfile.mkdtemp(prefix="morceaux_", dir=tmp_ram.dossier())
        self._files, self._events = {}, {}
        self._lock = threading.Lock()
        jobs = []
        ext = "mp3" if voice["moteur"] == "edge" else "wav"
        n_live = 0
        for text, _ in parts:
            if text in self._events:
                continue
            ev = threading.Event()
            self._events[text] = ev
            if isinstance(text, Live):
                path = os.path.join(self.tmpdir, f"live{n_live}.{ext}")
                n_live += 1
                jobs.append((text, path, ev))
                continue
            speak.remember_repetitive(str(text))
            path = _cache_file(str(text), voice)
            if os.path.exists(path):
                self._files[text] = path
                ev.set()
            else:
                jobs.append((text, path, ev))
        workers = 3 if voice["moteur"] == "edge" else 2
        self._pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs) or 1)))
        for text, path, ev in jobs:
            self._pool.submit(self._faire, text, path, ev)

    def _faire(self, text, path, ev):
        try:
            ok = False
            if self.voice["moteur"] == "edge":
                ok = _synth_edge(str(text), self.voice, path)
            elif self.voice["moteur"] == "piper":
                os.makedirs(os.path.dirname(path), exist_ok=True)
                ok = piper_out.to_file(str(text), path, voice_id=self.voice["voix"],
                                       scale_len=piper_out.length_scale(self.voice["debit"]))
            if ok:
                try:
                    rogner_fichier(path)
                except Exception:
                    pass
                with self._lock:
                    self._files[text] = path
        finally:
            ev.set()

    def chemin(self, text, timeout=30):
        ev = self._events.get(text)
        if ev is None or not ev.wait(timeout):
            return None
        with self._lock:
            return self._files.get(text)

    def relacher(self):
        """Les calculs sont finis ; le dossier temporaire reste (fichiers Live)."""
        self._pool.shutdown(wait=True)

    def fermer(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None


def ensure(parts, voice):
    """Calcule les morceaux qui manquent dans cette voix (choix de
    voix_sources), plusieurs a la fois. Rend {texte: fichier} ou None si la
    voix a lache (Microsoft en panne : a refaire en Piper)."""
    if voice["moteur"] not in ("edge", "piper"):
        return None
    chantier = Chantier(parts, voice)
    files = {}
    try:
        for text, _ in parts:
            path = chantier.chemin(text, timeout=40)
            if not path:
                return None
            files[text] = path
        files[None] = chantier.tmpdir
        chantier.relacher()
        return files
    finally:
        if None not in files:
            chantier.fermer()


def _lire_pcm(path):
    try:
        raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-f", "s16le",
                              "-ac", "1", "-ar", str(RATE), "-"], capture_output=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return np.zeros(0, dtype=np.int16)
    return np.frombuffer(raw, dtype=np.int16)


def rogner_pcm(audio, rate=RATE):
    """Coupe le silence colle au sample, au ras du son. f et v sont faibles :
    on les garde s'ils touchent le mot (70 ms), on ne garde pas le blanc du fichier.
    Le silence entre les mots est remis a l'assemblage."""
    if audio.size < rate // 20:
        return audio
    amp = np.abs(audio.astype(np.int32))
    peak = int(amp.max()) or 1
    # Noyau du mot, puis consonnes faibles collee (f, v, s), pas le souffle de fin.
    core = max(int(peak * 10 ** (-30 / 20)), int(32768 * 10 ** (-36 / 20)))
    soft = max(int(peak * 10 ** (-50 / 20)), int(32768 * 10 ** (-56 / 20)))
    loud = np.flatnonzero(amp >= core)
    if not len(loud):
        return audio
    marge = int(rate * 0.07)
    debut = int(loud[0])
    avant = amp[max(0, debut - marge):debut]
    bas = np.flatnonzero(avant >= soft)
    if len(bas):
        debut = max(0, debut - marge) + int(bas[0])
    fin = int(loud[-1])
    apres = amp[fin + 1:fin + 1 + marge]
    bas = np.flatnonzero(apres >= soft)
    if len(bas):
        fin = fin + 1 + int(bas[-1])
    pad = int(rate * 0.008)
    debut = max(0, debut - pad)
    fin = min(len(audio), fin + 1 + pad)
    return audio[debut:fin]


def rogner_fichier(path):
    """A la creation du sample : le fichier garde n'a plus son silence de synthese."""
    audio = _lire_pcm(path)
    if audio.size < RATE // 10:
        return
    court = rogner_pcm(audio)
    if len(audio) - len(court) < int(RATE * 0.03):
        return
    ext = os.path.splitext(path)[1].lower() or ".wav"
    fd, tmp = tempfile.mkstemp(suffix=ext, prefix="rogne_")
    os.close(fd)
    try:
        if ext == ".wav":
            with wave.open(tmp, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(court.tobytes())
        else:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(RATE),
                 "-ac", "1", "-i", "-", "-codec:a", "libmp3lame", "-q:a", "5", tmp],
                input=court.tobytes(), capture_output=True, timeout=30)
            if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 200:
                return
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _decode(path, tete=0.0, queue=0.0):
    return rogner_pcm(_lire_pcm(path))


def _morceau_complet(path, precedent, punct):
    """Le mot entier, deja rogne, puis le blanc qu'on remet entre les mots."""
    audio = _decode(path)
    blanc = int(RATE * PAUSES.get(punct, PAUSE_MOT))
    if blanc:
        silence = np.zeros(blanc, dtype=np.int16)
        audio = np.concatenate([audio, silence]) if len(audio) else silence
    return audio


def _iter_son(parts, chemin_de):
    """Morceaux PCM. chemin_de(texte) bloque jusqu'au fichier de CE morceau.
    Rend None si un morceau manque. Chaque yield est un mot fini."""
    precedent = ""
    for text, punct in parts:
        path = chemin_de(text)
        if not path:
            yield None
            return
        audio = _morceau_complet(path, precedent, punct)
        if len(audio):
            yield audio
        precedent = punct


def assemble(parts, voice):
    """Fichier WAV de l'annonce entiere, ou None."""
    files = ensure(parts, voice)
    if files is None:
        return None
    try:
        return _join(parts, files)
    finally:
        shutil.rmtree(files.get(None) or "/nonexistent", ignore_errors=True)


def _ecrire_wav(audio):
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="annonce_", dir=tmp_ram.dossier())
    os.close(fd)
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.tobytes())
    return out


def _meme_voix(a, b):
    if not a or not b:
        return False
    return (a.get("moteur") == b.get("moteur") and a.get("voix") == b.get("voix")
            and a.get("debit") == b.get("debit"))


def _join(parts, files):
    pieces = []
    for bout in _iter_son(parts, files.get):
        if bout is None:
            return None
        if len(bout):
            pieces.append(bout)
    if not pieces:
        return None
    return _ecrire_wav(np.concatenate(pieces))


class Lecture:
    """Joue des qu'un morceau est pret et fabrique les autres pendant ce temps.
    wait/kill : meme usage qu'un lecteur de la console."""

    def __init__(self, parts, voice, lead=None, label="voix", volume=1.0, regler=None,
                 journal=False, garder=False, repli=None):
        self.parts, self.voice, self.lead = parts, voice, lead
        self.label, self.volume, self.regler = label, volume, regler
        self.journal, self.garder, self.repli = journal, garder, repli
        self.a_parle, self.wav = False, None
        self._killed = False
        import audio_lock
        self._ticket = audio_lock.ticket_courant()
        self._player = None
        self._chantier = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _ouvrir(self):
        # Tampon court : le blanc d'attente, lui, est ecrit entre les mots,
        # donc la carte ne coupe pas le mot en cours quand la suite manque.
        return subprocess.Popen(
            ["aplay", "-q", "-D", "default", "-B", "120000", "-r", str(RATE),
             "-f", "S16_LE", "-c", "1"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _pousser(self, player, audio):
        pas = max(1, RATE // 10)
        for i in range(0, len(audio), pas):
            if self._killed:
                return
            bout = audio[i:i + pas]
            if self.volume != 1.0:
                bout = np.clip(bout.astype(np.float32) * self.volume, -32768, 32767).astype(np.int16)
            data = bout.tobytes()
            if self.regler:
                data = self.regler(data) or data
            try:
                player.stdin.write(data)
            except (BrokenPipeError, OSError):
                self._killed = True
                return

    def _poser(self, file_audio, item):
        while not self._killed:
            try:
                file_audio.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    def _produire(self, chantier, file_audio):
        """Decode le morceau suivant pendant que le precedent est deja dit."""
        precedent = ""
        try:
            for text, punct in self.parts:
                if self._killed:
                    break
                path = chantier.chemin(text, timeout=30)
                if not path:
                    break
                audio = _morceau_complet(path, precedent, punct)
                if len(audio):
                    self._poser(file_audio, audio)
                precedent = punct
        finally:
            self._poser(file_audio, None)

    def _prendre(self, file_audio, player, pcm):
        """Mot suivant, ou du silence si on l'attend. Le silence ne part
        qu'une fois le mot d'avant entierement envoye."""
        while not self._killed:
            try:
                return file_audio.get(timeout=0.02)
            except queue.Empty:
                if player[0] is None:
                    continue
                blanc = np.zeros(int(RATE * 0.02), dtype=np.int16)
                self._pousser(player[0], blanc)
                pcm.append(blanc)
        return None

    def _preparer(self, voice):
        """Vrai seulement si chaque morceau existe dans cette voix."""
        if not voice or voice.get("moteur") not in ("edge", "piper"):
            return False
        chantier = Chantier(self.parts, voice)
        self._chantier = chantier
        for text, _ in self.parts:
            if self._killed or not chantier.chemin(text, timeout=40):
                return False
        return True

    def _audio_espeak(self):
        """Phrase entière. Le wav temporaire n'entre pas dans un cache de voix."""
        text = spoken(self.parts)
        if not text:
            return None
        fd, path = tempfile.mkstemp(prefix="morceau_espeak_", suffix=".wav")
        os.close(fd)
        try:
            if not _espeak_mot(text, path):
                return None
            print("[morceaux] phrase entiere en espeak : voix et secours ont lache")
            return _lire_pcm(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _run(self):
        import audio_lock
        import sound_player
        if self._ticket is not None:
            audio_lock._local.ticket = self._ticket
        # Une voix pour toute l'annonce. Si la voix choisie lâche un morceau,
        # on recommence TOUT dans le secours (Piper choisi). Espeak seulement
        # si ce secours est espeak, ou si les deux ont lâché, et pour la
        # phrase entière : jamais l'alphabet dans une voix et le reste dans l'autre.
        if not self._preparer(self.voice) and not self._killed:
            if self._chantier is not None:
                self._chantier.fermer()
            self._chantier = None
            repli = self.repli
            if (repli and repli.get("moteur") in ("edge", "piper")
                    and not _meme_voix(repli, self.voice)):
                self.voice = repli
                if not self._preparer(self.voice):
                    if self._chantier is not None:
                        self._chantier.fermer()
                    self._chantier = None
        if self._killed:
            if self._chantier is not None:
                self._chantier.fermer()
            return
        pcm = []
        player = None
        try:
            if self._chantier is None:
                audio = self._audio_espeak()
                if audio is not None and len(audio):
                    player = self._ouvrir()
                    self._player = player
                    self.a_parle = True
                    self._pousser(player, audio)
                    pcm.append(audio)
                return
            chantier = self._chantier
            def suite():
                nonlocal player
                file_audio = queue.Queue(maxsize=2)
                threading.Thread(target=self._produire, args=(chantier, file_audio),
                                 daemon=True).start()
                lecteur = [None]
                while not self._killed:
                    audio = self._prendre(file_audio, lecteur, pcm)
                    if audio is None:
                        break
                    pcm.append(audio)
                    if player is None:
                        player = self._ouvrir()
                        self._player = player
                        lecteur[0] = player
                        self.a_parle = True
                    self._pousser(player, audio)

            if self.lead and not self._killed:
                sound_player.play_lead_then(self.lead, suite, label=self.label)
            else:
                suite()
        except Exception as exc:
            print(f"[morceaux] lecture : {exc!r}")
        finally:
            if player is not None:
                try:
                    player.stdin.close()
                except OSError:
                    pass
                try:
                    player.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    player.kill()
            if self._chantier is not None:   # absent quand espeak a parle a sa place
                self._chantier.fermer()
            if pcm and not self._killed:
                try:
                    self.wav = _ecrire_wav(np.concatenate(pcm))
                except Exception:
                    self.wav = None
            if self.wav and self.journal and sound_player.enregistrer_journal:
                try:
                    session = sound_player.get_recording_session()
                    if session is not None:
                        session.add_audio_file(self.wav)
                except Exception:
                    pass
            if self.wav and not self.garder:
                try:
                    os.remove(self.wav)
                except OSError:
                    pass
                self.wav = None

    def wait(self, timeout=None):
        self._thread.join(timeout if timeout is not None else 120)
        if self._thread.is_alive():
            self.kill()
            return None
        return 0 if self.a_parle else 1

    def kill(self):
        self._killed = True
        chantier = self._chantier
        if chantier is not None:
            for ev in list(chantier._events.values()):
                ev.set()
        player = self._player
        if player is not None and player.poll() is None:
            player.kill()
        self._thread.join(timeout=3)

    def poll(self):
        return None if self._thread.is_alive() else (0 if self.a_parle else 1)


def _demande_en_cache(parts, demande):
    """La voix demandee (Microsoft absent) si TOUS les morceaux sont deja en
    cache dans une meme voix (aleatoire : la premiere voix complete), sinon None :
    une annonce ne melange jamais deux voix."""
    if not demande or any(isinstance(t, Live) for t, _ in parts):
        return None
    candidates = [demande["voix"]]
    if demande.get("aleatoire"):
        candidates = random.sample(speak.EDGE_TTS_VOICES, len(speak.EDGE_TTS_VOICES))
    for voix in candidates:
        choix = dict(demande, voix=voix)
        if all(os.path.exists(_cache_file(t, choix)) for t, _ in parts):
            return choix
    return None


def _copie_temp(chemin):
    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="phrase_", dir=tmp_ram.dossier())
    os.close(fd)
    shutil.copyfile(chemin, tmp)
    return tmp


def _phrase_distante(parts, voice):
    """Google ou Mac : la phrase entière, gardée dans le cache de cette voix.
    On rend une copie : le lecteur efface le fichier qu'on lui donne."""
    text = spoken(parts)
    if not text:
        return None
    import speak
    cached = speak.piper_sample(text, voice)
    if not cached:
        return None
    return _copie_temp(cached)


def _phrase_directe(parts, voice):
    """MBROLA ou espeak, fichier temporaire, rien dans le cache."""
    text = spoken(parts)
    if not text:
        return None
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="direct_")
    os.close(fd)
    ok = False
    voix = (voice or {}).get("voix") or ""
    try:
        if voix.startswith("piper:mbrola#"):
            ok = piper_out.to_file(text, path, voice_id=voix,
                                   scale_len=piper_out.length_scale(voice.get("debit")))
        else:
            subprocess.run(["espeak", "-v", "fr", "-b", "1", "-w", path, text],
                           capture_output=True, timeout=30)
            ok = os.path.getsize(path) > 100
    except Exception as exc:
        print(f"[morceaux] direct : {exc!r}")
        ok = False
    if ok:
        return path
    try:
        os.remove(path)
    except OSError:
        pass
    return None


class Prepared:
    """Annonce calculee d'avance (prepare()), jouee plus tard par say()."""

    def __init__(self, parts, source=None):
        import voix_sources
        self.source = source or voix_sources.source_pour()
        self.parts, self.voice, self.path = parts, None, None
        self._done = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            with _lock:  # un calcul a la fois : le Pi fait aussi tourner le scanner
                import voix_sources
                self.voice = choose_voice(self.source)
                demande = _demande_en_cache(self.parts, self.voice.get("demande"))
                if demande:  # Microsoft absent, mais sa voix a deja tout dit : elle reste
                    self.voice = demande
                if voix_sources.en_direct(self.voice):
                    self.path = _phrase_directe(self.parts, self.voice)
                elif voix_sources.phrase_entiere(self.voice):
                    self.path = _phrase_distante(self.parts, self.voice)
                    if self.path is None:
                        self.voice = voix_sources.secours(self.source)
                        if voix_sources.en_direct(self.voice):
                            self.path = _phrase_directe(self.parts, self.voice)
                        elif self.voice.get("moteur") == "piper":
                            self.path = assemble(self.parts, self.voice)
                elif self.voice["moteur"] == "edge":
                    self.path = assemble(self.parts, self.voice)
                    if self.path is None:  # Microsoft a lache : voix de secours de la source
                        self.voice = voix_sources.secours(self.source)
                if self.path is None and self.voice["moteur"] == "piper":
                    self.path = assemble(self.parts, self.voice)
        except Exception as exc:
            print(f"[morceaux] preparation : {exc!r}")
        finally:
            self._done.set()

    def wait(self, timeout=60):
        self._done.wait(timeout)
        return self.path


def prepare(parts, source=None):
    """Lance le calcul en tache de fond ; rend un Prepared a passer a say()."""
    return Prepared(parts, source)


def say(lead_sound, parts, label="annonce", reverb=False, prepared=None, source=None):
    """Carillon puis annonce assemblee (sous le verrou audio de l'appelant).
    Repli : speak_with_lead_sound sur le texte entier."""
    import audio_lock
    from sound_player import play_sound_effect
    text = spoken(parts)
    if audio_lock.dropped():
        print(f"(muet, priorites) {text}")
        return
    import sons
    if lead_sound and sons.mode_chemin(lead_sound) != "son_phrase":  # commande "sons" : le son seul, ou rien
        print(f"(son seul) {text}")
        play_sound_effect(lead_sound, label=label, reverb="pa_jingle" if reverb else False)
        return
    path = (prepared or prepare(parts, source)).wait()
    if not path:
        if lead_sound:
            speak.speak_with_lead_sound(lead_sound, text, label=label, reverb=reverb, source=source)
        else:
            speak.speak_cloud(text, source=source)
        return
    print(text)
    import sound_player
    voix, pcm = path, None
    if reverb:
        # Effet de quai calcule AVANT le jingle (2026-10-22) : avant, il l'etait
        # apres sa derniere note (trois passes de sox), d'ou un long blanc. Voix
        # compressee et normalisee ("pa_voix"), jingle plus "sono" ("pa_jingle").
        import audio_effets
        pcm = audio_effets.pcm_gare(path, "pa_voix")
        if pcm is None:
            voix = audio_effets.reverb_gare(path, "pa_voix") or path
    try:
        # La voix part juste apres la derniere note du jingle, sur sa traine (2026-10-19).
        sound_player.play_lead_then(lead_sound, lambda: play_sound_effect(voix, timeout_sec=120, label=label,
                                                                          reverb=False, pcm=pcm),
                                    label=label, reverb="pa_jingle" if reverb else False)
    finally:
        for p in {path, voix}:
            try:
                os.remove(p)
            except OSError:
                pass


# --- a preparer d'avance (watchers/voix_preparation.py) -------------------------------

TEMPLATE_PREFIXES = ("trains.", "avion.", "radio.", "helico.")


def fixed_fragments():
    """Tous les morceaux connus d'avance : parties fixes des phrases des
    annonces, heures et minutes, "2 avions detectes"... jusqu'a 10, receptions
    numeriques. Le reste (gares, numeros...) est garde des sa premiere annonce."""
    return sorted(fixed_fragments_sources())


def fixed_fragments_sources():
    """{morceau: {veilles qui le disent}} (commande "voix" : chaque veille sa voix)."""
    import phrases
    import phrases_fixes
    found = {}

    def add(text, source):
        if text.strip():
            found.setdefault(text, set()).add(source)
    for key in phrases.base():
        if key.startswith(TEMPLATE_PREFIXES):
            template = texte(key) or ""
            values = {name: Live("x") for name in re.findall(r"\{(\w+)\}", template)}
            for t, _ in fragments(key, **values):
                if not isinstance(t, Live):
                    add(t, phrases_fixes.source_de(key))
    for t in [f"{h} heure" for h in range(24)] + [str(m) for m in range(1, 60)]:
        add(t, "trains")
    for n in range(2, 11):
        for t, _ in whole(phrases.dire("avion.plusieurs_detectes", nombre=phrases.nombre(n, "avion"))):
            add(t, "avions")
    try:
        import decodeurs
        for proto in set(decodeurs.DSD_PROTOCOLS.values()) | {"numérique"}:
            for key in ("radio.reception", "radio.reception_terminee"):
                for t, _ in whole(phrases.dire(key, protocol=proto)):
                    add(t, "radio")
    except ImportError:
        pass
    # Mots des nombres et de l'alphabet radio, plus les unites qui les suivent :
    # fabriques une fois, pour toutes les voix en service (source None).
    for mot in atomes_parole.vocabulaire() + [
            "heure", "heures", "minute", "minutes", "seconde", "secondes",
            "mètre", "mètres", "kilomètre", "kilomètres", "degré", "degrés"]:
        add(mot, None)
    return found
