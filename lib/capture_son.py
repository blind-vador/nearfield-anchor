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
ENCODAGE_X = 4.0   # vitesse d'encodage MP3 du Pi, en fois le temps reel (mesure 2026-10-19)


def _lire():
    try:
        with open(ETAT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _vivant(pid):
    """Vrai si le processus tourne encore. Un ffmpeg fini mais pas encore
    recueilli par son parent (zombie, "Z") compte comme fini : sinon l'arret
    attendait 10 s pour rien (2026-10-19)."""
    try:
        os.waitpid(pid, os.WNOHANG)  # notre enfant : le recueillir s'il est fini
    except (ChildProcessError, OSError):
        pass
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
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
    """Arrete la capture tout de suite et lance le MP3 en tache de fond (le
    Pi encode environ 4 fois plus vite que le temps reel : 23 minutes de
    capture bloquaient la console 5 minutes, 2026-10-19). Rend (infos, None)
    ou (None, raison) ; infos : nom, duree (s), mp3, encodeur (Popen) ou
    None si le MP3 n'a pas pu etre lance (le WAV reste, sous son nom final)."""
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
    duree = _infos(wav)["duree"]
    mp3 = os.path.join(DOSSIER, etat["nom"] + ".mp3")
    final_wav = os.path.join(DOSSIER, etat["nom"] + ".wav")
    os.replace(wav, final_wav)
    try:  # encodage detache, basse priorite : la console reste libre ; le WAV part une fois le MP3 fait
        enc = subprocess.Popen(
            ["nice", "-n", "15", "sh", "-c",
             'ffmpeg -hide_banner -loglevel error -nostdin -y -i "$1" -c:a libmp3lame -b:a ' + MP3_DEBIT
             + ' "$2.part.mp3" && mv "$2.part.mp3" "$2" && rm -f "$1" "$1.log"', "encode", final_wav, mp3],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(final_wav + ".log", "w"), start_new_session=True)
    except OSError:
        enc = None
    return {"nom": os.path.basename(mp3 if enc else final_wav), "duree": duree, "mp3": mp3,
            "encodeur": enc, "estimation": max(5, duree / ENCODAGE_X)}, None


def _infos(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout
        duree = float(out.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        duree = 0.0
    return {"chemin": path, "nom": os.path.basename(path), "duree": duree, "taille": os.path.getsize(path)}
