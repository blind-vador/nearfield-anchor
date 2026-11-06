"""Le gain d'une ecoute se mesure, il ne se fixe pas (2026-11-05).

Le balayage (rtl_power) et l'ancien chemin local ecoutent en gain automatique ;
le banc ecoutait a 32 dB fixes. Mesure sur l'antenne le 2026-11-05 : a 32 dB sur
446 MHz, le plancher reste a 2 dB du bruit propre du convertisseur, l'IQ ne bouge
que de 1 pas sur 255. On voyait une station au balayage et on l'ecoutait sourd.

On prend le gain le plus fort qui ne sature pas, mesure a l'instant sur cette
frequence : un emetteur voisin puissant redescend le gain tout seul."""
import os
import subprocess
import tempfile

import numpy as np

TAUX = 256_000
ESSAIS = (49.6, 42.1, 36.4, 29.7, 22.9, 14.4)   # gains du tuner R820T, du plus fort au plus faible
ECHANTILLONS = TAUX // 6
RMS_MAX = 30.0                                  # pas sur 127 : au-dela, le dongle n'a plus de marge
CRETE = 120.0
PART_CRETE_MAX = 0.001


def mesurer(freq_hz, gain):
    """(ecart-type, part d'echantillons ecretes) d'un instant d'IQ, ou None."""
    fd, chemin = tempfile.mkstemp(suffix=".iq")
    os.close(fd)
    try:
        subprocess.run(["rtl_sdr", "-f", str(int(freq_hz + TAUX // 4)), "-s", str(TAUX), "-g", str(gain),
                        "-n", str(ECHANTILLONS), chemin], capture_output=True, timeout=6)
        brut = np.fromfile(chemin, dtype=np.uint8).astype(np.float32)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.remove(chemin)
        except OSError:
            pass
    brut = brut[len(brut) // 4:]                # debut du flux : reglage du tuner
    if len(brut) < 1000:
        return None
    centre = brut - brut.mean()
    return float(centre.std()), float((np.abs(centre) >= CRETE).mean())


def choisir(freq_hz, repli):
    """Gain (dB) pour ecouter freq_hz ; repli si le dongle ne repond pas."""
    dernier = None
    for gain in ESSAIS:
        m = mesurer(freq_hz, gain)
        if m is None:
            break
        dernier = gain
        if m[0] <= RMS_MAX and m[1] <= PART_CRETE_MAX:
            print(f"[gain] {freq_hz / 1e6:.4f} MHz : {gain} dB (ecart-type {m[0]:.1f})", flush=True)
            return gain
    return dernier if dernier is not None else repli
