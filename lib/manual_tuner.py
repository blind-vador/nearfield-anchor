"""Ecoute manuelle en direct (mode "manuel" de la console).

Un pipeline rtl_fm | aplay sur la frequence choisie, SANS silencieux :
on entend le souffle, et donc la difference quand une porteuse apparait
(c'est ce qui permet de s'accorder a l'oreille). Relance a chaque
changement de frequence (~1 s de coupure, le temps que rtl_fm se cale).

Sortie sur le peripherique ALSA "default" (mixage dmix) et PAS sur
plughw comme le reste du scanner : la console doit pouvoir annoncer les
frequences PENDANT la diffusion (verifie le 2026-09-29 : deux lectures
simultanees reussissent sur "default", pas sur plughw).

Tient le verrou audio partage tant que l'ecoute dure : les annonces des
autres veilles attendent leur tour.
"""
import os
import signal
import subprocess

import audio_lock

# Memes reglages de demodulation que stream_until_quiet() dans radio_watch.py.
WFM_ARGS = ["-M", "wbfm", "-s", "200000", "-r", "48000", "-l", "0", "-E", "dc", "-E", "deemp"]
WFM_RATE = 48000
NARROW_RATE = 22050


class ManualStream:
    def __init__(self):
        self._proc = None
        self._key = None
        self._lock = None

    def tune(self, freq_hz, mode):
        """Ecoute freq_hz ; ne fait rien si c'est deja la frequence en cours."""
        key = (int(freq_hz), mode)
        if key == self._key and self._proc is not None and self._proc.poll() is None:
            return
        self._kill()
        if self._lock is None:
            self._lock = audio_lock.acquire()
        if mode == "wfm":
            args, rate = WFM_ARGS, WFM_RATE
        else:
            args, rate = ["-M", mode, "-s", str(NARROW_RATE), "-l", "0", "-E", "dc"], NARROW_RATE
        rtl = " ".join(["rtl_fm", "-f", str(int(freq_hz))] + args)
        play = f"aplay -q -D default -r {rate} -f S16_LE -c 1 -B 500000"
        # Groupe de processus dedie : tuer rtl_fm ET aplay d'un coup.
        self._proc = subprocess.Popen(f"{rtl} 2>/dev/null | {play} 2>/dev/null", shell=True,
                                      start_new_session=True)
        self._key = key
        print(f"[manual_tuner] ecoute manuelle {freq_hz / 1e6:.4f} MHz ({mode})")

    def _kill(self):
        if self._proc is None:
            return
        try:
            os.killpg(self._proc.pid, signal.SIGTERM)
            self._proc.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._proc = None
        self._key = None

    def stop(self):
        """Arrete l'ecoute et rend le verrou audio (sans effet si inactif)."""
        was_active = self._proc is not None
        self._kill()
        if self._lock is not None:
            audio_lock.release(self._lock)
            self._lock = None
        if was_active:
            print("[manual_tuner] fin de l'ecoute manuelle")
