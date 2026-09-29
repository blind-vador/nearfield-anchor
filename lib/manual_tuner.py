"""Ecoute manuelle en direct (mode "manuel" de la console).

rtl_fm -> (filtre optionnel) -> aplay, sur la frequence choisie. Sans
filtre, pas de silencieux : on entend le souffle, et donc la difference
quand une porteuse apparait. Touche F de la console : meme traitement
qu'en reception normale (filtre de bande vocale + silencieux RNNoise),
fourni par radio_watch.py via squelch_factory.

UNE SEULE sortie son pour toute la duree du mode manuel (2026-09-29) : la
version precedente relancait aplay a chaque changement de frequence, donc
refermait la carte son a chaque fois -- et le pilote du jack du Pi
(snd_bcm2835, "staging", noyau 6.12) echoue sur ces fermetures ("failed
to close VCHI service connection (status=-11)", par rafales pendant
l'usage du mode manuel), jusqu'au plantage complet du pilote (12 h 17 le
2026-09-29, apres un test d'endurance a relances multiples : plus aucun
son, redemarrage force). Desormais seul rtl_fm est relance ; aplay reste
ouvert a 48 kHz (le reste est reechantillonne ici) et n'est ferme qu'une
fois, en douceur (fin de flux), a la sortie du mode manuel.

Sortie sur ALSA "default" (mixage dmix) : la console annonce les
frequences PENDANT la diffusion.

Tient le verrou audio partage tant que l'ecoute dure.
"""
import audioop
import signal
import subprocess
import threading
import time

import audio_lock

SETTLE_SEC = 0.5        # frequence stable depuis ce delai avant de s'y caler
STOP_GRACE_SEC = 1.5    # SIGTERM, puis SIGKILL si toujours vivant
OUT_RATE = 48000
NARROW_RATE = 22050
CHUNK_MS = 200          # meme granularite que l'analyse bruit/signal en reception

# Memes reglages de demodulation que stream_until_quiet() dans radio_watch.py.
WFM_ARGS = ["-M", "wbfm", "-s", "200000", "-r", str(OUT_RATE), "-l", "0", "-E", "dc", "-E", "deemp"]


def _stop_process(proc):
    if proc is None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            return
        deadline = time.time() + STOP_GRACE_SEC
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
    if proc.poll() is None:
        print(f"[manual_tuner] ATTENTION : processus {proc.pid} toujours vivant apres SIGKILL")


class ManualStream:
    def __init__(self, squelch_factory=None):
        """squelch_factory(sample_rate) -> objet avec process(chunk) -> bytes
        et close() ; fourni par radio_watch.py (filtre + silencieux)."""
        self._squelch_factory = squelch_factory
        self._rtl = None
        self._aplay = None
        self._key = None
        self._lock = None
        self._generation = 0
        self._write_guard = threading.Lock()

    def tune(self, freq_hz, mode, filtered=False):
        """Ecoute freq_hz ; ne fait rien si rien n'a change."""
        filtered = bool(filtered and self._squelch_factory and mode != "wfm")
        key = (int(freq_hz), mode, filtered)
        if key == self._key and self._rtl is not None and self._rtl.poll() is None:
            return
        self._stop_receiver()
        if self._lock is None:
            self._lock = audio_lock.acquire()
        if self._aplay is None or self._aplay.poll() is not None:
            self._aplay = subprocess.Popen(
                ["aplay", "-q", "-D", "default", "-r", str(OUT_RATE), "-f", "S16_LE", "-c", "1", "-B", "500000"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if mode == "wfm":
            args, in_rate = WFM_ARGS, OUT_RATE
        else:
            args, in_rate = ["-M", mode, "-s", str(NARROW_RATE), "-l", "0", "-E", "dc"], NARROW_RATE
        self._rtl = subprocess.Popen(["rtl_fm", "-f", str(int(freq_hz))] + args,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._generation += 1
        squelch = self._squelch_factory(in_rate) if filtered else None
        threading.Thread(target=self._relay, args=(self._rtl, in_rate, squelch, self._generation),
                         daemon=True).start()
        self._key = key
        print(f"[manual_tuner] ecoute manuelle {freq_hz / 1e6:.4f} MHz ({mode}, filtre {'oui' if filtered else 'non'})")

    def _relay(self, rtl, in_rate, squelch, generation):
        chunk_bytes = int(in_rate * 2 * CHUNK_MS / 1000)
        resample_state = None
        try:
            while True:
                chunk = rtl.stdout.read(chunk_bytes)
                if not chunk:
                    break
                if squelch is not None:
                    chunk = squelch.process(chunk)
                if in_rate != OUT_RATE and chunk:
                    chunk, resample_state = audioop.ratecv(chunk, 2, 1, in_rate, OUT_RATE, resample_state)
                with self._write_guard:
                    if generation != self._generation or self._aplay is None:
                        break  # un nouveau recepteur a pris le relais
                    try:
                        self._aplay.stdin.write(chunk)
                    except (BrokenPipeError, ValueError, OSError):
                        break
        finally:
            if squelch is not None:
                squelch.close()

    def _stop_receiver(self):
        with self._write_guard:
            self._generation += 1  # le fil de relais en cours s'arrete a sa prochaine ecriture
        _stop_process(self._rtl)
        self._rtl = None
        self._key = None

    def stop(self):
        """Arrete l'ecoute et rend le verrou audio (sans effet si inactif)."""
        was_active = self._rtl is not None or self._aplay is not None
        self._stop_receiver()
        if self._aplay is not None:
            with self._write_guard:
                try:
                    self._aplay.stdin.close()  # fin de flux : aplay se vide et ferme la carte UNE fois
                except OSError:
                    pass
            deadline = time.time() + 3
            while time.time() < deadline and self._aplay.poll() is None:
                time.sleep(0.05)
            _stop_process(self._aplay)
            self._aplay = None
        if self._lock is not None:
            audio_lock.release(self._lock)
            self._lock = None
        if was_active:
            print("[manual_tuner] fin de l'ecoute manuelle")
