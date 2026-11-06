"""Reverberation "grand espace ouvert" par convolution (2026-10-22).

Demande utilisateur : la reverb sox (Freeverb) sonnait metallique, "tube",
avec un retour trop court, et sa queue etait coupee net (fichier tronque 3 s
apres la parole). But : entendre la sono d'une gare "en survolant les quais" :
grand espace ouvert, reflexion legere, queue longue et douce.

Pourquoi c'etait metallique : Freeverb = filtres en peigne et passe-tout en
parallele ; leurs retards fixes creent des resonances regulieres (le "tube"),
surtout audibles a fort volume. Ici, plus aucun peigne : la reverb est une
reponse impulsionnelle (RI) synthetique, convoluee par FFT (numpy) :

  * QUEUE DIFFUSE : bruit gaussien INDEPENDANT a gauche et a droite (aucune
    periodicite, image large), mis en forme par une decroissance
    exponentielle PAR BANDE (absorption de l'air en exterieur, ISO 9613-1 :
    les aigus meurent vite, le medium porte loin) :
        < 250 Hz : RT60 1,2 s   (espace ouvert, et un pavillon n'a pas de grave)
        250-1k   : 2,6 s
        1k-3k    : 2,3 s
        3k-6k    : 1,3 s
        > 6k     : 0,6 s
    Montee progressive (fondu en cosinus de 220 ms apres un pre-delai de
    95 ms) : pas d'attaque seche, la salle "arrive" doucement.
  * REFLEXIONS PRECOCES : quelques echos faibles et epars (55 a 240 ms, -15
    a -24 dB), alternes gauche/droite, chacun etale sur ~6 ms (pas de clic) :
    les autres haut-parleurs du quai et la marquise, loin. Retards non
    multiples entre eux (pas de flutter).
  * Avant convolution, la voie reverb est passe-haut 350 Hz et passe-bas
    5 kHz (anti-"tube", anti-sifflement).
  * Rien n'est tronque : la sortie dure le son + toute la RI (4,5 s, la queue
    finit sous -90 dB), puis un fondu de 50 ms par securite.

Puis niveau : moyenne (RMS des passages actifs) amenee a la cible du profil,
crete limitee a -1 dBFS. La RI de chaque profil est calculee une fois puis
gardee (graine fixe : la meme reverb a chaque fois).
"""
import os
import wave

import numpy as np

FS = 44100
RI_SEC = 4.5
PREDELAI_SEC = 0.095
MONTEE_SEC = 0.22
BANDES = [(0, 250, 1.2), (250, 1000, 2.6), (1000, 3000, 2.3), (3000, 6000, 1.3), (6000, FS / 2, 0.6)]
REFLEXIONS = [(0.055, -15, "g"), (0.083, -17, "d"), (0.127, -19, "g"), (0.176, -21, "d"), (0.241, -24, "c")]
VOIE_REVERB = (350.0, 5000.0)
PROFILS = {
    # humide (dB d'energie de la reverb par rapport au direct), cible RMS (dBFS)
    "pa": {"humide": -11.0, "cible": -25.0},
    "pa_voix": {"humide": -12.0, "cible": -25.0},     # la voix reste nette, la salle la porte
    "pa_jingle": {"humide": -7.0, "cible": -27.0},    # le jingle baigne davantage dans le quai
}
CACHE = "/tmp/gare_effets"
_ri = {}


def _passe_bande(x, bas, haut, n=None):
    n = n or len(x)
    X = np.fft.rfft(x, n)
    f = np.fft.rfftfreq(n, 1 / FS)
    X[(f < bas) | (f >= haut)] = 0
    return np.fft.irfft(X, n)[: len(x)]


def reponse_impulsionnelle(graine=1842):
    """RI stereo (2, N) normalisee en energie a 1 par canal."""
    if "ri" in _ri:
        return _ri["ri"]
    chemin = os.path.join(CACHE, f"ri_quai_{graine}.npy")
    try:
        ri = np.load(chemin)
        _ri["ri"] = ri
        return ri
    except (OSError, ValueError):
        pass
    rng = np.random.default_rng(graine)
    n = int(RI_SEC * FS)
    t = np.arange(n) / FS
    debut = int(PREDELAI_SEC * FS)
    montee = np.clip((t - PREDELAI_SEC) / MONTEE_SEC, 0, 1)
    montee = 0.5 - 0.5 * np.cos(np.pi * montee)
    ri = np.zeros((2, n), dtype=np.float64)
    for c in range(2):
        bruit = rng.standard_normal(n)
        queue = np.zeros(n)
        for bas, haut, rt60 in BANDES:
            queue += _passe_bande(bruit, bas, haut) * np.exp(-6.9078 * t / rt60)
        queue *= montee
        queue[:debut] = 0
        ri[c] = queue / np.sqrt(np.sum(queue ** 2))
    # reflexions precoces : petites bouffees de 6 ms, a energie relative fixee
    larg = int(0.006 * FS)
    fenetre = np.hanning(larg)
    for sec, db, cote in REFLEXIONS:
        i = int(sec * FS)
        for c in range(2):
            if (cote == "g" and c == 1) or (cote == "d" and c == 0):
                continue
            bouffee = rng.standard_normal(larg) * fenetre
            bouffee *= 10 ** (db / 20) / np.sqrt(np.sum(bouffee ** 2))
            ri[c, i:i + larg] += bouffee
    for c in range(2):
        ri[c] /= np.sqrt(np.sum(ri[c] ** 2))
    ri = ri.astype(np.float32)
    try:
        os.makedirs(CACHE, exist_ok=True)
        np.save(chemin, ri)
    except OSError:
        pass
    _ri["ri"] = ri
    return ri


def _lire(chemin):
    with wave.open(chemin) as w:
        ch, fs = w.getnchannels(), w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if fs != FS:
        raise ValueError(f"taux {fs} inattendu")
    return x.reshape(-1, ch).mean(axis=1)  # la sono est une source mono


def _ecrire(chemin, stereo):
    data = pcm_stereo(stereo)
    with wave.open(chemin, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(FS)
        w.writeframes(data)


def appliquer(direct_wav, sortie_wav, profil="pa"):
    """direct_wav : le son deja colore "haut-parleur" (sox, sans reverb), 44,1 kHz.
    Ecrit le direct + la salle dans sortie_wav (stereo 16 bits)."""
    _ecrire(sortie_wav, traiter(_lire(direct_wav), profil))


def pcm_stereo(stereo):
    """Tableau (2, N) -> octets 16 bits entrelaces."""
    return (np.clip(stereo.T, -1, 1) * 32767).astype(np.int16).tobytes()


def traiter(sec, profil="pa"):
    """sec : son mono deja colore, flottant, 44,1 kHz. Rend le direct + la salle (2, N)."""
    reglage = PROFILS.get(profil, PROFILS["pa"])
    ri = reponse_impulsionnelle()
    total = len(sec) + ri.shape[1]
    n = 1 << (total - 1).bit_length()
    voie = _passe_bande(sec, *VOIE_REVERB, n=len(sec))
    V = np.fft.rfft(voie, n)
    humide = np.stack([np.fft.irfft(V * np.fft.rfft(ri[c], n), n)[:total] for c in range(2)])
    # dosage : energie de la reverb = humide dB sous celle du direct
    e_sec = np.sqrt(np.mean(sec ** 2)) or 1e-9
    e_hum = np.sqrt(np.mean(humide[:, : len(sec)] ** 2)) or 1e-9
    humide *= (e_sec / e_hum) * 10 ** (reglage["humide"] / 20)
    sortie = humide
    sortie[:, : len(sec)] += sec
    # niveau : RMS des passages actifs a la cible, crete <= -1 dBFS
    mono = np.abs(sortie).max(axis=0)
    actifs = sortie[:, mono > mono.max() * 10 ** (-40 / 20)]
    rms = np.sqrt(np.mean(actifs ** 2)) or 1e-9
    gain = 10 ** (reglage["cible"] / 20) / rms
    gain = min(gain, 10 ** (-1 / 20) / (mono.max() or 1e-9))
    sortie *= gain
    fondu = int(0.05 * FS)
    sortie[:, -fondu:] *= np.linspace(1, 0, fondu)
    return sortie
