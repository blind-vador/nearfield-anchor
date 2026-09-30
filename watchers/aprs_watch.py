"""Veille APRS (144,800 MHz) : positions, stations meteo, messages et
bulletins des radioamateurs, reseau public tres actif en France.

rtl_fm (FM etroite) -> multimon-ng -A (AFSK1200, sortie texte TNC2) ->
aprslib pour decoder. Fenetre d'ecoute placee dans le cycle du scanner par
le systeme d'attention, comme l'ACARS.

Annonces (le reste est seulement archive, voir veille_donnees.archive) :
- messages adresses et bulletins : toujours (anti-repetition 1 h) ;
- station jamais annoncee depuis STATION_EVERY_SEC : indicatif, distance,
  direction, en mouvement ou non ;
- station qui passe a moins de NEAR_KM : re-annoncee (NEAR_EVERY_SEC) ;
- station meteo : releve toutes les WX_EVERY_SEC par station.
Les derniers releves meteo sont publies pour le flash et la console
(WX_PATH).
"""
import json
import os
import re
import subprocess
import threading
import time

import aprslib

import veille_donnees as vd
from sound_paths import RADIO_SCANER_DIR

APRS_FREQ_HZ = 144_800_000
SAMPLE_RATE = 22050
SOUND = os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3")
STATE_PATH = "/tmp/aprs_watch_state.json"
WX_PATH = "/tmp/meteo_stations_aprs.json"
STATION_EVERY_SEC = 6 * 3600
NEAR_KM = 3.0
NEAR_EVERY_SEC = 1800
WX_EVERY_SEC = 3 * 3600
MESSAGE_EVERY_SEC = 3600
MAX_DISTANCE_KM = 150      # au-dela : trajet anormal (digipeteur lointain), archive seulement
WX_KEEP_SEC = 3 * 3600
LINE = re.compile(r"^APRS: (\S+>.*)$")

_memory = vd.Memory(STATE_PATH)


def _weather_text(wx):
    parts = []
    if wx.get("temperature") is not None:
        parts.append(f"{vd.fr_number(wx['temperature'])} degrés")
    if wx.get("humidity") is not None:
        parts.append(f"humidité {int(wx['humidity'])} pour cent")
    if wx.get("wind_speed") is not None:
        parts.append(f"vent {round(wx['wind_speed'] * 3.6)} kilomètres heure")
    if wx.get("rain_1h"):
        parts.append(f"{vd.fr_number(wx['rain_1h'], 1)} millimètres de pluie en une heure")
    if wx.get("pressure") is not None:
        parts.append(f"pression {round(wx['pressure'])} hectopascals")
    return ", ".join(parts)


def _publish_weather(call, lat, lon, wx):
    try:
        with open(WX_PATH, encoding="utf-8") as f:
            stations = json.load(f)
    except (OSError, ValueError):
        stations = {}
    now = time.time()
    stations[call] = {"lat": lat, "lon": lon, "meteo": wx, "t": now}
    stations = {k: v for k, v in stations.items() if now - v.get("t", 0) < WX_KEEP_SEC}
    try:
        tmp = WX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stations, f)
        os.replace(tmp, WX_PATH)
    except OSError:
        pass


def _process(packet):
    """Annonce ce qui le merite. Rend True si une annonce a eu lieu."""
    vd.archive("aprs", packet)
    if packet.get("format") == "thirdparty" and isinstance(packet.get("subpacket"), dict):
        # Relaye par une passerelle (internet, LoRa) : le contenu reste local.
        packet = packet["subpacket"]
    call = packet.get("from", "")
    fmt = packet.get("format")
    if fmt == "message" and packet.get("message_text") and not packet.get("response"):
        to = packet.get("addresse", "")
        text = packet["message_text"].strip()
        if _memory.due(f"msg|{call}|{text[:60]}", MESSAGE_EVERY_SEC):
            vd.announce(f"Message APRS de {vd.spell(call)} pour {vd.spell(to)} : {text}", SOUND,
                        f"APRS_message_{call}", "aprs_watch")
            return True
        return False
    if fmt == "bulletin" or packet.get("bid") is not None:
        text = (packet.get("message_text") or "").strip()
        if text and _memory.due(f"bln|{call}|{text[:60]}", MESSAGE_EVERY_SEC):
            vd.announce(f"Bulletin APRS de {vd.spell(call)} : {text}", SOUND, f"APRS_bulletin_{call}", "aprs_watch")
            return True
        return False
    lat, lon = packet.get("latitude"), packet.get("longitude")
    if lat is None or lon is None:
        return False
    km = vd.distance_km(lat, lon)
    if km > MAX_DISTANCE_KM:
        return False
    name = (packet.get("object_name") or packet.get("item_name") or call).strip()
    wx = packet.get("weather")
    if wx:
        _publish_weather(name, lat, lon, wx)
        details = _weather_text(wx)
        if details and _memory.due(f"wx|{name}", WX_EVERY_SEC):
            vd.announce(f"Station météo APRS {vd.spell(name)}, {vd.where(lat, lon)} : {details}.", SOUND,
                        f"APRS_meteo_{name}", "aprs_watch")
            return True
        return False
    speed = packet.get("speed") or 0
    moving = speed >= 5
    text = f"Station APRS {vd.spell(name)}, {vd.where(lat, lon)}"
    if moving:
        text += f", en mouvement à {round(speed)} kilomètres heure"
    comment = (packet.get("comment") or packet.get("status") or "").strip()
    if comment and len(comment) <= 60:
        text += f". {comment}"
    first = _memory.due(f"station|{name}", STATION_EVERY_SEC)
    near = km <= NEAR_KM and _memory.due(f"near|{name}", NEAR_EVERY_SEC)
    if first or near:
        prefix = "Tout près : " if near and not first else ""
        vd.announce(prefix + text + ".", SOUND, f"APRS_{name}", "aprs_watch")
        return True
    return False


def _reader(proc, packets):
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        m = LINE.match(line)
        if not m:
            continue
        try:
            packets.append(aprslib.parse(m.group(1)))
        except (aprslib.ParseError, aprslib.UnknownFormat, ValueError) as exc:
            vd.archive("aprs", {"raw": m.group(1), "erreur": str(exc)[:100]})


def scan_window(dwell_sec=30):
    """Une passe d'ecoute APRS. Rend True si au moins une annonce a eu lieu."""
    try:
        rtl = subprocess.Popen(["rtl_fm", "-f", str(APRS_FREQ_HZ), "-M", "fm", "-s", str(SAMPLE_RATE),
                                "-l", "0", "-E", "dc", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("[aprs_watch] rtl_fm introuvable")
        return False
    try:
        mm = subprocess.Popen(["multimon-ng", "-A", "-a", "AFSK1200", "-t", "raw", "-"],
                              stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("[aprs_watch] multimon-ng introuvable")
        rtl.kill()
        rtl.wait()
        return False
    rtl.stdout.close()
    packets = []
    reader = threading.Thread(target=_reader, args=(mm, packets), daemon=True)
    reader.start()
    announced = False
    count = 0
    try:
        deadline = time.time() + dwell_sec
        while time.time() < deadline:
            time.sleep(0.2)
            while packets:
                count += 1
                if _process(packets.pop(0)):
                    announced = True
    finally:
        for p in (rtl, mm):
            if p.poll() is None:
                p.terminate()
        for p in (rtl, mm):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        while packets:  # recus pendant l'arret
            count += 1
            if _process(packets.pop(0)):
                announced = True
        _memory.save()
    if count:
        print(f"[aprs_watch] {count} paquet(s) APRS decodes en {dwell_sec:.0f} s")
    return announced
