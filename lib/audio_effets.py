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

# Chaine sox (ordre = ordre d'application). Objectif : une annonce de GARE
# comme sortie d'un haut-parleur de quai (bande limitee, graves coupes) et
# entendue de loin dans un grand hall reverberant -- diffus, pas metallique.
#
# Reperes sono de gare (recherche 2026-10-09) : les pavillons de sonorisation
# (type Tannoy/paging) ont une bande utile etroite, grossierement 300-400 Hz a
# 3,5-5 kHz, avec une bosse de presence vers 1,5-3 kHz (intelligibilite) et peu
# de grave. Le metallique de la premiere version venait d'une reverb trop
# claire (lowpass 8,5 kHz) et pas assez dense (reverberance 45 % = echos
# distincts facon peigne) : ici on assombrit et on densifie.
#
#   channels 1 / channels 2 : somme en MONO (le haut-parleur est une source
#       ponctuelle) puis re-etale en 2 voies -- le relief stereo ne viendra pas
#       du son lui-meme mais de la SALLE (reverb decorrelee G/D ci-dessous).
#       C'est la bonne version de l'astuce "mono joue sur G et D" : mono au
#       depart, largeur fournie par la reverb, pas par un panning du sec.
#   highpass 250  : coupe les graves -- un pavillon de quai ne les reproduit pas.
#   equalizer 2000 1.4q +3 : bosse de presence (le cote "nasillard" du pavillon,
#       et surtout l'intelligibilite de la voix -- essentiel ici).
#   lowpass 4500  : limite haute de la bande du pavillon ; enleve le sifflant
#       qui rendait la reverb metallique, et simule l'absorption des aigus par
#       la distance et l'air.
#   gain -8       : nettement plus attenue qu'avant (effet "plus loin").
#   pad 0 2.5     : silence ajoute en fin, sinon sox coupe la traine (teste) ;
#       laisse resonner la longue queue du hall.
#   echos ... 53/89 ms : deux reflexions precoces (rebond sur les parois du
#       hall), faibles et irregulieres -- donne le "claquement" d'un grand
#       volume sans flutter metallique.
#   reverb 78 82 100 100 65 0 : grand hall diffus. Reverberance 78 % (dense,
#       lisse, pas d'echos isoles), amortissement aigus 82 % (traine SOMBRE,
#       anti-metallique), piece + largeur stereo a fond (dispersion ouverte),
#       pre-delai 65 ms (recul = grand espace/eloignement), wet 0 dB (bien
#       present : son lointain = beaucoup de reverb par rapport au direct).
PA_CHAIN = [
    "channels", "1", "channels", "2",
    "highpass", "250",
    "equalizer", "2000", "1.4q", "3",
    "lowpass", "4500",
    "gain", "-8",
    "pad", "0", "2.5",
    "echos", "0.8", "0.9", "53", "0.28", "89", "0.2",
    "reverb", "78", "82", "100", "100", "65", "0",
]

# Profil "hall" : pour les sons d'AMBIANCE de la gare qui ne passent PAS par
# un haut-parleur (train qui passe, klaxon, bruitages d'arrivee/depart). On
# veut seulement les poser dans la grande salle reverberante -- SANS la
# coloration pavillon (pas de mono, pas de coupe des graves) : un TGV garde
# son grondement et son relief. Reverb plus legere et pleine bande, juste un
# peu de recul et de queue.
HALL_CHAIN = [
    "gain", "-3",
    "pad", "0", "2",
    "reverb", "55", "60", "100", "100", "40", "0",
    "lowpass", "9000",
]

CHAINS = {"pa": PA_CHAIN, "hall": HALL_CHAIN}


def reverb_gare(src, profile="pa"):
    """Rend `src` (mp3/wav...) traite facon gare dans un .wav temporaire, dont
    le chemin est retourne (l'appelant le supprime). profile="pa" : voix et
    jingles sortant d'un haut-parleur de quai (bande limitee + grand hall).
    profile="hall" : sons d'ambiance poses dans la salle, pleine bande. None si
    l'effet echoue -- l'appelant rejoue alors le son d'origine, tel quel."""
    chain = CHAINS.get(profile, PA_CHAIN)
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
            ["sox", "-t", "wav", "-", "-t", "wav", out] + chain,
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
