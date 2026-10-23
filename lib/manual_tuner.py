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

Decodeurs (2026-10-02 : "tous les decodeurs doivent fonctionner en mode
manuel comme dans le reste") : en FM etroite sans filtre, la cle n'est
ouverte qu'une fois (rtl_fm a 48 kHz) et son flux nourrit aussi
- dsd-fme (lib/decodeurs.py : auto-detection, plus le mode propre a la
  bande, dPMR / M17 / NXDN) par un petit serveur TCP local, comme SDR++ ;
  des qu'un decodage est confirme, la voix decodee remplace le gresillement
  numerique, apres le son de decodage ;
- multimon-ng (bipeurs, APRS, DTMF, morse...).
Les annonces ("Reception DMR", messages decodes) passent par la console
(EVENTS_PATH), qui parle deja en mode manuel.
"""
import audioop
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time

import audio_lock
import chaine_audio
import decodeurs
import dmr_filtres
from sound_paths import RADIO_SCANER_DIR
from phrases import dire

SETTLE_SEC = 0.5        # frequence stable depuis ce delai avant de s'y caler
STOP_GRACE_SEC = 1.5    # SIGTERM, puis SIGKILL si toujours vivant
OUT_RATE = 48000
NARROW_RATE = 22050
CHUNK_MS = 200

# Memes reglages de demodulation que stream_until_quiet() dans radio_watch.py.
WFM_ARGS = ["-M", "wbfm", "-s", "200000", "-r", str(OUT_RATE), "-l", "0", "-E", "dc", "-E", "deemp"]

DSD_TCP_PORT = 7360                 # serveur local qui nourrit dsd-fme (hors 7355 de SDR++)
DSD_UDP_PORTS = (23460, 23461)      # voix decodee (le scanner utilise 23456)
EVENTS_PATH = "/tmp/manuel_evenements.jsonl"
DECODING_SOUND = os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3")
DIGITAL_HOLD_SEC = 1.0              # analogique coupe tant que la voix numerique arrive
DTMF_GAP_SEC = 1.5                  # chiffres DTMF plus rapproches : une seule annonce
AIRBAND_UDP_PORT = 9290            # hors de la plage des fenetres (9100-9204)
AIRBAND_CONFIG = "/tmp/airband_manuel.conf"
# Centre du tuner : jamais sur le canal (pic DC). Celui de la fenetre airband
# qui contient la frequence quand il y en a une (eprouve en verrouillage),
# sinon un decalage fixe. NB : "[R82XX] PLL not locked!" s'affiche au
# demarrage de la cle quel que soit le centre -- ce n'est PAS une panne.
AIRBAND_CENTER_OFFSET_HZ = 300_000
AIRBAND_RATE = 16000                # sortie UDP de RTLSDR-Airband : PCM flottant 32 bits mono


def event(text):
    """Annonce pour la console en mode manuel (console/manuel.py la dit)."""
    try:
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "texte": text}, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[manual_tuner] annonce non transmise : {exc}")


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


def _airband_window(freq_hz, mode, target=None):
    """Reglages de la fenetre airband_bridge qui contient cette frequence
    (centre compris), sinon de la premiere fenetre du meme mode (volume
    calibre avec l'utilisateur), sur ce seul canal. Nom de la fenetre, ou de
    la bande ecoutee : le seuil appris des notes s'y applique comme au balayage."""
    import airband_bridge
    windows = airband_bridge.AIRBAND_WINDOWS
    own = next((w for w in windows if any(f == freq_hz for f, _ in w["channels"])), None)
    ref = own or next((w for w in windows if w["mode"] == mode), {})
    window = {k: ref[k] for k in ("gain", "ampfactor", "squelch_snr_threshold") if k in ref}
    center = own["centerfreq_hz"] if own else int(freq_hz) + AIRBAND_CENTER_OFFSET_HZ
    window.update(name=own["name"] if own else (target or ref.get("name")), centerfreq_hz=center, mode=mode,
                  channels=[(int(freq_hz), AIRBAND_UDP_PORT)])
    return airband_bridge._build_config(window), airband_bridge.AIRBAND_BIN


def _airband_window_name(freq_hz, target):
    import airband_bridge
    own = next((w for w in airband_bridge.AIRBAND_WINDOWS if any(f == freq_hz for f, _ in w["channels"])), None)
    return own["name"] if own else target


class ManualStream:
    def __init__(self):
        self._rx = None          # rtl_fm ou rtl_airband
        self._sock = None
        self._aplay = None
        self._key = None
        self._lock = None
        self._generation = 0
        self._write_guard = threading.Lock()
        self._target = self._chain_name = self._mode = None
        self._freq = 0
        self._helpers = []        # dsd-fme, multimon-ng
        self._sockets = []
        self._dsd_clients = []
        self._multimon = None
        self._digital_until = 0.0
        self._numerique = False
        self._chime = None

    def tune(self, freq_hz, mode, filtered=False, target=None, numerique=False):
        """Ecoute freq_hz ; ne fait rien si rien n'a change. target : la bande
        (reglages appris et filtres de la chaine commune, touche F). numerique :
        station DMR connue -> dsd-fme en DMR (-fs) et souffle analogique coupe
        (2026-10-08) : entre deux communications d'un relais, on veut le silence
        et la voix decodee, pas le gresillement."""
        airband = bool(filtered and mode in ("am", "nfm") and not numerique)
        key = (int(freq_hz), mode, airband, target, numerique)
        self._target, self._mode, self._numerique = target, mode, numerique
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
        elif mode == "nfm":
            # 48 kHz : ce que dsd-fme attend (comme SDR++), et la sortie son directe.
            args, in_rate = ["-M", "fm", "-s", str(OUT_RATE), "-l", "0", "-E", "dc"], OUT_RATE
            self._start_decoders(freq_hz, generation, numerique=self._numerique)
        else:
            args, in_rate = ["-M", mode, "-s", str(NARROW_RATE), "-l", "0", "-E", "dc"], NARROW_RATE
        self._rx = subprocess.Popen(["rtl_fm", "-f", str(int(freq_hz))] + args,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._relay_rtl_fm, args=(self._rx, in_rate, generation), daemon=True).start()

    def _relay_rtl_fm(self, rx, in_rate, generation):
        chunk_bytes = int(in_rate * 2 * CHUNK_MS / 1000)
        state = mm_state = None
        while True:
            chunk = rx.stdout.read(chunk_bytes)
            if not chunk:
                return
            self._feed_decoders(chunk, generation)
            if self._multimon is not None:
                try:
                    low, mm_state = audioop.ratecv(chunk, 2, 1, in_rate, decodeurs.MULTIMON_RATE, mm_state)
                    self._multimon.stdin.write(low)
                    self._multimon.stdin.flush()
                except (OSError, ValueError):
                    self._multimon = None
            if in_rate != OUT_RATE:
                chunk, state = audioop.ratecv(chunk, 2, 1, in_rate, OUT_RATE, state)
            if self._numerique or time.time() < self._digital_until:
                continue  # station DMR connue : jamais d'analogique ; sinon la voix
                # numerique decodee joue a la place du gresillement quand elle arrive
            if not self._write(chunk, generation):
                return

    # --- decodeurs sur le flux de l'ecoute manuelle ---
    def _start_decoders(self, freq_hz, generation, numerique=False):
        # Station DMR connue : dsd-fme en DMR seul (-fs), qui accroche bien
        # mieux que l'auto-detection (-fa) sujette aux fausses synchros M17.
        flags = ["-fs"] if numerique else ["-fa"]
        extra = decodeurs.extra_mode(freq_hz)
        if extra and not numerique:
            flags.append(extra[0])
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", DSD_TCP_PORT))
            server.listen(len(flags))
            server.settimeout(0.5)
            self._sockets.append(server)
            threading.Thread(target=self._accept_dsd, args=(server, generation), daemon=True).start()
            for flag, port in zip(flags, DSD_UDP_PORTS):
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                udp.bind(("127.0.0.1", port))
                udp.settimeout(0.5)
                self._sockets.append(udp)
                proc = subprocess.Popen(["dsd-fme", flag, "-i", f"tcp:127.0.0.1:{DSD_TCP_PORT}",
                                         "-o", f"udp:127.0.0.1:{port}"],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                self._helpers.append(proc)
                stats = decodeurs.new_stats(freq_hz)  # textes transportes dits au fil de l'eau
                threading.Thread(target=self._watch_dsd, args=(proc, stats), daemon=True).start()
                threading.Thread(target=self._relay_dsd, args=(udp, stats, flag, generation), daemon=True).start()
        except OSError as exc:
            print(f"[manual_tuner] decodage numerique indisponible : {exc}")
        try:
            self._multimon = subprocess.Popen(["multimon-ng"] + decodeurs.MULTIMON_ARGS, stdin=subprocess.PIPE,
                                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self._helpers.append(self._multimon)
            threading.Thread(target=self._watch_multimon, args=(self._multimon, generation), daemon=True).start()
        except OSError as exc:
            print(f"[manual_tuner] multimon-ng indisponible : {exc}")
            self._multimon = None

    def _accept_dsd(self, server, generation):
        while generation == self._generation:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.setblocking(False)
            self._dsd_clients.append(conn)

    def _feed_decoders(self, chunk, generation):
        for conn in list(self._dsd_clients):
            try:
                conn.send(chunk)  # non bloquant : un decodeur en retard perd un morceau, l'ecoute continue
            except BlockingIOError:
                pass
            except OSError:
                self._dsd_clients.remove(conn)

    @staticmethod
    def _watch_dsd(proc, stats):
        try:
            for raw in proc.stdout:
                decodeurs.dsd_line(stats, raw.decode(errors="replace").strip())
        except (OSError, ValueError):
            pass

    @staticmethod
    def _announce_calls(stats, calls):
        """Exploration dmr : dit chaque nouvel appel de groupe (groupe,
        intervalle, ignore ou favori), une fois par appel."""
        current = dict(((stats.get("dmr") or {}).get("en_cours") or {}))
        if current != calls and dmr_filtres.exploring():
            for slot, tg in current.items():
                if calls.get(slot) != tg:
                    state = dmr_filtres.group_state(tg)
                    etat = dire(f"dmr_exploration.etat_{state}") if state else ""
                    event(dire("dmr_exploration.appel", groupe=tg, intervalle=slot, etat=etat).strip())
        return current

    def _relay_dsd(self, udp, stats, flag, generation):
        announced = False
        state = None
        calls = {}
        while generation == self._generation:
            try:
                data, _ = udp.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not decodeurs.dsd_validated(stats):
                continue  # du bruit qui ressemble a du numerique : rien a jouer
            if not announced:
                announced = True
                protocol = stats["protocol"] or "numérique"
                print(f"[manual_tuner] decodage {protocol} confirme ({stats['clean']} synchros propres)")
                self._digital_until = time.time() + DIGITAL_HOLD_SEC + 3
                event(dire("manuel_radio.reception", protocol=protocol))
                self._play_chime(generation)
            stereo = len(data) == decodeurs.DSD_STEREO_DATAGRAM and flag not in decodeurs.DSD_MONO_MODES
            calls = self._announce_calls(stats, calls)
            # Ciblage DMR (lib/dmr_filtres.py) : groupe ignore, hors cible ou
            # autre que celui choisi dans l'exploration -> son coupe.
            data, heard = dmr_filtres.apply(data, stats, 2 if stereo else 1)
            if not heard:
                continue
            if stereo:
                data = audioop.tomono(data, 2, 0.5, 0.5)
            pcm, state = audioop.ratecv(data, 2, 1, decodeurs.DSD_VOICE_RATE, OUT_RATE, state)
            self._digital_until = time.time() + DIGITAL_HOLD_SEC
            if not self._write(pcm, generation):
                return

    def _play_chime(self, generation):
        if self._chime is None:
            try:
                self._chime = subprocess.run(["ffmpeg", "-v", "quiet", "-i", DECODING_SOUND, "-f", "s16le", "-ac", "1",
                                              "-ar", str(OUT_RATE), "-"], capture_output=True, timeout=10).stdout
            except (OSError, subprocess.TimeoutExpired):
                self._chime = b""
        if self._chime:
            self._write(self._chime, generation)

    def _watch_multimon(self, proc, generation):
        """Messages de donnees ; les chiffres DTMF d'une meme rafale (moins de
        DTMF_GAP_SEC entre deux) sont dits ensemble, pas un par un."""
        last = {}
        digits, digits_t = [], 0.0

        def flush():
            nonlocal digits
            if digits:
                event((dire("manuel_radio.tonalites_dtmf") + " ") + " ".join(digits) + ".")
                digits = []

        try:
            for raw in proc.stdout:
                text = raw.decode(errors="replace").strip()
                if not text or text.startswith(("Enabled demodulators", "multimon-ng")):
                    continue
                m = re.search(r"DTMF: ([0-9A-D*#])", text)
                if m:
                    if time.time() - digits_t > DTMF_GAP_SEC:
                        flush()
                    digits.append(m.group(1))
                    digits_t = time.time()
                    threading.Timer(DTMF_GAP_SEC + 0.1, lambda t=digits_t: t == digits_t and flush()).start()
                    continue
                if not decodeurs.meaningful(text) or time.time() - last.get(text, 0) < 30:
                    continue
                last[text] = time.time()
                print(f"[manual_tuner] decode : {text}")
                event(dire("manuel_radio.message_numerique_decode", text=text))
        except (OSError, ValueError):
            pass
        flush()

    # --- RTLSDR-Airband, avec silencieux (touche F) ---
    def _start_airband(self, freq_hz, mode, generation):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # rebind immediat apres un changement
        self._sock.bind(("127.0.0.1", AIRBAND_UDP_PORT))
        self._sock.settimeout(0.3)
        config, binary = _airband_window(freq_hz, mode, self._target)
        self._chain_name = _airband_window_name(freq_hz, self._target)
        self._freq = freq_hz
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
        import numpy as np
        # Touche F : EXACTEMENT la chaine du balayage et des canaux Airband
        # (lib/chaine_audio.py : filtre vocal, reglages appris, nettoyage).
        chain = chaine_audio.nouvelle(self._chain_name, self._freq, AIRBAND_RATE, self._mode)
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
            _, data = chain.process_f32le(data)
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
                # Fil de relais : le ticket est passe explicitement (priorites).
                self._aplay.stdin.write(audio_lock.scale(data, self._lock, canal="ecoute"))
                return True
            except (BrokenPipeError, ValueError, OSError):
                return False

    def _stop_receiver(self):
        with self._write_guard:
            self._generation += 1  # le fil de relais en cours s'arrete de lui-meme
        _stop_process(self._rx)
        self._rx = None
        for proc in self._helpers:
            _stop_process(proc)
        self._helpers, self._multimon = [], None
        for sock in self._sockets + self._dsd_clients:
            try:
                sock.close()
            except OSError:
                pass
        self._sockets, self._dsd_clients = [], []
        self._digital_until = 0.0
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
