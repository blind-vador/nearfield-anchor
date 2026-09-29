import subprocess

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


def play_sound_effect(path, timeout_sec=8, label="sound_player"):
    """Joue un court effet sonore (mp3) sur le peripherique audio partage,
    de maniere bloquante. Module commun a radio_watch.py, adsb_watch.py et
    meteo_watch.py -- une seule implementation, evite la divergence entre
    copies."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", path, "-f", "alsa", AUDIO_DEVICE],
            capture_output=True, timeout=timeout_sec,
        )
        if result.returncode != 0:
            print(f"[{label}] effet sonore echoue ({path}): {result.stderr.decode(errors='replace').strip()[-300:]}")
        elif _active_recording_session is not None:
            _active_recording_session.add_audio_file(path)
    except Exception as exc:
        print(f"[{label}] effet sonore indisponible ({path}): {exc}")
