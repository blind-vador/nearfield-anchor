"""Deposer un son pour les decodeurs (tickets partages) depuis n'importe quelle
ecoute : chaque decodeur repasse derriere celui qui a ecoute (2026-11-05).

Le dossier brut_dsd est lu par juges_watch, qui en fait un ticket pour toutes les
machines, Pi compris. Les decodeurs attendent 48 kHz mono."""
import json
import os
import threading
import time
import wave

import numpy as np

import chemins

BRUT_DIR = chemins.base("brut_dsd")
TAUX = 48_000
SECONDES_MIN = 1.0
SECONDES_MAX = 300


def _vers_48k(pcm, taux, format_):
    a = np.frombuffer(pcm[:len(pcm) // 4 * 4] if format_ == "f32le" else pcm[:len(pcm) // 2 * 2],
                      dtype=np.float32 if format_ == "f32le" else np.int16).astype(np.float32)
    if format_ != "f32le":
        a = a / 32768
    if taux != TAUX and len(a):
        a = np.interp(np.arange(len(a) * TAUX // taux) * taux / TAUX, np.arange(len(a)), a)
    return np.clip(a * 32767, -32768, 32767).astype("<i2")


def deposer(pcm, taux, freq_hz, band, format_="s16le", etiquette="inconnu", origine=None):
    """Rend le chemin du wav, ou None si le son est trop court ou le disque refuse."""
    a = _vers_48k(pcm, taux, format_)[:SECONDES_MAX * TAUX]
    if len(a) < SECONDES_MIN * TAUX:
        return None
    try:
        os.makedirs(BRUT_DIR, exist_ok=True)
        chemin = os.path.join(BRUT_DIR, f"{int(freq_hz)}_flux_{time.strftime('%Y%m%d_%H%M%S')}.wav")
        with wave.open(chemin + ".tmp", "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TAUX)
            w.writeframes(a.tobytes())
        with open(chemin[:-4] + ".json", "w", encoding="utf-8") as f:
            json.dump({"freq_hz": int(freq_hz), "mode": "flux", "band": band, "t": time.time(), "etiquette": etiquette,
                       "origine": origine, "direct": {"valide": False}}, f, ensure_ascii=False)
        os.replace(chemin + ".tmp", chemin)
        return chemin
    except OSError as exc:
        print(f"[flux] {freq_hz} : {exc}", flush=True)
        return None


# --- Les veilles a son (APRS, pagers, marine) : le son du decodeur specialise ---
# rtl_fm alimentait leur decodeur par un tuyau direct, et rien n'en restait. On copie
# au passage ; a la fin, s'il y a eu une porteuse, tous les decodeurs repassent :
# une veille APRS peut croiser autre chose que de l'APRS.

GRAIN_SEC = 0.1
PORTEUSE_CONTINUE_MAX = 3_000     # ecart d'un echantillon au suivant : le souffle en donne bien plus
PORTEUSE_MIN_SEC = 0.3


def porteuse_presente(pcm, taux):
    """La FM sans porteuse est un souffle a pleine echelle ; la porteuse le fait taire."""
    a = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
    n = int(taux * GRAIN_SEC)
    if not n or len(a) < n:
        return False
    d = np.diff(a[:len(a) // n * n].reshape(-1, n), axis=1)
    agitation = np.sqrt((d ** 2).mean(axis=1))
    haut = float(np.percentile(agitation, 90))
    if haut < PORTEUSE_CONTINUE_MAX:      # jamais de souffle de toute la fenetre
        return True
    return float((agitation < haut / 2).sum()) * GRAIN_SEC >= PORTEUSE_MIN_SEC    # le souffle s'est tu


class Copie:
    """Remplace stdin=rtl.stdout : relaie vers le decodeur et garde le son."""

    def __init__(self, source, destination, taux):
        self.taux = taux
        self.morceaux = []
        self.total = 0
        self._source, self._destination = source, destination
        self._fil = threading.Thread(target=self._relayer, daemon=True)
        self._fil.start()

    def _relayer(self):
        try:
            while True:
                bloc = self._source.read(8192)
                if not bloc:
                    break
                if self.total < SECONDES_MAX * self.taux * 2:
                    self.morceaux.append(bloc)
                    self.total += len(bloc)
                if self._destination is not None:
                    try:
                        self._destination.write(bloc)
                        self._destination.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        self._destination = None       # decodeur arrete : on finit de vider rtl_fm
        except (OSError, ValueError):
            pass
        finally:
            if self._destination is not None:
                try:
                    self._destination.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

    def deposer(self, freq_hz, band, origine):
        """Fin de fenetre. Rend le chemin deposee, ou None sans porteuse."""
        self._fil.join(timeout=3)
        pcm = b"".join(self.morceaux)
        self.morceaux = []
        if not porteuse_presente(pcm, self.taux):
            return None
        return deposer(pcm, self.taux, freq_hz, band, origine=origine)
