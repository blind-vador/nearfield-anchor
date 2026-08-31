import json
import socket
import subprocess
import time

import audio_lock
import radio_log
import sound_player
from speak import speak_with_lead_sound

# --- Veille ACARS (donnees numeriques avion/compagnie) ---
# Ajoute en session (2026-08-31, demande explicite de l'utilisateur) --
# libacars + acarsdec (TLeconte) compiles depuis les sources sur ce Pi (ni
# l'un ni l'autre n'est empaquete Debian), installes dans /usr/local/bin.
# Frequences europeennes standard groupees a moins de 200 kHz d'ecart pour
# tenir dans une seule capture RTL-SDR (131.525/131.550/131.725 MHz) --
# 136.900/136.975 MHz existent aussi mais sont trop loin (~5.4 MHz) pour
# rentrer dans la meme capture avec ce dongle, laissees de cote pour
# l'instant plutot que d'ajouter un second passage dedie.
ACARS_FREQUENCIES_MHZ = ["131.525", "131.550", "131.725"]
ACARS_UDP_PORT = 23457  # local uniquement, distinct du port dsd-fme (23456)
ACARS_DEDUP_WINDOW_SEC = 300  # meme evenement (vol+phase, ou vol+texte) pas repete avant 5 min
ACARS_DEDUP_PURGE_FACTOR = 4  # purge les entrees > 4x la fenetre pour ne pas grossir indefiniment

# Labels OOOI modernes (serie Q) uniquement -- acarsdec sait aussi decoder
# en interne une longue liste de labels numeriques/legacy (10,11,12,15,17,
# 20,21,26,2N,2Z,33,39,44,45,80,83,8D,8E,8S, cf. label.c) mais ce sont des
# formats plus anciens/rares en trafic reel europeen actuel, moins fiables
# a mapper sans validation sur du trafic capte -- pas inclus ici. S'ils
# portent du texte lisible, le filtre "texte present" ci-dessous les
# couvre deja independamment de cette table.
ACARS_PHASE_LABELS = {
    "QA": "sortie de porte",
    "QB": "décollage",
    "QC": "atterrissage",
    "QD": "arrivée à la porte",
    "QH": "sortie de porte",
    "QK": "atterrissage",
}

SOUND_EFFECTS_DIR = "/media/nas/nd1/server/radio_scaner"
ACARS_SOUND = f"{SOUND_EFFECTS_DIR}/decoding_numeric.mp3"

# Historique de deduplication, en memoire seulement (remis a zero a chaque
# redemarrage du service, comme le reste des traces glissantes du projet).
_recent_events = {}


def _identity(msg):
    return msg.get("flight") or msg.get("tail") or "inconnu"


def _should_announce(key):
    now = time.time()
    last = _recent_events.get(key)
    if last is not None and now - last < ACARS_DEDUP_WINDOW_SEC:
        return False
    _recent_events[key] = now
    stale_before = now - ACARS_DEDUP_WINDOW_SEC * ACARS_DEDUP_PURGE_FACTOR
    stale = [k for k, t in _recent_events.items() if t < stale_before]
    for k in stale:
        del _recent_events[k]
    return True


def _announce(text, label_for_log):
    """Meme schema que _handle_candidate dans radio_watch.py : session de
    journal audio active pendant l'annonce (son + voix TTS captes
    automatiquement via sound_player), verrou audio partage."""
    log_session = radio_log.RadioLogSession(label_for_log)
    sound_player.set_recording_session(log_session)
    lock = audio_lock.acquire()
    try:
        speak_with_lead_sound(ACARS_SOUND, text, label="acars_watch")
    finally:
        audio_lock.release(lock)
        sound_player.set_recording_session(None)
        log_session.finalize()


def _process_message(msg):
    """Annonce le message s'il est exploitable (texte libre present, ou
    label de phase de vol connu) et pas deja annonce recemment pour ce
    vol/cette phase. Retourne True si une annonce a reellement eu lieu."""
    identity = _identity(msg)
    label = msg.get("label", "")
    text = (msg.get("text") or "").strip()

    if text:
        key = (identity, "texte", text[:60])
        if not _should_announce(key):
            return False
        _announce(f"Message ACARS de {identity} : {text}", f"ACARS_texte_{identity}")
        return True

    phase = ACARS_PHASE_LABELS.get(label)
    if phase:
        key = (identity, phase)
        if not _should_announce(key):
            return False
        _announce(f"Vol {identity}, {phase}.", f"ACARS_{phase}_{identity}")
        return True

    return False


def scan_window(dwell_sec=30):
    """Une passe d'ecoute ACARS, meme discipline de partage du dongle que
    airband_bridge.scan_window : lance acarsdec sur les frequences
    surveillees, ecoute le flux JSON UDP local pendant dwell_sec, annonce
    ce qui merite de l'etre, arrete proprement. Retourne True si au moins
    une annonce reelle a eu lieu pendant cette passe."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", ACARS_UDP_PORT))
    except OSError as exc:
        print(f"[acars_watch] impossible d'ouvrir le port UDP local : {exc}")
        sock.close()
        return False
    sock.settimeout(1.0)

    try:
        # -r <device> <freqs...> doit etre EN DERNIER sur la ligne de
        # commande -- les frequences sont des arguments positionnels
        # consommes jusqu'a la fin de argv. Les mettre avant d'autres
        # options fait que ces options sont lues comme des frequences
        # supplementaires ("Frequencies too far apart", bug trouve en
        # testant l'integration).
        proc = subprocess.Popen(
            ["acarsdec", "-e", "-j", f"127.0.0.1:{ACARS_UDP_PORT}", "-o", "0",
             "-r", "0"] + ACARS_FREQUENCIES_MHZ,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError:
        print("[acars_watch] binaire acarsdec introuvable")
        sock.close()
        return False

    announced_any = False
    try:
        time.sleep(1.5)  # laisse acarsdec initialiser le dongle avant de lire
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            print(f"[acars_watch] acarsdec s'est arrete immediatement : {stderr[-300:]}")
            return False

        deadline = time.time() + dwell_sec
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            try:
                msg = json.loads(data.decode(errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if _process_message(msg):
                announced_any = True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        sock.close()

    return announced_any
