"""Prises vocales (2026-10-03, demande utilisateur) : un message decode
(avion, pager, APRS, LoRa...) ou un releve de capteur n'est pas lu en direct
par la voix de la console : son texte est SYNTHETISE dans un fichier audio
(carillon + voix), range au journal comme une prise radio, avec sa fiche
(<nom>.json : texte, source, details), puis ce fichier est diffuse.

Au fil de l'eau : les annonces passent par une file traitee par un fil
dedie ; la fenetre d'ecoute continue de capter pendant qu'on parle. Une
annonce peut etre un texte, ou une fonction qui le produit (traduction par
Gemini) : le travail lent se fait aussi dans le fil, jamais dans l'ecoute.

Voix : edge-tts (meme voix que les annonces, temps genereux : ce n'est pas
du direct) ; repli espeak ecrit dans un fichier, pour qu'il y ait toujours
une prise a garder.
"""
import asyncio
import datetime
import json
import os
import queue
import random
import shutil
import subprocess
import tempfile
import threading
import time

import audio_lock
import radio_log
import sound_player
from sound_paths import RADIO_LOG_DIR

SYNTH_TIMEOUT_SEC = 25
NOTICE_EVERY_SEC = 60      # avis "captation ajoutee au journal" : une fois par minute au plus
_notice = {"t": 0.0}
_queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()


# --- synthese ------------------------------------------------------------------------

def _edge(text, path):
    import edge_tts
    import speak
    voice = random.choice(speak.EDGE_TTS_VOICES)

    async def run():
        await asyncio.wait_for(edge_tts.Communicate(text, voice).save(path), timeout=SYNTH_TIMEOUT_SEC)
    asyncio.run(run())
    return os.path.exists(path) and os.path.getsize(path) > 0


def _espeak(text, path):
    wav = path[:-4] + ".wav"
    subprocess.run(["espeak", "-v", "fr", "-b", "1", "-w", wav, text], capture_output=True, timeout=60)
    ok = os.path.exists(wav) and os.path.getsize(wav) > 0
    if ok:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", wav, path],
                       capture_output=True, timeout=60)
        os.remove(wav)
    return os.path.exists(path) and os.path.getsize(path) > 0


def synthesize(text, path):
    """Voix du texte dans path (mp3). True si reussi."""
    try:
        if _edge(text, path):
            return True
    except Exception as exc:
        print(f"[prise_vocale] edge-tts : {exc!r}")
    try:
        return _espeak(text, path)
    except Exception as exc:
        print(f"[prise_vocale] espeak : {exc!r}")
        return False


def record(text, sound, label, meta=None):
    """Fabrique la prise (carillon + voix) au journal ; rend son chemin ou None."""
    tmp = tempfile.mkdtemp(prefix="prise_")
    try:
        voice = os.path.join(tmp, "voix.mp3")
        if not synthesize(text, voice):
            return None
        parts = [p for p in (sound, voice) if p and os.path.exists(p)]
        listing = os.path.join(tmp, "liste.txt")
        normalized = []
        for i, part in enumerate(parts):
            out = os.path.join(tmp, f"{i}.wav")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", part,
                            "-ar", str(radio_log.LOG_SAMPLE_RATE), "-ac", "1", out], capture_output=True, timeout=30)
            if os.path.exists(out):
                normalized.append(out)
        with open(listing, "w") as f:
            f.writelines(f"file '{p}'\n" for p in normalized)
        os.makedirs(RADIO_LOG_DIR, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in label).strip("_")[:40] or "message"
        stamp = radio_log._readable_timestamp(datetime.datetime.now())
        final = os.path.join(RADIO_LOG_DIR, f"{safe}_{stamp}.mp3")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", listing, "-ar", str(radio_log.LOG_SAMPLE_RATE), "-ac", "1", final],
                       capture_output=True, timeout=60)
        if not (os.path.exists(final) and os.path.getsize(final) > 0):
            return None
        with open(final[:-4] + ".json", "w", encoding="utf-8") as f:
            json.dump(dict(meta or {}, texte=text, t=time.time()), f, ensure_ascii=False, default=str)
        radio_log._touch(radio_log.JOURNAL_UPDATE_FLAG)
        print(f"[prise_vocale] prise enregistree : {final}")
        return final
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def play(path, label):
    """Diffuse la prise sous le verrou audio partage (source "donnees")."""
    lock = audio_lock.acquire("donnees")
    try:
        if not lock.dropped:
            sound_player.play_sound_effect(path, timeout_sec=600, label=label)
    finally:
        audio_lock.release(lock)
    if getattr(lock, "silent", False) and time.time() - _notice["t"] >= NOTICE_EVERY_SEC:
        # journal ouvert : enregistree sans bruit, un mot pour prevenir (pas a chaque releve)
        _notice["t"] = time.time()
        import speak
        speak.journal_notice()


# --- file au fil de l'eau --------------------------------------------------------------

def _run():
    while True:
        item = _queue.get()
        try:
            text = item["texte"]() if callable(item["texte"]) else item["texte"]
            if not text:
                continue
            print(f"[{item['tag']}] annonce : {text}")
            path = record(text, item["son"], item["etiquette"], item["fiche"])
            if path:
                play(path, item["tag"])
            else:  # synthese impossible : on dit quand meme, en direct
                import speak
                lock = audio_lock.acquire("donnees")
                try:
                    speak.speak_with_lead_sound(item["son"], text, label=item["tag"])
                finally:
                    audio_lock.release(lock)
        except Exception as exc:  # une annonce ratee n'arrete pas la file
            print(f"[prise_vocale] annonce echouee ({item.get('tag')}) : {exc!r}")
        finally:
            if item.get("fait"):
                item["fait"].set()


def announce(text, sound, label, tag, fiche=None, wait=False):
    """Met une annonce en file (texte, ou fonction qui le rend). wait=True :
    rend la main quand elle a ete dite (annonce d'un passage de satellite
    juste avant l'ecoute, par exemple)."""
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, daemon=True, name="prise_vocale")
            _worker.start()
    done = threading.Event() if wait else None
    _queue.put({"texte": text, "son": sound, "etiquette": label, "tag": tag, "fiche": fiche, "fait": done})
    if done:
        done.wait(timeout=600)
