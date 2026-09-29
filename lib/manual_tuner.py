"""Ecoute manuelle en direct (mode "manuel" de la console).

Deux recepteurs possibles sur la frequence choisie :
- sans filtre : rtl_fm, SANS silencieux -- on entend le souffle, donc la
  difference quand une porteuse apparait (accord a l'oreille) ;
- touche F (AM/NFM) : RTLSDR-Airband sur ce seul canal, avec les reglages
  des fenetres de airband_bridge.py (gain, volume, silencieux SNR) + filtre
  de bande vocale. C'est EXACTEMENT la chaine du verrouillage, qui restitue
  un talkie tout proche "fort et clair". Historique (2026-09-29) : un
  silencieux maison sur l'audio de rtl_fm (RNNoise + souffle aigu) laissait
  passer tonalites et DTMF mais jamais la voix de ce talkie (audio sature,
  RNNoise "voix 0 %", souffle aigu MONTANT a 0,85 pendant la parole).
  RTLSDR-Airband decide sur le signal radio lui-meme, avant demodulation.
  En WFM (radio FM), F est sans effet : pas de silencieux sur de la musique.

UNE SEULE sortie son pour toute la duree du mode manuel : seul le recepteur
est relance a chaque frequence ; aplay reste ouvert a 48 kHz (le reste est
reechantillonne ici) et n'est ferme qu'une fois, en douceur. La version
precedente refermait la carte a chaque frequence et faisait planter le
pilote du jack integre (voir ROADMAP).

Sortie sur ALSA "default" (mixeur de l'adaptateur USB) : la console
annonce les frequences PENDANT la diffusion. Tient le verrou audio partage
tant que l'ecoute dure.
"""
import audioop
import signal
import socket
import subprocess
import threading
import time

import audio_lock

SETTLE_SEC = 0.5        # frequence stable depuis ce delai avant de s'y caler
STOP_GRACE_SEC = 1.5    # SIGTERM, puis SIGKILL si toujours vivant
OUT_RATE = 48000
NARROW_RATE = 22050
CHUNK_MS = 200

# Memes reglages de demodulation que stream_until_quiet() dans radio_watch.py.
WFM_ARGS = ["-M", "wbfm", "-s", "200000", "-r", str(OUT_RATE), "-l", "0", "-E", "dc", "-E", "deemp"]

AIRBAND_UDP_PORT = 9290            # hors de la plage des fenetres (9100-9204)
AIRBAND_CONFIG = "/tmp/airband_manuel.conf"
# Centre du tuner : jamais sur le canal (pic DC). Celui de la fenetre airband
# qui contient la frequence quand il y en a une (eprouve en verrouillage),
# sinon un decalage fixe. NB : "[R82XX] PLL not locked!" s'affiche au
# demarrage de la cle quel que soit le centre -- ce n'est PAS une panne.
AIRBAND_CENTER_OFFSET_HZ = 300_000
AIRBAND_RATE = 16000                # sortie UDP de RTLSDR-Airband : PCM flottant 32 bits mono


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


def _airband_window(freq_hz, mode):
    """Reglages de la fenetre airband_bridge qui contient cette frequence
    (centre compris), sinon de la premiere fenetre du meme mode (volume
    calibre avec l'utilisateur), sur ce seul canal."""
    import airband_bridge
    windows = airband_bridge.AIRBAND_WINDOWS
    own = next((w for w in windows if any(f == freq_hz for f, _ in w["channels"])), None)
    ref = own or next((w for w in windows if w["mode"] == mode), {})
    window = {k: ref[k] for k in ("gain", "ampfactor", "squelch_snr_threshold") if k in ref}
    center = own["centerfreq_hz"] if own else int(freq_hz) + AIRBAND_CENTER_OFFSET_HZ
    window.update(centerfreq_hz=center, mode=mode, channels=[(int(freq_hz), AIRBAND_UDP_PORT)])
    return airband_bridge._build_config(window), airband_bridge.AIRBAND_BIN


class ManualStream:
    def __init__(self):
        self._rx = None          # rtl_fm ou rtl_airband
        self._sock = None
        self._aplay = None
        self._key = None
        self._lock = None
        self._generation = 0
        self._write_guard = threading.Lock()

    def tune(self, freq_hz, mode, filtered=False):
        """Ecoute freq_hz ; ne fait rien si rien n'a change."""
        airband = bool(filtered and mode in ("am", "nfm"))
        key = (int(freq_hz), mode, airband)
        if key == self._key and self._rx is not None and self._rx.poll() is None:
            return
        self._stop_receiver()
        if self._lock is None:
            self._lock = audio_lock.acquire()
        if self._aplay is None or self._aplay.poll() is not None:
            self._aplay = subprocess.Popen(
                ["aplay", "-q", "-D", "default", "-r", str(OUT_RATE), "-f", "S16_LE", "-c", "1", "-B", "500000"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._generation += 1
        if airband:
            self._start_airband(freq_hz, mode, self._generation)
        else:
            self._start_rtl_fm(freq_hz, mode, self._generation)
        self._key = key
        print(f"[manual_tuner] ecoute manuelle {freq_hz / 1e6:.4f} MHz ({mode}, "
              f"{'RTLSDR-Airband avec silencieux' if airband else 'rtl_fm sans silencieux'})")

    # --- rtl_fm, sans silencieux ---
    def _start_rtl_fm(self, freq_hz, mode, generation):
        if mode == "wfm":
            args, in_rate = WFM_ARGS, OUT_RATE
        else:
            args, in_rate = ["-M", mode, "-s", str(NARROW_RATE), "-l", "0", "-E", "dc"], NARROW_RATE
        self._rx = subprocess.Popen(["rtl_fm", "-f", str(int(freq_hz))] + args,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._relay_rtl_fm, args=(self._rx, in_rate, generation), daemon=True).start()

    def _relay_rtl_fm(self, rx, in_rate, generation):
        chunk_bytes = int(in_rate * 2 * CHUNK_MS / 1000)
        state = None
        while True:
            chunk = rx.stdout.read(chunk_bytes)
            if not chunk:
                return
            if in_rate != OUT_RATE:
                chunk, state = audioop.ratecv(chunk, 2, 1, in_rate, OUT_RATE, state)
            if not self._write(chunk, generation):
                return

    # --- RTLSDR-Airband, avec silencieux (touche F) ---
    def _start_airband(self, freq_hz, mode, generation):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # rebind immediat apres un changement
        self._sock.bind(("127.0.0.1", AIRBAND_UDP_PORT))
        self._sock.settimeout(0.3)
        config, binary = _airband_window(freq_hz, mode)
        with open(AIRBAND_CONFIG, "w") as f:
            f.write(config)
        self._rx = subprocess.Popen([binary, "-F", "-e", "-c", AIRBAND_CONFIG],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
        threading.Thread(target=self._log_stderr, args=(self._rx,), daemon=True).start()
        threading.Thread(target=self._relay_airband, args=(self._sock, generation), daemon=True).start()

    @staticmethod
    def _log_stderr(rx):
        # Messages de rtl_airband dans le journal (diagnostic de la touche F).
        for line in rx.stderr:
            line = line.strip()
            if line:
                print(f"[manual_tuner] rtl_airband : {line[:160]}")

    def _relay_airband(self, sock, generation):
        import audio_filters
        import numpy as np
        voice_filter = audio_filters.VoiceBandpassFilter(AIRBAND_RATE)  # comme _relay_channel()
        state = None
        packets = written = 0
        last_report = time.time()
        while generation == self._generation:
            if time.time() - last_report >= 5:
                print(f"[manual_tuner] airband 5 s : {packets} paquets recus, {written} octets joues")
                packets = written = 0
                last_report = time.time()
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue  # silencieux ferme : rien a jouer
            except OSError:
                return
            packets += 1
            data = voice_filter.process_f32le(data)
            samples = np.frombuffer(data[: len(data) - len(data) % 4], dtype=np.float32)
            pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16).tobytes()
            pcm, state = audioop.ratecv(pcm, 2, 1, AIRBAND_RATE, OUT_RATE, state)
            if not self._write(pcm, generation):
                print("[manual_tuner] airband : ecriture vers la sortie son refusee, relais arrete")
                return
            written += len(pcm)

    def _write(self, data, generation):
        with self._write_guard:
            if generation != self._generation or self._aplay is None:
                return False  # un nouveau recepteur a pris le relais
            try:
                self._aplay.stdin.write(data)
                return True
            except (BrokenPipeError, ValueError, OSError):
                return False

    def _stop_receiver(self):
        with self._write_guard:
            self._generation += 1  # le fil de relais en cours s'arrete de lui-meme
        _stop_process(self._rx)
        self._rx = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._key = None

    def stop(self):
        """Arrete l'ecoute et rend le verrou audio (sans effet si inactif)."""
        was_active = self._rx is not None or self._aplay is not None
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
