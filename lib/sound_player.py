import os
import subprocess
import time

import audio_lock

EFFECT_RATE = 44100
EFFECT_CHANNELS = 2

# Peripherique audio explicite -- "default" route vers PulseAudio, qui se
# heurte a un bug connu du pilote audio embarque du Pi (snd_bcm2835) et
# echoue silencieusement a produire du son (voir ROADMAP.md). Meme
# peripherique utilise partout dans tous les services audio de ce projet.
# "default" = adaptateur son USB via son mixeur dmix (/etc/asound.conf,
# 2026-09-29) : plusieurs sons peuvent jouer en meme temps, la console parle
# donc meme pendant une diffusion radio. Avant : plughw exclusif du jack
# integre, dont le pilote (snd_bcm2835) plantait.
AUDIO_DEVICE = "default"

# Session d'enregistrement active (voir radio_log.py) -- si definie, tout
# son joue via play_sound_effect() y est aussi copie. Global simple (pas
# de contextvar) : audio_lock garantit deja qu'un seul appelant utilise le
# peripherique audio a la fois dans tout le projet, donc pas de risque de
# melange entre sessions concurrentes. N'importe quel objet exposant une
# methode add_audio_file(path) convient -- pas de dependance directe sur
# radio_log.py ici, pour ne pas coupler ce module generique a une
# fonctionnalite radio-specifique.
_active_recording_session = None


def set_recording_session(session):
    global _active_recording_session
    _active_recording_session = session


def get_recording_session():
    return _active_recording_session


def play_sound_effect(path, timeout_sec=8, label="sound_player", reverb=False):
    """Joue un court effet sonore (mp3) sur le peripherique audio partage,
    de maniere bloquante. Module commun a radio_watch.py, adsb_watch.py et
    meteo_watch.py -- une seule implementation, evite la divergence entre
    copies.

    reverb (2026-10-09) : joue une COPIE traitee facon gare (voir
    audio_effets.reverb_gare) -- reserve aux sons de la GARE. Profil "pa"
    (ou True) = voix/jingles comme sortis d'un haut-parleur de quai ; "hall" =
    sons d'ambiance poses dans la salle, pleine bande ; False = aucun effet.
    Le journal garde malgre tout la source propre (non traitee). Si l'effet
    echoue, on joue le son d'origine, jamais de silence."""
    # Priorites (audio_lock, 2026-10-01) : decode en PCM puis relais par
    # morceaux de 100 ms, pour appliquer le volume du moment (attenue si une
    # source plus prioritaire parle par-dessus) ; rien si la source a abandonne.
    if audio_lock.dropped():
        # Muet (journal ouvert, source qui a cede sa place) : rien sur les
        # haut-parleurs, mais l'enregistrement du journal garde son carillon
        # (2026-10-03 : des prises sans habillage dans le journal).
        if _active_recording_session is not None:
            _active_recording_session.add_audio_file(path)
        return
    reverb_tmp = None
    if reverb:
        import audio_effets
        profile = "pa" if reverb is True else reverb
        reverb_tmp = audio_effets.reverb_gare(path, profile)
    to_play = reverb_tmp or path  # repli sur le son sec si la reverb a echoue
    decoder = player = None
    try:
        decoder = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", to_play,
             "-f", "s16le", "-ac", str(EFFECT_CHANNELS), "-ar", str(EFFECT_RATE), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        player = subprocess.Popen(
            ["aplay", "-q", "-D", AUDIO_DEVICE, "-r", str(EFFECT_RATE), "-f", "S16_LE",
             "-c", str(EFFECT_CHANNELS)], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        deadline = time.time() + timeout_sec
        chunk = EFFECT_RATE * EFFECT_CHANNELS * 2 // 10
        while time.time() < deadline:
            data = decoder.stdout.read(chunk)
            if not data:
                break
            player.stdin.write(audio_lock.scale(data))
        player.stdin.close()
        player.wait(timeout=max(1.0, deadline - time.time() + 1))
        decoder.wait(timeout=2)
        if decoder.returncode != 0:
            err = decoder.stderr.read().decode(errors="replace").strip()[-300:]
            print(f"[{label}] effet sonore echoue ({path}): {err}")
        elif _active_recording_session is not None:
            _active_recording_session.add_audio_file(path)
    except Exception as exc:
        print(f"[{label}] effet sonore indisponible ({path}): {exc}")
    finally:
        for proc in (decoder, player):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
        if reverb_tmp is not None:
            try:
                os.remove(reverb_tmp)
            except OSError:
                pass
