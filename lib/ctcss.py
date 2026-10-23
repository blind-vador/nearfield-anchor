"""Tonalite subaudible CTCSS d'une captation FM (2026-10-23) : un relais
radioamateur annonce souvent sa tonalite d'acces sur l'APRS ("T123") ; si on
la retrouve dans le son capte sur sa frequence, c'est bien lui qu'on entend.

Le son brut de rtl_fm (avant le filtre vocal 300-3000 Hz, qui la supprime)
contient la tonalite, continue, faible, entre 67 et 254 Hz. Detection : FFT
fine (resolution ~0,1 Hz sur 10 s) de la bande 60-260 Hz ; le pic doit
depasser nettement le reste de la bande (MARGE_DB) et tomber a moins de
TOLERANCE_HZ d'une tonalite normalisee (EIA/CEPT).
"""
import numpy as np

TONALITES = [67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2,
             110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5,
             167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5, 206.5, 210.7,
             218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1]
TOLERANCE_HZ = 1.2
MARGE_DB = 12.0
DUREE_MAX_SEC = 12
DUREE_MIN_SEC = 2


def detecter(pcm_s16le, taux):
    """Tonalite CTCSS (Hz, valeur normalisee) presente dans ce son mono, ou None."""
    x = np.frombuffer(pcm_s16le[: len(pcm_s16le) - len(pcm_s16le) % 2], dtype=np.int16).astype(np.float32)
    if len(x) < taux * DUREE_MIN_SEC:
        return None
    x = x[-int(taux * DUREE_MAX_SEC):]
    x = x - x.mean()
    spectre = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1 / taux)
    bande = (f >= 60) & (f <= 260)
    if not bande.any():
        return None
    s, fb = spectre[bande], f[bande]
    i = int(np.argmax(s))
    fond = np.median(s) or 1e-9
    if 20 * np.log10(s[i] / fond) < MARGE_DB:
        return None
    proche = min(TONALITES, key=lambda t: abs(t - fb[i]))
    return proche if abs(proche - fb[i]) <= TOLERANCE_HZ else None
