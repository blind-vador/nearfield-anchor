"""Effets audio composes en Python et appliques via sox (2026-10-09).

Sert a faire sonner les jingles et les annonces de la GARE comme s'ils etaient
entendus dans le hall : une reverberation (reflexions sur les grandes surfaces
du hall) qui disperse le son et l'attenue un peu, exactement le grain d'une
sonorisation de gare.

Pourquoi sox et pas une lib DSP Python pure (pedalboard, pysndfx...) : le Pi
est en armv7l (32 bits) -- pedalboard n'a pas de roue pour cette architecture
et scipy y est en conflit de version numpy. sox (14.4.2, deja installe par
install.sh) embarque un vrai reverb Freeverb/Schroeder, parfait pour un hall,
et tourne pour trois fois rien sur des clips courts. Ce module compose la
chaine d'effets cote Python ; sox n'est que le moteur.

reverb_gare(src) rend une COPIE reverberee dans un fichier temporaire .wav et
en retourne le chemin (a l'appelant de le supprimer). En cas de pepin, retourne
None : l'appelant joue alors le son d'origine, jamais de silence.
"""
import os
import subprocess
import tempfile

RATE = 44100

# Chaine sox (ordre = ordre d'application) :
#   gain -3      : un peu de marge + l'attenuation demandee ("un peu")
#   reverb R HF room stereo predelai wet-gain : hall de gare. Reverberance 45 %
#                  (queue audible mais la voix reste intelligible, pas noyee),
#                  amortissement des aigus 50 %, piece a fond (grand volume),
#                  largeur stereo a fond (dispersion), pre-delai 18 ms (recul),
#                  wet-gain 0 dB (melange humide/sec equilibre : le sec garde la
#                  clarte de l'annonce).
#   lowpass 8500 : les aigus les plus fins se perdent dans un grand hall / une
#                  sono PA -- couleur typique, evite un reverb trop "cristallin".
REVERB_CHAIN = ["gain", "-3", "reverb", "45", "50", "100", "100", "18", "0", "lowpass", "8500"]


def reverb_gare(src):
    """Rend `src` (mp3/wav...) reverbere facon hall de gare dans un .wav
    temporaire, dont le chemin est retourne (l'appelant le supprime). None si
    l'effet echoue -- l'appelant rejoue alors le son d'origine, tel quel."""
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="gare_reverb_")
    os.close(fd)
    dec = None
    try:
        # ffmpeg decode (tout format) -> sox applique la reverb -> wav.
        # (sox 14.4.2 ne lit pas le mp3 sans libsox-fmt-mp3 ; ffmpeg, lui, lit
        # tout et est deja la chaine de decodage du projet.)
        dec = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", src,
             "-f", "wav", "-ar", str(RATE), "-ac", "2", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        sox = subprocess.run(
            ["sox", "-t", "wav", "-", "-t", "wav", out] + REVERB_CHAIN,
            stdin=dec.stdout, stderr=subprocess.PIPE)
        if dec.stdout:
            dec.stdout.close()
        dec.wait(timeout=5)
        if sox.returncode != 0 or os.path.getsize(out) <= 44:  # 44 o = entete wav vide
            err = sox.stderr.decode(errors="replace").strip()[-200:]
            raise RuntimeError(f"sox reverb: {err or 'sortie vide'}")
        return out
    except Exception as exc:
        print(f"[audio_effets] reverb indisponible ({os.path.basename(src)}): {exc}")
        if dec is not None and dec.poll() is None:
            dec.kill()
        try:
            os.remove(out)
        except OSError:
            pass
        return None
