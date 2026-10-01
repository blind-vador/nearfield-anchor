"""Veille des capteurs sans fil 433,92 et 868,3 MHz (rtl_433) : stations
meteo et thermometres, detecteurs de fumee, sonnettes, telecommandes...
tout ce qui emet en clair a portee du Pi.

Une frequence par passage, en alternance. Annonces :
- alarmes (fumee, eau, intrusion...) : a chaque evenement (anti-repetition
  ALARM_EVERY_SEC) ;
- sonnettes et boutons : a chaque appui (BUTTON_EVERY_SEC) ;
- capteur jamais vu depuis NEW_EVERY_SEC : presente une fois, avec ses
  valeurs ;
- capteurs de pneus (voitures qui passent) : jamais annonces, archives.
Les derniers releves sont publies (READINGS_PATH) pour la console et le
flash ; tout est archive.
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime

import veille_donnees as vd
import voitures
from sound_paths import RADIO_SCANER_DIR

FREQUENCIES = ["433.92M", "868.3M"]
SOUND = os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3")
STATE_PATH = "/tmp/capteurs_watch_state.json"
READINGS_PATH = "/tmp/capteurs_releves.json"
NEW_EVERY_SEC = 7 * 86400
ALARM_EVERY_SEC = 120
BUTTON_EVERY_SEC = 60
READINGS_KEEP_SEC = 6 * 3600

ALARM_WORDS = ("smoke", "fumee", "alarm", "water", "leak", "flood", "tamper", "intrusion", "gas", "co_")
BUTTON_WORDS = ("doorbell", "bell", "button", "remote", "switch", "keyfob")
IGNORED_KEYS = {"time", "model", "id", "channel", "mic", "mod", "freq", "freq1", "freq2", "rssi",
                "snr", "noise", "protocol", "subtype", "flags", "raw", "code", "cmd", "rbt"}

_memory = vd.Memory(STATE_PATH)
_next = 0
# Une meme emission peut etre reconnue par plusieurs decodeurs de rtl_433
# (2026-10-01 : "Rubicson-Temperature" et "Solight-TE44", meme identifiant,
# canal et temperature, a la meme seconde : UN thermometre annonce deux
# fois). Une trame identique d'un autre modele dans DOUBLE_DECODE_SEC est
# ignoree.
DOUBLE_DECODE_SEC = 3
_recent_frames = {}


def _key(event):
    return "|".join(str(event.get(k, "")) for k in ("model", "id", "channel"))


def _values(event):
    parts = []
    if event.get("temperature_C") is not None:
        parts.append(f"{vd.fr_number(event['temperature_C'], 1)} degrés")
    if event.get("humidity") is not None:
        parts.append(f"humidité {event['humidity']} pour cent")
    if event.get("wind_avg_km_h") is not None:
        parts.append(f"vent {round(event['wind_avg_km_h'])} kilomètres heure")
    if event.get("rain_mm") is not None:
        parts.append(f"pluie cumulée {vd.fr_number(event['rain_mm'], 1)} millimètres")
    if event.get("battery_ok") == 0:
        parts.append("pile faible")
    return ", ".join(parts)


def _kind(event):
    blob = json.dumps(event).lower()
    if "tpms" in blob or "pressure_kpa" in blob or "pressure_psi" in blob:
        return "pneu"
    if any(w in blob for w in ALARM_WORDS) and event.get("alarm", 1) not in (0, "0", False):
        return "alarme"
    if any(w in blob for w in BUTTON_WORDS):
        return "bouton"
    if "temperature_c" in blob or "humidity" in blob:
        return "meteo"
    return "autre"


def _name(event):
    return str(event.get("model", "capteur")).replace("-", " ").replace("_", " ")


def _publish(key, event):
    try:
        with open(READINGS_PATH, encoding="utf-8") as f:
            readings = json.load(f)
    except (OSError, ValueError):
        readings = {}
    now = time.time()
    readings[key] = {"nom": _name(event), "valeurs": {k: v for k, v in event.items() if k not in IGNORED_KEYS},
                     "t": now}
    readings = {k: v for k, v in readings.items() if now - v.get("t", 0) < READINGS_KEEP_SEC}
    try:
        tmp = READINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(readings, f, ensure_ascii=False)
        os.replace(tmp, READINGS_PATH)
    except OSError:
        pass


def _pressure_kpa(event):
    if event.get("pressure_kPa") is not None:
        return float(event["pressure_kPa"])
    if event.get("pressure_PSI") is not None:
        return float(event["pressure_PSI"]) * 6.895
    return None


def _received_at(event, at):
    """Heure de reception notee par rtl_433 (identique pour les decodages
    multiples d'une meme emission), sinon l'heure de lecture."""
    try:
        return datetime.strptime(event["time"], "%Y-%m-%d %H:%M:%S").timestamp()
    except (KeyError, TypeError, ValueError):
        return at


def _double_decode(event, at):
    at = _received_at(event, at)
    values = tuple(event.get(k) for k in ("id", "channel", "temperature_C", "humidity", "pressure_kPa"))
    if values[0] is None:
        return False
    for key in [k for k, (t, _) in _recent_frames.items() if at - t > 60]:
        del _recent_frames[key]
    previous = _recent_frames.get(values)
    _recent_frames[values] = (at, event.get("model"))
    return previous is not None and at - previous[0] <= DOUBLE_DECODE_SEC and previous[1] != event.get("model")


def _process(event, freq, tpms, at):
    if _double_decode(event, at):
        vd.archive("capteurs", dict(event, bande=freq, doublon=True))
        return False
    vd.archive("capteurs", dict(event, bande=freq))
    key = _key(event)
    kind = _kind(event)
    if kind == "pneu":
        # Pas d'annonce directe : apprentissage des vehicules (lib/voitures.py).
        tpms.append((at, key, _pressure_kpa(event)))
        return False
    _publish(key, event)
    values = _values(event)
    if kind == "alarme" and _memory.due(f"alarme|{key}", ALARM_EVERY_SEC):
        vd.announce(f"Alarme d'un capteur sans fil proche : {_name(event)}." + (f" {values}." if values else ""),
                    SOUND, f"capteur_alarme_{key}", "capteurs_watch")
        return True
    if kind == "bouton" and _memory.due(f"bouton|{key}", BUTTON_EVERY_SEC):
        vd.announce(f"Appui sur une sonnette ou télécommande sans fil : {_name(event)}.", SOUND,
                    f"capteur_bouton_{key}", "capteurs_watch")
        return True
    if _memory.due(f"nouveau|{key}", NEW_EVERY_SEC):
        what = {"meteo": "thermomètre ou station météo", "alarme": "détecteur", "bouton": "bouton"}.get(kind, "capteur")
        freq_txt = "433" if freq.startswith("433") else "868"
        number = f", numéro {event['id']}" if event.get("id") is not None else ""
        vd.announce(f"Nouveau {what} sans fil en {freq_txt} mégahertz : {_name(event)}{number}"
                    + (f", {values}" if values else "") + ".", SOUND, f"capteur_nouveau_{key}", "capteurs_watch")
        return True
    return False


def _reader(proc, events):
    for raw in proc.stdout:
        try:
            events.append((time.time(), json.loads(raw)))
        except ValueError:
            continue


def _cars(tpms, freq, listened_sec):
    """Apprentissage et annonces des vehicules (pneus) apres une passe."""
    if not freq.startswith("433"):
        return False
    try:
        voitures.record_listening(listened_sec)
        sentences = voitures.learn(tpms)
    except Exception as exc:  # jamais bloquant pour le reste de la veille
        print(f"[capteurs_watch] voitures : {exc!r}")
        return False
    announced = False
    for sentence in sentences:
        if _memory.due(f"voiture|{sentence[:40]}", 600):
            vd.announce(sentence, SOUND, "voiture", "capteurs_watch")
            announced = True
    return announced


def scan_window(dwell_sec=30):
    """Une passe rtl_433, sur la frequence suivante. Rend True si annonce."""
    global _next
    freq = FREQUENCIES[_next % len(FREQUENCIES)]
    _next += 1
    try:
        proc = subprocess.Popen(["rtl_433", "-d", "0", "-f", freq, "-F", "json", "-M", "level",
                                 "-C", "si", "-T", str(int(dwell_sec))],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("[capteurs_watch] rtl_433 introuvable")
        return False
    events, tpms = [], []
    reader = threading.Thread(target=_reader, args=(proc, events), daemon=True)
    reader.start()
    announced = False
    count = 0
    started = time.time()
    try:
        deadline = time.time() + dwell_sec + 5
        while time.time() < deadline and (proc.poll() is None or events):
            time.sleep(0.2)
            while events:
                count += 1
                at, event = events.pop(0)
                if _process(event, freq, tpms, at):
                    announced = True
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        reader.join(timeout=2)
        while events:
            count += 1
            at, event = events.pop(0)
            if _process(event, freq, tpms, at):
                announced = True
        if _cars(tpms, freq, min(dwell_sec, time.time() - started)):
            announced = True
        _memory.save()
    if count:
        print(f"[capteurs_watch] {count} trame(s) capteurs sur {freq}")
    return announced
