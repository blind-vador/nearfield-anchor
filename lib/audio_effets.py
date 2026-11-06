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
# Voie directe : la coloration de base PUIS un renfort de caractere "pavillon"
# applique SEULEMENT au direct (pas a la reverb, qu'on garde propre) --
# retabli le 2026-10-09 apres que le nettoyage de la reverb a fait paraitre le
# direct trop "neutre" :
#   equalizer 950 1.0q +4  : la bosse "nasillarde" ~900-1000 Hz, signature du
#                            pavillon/megaphone -- c'est elle qui fait "sono".
#   equalizer 2500 1.4q +3 : renfort de presence (mordant + intelligibilite).
# puis le silence de fin (pour aligner sur la queue de reverb).
_PA_DRY = _PA_COLOR + [
    "equalizer", "950", "1.0q", "4",
    "equalizer", "2500", "1.4q", "3",
    "pad", "0", PAD_SEC,
]
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
# --- Profils "pa_jingle" et "pa_voix" (2026-10-22, demande utilisateur) ------
# Avec "pa" pour tout, la voix des annonces sortait trop faible a cote du
# jingle (une voix de synthese est moins "dense" qu'un jingle masterise) et le
# jingle n'avait pas assez l'air de sortir d'une sono de quai.
#   pa_jingle : pavillon plus marque (bosse nasillarde +7 dB, bande plus
#               etroite 300-4000 Hz) et salle plus presente (voie humide a
#               -4 au lieu de -8, queue plus longue), puis normalise a -10 dBFS (+3 dB a peine sur l ancien) ;
#   pa_voix   : meme sono que "pa", puis compresseur (la voix garde un niveau
#               regulier, les syllabes faibles remontent) et normalisation a
#               -4,5 dBFS : la voix passe 2 dB au-dessus du jingle (mesure sox stats).
_JINGLE_COLOR = [
    "channels", "1", "channels", "2",
    "highpass", "300",
    "equalizer", "2000", "1.4q", "3",
    "lowpass", "4000",
    "gain", "-8",
]
_JINGLE_DRY = _JINGLE_COLOR + [
    "equalizer", "950", "1.0q", "7",
    "equalizer", "2500", "1.4q", "4",
    "pad", "0", PAD_SEC,
]
_JINGLE_WET = _JINGLE_COLOR + [
    "highpass", "500",
    "pad", "0", PAD_SEC,
    "reverb", "-w", "75", "70", "100", "100", "140", "0",
    "gain", "-4",
]
_FINAL = {
    "pa_jingle": ["gain", "-n", "-10"],
    "pa_voix": ["compand", "0.01,0.25", "-70,-70,-60,-36,-24,-14,0,-6", "0", "-90", "0.05",
                "gain", "-n", "-4.5"],
}
CACHE_DIR = "/tmp/gare_effets"     # jingles traites gardes (calcules une fois)

HALL_CHAIN = [
    "gain", "-3",
    "pad", "0", "2",
    "reverb", "55", "60", "100", "100", "40", "0",
    "lowpass", "9000",
]


# --- Rendu en memoire (2026-11-06) --------------------------------------------------
# Decodage, sox et salle passent par des tuyaux et des tableaux : plus aucun
# fichier pour un son de gare (19 Mo a chaque annonce sur la carte SD). La chaine
# par fichiers (reverb_gare) ne sert plus que de secours.

def _decoder_pcm(src):
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", src,
         "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout


def _wav_pcm(octets):
    """(canaux, taux, donnees) d'un wav recu par un tuyau : sox n'a pas pu y ecrire
    la vraie longueur, les donnees vont donc jusqu'a la fin si elle est fausse."""
    pos, canaux, taux = 12, None, None
    while pos + 8 <= len(octets):
        nom, taille = octets[pos:pos + 4], int.from_bytes(octets[pos + 4:pos + 8], "little")
        if nom == b"fmt ":
            canaux = int.from_bytes(octets[pos + 10:pos + 12], "little")
            taux = int.from_bytes(octets[pos + 12:pos + 16], "little")
        elif nom == b"data":
            debut = pos + 8
            fin = debut + taille if 0 < taille and debut + taille <= len(octets) else len(octets)
            return canaux, taux, octets[debut:fin]
        pos += 8 + taille + (taille & 1)
    raise RuntimeError("wav sans donnees")


def _sox_pcm(pcm, effets, taux=None):
    """PCM stereo -> sox -> (canaux, taux, donnees), sans fichier sauf le tampon
    interne de sox pour "gain -n", en memoire vive."""
    import tmp_ram
    r = subprocess.run(
        ["sox", "--temp", tmp_ram.dossier() or tempfile.gettempdir(),
         "-t", "raw", "-r", str(RATE), "-e", "signed", "-b", "16", "-c", "2", "-",
         "-t", "wav"] + (["-r", str(taux)] if taux else []) + ["-"] + effets,
        input=pcm, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("sox: " + r.stderr.decode(errors="replace").strip()[-200:])
    return _wav_pcm(r.stdout)


def _rendu_pcm(src, profile):
    dec = _decoder_pcm(src)
    if profile == "hall":
        canaux, taux, sortie = _sox_pcm(dec, HALL_CHAIN)
    else:
        import numpy as np
        import reverb_quai
        couleur = _JINGLE_DRY if profile == "pa_jingle" else _PA_DRY
        if couleur[-3:] == ["pad", "0", PAD_SEC]:
            couleur = couleur[:-3]
        if profile == "pa_voix":
            couleur = couleur + _FINAL["pa_voix"][:6]
        ch, fs, brut = _sox_pcm(dec, couleur, reverb_quai.FS)
        if fs != reverb_quai.FS:
            raise RuntimeError(f"taux {fs} inattendu")
        sec = np.frombuffer(brut[: len(brut) - len(brut) % 2], dtype=np.int16).astype(np.float32) / 32768
        sec = sec[: len(sec) - len(sec) % ch].reshape(-1, ch).mean(axis=1)
        canaux, taux, sortie = 2, reverb_quai.FS, reverb_quai.pcm_stereo(reverb_quai.traiter(sec, profile))
    if canaux != 2 or taux != RATE or not sortie:
        raise RuntimeError(f"sortie inattendue ({canaux} canaux, {taux} Hz, {len(sortie)} octets)")
    return sortie


def _jingle_pcm(src):
    """Le jingle traite reste garde dans CACHE_DIR : calcule une fois, puis relu."""
    import hashlib
    import wave
    cle = hashlib.md5(f"{src}|{os.path.getmtime(src)}|pa_jingle".encode()).hexdigest()
    garde = os.path.join(CACHE_DIR, cle + ".wav")
    try:
        with wave.open(garde) as w:
            if w.getnchannels() == 2 and w.getframerate() == RATE and w.getsampwidth() == 2:
                return w.readframes(w.getnframes())
    except (OSError, EOFError, wave.Error):
        pass
    pcm = _rendu_pcm(src, "pa_jingle")
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = f"{garde}.{os.getpid()}.tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    os.replace(tmp, garde)
    return pcm


def pcm_gare(src, profile="pa"):
    """Le son traite facon gare, en octets (stereo, 16 bits, RATE) ; None si l'effet
    echoue : l'appelant reprend reverb_gare, puis le son sec."""
    try:
        return _jingle_pcm(src) if profile == "pa_jingle" else _rendu_pcm(src, profile)
    except Exception as exc:
        print(f"[audio_effets] rendu en memoire indisponible ({os.path.basename(src)}): {exc}")
        return None


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
    """Comme _reverb_gare ; le jingle traite ("pa_jingle") est garde dans
    CACHE_DIR et rendu en copie : calcule une seule fois, il part tout de suite."""
    if profile != "pa_jingle":
        return _reverb_gare(src, profile)
    import hashlib
    import shutil
    try:
        cle = hashlib.md5(f"{src}|{os.path.getmtime(src)}|{profile}".encode()).hexdigest()
    except OSError:
        return _reverb_gare(src, profile)
    garde = os.path.join(CACHE_DIR, cle + ".wav")
    if not os.path.exists(garde):
        fait = _reverb_gare(src, profile)
        if not fait:
            return None
        os.makedirs(CACHE_DIR, exist_ok=True)
        os.replace(fait, garde)
    fd, copie = tempfile.mkstemp(suffix=".wav", prefix="gare_reverb_")
    os.close(fd)
    shutil.copyfile(garde, copie)
    return copie


def _quai(dec, out, profile):
    """Sono de quai (2026-10-22) : coloration haut-parleur par sox (sans reverb,
    sans silence ajoute), compresseur pour la voix, puis la salle par
    convolution (reverb_quai : grand espace ouvert, queue entiere). Faux si
    quelque chose manque : l'appelant reprend l'ancienne chaine sox."""
    couleur = _JINGLE_DRY if profile == "pa_jingle" else _PA_DRY
    if couleur[-3:] == ["pad", "0", PAD_SEC]:
        couleur = couleur[:-3]
    if profile == "pa_voix":
        couleur = couleur + _FINAL["pa_voix"][:6]  # compand seul : le niveau est regle ensuite
    fd, sec = tempfile.mkstemp(suffix=".wav", prefix="gare_sec_")
    os.close(fd)
    try:
        import reverb_quai
        _sox([dec, "-t", "wav", "-r", str(reverb_quai.FS), sec] + couleur)
        reverb_quai.appliquer(sec, out, profile)
        return True
    except Exception as exc:
        print(f"[audio_effets] reverb de quai indisponible ({exc!r}) : ancienne reverb")
        return False
    finally:
        try:
            os.remove(sec)
        except OSError:
            pass


def _reverb_gare(src, profile="pa"):
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
        elif _quai(dec, out, profile):
            pass  # reverb de grand espace ouvert par convolution (lib/reverb_quai.py)
        else:  # secours : l'ancienne reverb sox, deux voies paralleles puis mixage
            dry, wet = (_JINGLE_DRY, _JINGLE_WET) if profile == "pa_jingle" else (_PA_DRY, _PA_WET)
            fd, tmp_dry = tempfile.mkstemp(suffix=".wav", prefix="gare_dry_")
            os.close(fd)
            fd, tmp_wet = tempfile.mkstemp(suffix=".wav", prefix="gare_wet_")
            os.close(fd)
            _sox([dec, tmp_dry] + dry)
            _sox([dec, tmp_wet] + wet)
            _sox(["-m", tmp_dry, tmp_wet, "-t", "wav", out] + _FINAL.get(profile, []))
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
