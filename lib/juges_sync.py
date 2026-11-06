"""Juges de la BLU : ils ne regardent pas la fréquence, seulement la synchro.

FreeDV a un préambule. FT8 et FT4 ont leurs bits de synchro (Costas). S'ils
accrochent, c'est ce mode-là. Sinon on n'invente rien : les autres juges,
déjà en place, gardent la main, et leurs rendus restent des propositions.
"""
import audioop
import os
import re
import shutil
import subprocess
import tempfile
import wave

import numpy as np

FREEDV = ("700D", "700E", "1600")
_PAROLE_MIN = 1600          # 0,1 s de parole à 8 kHz : en dessous, pas de synchro
# La vraie preuve (2026-11-05) : les trames où freedv_rx -v annonce "sync: 1". La
# présence de son ne prouvait rien : 38 « accroche » en un jour, sur du DMR, en USB
# et en LSB à la fois, et deux machines le confirmaient, c'est déterministe.
_SYNCHRO_MIN = 5
# Mesure du 2026-11-05 : vrai 700D, 63 trames sur 65 ; un signal qui n'en est pas,
# 9 sur 1236 et 0,2 s de son. Moins de 5 % de trames et moins d'1 s : rien a ecouter.
_SYNCHRO_PART_MIN = 0.05
_PAROLE_COURTE = 16000


def _binaire(*noms):
    for nom in noms:
        chemin = shutil.which(nom)
        if chemin:
            return chemin
    base = os.path.expanduser("~/autopilot/bin")
    for nom in noms:
        chemin = os.path.join(base, nom)
        if os.path.isfile(chemin) and os.access(chemin, os.X_OK):
            return chemin
    return None


def _mono_8_ou_12(chemin, taux_voulu):
    with wave.open(chemin) as w:
        if w.getsampwidth() != 2:
            return None
        pcm = w.readframes(w.getnframes())
        if w.getnchannels() == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        taux = w.getframerate()
        if taux != taux_voulu and taux > 0:
            pcm, _ = audioop.ratecv(pcm, 2, 1, taux, taux_voulu, None)
        return pcm


def _recaler_freedv(pcm, taux=8000):
    """FreeDV attend son signal a 1,5 kHz, a quelques dizaines de Hz pres, et une BLU sans
    porteuse n'a pas de zero connu : on ramene le milieu de ce qui occupe l'audio a 1,5 kHz.
    Mesure 2026-11-05 : sans ca, aucun signal parfait ne prenait la synchro apres le banc."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if len(x) < taux:
        return pcm
    n = 4096
    trames = x[:(len(x) // n) * n].reshape(-1, n) * np.hanning(n)
    p = (np.abs(np.fft.rfft(trames, axis=1)) ** 2).mean(axis=0)
    f = np.fft.rfftfreq(n, 1 / taux)
    garde = (f >= 200) & (f <= 3500)
    fg, pg = f[garde], p[garde]
    exces = np.maximum(pg - 4 * np.median(pg), 0)
    if exces.sum() <= 0:
        return pcm
    cumul = np.cumsum(exces) / exces.sum()
    milieu = (fg[np.searchsorted(cumul, 0.05)] + fg[np.searchsorted(cumul, 0.95)]) / 2
    ecart = 1500 - milieu
    if abs(ecart) < 10:
        return pcm
    spectre = np.fft.rfft(x)
    spectre[1:] *= 2
    analytique = np.fft.irfft(spectre, len(x)) + 1j * np.fft.irfft(-1j * spectre, len(x))
    y = (analytique * np.exp(2j * np.pi * ecart * np.arange(len(x)) / taux)).real / 2
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def freedv_mesure(binaire, mode, pcm):
    """{wav, synchro, trames} si ce mode a vraiment sa synchro, sinon None."""
    pcm = _recaler_freedv(pcm)
    dossier = tempfile.mkdtemp(prefix="freedv_")
    src = os.path.join(dossier, "in.raw")
    dst = os.path.join(dossier, "out.raw")
    try:
        with open(src, "wb") as f:
            f.write(pcm)
        proc = subprocess.run([binaire, "-v", "--squelch", "0", mode, src, dst],
                              capture_output=True, timeout=20)
        journal = (proc.stdout + proc.stderr).decode(errors="replace")
        synchro = len(re.findall(r"\bsync: 1\b", journal))
        trames = len(re.findall(r"\bsync: [01]\b", journal))
        taille = os.path.getsize(dst) if os.path.isfile(dst) else 0
        if synchro < _SYNCHRO_MIN or taille < _PAROLE_MIN or (
                synchro < _SYNCHRO_PART_MIN * trames and taille < _PAROLE_COURTE):
            shutil.rmtree(dossier, ignore_errors=True)
            return None
        parole = open(dst, "rb").read()
        if audioop.rms(parole, 2) < 80:
            shutil.rmtree(dossier, ignore_errors=True)
            return None
        wav = os.path.join(dossier, f"freedv_{mode}.wav")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(parole)
        return {"wav": wav, "synchro": synchro, "trames": trames}
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(dossier, ignore_errors=True)
        return None


def _freedv_un(binaire, mode, pcm):
    m = freedv_mesure(binaire, mode, pcm)
    return m["wav"] if m else None


def voix_freedv(chemin):
    """(mode, wav 8 kHz) si un mode FreeDV accroche sa synchro, sinon None."""
    from concurrent.futures import ThreadPoolExecutor
    binaire = _binaire("freedv_rx")
    pcm = _mono_8_ou_12(chemin, 8000) if chemin and os.path.isfile(chemin) else None
    if not binaire or not pcm or len(pcm) < 8000:
        return None
    with ThreadPoolExecutor(max_workers=len(FREEDV)) as pool:
        essais = {mode: pool.submit(_freedv_un, binaire, mode, pcm) for mode in FREEDV}
    gagnant = None
    for mode in FREEDV:
        wav = essais[mode].result()
        if wav and gagnant is None:
            gagnant = (mode, wav)
        elif wav:
            shutil.rmtree(os.path.dirname(wav), ignore_errors=True)
    return gagnant


def _jt9(binaire, option, pcm, taux):
    dossier = tempfile.mkdtemp(prefix="jt9_")
    wav = os.path.join(dossier, "in.wav")
    try:
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(taux)
            w.writeframes(pcm)
        proc = subprocess.run([binaire, option, wav], capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError):
        return []
    finally:
        shutil.rmtree(dossier, ignore_errors=True)
    lignes = []
    for ligne in (proc.stdout or "").splitlines():
        morceaux = ligne.split()
        # hhmmss  snr  dt  freq  message...
        if len(morceaux) >= 5 and morceaux[0].isdigit() and len(morceaux[0]) == 6:
            texte = " ".join(morceaux[4:]).strip()
            if texte and texte not in lignes:
                lignes.append(texte)
    return lignes


def textes_faibles(chemin):
    """Textes FT8 ou FT4 si leurs bits de synchro accrochent, sinon []."""
    binaire = _binaire("jt9")
    pcm = _mono_8_ou_12(chemin, 12000) if chemin and os.path.isfile(chemin) else None
    if not binaire or not pcm or len(pcm) < 12000 * 6:
        return []
    trouvés = []
    for option, nom in (("-8", "FT8"), ("--ft4", "FT4")):
        for texte in _jt9(binaire, option, pcm, 12000):
            trouvés.append((nom, texte))
    return trouvés
