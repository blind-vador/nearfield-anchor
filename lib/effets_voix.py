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
    # 2026-11-14, demande utilisateur : "echo plus long, autres reverbes"
    "echo_long": "aecho=0.8:0.6:420|840|1260:0.45|0.3|0.18",
    "cathedrale": ("aecho=0.8:0.9:61|113|167|229|293|389|523|683|887|1103:"
                   "0.55|0.5|0.46|0.42|0.38|0.33|0.28|0.23|0.18|0.13"),
    "salle": "aecho=0.9:0.8:19|31|47|67|89:0.35|0.3|0.25|0.2|0.15",
    "megaphone": "highpass=f=400,lowpass=f=3600,equalizer=f=1700:t=q:w=1.5:g=6,acompressor=threshold=0.1:ratio=4",
    "radio_ancienne": "highpass=f=320,lowpass=f=2600,acrusher=bits=9:mode=log:aa=1,tremolo=f=8:d=0.12",
    "lointain": "lowpass=f=1800,aecho=0.7:0.6:90|170:0.35|0.25",
    "phaser": "aphaser=in_gain=0.6:out_gain=0.9:delay=3:decay=0.5:speed=0.5",
}
# Correction de volume (dB) par effet, mesuree sur des voix du cache (2026-11-14,
# niveau de la parole active, 5 voix Microsoft) : chaque effet sonne aussi fort
# que la voix seche. Robot perdait 14,5 dB, echo 5,8, flanger 5,1, telephone 4,1.
GAINS_DB = {
    "echo": 5.8, "reverbe": 0.9, "harmonique": -0.8, "dissonant": -0.9, "choeur": 2.0,
    "robot": 14.5, "telephone": 4.1, "flanger": 5.1, "vibrato": -0.8,
    "echo_long": 9.6, "cathedrale": 2.5, "salle": 1.6, "megaphone": 8.0,
    "radio_ancienne": 5.2, "lointain": 7.3, "phaser": 5.3,
}
# Apres le gain : les cretes restent sous -1 dBFS (pas de saturation).
LIMITEUR = "alimiter=limit=0.89:attack=5:release=60:level=0"
CHOIX = [AUCUN, ALEATOIRE] + list(EFFETS)


def tirer(reglage):
    """Id d'effet a appliquer maintenant (None : la voix telle quelle)."""
    if reglage == ALEATOIRE:
        return random.choice(list(EFFETS))
    return reglage if reglage in EFFETS else None


def filtre(effet_id):
    """Chaine -af complete, ou None."""
    f = EFFETS.get(effet_id or "")
    if not f:
        return None
    gain = GAINS_DB.get(effet_id, 0.0)
    regle = f",volume={gain:+.1f}dB" if gain else ""
    return f"aresample={_TAUX},{f}{regle},{LIMITEUR}"


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
