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


def freedv_mesure(binaire, mode, pcm):
    """{wav, synchro, trames} si ce mode a vraiment sa synchro, sinon None."""
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
