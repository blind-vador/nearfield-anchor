"""Effets sur la voix des annonces (2026-11-13, commande "voix") : chaque source
d'annonces (veille gare, avions, radio...) a un effet, "aucun" (la voix telle
quelle, par defaut) ou "aleatoire" (un effet tire a chaque annonce, comme les
voix aleatoires).

Les effets sont des filtres ffmpeg, appliques au son de la voix seulement (le
jingle reste sec), apres la synthese : les caches de voix restent propres.

  lecture       sound_player.play_sound_effect(..., effet=filtre) : filtre
                pose dans le decodeur
  fichier       cuire(chemin, filtre) : la prise du journal garde la voix
                avec son effet (lib/prise_vocale.py, annonces de gare)
"""
import os
import random
import subprocess
import tempfile

AUCUN = "aucun"
ALEATOIRE = "aleatoire"
_TAUX = 44100

# id -> filtre ffmpeg (l'entree est rendue a 44,1 kHz d'abord : asetrate en depend)
EFFETS = {
    "echo": "aecho=0.8:0.7:180|360:0.4|0.2",
    "reverbe": "aecho=0.8:0.85:43|71|97|131|173|229|281:0.5|0.45|0.4|0.35|0.3|0.25|0.2",
    "harmonique": (f"asplit[a][b];[b]asetrate={_TAUX}*1.5,aresample={_TAUX},atempo=0.6667,volume=0.5[c];"
                   "[a][c]amix=inputs=2:normalize=0"),
    "dissonant": (f"asplit[a][b];[b]asetrate={_TAUX}*1.4142,aresample={_TAUX},atempo=0.7071,volume=0.5[c];"
                  "[a][c]amix=inputs=2:normalize=0"),
    "choeur": "chorus=0.6:0.9:50|60|40:0.4|0.32|0.3:0.25|0.4|0.3:2|2.3|1.3",
    "robot": "afftfilt=real='hypot(re,im)*sin(0)':imag='hypot(re,im)*cos(0)':win_size=512:overlap=0.75",
    "telephone": "highpass=f=350,lowpass=f=3200,acompressor=threshold=0.1:ratio=6,volume=1.6",
    "flanger": "flanger=delay=2:depth=4:speed=0.4",
    "vibrato": "vibrato=f=6:d=0.4",
    "grave": f"asetrate={_TAUX}*0.82,aresample={_TAUX},atempo=1.2195",
    "aigu": f"asetrate={_TAUX}*1.2,aresample={_TAUX},atempo=0.8333",
}
CHOIX = [AUCUN, ALEATOIRE] + list(EFFETS)


def tirer(reglage):
    """Id d'effet a appliquer maintenant (None : la voix telle quelle)."""
    if reglage == ALEATOIRE:
        return random.choice(list(EFFETS))
    return reglage if reglage in EFFETS else None


def filtre(effet_id):
    """Chaine -af complete, ou None."""
    f = EFFETS.get(effet_id or "")
    return f"aresample={_TAUX},{f}" if f else None


def cuire(chemin, effet_id):
    """Applique l'effet au fichier, en place (meme format de sortie que l'extension).
    Rend True si l'effet est pose ; le fichier reste intact sinon."""
    f = filtre(effet_id)
    if not f or not chemin or not os.path.isfile(chemin):
        return False
    ext = os.path.splitext(chemin)[1] or ".wav"
    fd, tmp = tempfile.mkstemp(suffix=ext, prefix="effet_", dir=os.path.dirname(chemin))
    os.close(fd)
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", chemin,
                            "-af", f, tmp], capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.getsize(tmp) > 1000:
            os.replace(tmp, chemin)
            return True
        print(f"[effets_voix] {effet_id} : {r.stderr.decode(errors='replace')[-200:]}", flush=True)
    except Exception as exc:
        print(f"[effets_voix] {effet_id} : {exc!r}", flush=True)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return False


def ecouter(chemin, effet_id):
    """Joue le fichier avec l'effet (apercu de la commande voix) ; rend le processus."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", chemin]
    f = filtre(effet_id)
    if f:
        args += ["-af", f]
    return subprocess.Popen(args + ["-f", "alsa", "default"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
