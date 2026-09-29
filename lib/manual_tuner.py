"""Ecoute manuelle en direct (mode "manuel" de la console).

rtl_fm -> aplay sur la frequence choisie, SANS silencieux : on entend le
souffle, et donc la difference quand une porteuse apparait (c'est ce qui
permet de s'accorder a l'oreille).

Sortie sur le peripherique ALSA "default" (mixage dmix) et PAS sur
plughw comme le reste du scanner : la console doit pouvoir annoncer les
frequences PENDANT la diffusion (verifie le 2026-09-29 : deux lectures
simultanees reussissent sur "default", pas sur plughw).

Arret fiable (bug du 2026-09-29) : la premiere version passait par
"sh -c 'rtl_fm | aplay'" et n'attendait que le shell. En changeant vite de
frequence, un rtl_fm encore en demarrage ignorait SIGTERM et restait
accroche au dongle : les rtl_fm suivants ne pouvaient plus l'ouvrir
(usb_claim_interface error -6), et le scanner non plus apres "decroche".
Desormais rtl_fm et aplay sont des enfants directs, arretes un par un, et
on ATTEND qu'ils soient morts (SIGKILL au besoin) avant de relancer quoi
que ce soit. (Pas de groupe de processus commun : setpgid vers le groupe
d'une autre session est refuse, EPERM.) radio_watch.py n'accorde en plus qu'apres
SETTLE_SEC sans changement (defilement rapide aux fleches = un seul
redemarrage, sur la frequence finale).

Tient le verrou audio partage tant que l'ecoute dure : les annonces des
autres veilles attendent leur tour.
"""
import signal
import subprocess
import time

import audio_lock

SETTLE_SEC = 0.5        # frequence stable depuis ce delai avant de s'y caler
STOP_GRACE_SEC = 1.5    # SIGTERM, puis SIGKILL si toujours vivant

# Memes reglages de demodulation que stream_until_quiet() dans radio_watch.py.
WFM_ARGS = ["-M", "wbfm", "-s", "200000", "-r", "48000", "-l", "0", "-E", "dc", "-E", "deemp"]
WFM_RATE = 48000
NARROW_RATE = 22050


class ManualStream:
    def __init__(self):
        self._rtl = None
        self._aplay = None
        self._key = None
        self._lock = None

    def _alive(self):
        return self._rtl is not None and self._rtl.poll() is None

    def tune(self, freq_hz, mode):
        """Ecoute freq_hz ; ne fait rien si c'est deja la frequence en cours."""
        key = (int(freq_hz), mode)
        if key == self._key and self._alive():
            return
        self._kill()
        if self._lock is None:
            self._lock = audio_lock.acquire()
        if mode == "wfm":
            args, rate = WFM_ARGS, WFM_RATE
        else:
            args, rate = ["-M", mode, "-s", str(NARROW_RATE), "-l", "0", "-E", "dc"], NARROW_RATE
        self._rtl = subprocess.Popen(["rtl_fm", "-f", str(int(freq_hz))] + args,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._aplay = subprocess.Popen(
            ["aplay", "-q", "-D", "default", "-r", str(rate), "-f", "S16_LE", "-c", "1", "-B", "500000"],
            stdin=self._rtl.stdout, stderr=subprocess.DEVNULL)
        self._rtl.stdout.close()  # aplay est desormais le seul lecteur du tube
        self._key = key
        print(f"[manual_tuner] ecoute manuelle {freq_hz / 1e6:.4f} MHz ({mode})")

    def _kill(self):
        procs = [p for p in (self._aplay, self._rtl) if p is not None]
        if procs:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for p in procs:
                    if p.poll() is None:
                        try:
                            p.send_signal(sig)
                        except ProcessLookupError:
                            pass
                deadline = time.time() + STOP_GRACE_SEC
                while time.time() < deadline and any(p.poll() is None for p in procs):
                    time.sleep(0.05)
                if all(p.poll() is not None for p in procs):
                    break
            for p in procs:
                if p.poll() is None:
                    print(f"[manual_tuner] ATTENTION : processus {p.pid} toujours vivant apres SIGKILL")
        self._rtl = self._aplay = None
        self._key = None

    def stop(self):
        """Arrete l'ecoute et rend le verrou audio (sans effet si inactif)."""
        was_active = self._rtl is not None
        self._kill()
        if self._lock is not None:
            audio_lock.release(self._lock)
            self._lock = None
        if was_active:
            print("[manual_tuner] fin de l'ecoute manuelle")
