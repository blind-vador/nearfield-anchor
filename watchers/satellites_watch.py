"""Ecoute des passages de satellites (lib/satellites.py), lancee par le
scanner quand un passage commence ; la cle est occupee pendant le passage.

  meteo  enregistrement I/Q large (137,1 et 137,9 MHz d'un coup) sur le
         disque de stockage, puis traitement en arriere-plan, au ralenti
         (tools/meteor_traitement.py) : image des nuages decrite par Gemini ;
  iss    relais APRS de la station spatiale (145,825 MHz) : paquets annonces ;
  fm     relais FM radioamateur : le passage n'est garde au journal que si
         une voix a ete entendue (detecteur de voix du scanner).
"""
import os
import subprocess
import sys
import time

import radio_log
import veille_donnees as vd
from sound_paths import RADIO_SCANER_DIR

SOUND = os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3")
METEOR_DIR = "/media/nas/nd2/storage/radio/satellites"
METEOR_CENTER_HZ = 137_500_000
METEOR_RATE = 1_024_000
METEOR_SCRIPT = "/home/vador/autopilot/pi_console/meteor_traitement.py"
FM_RATE = 22050
FM_CHUNK_SEC = 0.2
FM_MIN_VOICE_SEC = 4
FAMILIES = {"meteo": "du satellite météo", "iss": "de la station spatiale",
            "fm": "du satellite radioamateur"}


def _announce(text, label):
    vd.announce(text, SOUND, label, "satellites_watch")


def _meteo(p, seconds):
    os.makedirs(METEOR_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%Hh%M")
    path = os.path.join(METEOR_DIR, f"{p['nom'].replace(' ', '_')}_{stamp}.u8")
    proc = subprocess.run(["rtl_sdr", "-f", str(METEOR_CENTER_HZ), "-s", str(METEOR_RATE), "-g", "40",
                           "-n", str(int(seconds * METEOR_RATE)), path],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=seconds + 60)
    if proc.returncode != 0 or not os.path.exists(path):
        print(f"[satellites_watch] enregistrement Meteor echoue ({proc.returncode})")
        return
    # Traitement au ralenti, hors du scanner (CPU, chaleur) : demodulation,
    # decodage, image decrite par Gemini puis annoncee. Lance comme tache
    # utilisateur systemd a part : un simple processus detache est tue avec
    # le service quand le scanner redemarre (constate le 2026-10-01).
    env = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}")
    unit = f"meteor-{time.strftime('%Y%m%d-%H%M')}"
    subprocess.Popen(["systemd-run", "--user", f"--unit={unit}", "--quiet", "nice", "-n", "19",
                      sys.executable, METEOR_SCRIPT, path, p["nom"]],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    print(f"[satellites_watch] passage Meteor enregistre : {path}")


def _iss(p, seconds):
    import aprs_watch
    aprs_watch.scan_window(dwell_sec=seconds, freq_hz=p["frequences"][0], via="la station spatiale internationale")


def _fm(p, seconds, speech_detector):
    freq = p["frequences"][0]
    # Doppler jusqu'a +-10 kHz a 436 MHz : canal elargi a 40 kHz.
    proc = subprocess.Popen(["rtl_fm", "-f", str(freq), "-M", "fm", "-s", "40000", "-r", str(FM_RATE),
                             "-l", "0", "-E", "dc", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    is_voice = speech_detector() if speech_detector else (lambda chunk, rate: False)
    chunk_bytes = int(FM_RATE * 2 * FM_CHUNK_SEC)
    voice_chunks, kept, tail = 0, [], 0
    end = time.time() + seconds
    try:
        while time.time() < end:
            chunk = proc.stdout.read(chunk_bytes)
            if not chunk:
                break
            if is_voice(chunk, FM_RATE):
                voice_chunks += 1
                kept.append(chunk)
                tail = 5  # garde une seconde apres la voix (fins de phrase)
            elif tail:
                kept.append(chunk)
                tail -= 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    voice_sec = voice_chunks * FM_CHUNK_SEC
    if voice_sec < FM_MIN_VOICE_SEC:
        print(f"[satellites_watch] {p['nom']} : pas de voix ({voice_sec:.0f} s)")
        return
    session = radio_log.RadioLogSession(f"satellite {p['nom']}")
    session.add_raw_pcm(b"".join(kept), FM_RATE, "s16le")
    session.finalize()
    _announce(f"Passage du satellite {p['nom']} : {round(voice_sec)} secondes de voix enregistrées dans le journal.",
              f"satellite_{p['nom']}")


def run_pass(p, speech_detector=None):
    """Ecoute un passage jusqu'a sa fin (bloquant). speech_detector() doit
    rendre une fonction (morceau PCM 16 bits, frequence) -> voix ?"""
    seconds = max(30, int(p["fin"] - time.time()))
    minutes = max(1, round(seconds / 60))
    _announce(f"Passage {FAMILIES[p['famille']]} {p['nom']}, à {p['elevation_max']} degrés au plus haut : "
              f"j'écoute pendant {minutes} minute{'s' if minutes > 1 else ''}.", f"passage_{p['nom']}")
    try:
        if p["famille"] == "meteo":
            _meteo(p, seconds)
        elif p["famille"] == "iss":
            _iss(p, seconds)
        else:
            _fm(p, seconds, speech_detector)
    except Exception as exc:  # un passage rate ne doit pas arreter le scanner
        print(f"[satellites_watch] passage {p['nom']} : {exc!r}")
