"""Estimation aveugle du debit symbole (2026-11-11, plan phase 6, audit point 8).

Avant les decodeurs, un regard sur le son du discriminateur FM (celui que dsd-fme
recoit, 48 kHz) : une modulation numerique a symboles (4FSK, GMSK, FFSK...) laisse
une raie spectrale au rythme des symboles dans l'energie de ses transitions
(|dx/dt|^2, detection classique de la cadence symbole, cf. Gardner 1986 ;
Dobre et al., IET Comm. 2007 pour la classification aveugle). On cherche cette raie
aux debits d'usage :
  2 400 bd   NXDN48, dPMR ; 4 800 bd   DMR, D-Star, YSF, P25 ph.1, M17, NXDN96, X2-TDMA ;
  6 000 bd   P25 phase 2 ; 9 600 bd   ProVoice, EDACS ; 1 200 bd   AFSK (APRS, POCSAG 1200).
Le resultat ORDONNE les juges (les compatibles passent devant), il n'en exclut aucun :
un son ambigu ou analogique ne donne rien et l'ordre habituel reste.
"""
import wave

import numpy as np

DEBITS = (1200, 2400, 4800, 6000, 9600)
JUGES_DU_DEBIT = {
    2400: ("-fi", "-fm"),
    4800: ("-fs", "-fd", "-fy", "-f1", "-fz", "-fn", "-fx"),
    6000: ("-f2",),
    9600: ("-fp", "-fh", "-fe"),
}
RAPPORT_MIN = 6.0        # raie / niveau median autour : en dessous, on ne dit rien
SEGMENT = 1 << 14        # ~0,34 s a 48 kHz
DUREE_MAX_S = 20


def _lire_wav(chemin):
    with wave.open(chemin, "rb") as w:
        taux, n, largeur, canaux = w.getframerate(), w.getnframes(), w.getsampwidth(), w.getnchannels()
        brut = w.readframes(min(n, int(taux * DUREE_MAX_S)))
    if largeur != 2:
        return None, taux
    x = np.frombuffer(brut, dtype="<i2").astype(np.float32)
    if canaux > 1:
        x = x.reshape(-1, canaux).mean(axis=1)
    return x, taux


def _spectre(e):
    fen = np.hanning(SEGMENT)
    spectre = np.zeros(SEGMENT // 2 + 1)
    n = 0
    for debut in range(0, len(e) - SEGMENT, SEGMENT // 2):
        seg = e[debut:debut + SEGMENT]
        if not np.any(seg):
            continue
        spectre += np.abs(np.fft.rfft((seg - seg.mean()) * fen))
        n += 1
    return spectre if n else None


def _rapport(x, taux, debit):
    """Force de la raie a `debit` dans l'energie des transitions, le son etant
    d'abord lisse a la largeur de bande de ce debit (le souffle haut ne la noie pas)."""
    largeur = max(1, int(round(taux / (2.0 * debit))))
    lisse = np.convolve(x, np.ones(largeur) / largeur, mode="same")
    spectre = _spectre(np.diff(lisse) ** 2)
    if spectre is None:
        return 0.0
    i = int(round(debit / (taux / SEGMENT)))
    raie = spectre[max(i - 2, 0):i + 3].max()
    autour = np.concatenate([spectre[max(i - 60, 0):max(i - 6, 0)], spectre[i + 6:i + 60]])
    return float(raie / (np.median(autour) + 1e-9)) if len(autour) else 0.0


def estimer(x, taux):
    """(debit, rapport) du rythme symbole le plus net, ou (None, rapport)."""
    if x is None or len(x) < SEGMENT * 2 or taux < 2 * max(DEBITS):
        return None, 0.0
    x = x - np.mean(x)
    rapports = {d: _rapport(x, taux, d) for d in DEBITS}
    meilleur = max(rapports, key=rapports.get)
    # une raie a 2 400 se repete a 4 800 (harmonique) : le debit le plus bas qui
    # tient presque autant l'emporte
    for d in sorted(rapports):
        if d < meilleur and meilleur % d == 0 and rapports[d] >= 0.6 * rapports[meilleur]:
            meilleur = d
            break
    r = rapports[meilleur]
    return (meilleur, r) if r >= RAPPORT_MIN else (None, r)


def estimer_fichier(chemin):
    try:
        return estimer(*_lire_wav(chemin))
    except (OSError, EOFError, ValueError, wave.Error) as exc:
        print(f"[debit_symbole] {chemin} : {exc!r}")
        return None, 0.0


def juges_compatibles(debit):
    return JUGES_DU_DEBIT.get(debit, ())
