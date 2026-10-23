import queue
import select
import socket
import subprocess
import threading
import time

from sound_player import AUDIO_DEVICE, play_sound_effect
import sound_player
from speak import journal_notice, speak_with_lead_sound
import audio_lock
import radio_log
import noise_signatures
import ignores  # commande "ignorer" (2026-10-22)
import maintien  # tenir une ecoute selon l'interet ; "zapper" (2026-10-22)
import audio_filters
import chaine_audio
from sound_paths import RADIO_SCANER_DIR

# --- RTLSDR-Airband : chemin de detection pour les bandes a canaux fixes ---
# Remplace, pour ces bandes precises, le balayage rtl_power + capture rtl_fm +
# discrimination RNNoise par le squelch reel (base sur le rapport signal/bruit
# par canal, calibre en continu sur le plancher de bruit local) de
# RTLSDR-Airband. Contrairement a rtl_power, ce projet ne sait pas balayer un
# spectre inconnu pour trouver un pic -- il lui faut une liste de frequences de
# canaux CONNUES A L'AVANCE. C'est exactement le cas des bandes "fm_channels"
# (PMR446, frequences aeronautiques nommees) mais pas des bandes "sweep"
# (radioamateur, CB, aviation militaire...), qui restent donc gerees par
# l'ancien mecanisme dans radio_watch.py -- migration hybride assumee, pas un
# remplacement complet. Voir ROADMAP.md (session RTLSDR-Airband, 2026-08-30).

AIRBAND_BIN = "/home/vador/autopilot/pi_console/rtl_airband/src/build/src/rtl_airband"
AIRBAND_CONFIG_DIR = "/tmp"
AIRBAND_SAMPLE_RATE_HZ = 16000  # WAVE_RATE avec NFM=ON : PCM mono flottant 32 bits
AIRBAND_STARTUP_WAIT_SEC = 3.0  # laisse le tuner verrouiller et le squelch calibrer son plancher de bruit avant d'ecouter
AIRBAND_PROC_STOP_TIMEOUT_SEC = 3

SOUND_EFFECTS_DIR = RADIO_SCANER_DIR  # chemin de base centralise dans sound_paths.py (2026-08-31)
FREQ_OPEN_SOUND = f"{SOUND_EFFECTS_DIR}/freq_open.mp3"
FREQ_OVER_SOUND = f"{SOUND_EFFECTS_DIR}/freq_over.mp3"
INCOMING_SOUND = f"{SOUND_EFFECTS_DIR}/incoming.mp3"

# 1,5 -> 3 s (2026-09-30) : une fin coupee trop tot est pire qu'un peu de
# silence en fin de capture (priorites de veille fixees avec l'utilisateur).
SILENCE_DEBOUNCE_SEC = 3.0
MIN_AUDIO_SEC = 0.6          # en dessous : eclat de squelch, prise abandonnee
MIN_ECOUTE_SEC = 8.0         # duree minimale d'ecoute d'un canal ouvert (meme valeur que radio_watch)
GAP_SEC = 0.3  # un trou plus long entre deux paquets = le silencieux s'est ferme puis rouvert
MAX_HOLD_SEC = 45

# centerfreq toujours decale de la frequence des canaux (jamais confondu) :
# le tuner R820T a un artefact materiel de pic DC au centre exact de la bande
# accordee, deja identifie et contourne ailleurs dans ce projet (voir
# radio_watch.py, DC_SPIKE_GUARD_BINS et "-E dc" de rtl_fm).
AIRBAND_WINDOWS = [
    {
        "name": "pmr446",
        "name_spoken": "446 civil",
        "centerfreq_hz": 446_450_000,
        "mode": "nfm",
        # Volume juge trop faible par l'utilisateur sur signal reel (deux
        # transmissions captees en test, 2026-08-30) compare au reste du
        # systeme -- ampfactor multiplie l'audio demodule avant sortie UDP.
        # Calibrage iteratif en session sur signal reel PMR446 (2026-08-30) :
        # 1.5 -> 2.5 -> 4 -> 5.5 (valeur finale, confirmee par l'utilisateur :
        # "je pense qu'on est bon", niveau egalise avec le volume principal
        # du systeme).
        "ampfactor": 5.5,
        # Coupe trop tot (notes de l'utilisateur 2026-10-01/02 : on entend le
        # "a" d'ouverture puis plus rien ; mesure : 0,2 s de son sur 6,3 s) :
        # avec le seuil par defaut (9,54 dB) le silencieux se referme des que
        # le talkie faiblit. Premier reglage plus tolerant, a ajuster avec
        # les notes ; et 5 s de silence avant de clore, pour garder les
        # reponses d'une conversation dans le meme enregistrement.
        "squelch_snr_threshold": 5,
        "silence_debounce_sec": 5.0,
        "dwell_sec": 15,  # usage personnel (talky) le plus probable des 6 fenetres
        "channels": [
            (446_006_250, 9100), (446_018_750, 9101), (446_031_250, 9102), (446_043_750, 9103),
            (446_056_250, 9104), (446_068_750, 9105), (446_081_250, 9106), (446_093_750, 9107),
            (446_106_250, 9108), (446_118_750, 9109), (446_131_250, 9110), (446_143_750, 9111),
            (446_156_250, 9112), (446_168_750, 9113), (446_181_250, 9114), (446_193_750, 9115),
        ],
    },
    {
        # Frequence isolee du reste d'aviation_locale (voir radio_watch.py) :
        # boucle continue (meteo/piste pre-enregistree), signal de test fiable
        # deja utilise en session pour prouver le pipeline de bout en bout.
        # squelch_snr_threshold calibre en session (2026-08-30) sur signal
        # reel : le defaut du projet (9.54 dB) ne s'ouvrait jamais sur cet
        # emetteur distant, 5 dB non plus, 4 dB non plus -- 3 dB confirme a
        # deux reprises (12 puis 17 paquets, amplitude reelle variee 0.68-0.73,
        # pas saturee). Meme methodologie que NOISE_MARGIN_DB dans
        # radio_watch.py (mesure sur signal reel, pas une valeur par defaut
        # non verifiee).
        "name": "aviation_atis",
        "name_spoken": "ATIS Lyon Saint-Exupéry",
        "centerfreq_hz": 126_400_000,
        "mode": "am",
        "squelch_snr_threshold": 3,
        "ampfactor": 5.5,
        "gain": 20,  # voir note "gain reduit" ci-dessous (2026-08-30)
        "dwell_sec": 10,  # boucle continue, pas besoin d'ecouter longtemps pour capter
        "channels": [(126_175_000, 9200)],
    },
    {
        # ATIS de l'aeroport local Saint-Etienne Boutheon (132,800 MHz,
        # 2026-10-07). Meteo d'aeroport en boucle, en clair : le scanner la
        # capte et la met au journal radio comme toute voix (pas de Gemini,
        # demande utilisateur : la voix suffit, on l'ecoute). Peut rester
        # muet hors heures d'ouverture de l'aerodrome. centerfreq decale de
        # +225 kHz du canal, comme la fenetre ATIS de Lyon.
        "name": "aviation_atis_st_etienne",
        "name_spoken": "ATIS Saint-Étienne Bouthéon",
        "centerfreq_hz": 133_025_000,
        "mode": "am",
        "squelch_snr_threshold": 3,
        "ampfactor": 5.5,
        "gain": 20,
        "dwell_sec": 10,
        "channels": [(132_800_000, 9205)],
    },
    # Les 4 frequences restantes d'aviation_locale (voir radio_watch.py) :
    # separees en fenetres a un seul canal (pas groupees) -- seul l'offset
    # centerfreq/canal a 225 kHz a ete valide en session (ATIS), regrouper
    # plusieurs canaux distants de plus d'un MHz du centre n'a jamais ete
    # teste, pas de raison de le risquer ici.
    #
    # squelch_snr_threshold=3 (copie par inference depuis la mesure ATIS)
    # RETIRE en session (2026-08-30) : diagnostic demande par l'utilisateur
    # sur un "gresillement hache, plein de saccades" systematique sur ces
    # frequences -- capture du rythme reel des paquets UDP (squelch=3
    # inchange, pas force) confirmee : aviation_approche produisait 11
    # rafales de ~0.17s separees de silences de ~1s (chatter mecanique
    # classique d'un squelch qui oscille pile a son seuil), aviation_sol une
    # unique ouverture continue de 13.5s (bien trop long pour un vrai
    # echange ATC). Pas une interference externe -- le seuil de 3dB, jamais
    # verifie independamment sur ces 4 frequences (voir historique), etait
    # simplement trop permissif. Retour au defaut logiciel du projet
    # (9.54 dB, pas de champ = pas de squelch_snr_threshold ici, meme
    # traitement que pmr446) -- ces canaux ayant un trafic plus rare, rater
    # occasionnellement une transmission coute moins cher qu'un chatter
    # permanent.
    #
    # Constate en session (2026-08-30) : MEME au defaut, aviation_sol et
    # aviation_st_etienne continuaient a declencher a repetition (plusieurs
    # blocages de 45s consecutifs, plafond MAX_HOLD_SEC atteint plusieurs
    # fois de suite) -- suffisant pour monopoliser le cycle principal de
    # radio_watch.py et affamer les bandes "sweep" de leur temps d'antenne
    # (signale par l'utilisateur : "il manque des plages de freq qu'on
    # scannait avant"). Deux mesures prises ensemble :
    # 1. gain=20 sur les 5 fenetres aviation (au lieu de 30) -- recherche
    #    web en session (voir ROADMAP.md) : le guide ATCO2 (recherche pro
    #    sur la voix ATC) recommande explicitement un gain BAS, et un
    #    exemple communautaire reel pour un scanner VHF marine tourne a
    #    gain=20 avec un squelch proche du defaut. Hypothese : gain=30
    #    amplifiait le bruit/la friture autant que le signal utile,
    #    faussant le calcul SNR du squelch. Pas encore confirme sur signal
    #    reel (RF trop variable pour re-declencher a la demande au moment
    #    du test) -- a surveiller.
    # 2. max_hold_sec=20 (au lieu des 45s globaux) sur ces 4 canaux
    #    precisement -- garde-fou independant du reglage squelch/gain :
    #    meme si un faux declenchement se reproduit, il ne peut plus
    #    monopoliser le cycle aussi longtemps. ATIS et pmr446 gardent 45s
    #    (trafic legitime long -- boucle meteo complete, vraie conversation).
    {
        "name": "aviation_st_etienne",
        "name_spoken": "Saint-Étienne Andrézieux-Bouthéon",
        "centerfreq_hz": 119_475_000,
        "mode": "am",
        "ampfactor": 5.5,
        "gain": 20,
        "max_hold_sec": 20,
        "dwell_sec": 8,
        "channels": [(119_250_000, 9201)],
    },
    {
        "name": "aviation_tour",
        "name_spoken": "Lyon Saint-Exupéry tour",
        "centerfreq_hz": 120_675_000,
        "mode": "am",
        "ampfactor": 5.5,
        "gain": 20,
        "max_hold_sec": 20,
        "dwell_sec": 8,
        "channels": [(120_450_000, 9202)],
    },
    {
        "name": "aviation_sol",
        "name_spoken": "Lyon Saint-Exupéry sol",
        "centerfreq_hz": 122_050_000,
        "mode": "am",
        "ampfactor": 5.5,
        "gain": 20,
        "max_hold_sec": 20,
        "dwell_sec": 8,
        "channels": [(121_825_000, 9203)],
    },
    {
        "name": "aviation_approche",
        "name_spoken": "Lyon Saint-Exupéry approche",
        "centerfreq_hz": 136_300_000,
        "mode": "am",
        "ampfactor": 5.5,
        "gain": 20,
        "max_hold_sec": 20,
        "dwell_sec": 8,
        "channels": [(136_075_000, 9204)],
    },
]


def _settings(window_name):
    try:
        import reglages_audio
        return reglages_audio.get(window_name)
    except Exception as exc:
        print(f"[airband_bridge] reglages audio illisibles : {exc!r}")
        import reglages_audio
        return dict(reglages_audio.DEFAULTS)


def _build_config(window):
    lines = [
        "devices:",
        "({",
        '  type = "rtlsdr";',
        "  index = 0;",
        f"  gain = {window.get('gain', 30)};",
        f"  centerfreq = {window['centerfreq_hz'] / 1e6:.6f};",
        '  mode = "multichannel";',
        "  channels:",
        "  (",
    ]
    extra_clauses = ""
    # Marge de detection ajustee par le bilan des notes (lib/reglages_audio.py) :
    # s'ajoute au seuil du silencieux (9,54 dB par defaut dans RTLSDR-Airband).
    margin = _settings(window["name"])["marge_detection_db"]
    if "squelch_snr_threshold" in window or margin:
        threshold = max(1.0, window.get("squelch_snr_threshold", 9.54) + margin)
        extra_clauses += f"squelch_snr_threshold = {threshold:g}; "
    if "ampfactor" in window:
        # Doit avoir un point decimal -- libconfig distingue int et float, et
        # le parseur d'ampfactor (contrairement a squelch_snr_threshold) ne
        # gere pas le cas entier, echoue avec "invalid parameter type"
        # (confirme en session).
        extra_clauses += f"ampfactor = {float(window['ampfactor']):.2f}; "

    channel_entries = []
    for freq_hz, port in window["channels"]:
        channel_entries.append(
            '    { freq = %.6f; modulation = "%s"; %soutputs: ({ type = "udp_stream"; '
            'dest_address = "127.0.0.1"; dest_port = %d; }); }'
            % (freq_hz / 1e6, window["mode"], extra_clauses, port)
        )
    lines.append(",\n".join(channel_entries))
    lines += ["  );", " }", ");"]
    return "\n".join(lines)


def _drain_stderr(proc, tail):
    try:
        for line in proc.stderr:
            tail.append(line.decode(errors="replace"))
            if len(tail) > 20:
                tail.pop(0)
    except Exception:
        pass


def _udp_reader(sock, out_queue, stop_event):
    """Tourne dans un thread separe, demarre AVANT l'annonce -- capte les
    paquets UDP des leur arrivee dans une file, independamment du rythme de
    consommation cote lecture. Comble le trou signale par l'utilisateur
    (2026-08-30) : sans ca, le debut d'une transmission pouvait etre perdu
    (ou dependre du seul tampon reseau du noyau, non garanti) pendant les
    quelques secondes de l'annonce parlee. Meme principe que
    _rtl_fm_reader dans radio_watch.py, qui le fait deja pour le chemin
    rtl_fm -- ici cote UDP RTLSDR-Airband, ou ca manquait."""
    while not stop_event.is_set():
        try:
            data, _ = sock.recvfrom(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        out_queue.put(data)
    out_queue.put(None)


def _save_raw(path, pcm_f32):
    """Son brut a cote de l'enregistrement : <nom>.brut.mp3 (touche B du journal)."""
    def work():
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le",
                            "-ar", str(AIRBAND_SAMPLE_RATE_HZ), "-ac", "1", "-i", "-", path[:-4] + ".brut.mp3"],
                           input=pcm_f32, timeout=60)
        except Exception as exc:
            print(f"[airband_bridge] son brut non garde : {exc!r}")
    threading.Thread(target=work, daemon=True).start()


# Detecteur de voix fourni par radio_watch au demarrage (meme RNNoise que le
# balayage) : fabrique -> fonction (morceau PCM s16le, frequence) -> voix ?
SPEECH_DETECTOR_FACTORY = None
PRELISTEN_MAX_SEC = 6.0
PRELISTEN_WINDOW_SEC = 0.2
PRELISTEN_VOICE_WINDOWS = 3


def _prelisten(packet_queue):
    """(paquets recus, voix reconnue ?) apres une ecoute silencieuse."""
    import numpy as np
    packets, pending = [], b""
    check = SPEECH_DETECTOR_FACTORY() if SPEECH_DETECTOR_FACTORY else None
    window = int(AIRBAND_SAMPLE_RATE_HZ * 4 * PRELISTEN_WINDOW_SEC)
    voiced = consecutive = 0
    total = 0
    start = time.time()
    while time.time() - start < PRELISTEN_MAX_SEC:
        try:
            data = packet_queue.get(timeout=0.3)
        except queue.Empty:
            continue
        packets.append(data)
        if data is None:
            break
        total += len(data)
        pending += data
        while len(pending) >= window:
            block, pending = pending[:window], pending[window:]
            if check is None:  # pas de detecteur : au moins une seconde de son suffit
                voiced += 1
                consecutive = voiced if total >= AIRBAND_SAMPLE_RATE_HZ * 4 else 0
            else:
                pcm = (np.clip(np.frombuffer(block, dtype="<f4"), -1, 1) * 32767).astype("<i2").tobytes()
                consecutive = consecutive + 1 if check(pcm, AIRBAND_SAMPLE_RATE_HZ) else 0
            if consecutive >= PRELISTEN_VOICE_WINDOWS:
                print(f"[airband_bridge] pre-ecoute : voix reconnue en {time.time() - start:.1f} s")
                return packets, True
    return packets, False


def _relay_channel(sock, freq_hz, name_spoken, max_hold_sec=MAX_HOLD_SEC, window_name=None):
    """Diffuse en direct l'audio UDP recu tant qu'il arrive, coupe apres
    SILENCE_DEBOUNCE_SEC sans paquet (le squelch de RTLSDR-Airband a alors
    ferme -- pas besoin de RNNoise ici, la decision "signal reel" est deja
    prise en amont par le squelch SNR reel). Retourne toujours True : on
    n'entre ici que si au moins un paquet est deja arrive."""
    lock = audio_lock.acquire()
    # Journal audio (radio_log.py, 2026-08-31, demande explicite) : voir le
    # commentaire equivalent dans radio_watch.py/_handle_candidate -- meme
    # principe, mais ici tout le cycle (annonce+relais+fin) tient dans
    # cette seule fonction, qui retourne toujours True (un appel n'arrive
    # que si un paquet reel est deja arrive), donc toujours sauvegardee.
    log_session = radio_log.RadioLogSession(name_spoken)
    sound_player.set_recording_session(log_session)
    try:
        # Capture demarree AVANT l'annonce (voir _udp_reader) -- tout ce qui
        # arrive pendant que "incoming"/l'annonce/le chime jouent s'accumule
        # dans packet_queue au lieu d'etre perdu ou de dependre du seul
        # tampon reseau du noyau. La lecture ci-dessous depuis la file
        # (pas le socket directement) rattrape ce retard des que l'annonce
        # se termine -- au prix de quelques secondes d'ecart avec le direct,
        # accepte explicitement par l'utilisateur plutot que de perdre le
        # debut de la transmission.
        sock.settimeout(0.3)
        packet_queue = queue.Queue()
        stop_event = threading.Event()
        reader_thread = threading.Thread(target=_udp_reader, args=(sock, packet_queue, stop_event), daemon=True)
        reader_thread.start()

        # Pre-ecoute silencieuse (2026-10-03, demande utilisateur) : on n'ouvre
        # le son que si ca vaut la peine -- voix reconnue (RNNoise) -- puis on
        # joue tout ce qui a ete mis en attente. Un bref parasite n'ouvre plus
        # rien : ni carillon, ni "crcrr", ni prise au journal.
        prebuffer, worth = _prelisten(packet_queue)
        if not worth:
            print(f"[airband_bridge] pre-ecoute {freq_hz/1e6:.5f} MHz : pas de voix, son jamais ouvert")
            stop_event.set()
            log_session.discard()
            return False
        # Parasites connus (commande console "parasite") : compares sur le meme son.
        if noise_signatures.has_signatures_near(freq_hz):
            pre_pcm = audio_filters.VoiceBandpassFilter(AIRBAND_SAMPLE_RATE_HZ).process_f32le(
                b"".join(d for d in prebuffer if d))
            is_noise, sim = noise_signatures.matches_known_noise(
                freq_hz, pre_pcm, AIRBAND_SAMPLE_RATE_HZ, "f32le")
            if is_noise:
                print(f"[airband_bridge] parasite connu ignore a {freq_hz/1e6:.5f} MHz (similarite {sim:.2f})")
                stop_event.set()
                log_session.discard()
                return False

        # "incoming" avant l'annonce (tampon pendant la synthese cloud, voir
        # speak_with_lead_sound) ; annonce simplifiee (2026-08-30, demande de
        # l'utilisateur) : le nom d'usage seul, sans la frequence exacte en
        # MHz -- peu intelligible a l'oral, name_spoken porte deja le nom
        # clair ("446 civil", "ATIS Lyon Saint-Exupéry"...).
        # name_spoken : petit ensemble fixe (voir AIRBAND_WINDOWS) --
        # candidat naturel au cache repetitif (2026-08-31).
        speak_with_lead_sound(INCOMING_SOUND, name_spoken, label="airband_bridge", repetitive=True)
        play_sound_effect(FREQ_OPEN_SOUND, label="airband_bridge")
        # Ce qu'on entend maintenant : cible de "verrouille" pendant l'ecoute.
        live_window = next((w for w in AIRBAND_WINDOWS if w["name"] == window_name), {})
        noise_signatures.capture_started("airband", freq_hz, live_window.get("mode", "am"), name_spoken, window_name)

        aplay = subprocess.Popen(
            ["aplay", "-D", AUDIO_DEVICE, "-r", str(AIRBAND_SAMPLE_RATE_HZ),
             "-f", "FLOAT_LE", "-c", "1", "-q", "-B", "500000"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

        start = time.time()
        last_packet_time = time.time()
        total_bytes = 0
        max_hold_sec = maintien.plafond(max_hold_sec, window_name, freq_hz)  # plus de coupure d'un echange interessant
        max_bytes = int(AIRBAND_SAMPLE_RATE_HZ * 4 * max_hold_sec)
        captured_chunks = []
        fingerprint_chunks = []  # filtre vocal seul : empreintes comparables aux anciennes
        gaps = 0
        # Filtre passe-bande vocal (300-3000Hz, 2026-08-31, demande
        # explicite de l'utilisateur suite a l'analyse spectrale de
        # noise_study/) -- une instance par reception, etat frais a chaque
        # fois. Applique AVANT l'ecriture vers aplay ET avant l'archivage
        # (radio_log) : le journal reflete ce qui a ete reellement entendu.
        window = next((w for w in AIRBAND_WINDOWS if w["name"] == window_name), {})
        # Chaine de filtres commune (lib/chaine_audio.py : la meme que le
        # balayage et que la touche F du mode manuel) ; son brut garde pour B.
        chain = chaine_audio.nouvelle(window_name, freq_hz, AIRBAND_SAMPLE_RATE_HZ, window.get("mode", "am"))
        debounce = maintien.delai(chain.settings["attente_fin_s"] or window.get("silence_debounce_sec", SILENCE_DEBOUNCE_SEC),
                                  window_name, freq_hz)
        raw_chunks = []
        try:
            while True:
                if time.time() - start > max_hold_sec + 5:
                    break
                if maintien.zap_depuis(start):
                    print("[airband_bridge] zapper : ecoute arretee a la demande")
                    break
                # Au moins MIN_ECOUTE_SEC d'ecoute (2026-10-19) : un clic de
                # squelch ne ferme pas le canal en 3 s, une reponse n'est pas perdue.
                if time.time() - last_packet_time > debounce and time.time() - start >= MIN_ECOUTE_SEC:
                    break
                if prebuffer:
                    data = prebuffer.pop(0)
                else:
                    try:
                        data = packet_queue.get(timeout=0.3)
                    except queue.Empty:
                        continue
                if data is None:
                    break
                if time.time() - last_packet_time > GAP_SEC:
                    gaps += 1
                last_packet_time = time.time()
                raw_chunks.append(data)
                filtered, data = chain.process_f32le(data)
                fingerprint_chunks.append(filtered)
                captured_chunks.append(data)
                total_bytes += len(data)
                try:
                    aplay.stdin.write(audio_lock.scale_float(data, canal="ecoute"))
                except Exception:
                    break
                if total_bytes >= max_bytes:
                    break
        finally:
            stop_event.set()
            try:
                aplay.stdin.close()
            except Exception:
                pass
            try:
                aplay.terminate()
                aplay.wait(timeout=2)
            except Exception:
                try:
                    aplay.kill()
                except Exception:
                    pass

        audio_sec = total_bytes / (AIRBAND_SAMPLE_RATE_HZ * 4)
        if audio_sec < MIN_AUDIO_SEC:
            # Simple eclat de squelch (2026-10-03 : des prises de 0,1 s dans le
            # journal) : rien a garder.
            print(f"[airband_bridge] canal ignore : {audio_sec:.1f} s de son seulement")
            play_sound_effect(FREQ_OVER_SOUND, label="airband_bridge")
            log_session.discard()
            return False
        if captured_chunks:
            log_session.add_raw_pcm(b"".join(captured_chunks), AIRBAND_SAMPLE_RATE_HZ, "f32le")

        print(f"[airband_bridge] canal termine : {total_bytes} octets relayes")
        play_sound_effect(FREQ_OVER_SOUND, label="airband_bridge")
        path = log_session.finalize()
        if path and raw_chunks:
            _save_raw(path, b"".join(raw_chunks))
        fp = (noise_signatures.fingerprint(b"".join(fingerprint_chunks), AIRBAND_SAMPLE_RATE_HZ, "f32le")
              if fingerprint_chunks else None)
        mode = window.get("mode", "am")  # le 446 est en FM etroite (etait note "am" a tort)
        noise_signatures.record_capture("airband", freq_hz, mode, name_spoken, window_name, path, fp, {
            "duree_audio_s": round(total_bytes / (AIRBAND_SAMPLE_RATE_HZ * 4), 1),
            "duree_totale_s": round(time.time() - start, 1),
            "trous_silencieux": gaps,
        })
        return True
    finally:
        noise_signatures.capture_ended()
        sound_player.set_recording_session(None)
        audio_lock.release(lock)
        if lock.silent:
            journal_notice()


def scan_window(window, dwell_sec=25):
    """Ecoute simultanement tous les canaux de la fenetre pendant dwell_sec,
    via le squelch reel de RTLSDR-Airband. Des qu'un canal recoit un paquet
    UDP (donc que le squelch s'est ouvert dessus), relaie l'audio en direct
    jusqu'a extinction puis retourne True. Retourne False si rien ne s'est
    passe pendant tout le dwell. Un seul processus rtl_airband a la fois
    (meme dongle que radio_watch.py -- appele depuis la meme boucle
    sequentielle, jamais en parallele)."""
    config_path = f"{AIRBAND_CONFIG_DIR}/airband_{window['name']}.conf"
    with open(config_path, "w") as f:
        f.write(_build_config(window))

    proc = subprocess.Popen(
        [AIRBAND_BIN, "-F", "-e", "-c", config_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    stderr_tail = []
    stderr_thread = threading.Thread(target=_drain_stderr, args=(proc, stderr_tail), daemon=True)
    stderr_thread.start()

    socks = {}
    try:
        for freq_hz, port in window["channels"]:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", port))
            socks[s] = freq_hz

        if proc.poll() is not None:
            print(f"[airband_bridge] rtl_airband n'a pas demarre ({window['name']}) : {''.join(stderr_tail[-5:])}")
            return False

        time.sleep(AIRBAND_STARTUP_WAIT_SEC)

        # Vide les paquets accumules pendant le demarrage/calibration du
        # squelch -- pas des transmissions reelles, juste l'amorçage.
        for s in socks:
            s.setblocking(False)
            try:
                while True:
                    s.recvfrom(65536)
            except BlockingIOError:
                pass
            s.setblocking(True)

        debut = time.time()
        deadline = debut + dwell_sec
        while time.time() < deadline:
            if maintien.zap_depuis(debut):
                return False  # zapper : fenetre suivante
            if proc.poll() is not None:
                print(f"[airband_bridge] rtl_airband s'est arrete de façon inattendue ({window['name']}) : {''.join(stderr_tail[-5:])}")
                return False
            remaining = deadline - time.time()
            ready, _, _ = select.select(list(socks.keys()), [], [], min(0.5, max(0.0, remaining)))
            if not ready:
                continue
            s = ready[0]
            freq_hz = socks[s]
            if ignores.frequence_ignoree(freq_hz):  # mise de cote : on ecoute les autres canaux
                try:
                    s.recvfrom(65536)
                except OSError:
                    pass
                continue
            print(f"[airband_bridge] activite detectee sur {window['name']} a {freq_hz/1e6:.5f} MHz")
            return _relay_channel(s, freq_hz, window["name_spoken"], max_hold_sec=window.get("max_hold_sec", MAX_HOLD_SEC),
                                  window_name=window["name"])
        return False
    finally:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=AIRBAND_PROC_STOP_TIMEOUT_SEC)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
