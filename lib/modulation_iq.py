"""Preuve de la modulation sur le signal radio lui-meme (2026-11-01).

Le second regard essayait toutes les modulations a l'aveugle : une emission
AM (aviation) passait en FM etroite, sortait deformee, et le juge de voix la
prenait pour de la voix -- des minutes de bruit mal demodule au journal. On
regarde maintenant l'IQ brut du dongle (une seconde) avant de choisir, et on ne
retient qu'une modulation dont le signal porte la signature physique :
  - enveloppe quasi constante et frequence instantanee qui bouge -> FM (et
    modes numeriques FSK, decodes en FM) ;
  - raie de porteuse au centre et frequence instantanee fixe -> AM (une AM peu
    modulee a une enveloppe presque aussi stable qu'une FM : c'est la frequence
    instantanee qui les separe) ;
  - spectre d'un seul cote, sans porteuse -> BLU haute ou basse ;
  - bande occupee tres large -> FM large.
Aucune regle par frequence (doctrine) : seulement le signal. Trop faible ou
ambigu -> "indecis", et l'appelant ne s'autorise que sa premiere hypothese."""
import os
import subprocess
import tempfile

import numpy as np

TAUX = 240_000                # plage native du dongle, assez large pour la FM large
DECALAGE_HZ = 60_000          # on accorde a cote de la cible : jamais sur le pic DC du dongle
DUREE_SEC = 0.5
NFFT = 4096
ENVELOPPE_FM_MAX = 0.15       # ecart-type / moyenne de l'enveloppe (bruit pur ~0,52 ; DMR reel 0,07-0,10 ;
                              # GSM 0,25 ; un signal douteux a 1353 MHz passait a 0,20)
PORTEUSE_MIN_DB = 10.0        # raie centrale au-dessus des bandes laterales
ASYMETRIE_BLU_DB = 8.0
NIVEAU_BLU_MIN_DB = 10.0      # la bande occupee doit depasser le bruit voisin (une raie parasite seule ne suffit pas)
DEVIATION_FM_MIN_HZ = 400     # ecart-type de la frequence instantanee : FM au-dessus, porteuse AM en dessous
DEVIATION_WFM_MIN_HZ = 15_000  # FM large (diffusion : +-75 kHz) ; le TETRA voisin donne ~9 kHz


def capturer(freq_hz, gain=None):
    """IQ complexe (TAUX, cible ramenee a 0 Hz) ou None."""
    fd, chemin = tempfile.mkstemp(suffix=".iq")
    os.close(fd)
    try:
        cmd = ["rtl_sdr", "-f", str(int(freq_hz + DECALAGE_HZ)), "-s", str(TAUX), "-n", str(int(TAUX * DUREE_SEC))]
        if gain is not None:
            cmd += ["-g", str(gain)]
        subprocess.run(cmd + [chemin], capture_output=True, timeout=15)
        brut = np.fromfile(chemin, dtype=np.uint8)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.remove(chemin)
        except OSError:
            pass
    if len(brut) < TAUX:
        return None
    x = (brut[0::2].astype(np.float32) - 127.5) + 1j * (brut[1::2].astype(np.float32) - 127.5)
    x = x[TAUX // 20:]                                   # debut du flux : reglage du tuner
    x -= x.mean()                                        # pic DC (a +DECALAGE de la cible)
    return x * np.exp(2j * np.pi * DECALAGE_HZ * np.arange(len(x)) / TAUX)


def _psd(x, taux=TAUX):
    blocs = len(x) // NFFT
    f = np.fft.fftshift(np.fft.fftfreq(NFFT, 1 / taux))
    if not blocs:
        return f, None
    w = np.hanning(NFFT)
    p = np.mean([np.abs(np.fft.fft(x[i * NFFT:(i + 1) * NFFT] * w)) ** 2 for i in range(blocs)], axis=0)
    return f, np.fft.fftshift(p)


def _passe_bas(x, largeur_hz, taux=TAUX):
    X = np.fft.fft(x)
    f = np.fft.fftfreq(len(x), 1 / taux)
    X[np.abs(f) > largeur_hz] = 0
    return np.fft.ifft(X)


def mesurer(x, taux=TAUX):
    """Signatures du signal (dict), sans decision. taux : la capture du Pi
    (240 kS/s) ou le canal deja ramene a 32 kHz par le banc de la compagne
    (bridge/banc_demod) -- memes regles, bandes de reference adaptees."""
    f, p = _psd(x, taux)
    if p is None:
        return None
    db = 10 * np.log10(p + 1e-12)
    loin = np.abs(f) > min(90_000, 0.8 * taux / 2)
    plancher = np.median(db[loin]) if np.any(loin) else np.median(db)
    dans = np.abs(f) <= 12_500
    snr = float(np.max(db[dans]) - plancher)
    occupe = f[db > plancher + 10]
    largeur = float(occupe.max() - occupe.min()) if len(occupe) else 0.0
    centre = np.abs(f) <= 300
    # porteuse : raie centrale face a la bande laterale la plus forte (en BLU,
    # l'autre cote est vide : il ne doit pas faire croire a une porteuse)
    cote_haut = p[(f > 1_000) & (f <= 6_000)].mean()
    cote_bas = p[(f < -1_000) & (f >= -6_000)].mean()
    porteuse = float(np.max(db[centre]) - 10 * np.log10(max(cote_haut, cote_bas) + 1e-12))
    anneau = ((np.abs(f) >= 20_000) & (np.abs(f) <= 40_000)) if taux > 100_000 else \
        ((np.abs(f) >= 0.6 * taux / 2) & (np.abs(f) <= 0.9 * taux / 2))
    voisin = np.median(p[anneau])
    niveau = float(10 * np.log10(p[np.abs(f) <= 4_000].mean() / (voisin + 1e-12)))
    haut = p[(f > 300) & (f <= 4_000)].sum()
    bas = p[(f < -300) & (f >= -4_000)].sum()
    asym = float(10 * np.log10((haut + 1e-12) / (bas + 1e-12)))
    nb = _passe_bas(x, 100_000 if largeur > 60_000 else 12_500, taux)
    env = np.abs(nb)
    actif = env > np.percentile(env, 30)                # ignore les creux (fin d'emission)
    cv = float(np.std(env[actif]) / (np.mean(env[actif]) + 1e-9))
    inst = np.angle(nb[1:] * np.conj(nb[:-1])) * taux / (2 * np.pi)
    dev = float(np.std(inst[actif[1:]]))
    return {"snr_db": round(snr, 1), "largeur_hz": round(largeur), "porteuse_db": round(porteuse, 1),
            "asymetrie_db": round(asym, 1), "enveloppe_cv": round(cv, 3), "deviation_hz": round(dev),
            "niveau_db": round(niveau, 1)}


def decider(m):
    """(modulations prouvees par ordre de preference, raison). [] = indecis.
    Chaque regle porte sa propre preuve (mesures sur le terrain 2026-11-01 : le
    plancher de bruit d'une capture est fausse par le filtre du dongle, un
    rapport signal/bruit global ne prouve rien). Une enveloppe stable suffit a
    signer un vrai signal : le bruit seul varie d'environ 50 %."""
    if not m:
        return [], "pas de mesure"
    cv, dev = m["enveloppe_cv"], m["deviation_hz"]
    if cv <= ENVELOPPE_FM_MAX and dev >= DEVIATION_WFM_MIN_HZ:
        return ["wfm"], f"enveloppe constante (variation {cv:.2f}), deviation large ({dev / 1000:.0f} kHz)"
    if cv <= ENVELOPPE_FM_MAX and dev >= DEVIATION_FM_MIN_HZ:
        return ["nfm"], f"enveloppe constante (variation {cv:.2f}), frequence qui bouge ({dev} Hz) : FM ou numerique FSK"
    if m["porteuse_db"] >= PORTEUSE_MIN_DB and dev < DEVIATION_FM_MIN_HZ:
        if cv <= 0.05:
            # porteuse nue : AM ou FM sans modulation, rien ne les distingue
            return ["am", "nfm"], f"porteuse seule ({m['porteuse_db']:.0f} dB), sans modulation visible"
        return ["am"], f"porteuse {m['porteuse_db']:.0f} dB au-dessus des bandes laterales, frequence fixe ({dev} Hz)"
    if (abs(m["asymetrie_db"]) >= ASYMETRIE_BLU_DB and m["porteuse_db"] < PORTEUSE_MIN_DB
            and m["niveau_db"] >= NIVEAU_BLU_MIN_DB):
        cote = "usb" if m["asymetrie_db"] > 0 else "lsb"
        return [cote], f"spectre d'un seul cote ({m['asymetrie_db']:+.0f} dB), sans porteuse"
    return [], (f"aucune signature nette (enveloppe {cv:.2f}, porteuse {m['porteuse_db']:.0f} dB, "
                f"asymetrie {m['asymetrie_db']:+.0f} dB)")


def prouver(freq_hz, gain=None):
    """(modulations prouvees, raison, mesures) pour freq_hz."""
    x = capturer(freq_hz, gain)
    if x is None:
        return [], "capture IQ impossible", None
    m = mesurer(x)
    mods, raison = decider(m)
    return mods, raison, m
