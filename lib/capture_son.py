"""Capture de ce qui sort du haut-parleur (commande "capture", 2026-10-19) :
tout ce que joue la carte son, en stereo, tel qu'on l'entend (annonces,
scanner, ambiance de la gare, console...), pour garder et partager un
extrait.

La sortie par defaut (/etc/asound.conf, sortie_double) envoie chaque son a
la fois a la carte USB et a une carte "boucle" (snd-aloop, id Loopback) ;
la capture enregistre l'autre bout de la boucle (hw:Loopback,1,0), sans
toucher a ce qu'on entend. Enregistrement en WAV pendant la capture (rien
de perdu si le Pi s'arrete), MP3 320 kb/s a l'arret : le fichier a partager,
dans storage/capture du disque nd2 (partage reseau "stockage").

L'etat vit dans un fichier : une capture survit a un redemarrage de la
console et s'arrete depuis n'importe ou. Garde-fou : MAX_SEC.
"""
import json
import os
import signal
import subprocess
import time
from datetime import datetime

DOSSIER = "/media/nas/nd2/storage/capture"
ETAT = "/home/vador/autopilot/capture_en_cours.json"
SOURCE = "hw:Loopback,1,0"
RATE, CANAUX = 48000, 2   # ceux de la boucle (asound.conf, boucle_mix)
MAX_SEC = 2 * 3600
MP3_DEBIT = "320k"


def _lire():
    try:
        with open(ETAT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _vivant(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def en_cours():
    """L'etat de la capture en cours, ou None."""
    etat = _lire()
    if etat and _vivant(etat["pid"]):
        return etat
    return None


def demarrer():
    """Lance la capture. Rend (etat, None) ou (None, raison)."""
    if en_cours():
        return None, "deja"
    if not os.path.isdir(os.path.dirname(DOSSIER)):
        return None, "disque_absent"
    os.makedirs(DOSSIER, exist_ok=True)
    nom = datetime.now().strftime("capture_%Y-%m-%d_%Hh%M-%S")
    wav = os.path.join(DOSSIER, nom + ".en_cours.wav")
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "alsa", "-channels", str(CANAUX),
         "-sample_rate", str(RATE), "-i", SOURCE, "-t", str(MAX_SEC), "-c:a", "pcm_s16le", wav],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    time.sleep(1.0)
    if proc.poll() is not None:
        err = (proc.stderr.read() or b"").decode(errors="replace").strip()
        print(f"[capture] ffmpeg : {err[-300:]}")
        return None, "boucle_absente"
    etat = {"pid": proc.pid, "wav": wav, "nom": nom, "debut": time.time()}
    tmp = ETAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(etat, f)
    os.replace(tmp, ETAT)
    return etat, None


def arreter():
    """Arrete la capture et fait le MP3. Rend (infos, None) ou (None, raison) ;
    infos : chemin, nom, duree (s), taille (octets)."""
    etat = _lire()
    if not etat:
        return None, "aucune"
    if _vivant(etat["pid"]):
        os.kill(etat["pid"], signal.SIGINT)  # ffmpeg ferme proprement le WAV
        for _ in range(100):
            if not _vivant(etat["pid"]):
                break
            time.sleep(0.1)
        else:
            os.kill(etat["pid"], signal.SIGKILL)
    try:
        os.remove(ETAT)
    except OSError:
        pass
    wav = etat["wav"]
    if not os.path.exists(wav) or os.path.getsize(wav) < 1000:
        return None, "vide"
    mp3 = os.path.join(DOSSIER, etat["nom"] + ".mp3")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", wav,
                        "-c:a", "libmp3lame", "-b:a", MP3_DEBIT, mp3], capture_output=True, timeout=1800)
    if r.returncode != 0 or not os.path.exists(mp3):
        final = os.path.join(DOSSIER, etat["nom"] + ".wav")  # au moins le WAV, sous son nom definitif
        os.replace(wav, final)
        return _infos(final), None
    os.remove(wav)
    return _infos(mp3), None


def _infos(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout
        duree = float(out.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        duree = 0.0
    return {"chemin": path, "nom": os.path.basename(path), "duree": duree, "taille": os.path.getsize(path)}
