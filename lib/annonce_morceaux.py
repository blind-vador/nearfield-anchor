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
  Live(str)    calcule a chaque fois, jamais garde (indicatif d'avion...)
"""
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave

import numpy as np

import piper_out
import speak
from phrases import texte

RATE = 22050
EDGE_RETRY_SEC = 1800
SILENCE_DB = -42
PAUSES = {",": 0.16, ":": 0.22, ";": 0.22, ".": 0.32, "!": 0.32, "?": 0.32}
PAUSE_MOT = 0.06
MOT_COURT = 3   # "Le", "et", "de" : jamais seuls
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
    return _glue(merged)


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


def whole(text, live=False):
    """Une phrase entiere comme un seul morceau ("3 avions detectes")."""
    text = text.strip()
    punct = text[-1] if text[-1:] in PAUSES else ""
    body = text[:-1].strip() if punct else text
    return [(Live(body) if live else body, punct)]


def spoken(parts):
    return " ".join(t + p for t, p in parts)


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
    except OSError:
        return False
    return True


def ensure(parts, voice):
    """Calcule les morceaux qui manquent dans cette voix (choix de
    voix_sources). Rend {texte: fichier} ou None si la voix a lache
    (Microsoft en panne : a refaire en Piper)."""
    kind, name = voice["moteur"], voice["voix"]
    files, missing, live = {}, [], []
    for text, _ in parts:
        if isinstance(text, Live):
            live.append(text)
            continue
        speak.remember_repetitive(text)
        path = _cache_file(text, voice)
        if os.path.exists(path):
            files[text] = path
        elif (text, path) not in missing:
            missing.append((text, path))
    tmpdir = tempfile.mkdtemp(prefix="morceaux_")
    files[None] = tmpdir  # morceaux "Live", effaces apres l'assemblage
    for i, text in enumerate(live):
        missing.append((text, os.path.join(tmpdir, f"live{i}.{'mp3' if kind == 'edge' else 'wav'}")))
    if kind == "edge":
        for text, path in missing:
            if not _synth_edge(text, voice, path):
                shutil.rmtree(tmpdir, ignore_errors=True)
                return None
            files[text] = path
    elif kind == "piper" and missing:
        for text, path in missing:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        piper_out.batch(name, missing, piper_out.length_scale(voice["debit"]))
        for text, path in missing:
            if os.path.exists(path):
                files[text] = path
    return files


def _decode(path):
    try:
        raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-f", "s16le",
                              "-ac", "1", "-ar", str(RATE), "-"], capture_output=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return np.zeros(0, dtype=np.int16)
    audio = np.frombuffer(raw, dtype=np.int16)
    if not len(audio):
        return audio
    level = 32768 * 10 ** (SILENCE_DB / 20)
    loud = np.flatnonzero(np.abs(audio) > level)
    if not len(loud):
        return audio
    start, end = max(0, loud[0] - int(RATE * 0.02)), min(len(audio), loud[-1] + int(RATE * 0.04))
    return audio[start:end]


def assemble(parts, voice):
    """Fichier WAV de l'annonce entiere, ou None."""
    files = ensure(parts, voice)
    if files is None:
        return None
    try:
        return _join(parts, files)
    finally:
        shutil.rmtree(files.get(None) or "/nonexistent", ignore_errors=True)


def _join(parts, files):
    pieces = []
    for text, punct in parts:
        path = files.get(text)
        if path is None:
            return None  # morceau impossible a calculer : l'appelant passe par speak()
        pieces.append(_decode(path))
        pieces.append(np.zeros(int(RATE * PAUSES.get(punct, PAUSE_MOT)), dtype=np.int16))
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="annonce_")
    os.close(fd)
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.concatenate(pieces).tobytes())
    return out


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
                if self.voice["moteur"] == "edge":
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
    voix = path
    if reverb:
        # Effet de quai calcule AVANT le jingle (2026-10-22) : avant, il l'etait
        # apres sa derniere note (trois passes de sox), d'ou un long blanc. Voix
        # compressee et normalisee ("pa_voix"), jingle plus "sono" ("pa_jingle").
        import audio_effets
        voix = audio_effets.reverb_gare(path, "pa_voix") or path
    try:
        # La voix part juste apres la derniere note du jingle, sur sa traine (2026-10-19).
        sound_player.play_lead_then(lead_sound, lambda: play_sound_effect(voix, timeout_sec=120, label=label,
                                                                          reverb=False),
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
    return found
