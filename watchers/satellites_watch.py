"""Ecoute des passages de satellites (lib/satellites.py), lancee par le
scanner quand un passage commence ; la cle est occupee pendant le passage.

  meteo  enregistrement I/Q large (137,1 et 137,9 MHz d'un coup) dans un
         travail du pont (lib/compagnon.py, disque nd1) : la machine compagne
         calcule l'image, le Pi la fait decrire par Gemini et l'annonce
         (resultats_compagnon). Sans machine compagne, pas d'ecoute Meteor ;
  iss    relais APRS de la station spatiale (145,825 MHz) : paquets annonces ;
  fm     relais FM radioamateur : le passage n'est garde au journal que si
         une voix a ete entendue (detecteur de voix du scanner).
"""
import os
import subprocess
import time

import radio_log
import ceremonie_radio as ceremonie
import veille_donnees as vd
from sound_paths import RADIO_SCANER_DIR
from lecture_flux import lire
from phrases import Etiquettes, dire

import sons
SOUND = sons.chemin("satellite", os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3"))  # son propre (2026-10-23, sons.py)
# Encadrement de toute diffusion dans le journal, comme le scanner (2026-10-02).
INCOMING_SOUND = os.path.join(RADIO_SCANER_DIR, "incoming.mp3")
FREQ_OPEN_SOUND = os.path.join(RADIO_SCANER_DIR, "freq_open.mp3")
FREQ_OVER_SOUND = os.path.join(RADIO_SCANER_DIR, "freq_over.mp3")
METEOR_CENTER_HZ = 137_500_000
METEOR_RATE = 1_024_000
FM_RATE = 22050
FM_CHUNK_SEC = 0.2
FM_MIN_VOICE_SEC = 4
FAMILIES = Etiquettes("satellite.passage", {"meteo": "du satellite météo", "iss": "de la station spatiale",
                                             "fm": "du satellite radioamateur"}, "après « Passage » : Passage du satellite météo")


def _announce(text, label):
    vd.announce(text, SOUND, label, "satellites_watch", wait=True)  # dit avant l'ecoute du passage


def _meteo(p, seconds):
    import compagnon
    params = {"satellite": p["nom"], "fichier": "iq.u8", "centre": METEOR_CENTER_HZ, "taux": METEOR_RATE,
              "frequences": p["frequences"], "format": "u8"}
    job_id, folder, job = compagnon.new_job("meteor", params)
    path = os.path.join(folder, "iq.u8")
    proc = subprocess.run(["rtl_sdr", "-f", str(METEOR_CENTER_HZ), "-s", str(METEOR_RATE), "-g", "40",
                           "-n", str(int(seconds * METEOR_RATE)), path],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=seconds + 60)
    if proc.returncode != 0 or not os.path.exists(path):
        print(f"[satellites_watch] enregistrement Meteor echoue ({proc.returncode})")
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        return
    compagnon.submit(job)  # la machine compagne prend la suite
    print(f"[satellites_watch] passage Meteor confie a la machine compagne : {job_id}")


def _iss(p, seconds):
    import aprs_watch
    aprs_watch.scan_window(dwell_sec=seconds, freq_hz=p["frequences"][0], via=dire("satellite.iss"))


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
            chunk = lire(proc.stdout, chunk_bytes)
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
    ceremonie.poser_ouverture(session, p["nom"], "FM")
    session.add_raw_pcm(b"".join(kept), FM_RATE, "s16le")
    ceremonie.poser_fin(session)
    session.finalize()
    _announce(dire("satellite.passage_satellite_secondes_voix", nom=p['nom'], round=round(voice_sec)),
              f"satellite_{p['nom']}")


def run_pass(p, speech_detector=None):
    """Ecoute un passage jusqu'a sa fin (bloquant). speech_detector() doit
    rendre une fonction (morceau PCM 16 bits, frequence) -> voix ?"""
    seconds = max(30, int(p["fin"] - time.time()))
    minutes = max(1, round(seconds / 60))
    _announce(dire("satellite.passage_degres_au_plus", v1=FAMILIES[p['famille']], nom=p['nom'], elevation_max=p['elevation_max'], minutes=minutes, pluriel='s' if minutes > 1 else ''), f"passage_{p['nom']}")
    try:
        if p["famille"] == "meteo":
            _meteo(p, seconds)
        elif p["famille"] == "iss":
            _iss(p, seconds)
        else:
            _fm(p, seconds, speech_detector)
    except Exception as exc:  # un passage rate ne doit pas arreter le scanner
        print(f"[satellites_watch] passage {p['nom']} : {exc!r}")
