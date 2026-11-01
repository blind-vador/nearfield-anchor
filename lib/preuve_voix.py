"""Deuxieme preuve de voix, independante de RNNoise (2026-11-01) : le rythme
syllabique. La parole module son energie a 2-8 Hz (environ quatre syllabes par
seconde) ; un bourdonnement, un radar, du bruit ou une modulation mal
demodulee, non. RNNoise seul se laissait tromper par des sons tonals (90 s de
signal a 1353 MHz gardees comme "voix").

Mesure sur les prises reelles du journal le 2026-11-01 : vraie voix 0,43 a 0,64
(PMR446, voix DMR, station reperee, aviation) ; prise douteuse 0,26 ; un
spectre plat (bruit) donne environ 0,25. Sur des extraits de 3 s : 93 % des
vraies voix au-dessus de 0,35 (86 % au-dessus de 0,40), contre 10 % (5 %) du
signal douteux -- d'ou un seuil plus exigeant tant que l'extrait est court."""
import numpy as np

SEUIL = 0.35                 # mesure sur au moins DUREE_LONGUE_SEC
SEUIL_COURT = 0.40           # extrait plus court : plus de dispersion, on exige plus
DUREE_LONGUE_SEC = 4.0
DUREE_MIN_SEC = 2.5          # en dessous, le rythme ne se mesure pas : on garde le son en tampon
DUREE_REFUS_SEC = 6.0        # tant de son sans rythme de parole : ce n'en est pas, on arrete d'attendre
PRE_ECOUTE_PLAFOND_SEC = 20  # une pre-ecoute ne s'etire jamais au-dela (bourdonnement continu juge "voix")


def syllabique(pcm_s16, taux):
    """Part de l'energie de modulation de l'enveloppe entre 2 et 8 Hz, ou None
    si l'audio est trop court pour la mesurer."""
    x = np.frombuffer(pcm_s16[:len(pcm_s16) // 2 * 2], dtype=np.int16).astype(np.float32)
    trame = max(1, taux // 100)                         # enveloppe a 100 Hz
    n = len(x) // trame
    if n < DUREE_MIN_SEC * 100:
        return None
    env = np.sqrt((x[:n * trame].reshape(n, trame) ** 2).mean(axis=1))
    env -= env.mean()
    spec = np.abs(np.fft.rfft(env * np.hanning(n))) ** 2
    f = np.fft.rfftfreq(n, 1 / 100)
    return float(spec[(f >= 2) & (f <= 8)].sum() / (spec[(f >= 0.5) & (f <= 25)].sum() + 1e-9))


def est_voix(pcm_s16, taux):
    """(True/False/None, mesure) : None = pas encore assez de son pour juger."""
    s = syllabique(pcm_s16, taux)
    if s is None:
        return None, None
    duree = len(pcm_s16) / 2 / taux
    return s >= (SEUIL if duree >= DUREE_LONGUE_SEC else SEUIL_COURT), s
