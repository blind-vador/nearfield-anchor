"""Lecture d'un long texte d'une traite (flash info, 2026-10-19) : en voix
locale Piper, la console calculait phrase apres phrase, avec un blanc de
calcul entre chaque ; un flash entier devenait tres long a ecouter. Ici le
son du texte entier est fait d'abord, puis joue sans coupure :

  1. machine compagne connectee (Mac, travail "voix") : elle calcule tout,
     ~20 fois plus vite que le Pi ; si elle n'a pas rendu le son a temps,
  2. le Pi le calcule lui-meme, a son rythme (l'attente est assumee).

Voix Microsoft (en direct, sans blanc) et espeak : lecture habituelle.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import audio_lock  # noqa: E402
import compagnon  # noqa: E402
import espeak_out  # noqa: E402
import piper_out  # noqa: E402
import sound_player  # noqa: E402
import voix_sources  # noqa: E402
from phrases import dire  # noqa: E402

DOSSIER = "/home/vador/autopilot/lecture_tmp"
COMPAGNE_BASE_SEC = 60        # delai laisse a la machine compagne...
COMPAGNE_PAR_CAR_SEC = 0.02   # ...plus un peu par caractere (le travail passe par le NAS)


def _par_compagne(text, voice, rate, dest):
    """Rend True si la machine compagne a rendu le son dans dest a temps."""
    machine = compagnon.available("voix")
    if not machine or not machine.get("presente"):
        return False
    import voix_preparation
    try:
        voix_preparation.confier([(voice, rate, text, dest)])
    except Exception as exc:
        print(f"[lecture_longue] machine compagne : {exc!r}", file=sys.stderr)
        return False
    fin = time.time() + COMPAGNE_BASE_SEC + len(text) * COMPAGNE_PAR_CAR_SEC
    while time.time() < fin:
        if os.path.exists(dest) and os.path.getsize(dest) > 1000:
            return True
        time.sleep(1)
    return False


def _morceaux(text):
    """[(evenement du jingle ou None, texte)] selon les reperes de flash_info.son()."""
    import flash_info
    parts = flash_info.MARQUE_SON.split(text)
    out = [(None, parts[0].strip())] + [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts), 2)]
    return [(e, t) for e, t in out if e or t]


def _jingle(evenement, voix):
    import sons
    chemin = sons.chemin(evenement)
    if not chemin:
        return
    voix.stop()
    ticket = audio_lock.acquire("console", timeout=30)
    try:
        sound_player.play_sound_effect(chemin, timeout_sec=30, label="lecture")
    finally:
        audio_lock.release(ticket)


def lire(text, voix, say_and_wait):
    """Dit text d'une traite (voir en-tete), jingles compris (2026-10-23 :
    reperes de flash_info.son(), chaque morceau entre deux jingles est fait
    d'une traite). voix : console/voix.py."""
    morceaux = _morceaux(text)
    if len(morceaux) > 1 or (morceaux and morceaux[0][0]):
        for i, (evenement, morceau) in enumerate(morceaux):
            if evenement:
                _jingle(evenement, voix)
            if morceau:
                _lire_bloc(morceau, voix, say_and_wait, annonce=i == 0 or not any(m for _, m in morceaux[:i]))
        return
    _lire_bloc(text, voix, say_and_wait)


def _lire_bloc(text, voix, say_and_wait, annonce=True):
    choix = voix_sources.choisir("console", ms_ok=voix.cloud_ok())
    if choix["moteur"] != "piper" or not text.strip():
        say_and_wait(text)
        return
    os.makedirs(DOSSIER, exist_ok=True)  # pas /tmp : la compagne est recue par un autre service
    fd, dest = tempfile.mkstemp(prefix="lecture_", suffix=".wav", dir=DOSSIER)
    os.close(fd)
    os.remove(dest)  # le resultat de la compagne n'est pose que si le fichier n'existe pas
    try:
        machine = compagnon.available("voix")
        ok = False
        if machine and machine.get("presente"):
            if annonce:
                say_and_wait(dire("lecture.preparation_compagne"))
            attente = piper_out.Attente(0)  # boucle d'attente pendant le calcul (2026-10-23)
            try:
                ok = _par_compagne(text, choix["voix"], choix["debit"], dest)
            finally:
                attente.stop()
        if not ok:
            if annonce:
                say_and_wait(dire("lecture.preparation_pi"))
            attente = piper_out.Attente(0)
            try:
                ok = piper_out.to_file(text, dest, timeout=espeak_out.duree_max(text, 120) * 4, voice_id=choix["voix"],
                                       scale_len=piper_out.length_scale(choix["debit"]))
            finally:
                attente.stop()
        if not ok:
            say_and_wait(text)  # dernier recours : la lecture habituelle
            return
        voix.stop()
        ticket = audio_lock.acquire("console", timeout=30)
        try:
            sound_player.play_sound_effect(dest, timeout_sec=espeak_out.duree_max(text, 120) * 2, label="lecture")
        finally:
            audio_lock.release(ticket)
    finally:
        try:
            os.remove(dest)
        except OSError:
            pass
