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

def _edge(text, path, choix):
    import edge_tts
    import edge_langue
    import voix_sources
    edge_langue.force_language()

    async def run():
        await asyncio.wait_for(edge_tts.Communicate(text, choix["voix"], rate=choix["debit"],
                                                    pitch=choix["hauteur"]).save(path), timeout=SYNTH_TIMEOUT_SEC)
    try:
        asyncio.run(run())
    except Exception as exc:
        voix_sources.noter_microsoft(False)
        voix_sources.erreur_microsoft(exc)
        raise
    ok = os.path.exists(path) and os.path.getsize(path) > 0
    voix_sources.noter_microsoft(ok)
    return ok


def _espeak(text, path):
    wav = path[:-4] + ".wav"
    subprocess.run(["espeak", "-v", "fr", "-b", "1", "-w", wav, text], capture_output=True, timeout=60)
    ok = os.path.exists(wav) and os.path.getsize(wav) > 0
    if ok:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", wav, path],
                       capture_output=True, timeout=60)
        os.remove(wav)
    return os.path.exists(path) and os.path.getsize(path) > 0


def _piper(text, path, choix):
    """Voix Piper, phrase par phrase (2026-10-18) : les phrases fixes des
    annonces viennent du cache (preparees d'avance, une voix par annonce),
    seul le texte qui change est calcule."""
    import annonce_morceaux as morceaux
    import phrases_fixes
    import piper_out
    if choix["moteur"] != "piper" or not piper_out.available(choix.get("voix")):
        return False
    parts = []
    for sentence in phrases_fixes.split(text):
        body, punct = (sentence[:-1], sentence[-1]) if sentence[-1:] in ".!?" else (sentence, ".")
        parts.append((body if phrases_fixes.est_fixe(sentence) else morceaux.Live(body), punct))
    wav = morceaux.assemble(parts, choix)
    if not wav:
        return False
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", wav, path],
                   capture_output=True, timeout=60)
    os.remove(wav)
    return os.path.exists(path) and os.path.getsize(path) > 0


def synthesize(text, path, source=None):
    """Voix du texte dans path (mp3), selon la voix de la source (commande
    "voix") : Microsoft s'il est choisi et present, sinon la voix de secours
    Piper (2026-10-18), espeak en dernier. True si reussi."""
    import voix_sources
    source = source or voix_sources.source_pour()
    choix = voix_sources.choisir(source)
    if choix["moteur"] == "edge":
        try:
            if _edge(text, path, choix):
                return True
        except Exception as exc:
            print(f"[prise_vocale] edge-tts : {str(exc)[:120]}")
        choix = voix_sources.secours(source)
    try:
        if _piper(text, path, choix):
            return True
    except Exception as exc:
        print(f"[prise_vocale] piper : {exc!r}")
    try:
        return _espeak(text, path)
    except Exception as exc:
        print(f"[prise_vocale] espeak : {exc!r}")
        return False


def record(text, sound, label, meta=None, source=None):
    """Fabrique la prise (carillon + voix) au journal ; rend son chemin ou None."""
    tmp = tempfile.mkdtemp(prefix="prise_")
    try:
        voice = os.path.join(tmp, "voix.mp3")
        if not synthesize(text, voice, source):
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
            path = record(text, item["son"], item["etiquette"], item["fiche"], item.get("source"))
            import sons
            if item["son"] and sons.mode_chemin(item["son"]) != "son_phrase":
                # commande "sons" : la prise reste au journal ; on ne fait entendre que le son, ou rien
                play(item["son"], item["tag"])
                continue
            if path:
                play(path, item["tag"])
            else:  # synthese impossible : on dit quand meme, en direct
                import speak
                lock = audio_lock.acquire("donnees")
                try:
                    speak.speak_with_lead_sound(item["son"], text, label=item["tag"], source=item.get("source"))
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
    import voix_sources  # la source se decide ici, dans le service qui annonce
    _queue.put({"texte": text, "son": sound, "etiquette": label, "tag": tag, "fiche": fiche, "fait": done,
                "source": voix_sources.source_pour(tag)})
    if done:
        done.wait(timeout=600)
