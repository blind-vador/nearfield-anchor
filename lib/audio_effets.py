"""Effets audio composes en Python et appliques via sox (2026-10-09).

Fait sonner les annonces/jingles de la GARE comme s'ils sortaient d'un
haut-parleur de quai, entendu de loin dans un grand espace qui absorbe et ne
renvoie qu'un peu de reverbe (profil "pa"), et pose les sons d'ambiance (train
qui passe, klaxon) dans cette meme salle sans coloration haut-parleur (profil
"hall").

Pourquoi sox et pas une lib DSP Python pure (pedalboard, pysndfx...) : le Pi
est en armv7l (32 bits) -- pedalboard n'a pas de roue pour cette architecture
et scipy y est en conflit de version numpy. sox (14.4.2, deja installe par
install.sh) embarque un vrai reverb Freeverb/Schroeder et tourne pour trois
fois rien sur des clips courts. Ce module compose les chaines cote Python ;
sox n'est que le moteur.

reverb_gare(src, profile) rend une COPIE traitee dans un .wav temporaire et en
retourne le chemin (a l'appelant de le supprimer). None en cas de pepin :
l'appelant rejoue alors le son d'origine, jamais de silence.
"""
import os
import subprocess
import tempfile

RATE = 44100
PAD_SEC = "3"  # silence ajoute en fin : sinon sox coupe la queue de reverb

# --- Profil "pa" : haut-parleur de quai, en DEUX voies paralleles ------------
# En chaine sox unique, la reverb traite le meme flux que le direct : les
# graves et les resonances du direct bavaient dans la queue -> son "tube"
# metallique. On separe donc :
#   * une voie DIRECTE (sec, colore haut-parleur) ;
#   * une voie REVERB SEULE (sox reverb -w), que l'on filtre et dose a part ;
# puis on mixe. Ca permet exactement ce que l'utilisateur demande : moins de
# graves DANS la reverb, un delai plus long avant son retour, et un grand
# espace qui absorbe et ne renvoie qu'un peu.
#
# Coloration haut-parleur commune aux deux voies (reperes sono de gare :
# pavillons ~300-400 Hz a 3,5-5 kHz, bosse de presence, peu de grave) :
#   channels 1/2 : source MONO (haut-parleur = source ponctuelle) re-etalee en
#                  2 voies -- le relief viendra de la SALLE (reverb decorrelee).
#   highpass 250 : coupe les graves (un pavillon ne les reproduit pas).
#   equalizer 2000 1.4q +3 : bosse de presence (intelligibilite de la voix).
#   lowpass 4500 : limite haute de la bande du pavillon.
#   gain -8      : attenuation (effet "plus loin").
_PA_COLOR = [
    "channels", "1", "channels", "2",
    "highpass", "250",
    "equalizer", "2000", "1.4q", "3",
    "lowpass", "4500",
    "gain", "-8",
]
# Voie directe : la coloration + le silence de fin (pour aligner sur la queue).
_PA_DRY = _PA_COLOR + ["pad", "0", PAD_SEC]
# Voie reverb seule : meme coloration, PUIS
#   highpass 500 : on retire les graves de la REVERB (anti-"tube", demande) --
#                  le direct garde son bas-medium, mais l'espace ne renvoie que
#                  le haut-medium.
#   pad 0 3      : de la place pour la queue.
#   reverb -w R HF room stereo predelai wet : REVERB SEULE. Reverberance 65 %
#                  = queue DENSE et lisse (la densite, pas la faible quantite,
#                  est ce qui evite le metallique) ; amortissement aigus 75 %
#                  = traine sombre, grand espace exterieur qui absorbe ; piece
#                  et largeur stereo a fond (dispersion ouverte) ; pre-delai
#                  140 ms = long delai avant que la reverb revienne (grand
#                  espace, eloignement) ; wet 0 (le dosage se fait au gain
#                  suivant).
#   gain -9      : la salle ne renvoie qu'UN PEU (voie humide bien sous le
#                  direct).
_PA_WET = _PA_COLOR + [
    "highpass", "500",
    "pad", "0", PAD_SEC,
    "reverb", "-w", "65", "75", "100", "100", "140", "0",
    "gain", "-8",
]

# --- Profil "hall" : sons d'ambiance (train qui passe, klaxon) ----------------
# Pas de coloration haut-parleur (pas de mono, pas de coupe des graves) : un TGV
# garde son grondement. Juste la salle, legere et pleine bande.
HALL_CHAIN = [
    "gain", "-3",
    "pad", "0", "2",
    "reverb", "55", "60", "100", "100", "40", "0",
    "lowpass", "9000",
]


def _decode_to_wav(src):
    """Decode src (tout format) en wav stereo via ffmpeg -- sox 14.4.2 ne lit
    pas le mp3 sans libsox-fmt-mp3, ffmpeg si. Retourne le chemin du wav."""
    fd, dec = tempfile.mkstemp(suffix=".wav", prefix="gare_dec_")
    os.close(fd)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", src,
         "-f", "wav", "-ar", str(RATE), "-ac", "2", dec],
        stderr=subprocess.DEVNULL, check=True)
    return dec


def _sox(args):
    r = subprocess.run(["sox"] + args, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError("sox: " + r.stderr.decode(errors="replace").strip()[-200:])


def reverb_gare(src, profile="pa"):
    """Rend `src` traite facon gare dans un .wav temporaire (chemin retourne,
    l'appelant le supprime). profile="pa" : voix/jingles facon haut-parleur de
    quai dans un grand espace (direct sec + reverb seule dosee). profile="hall"
    : sons d'ambiance poses dans la salle, pleine bande. None si l'effet echoue
    -- l'appelant rejoue le son d'origine, jamais de silence."""
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="gare_reverb_")
    os.close(fd)
    dec = tmp_dry = tmp_wet = None
    try:
        dec = _decode_to_wav(src)
        if profile == "hall":
            _sox([dec, "-t", "wav", out] + HALL_CHAIN)
        else:  # "pa" : deux voies paralleles puis mixage
            fd, tmp_dry = tempfile.mkstemp(suffix=".wav", prefix="gare_dry_")
            os.close(fd)
            fd, tmp_wet = tempfile.mkstemp(suffix=".wav", prefix="gare_wet_")
            os.close(fd)
            _sox([dec, tmp_dry] + _PA_DRY)
            _sox([dec, tmp_wet] + _PA_WET)
            _sox(["-m", tmp_dry, tmp_wet, "-t", "wav", out])
        if os.path.getsize(out) <= 44:  # 44 o = entete wav vide
            raise RuntimeError("sortie vide")
        return out
    except Exception as exc:
        print(f"[audio_effets] effet indisponible ({os.path.basename(src)}): {exc}")
        try:
            os.remove(out)
        except OSError:
            pass
        return None
    finally:
        for p in (dec, tmp_dry, tmp_wet):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass
