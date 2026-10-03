"""Planeurs, ULM, parapentes, helicopteres legers... par l'Open Glider
Network (OGN) : reseau libre de recepteurs FLARM, FANET et OGN, dont les
positions sont diffusees en direct sur un serveur APRS public. Lecture
seule par internet (aucun temps pris sur la cle radio), dans un fil de la
veille avions (adsb_watch).

Annonce un appareil a moins de RADIUS_KM, une fois par ANNOUNCE_EVERY_SEC,
avec son type (code dans l'identifiant OGN), sa distance, sa direction et
son altitude. Les relais d'ADS-B (deja couverts par OpenSky et l'antenne)
et les stations receptrices sont ignores.
"""
import re
import socket
import threading
import time

import aprslib

import veille_donnees as vd
from sound_paths import FLIGHT_SCANER_DIR
from phrases import Etiquettes, dire
from phrases import dire

SERVER = ("aprs.glidernet.org", 14580)
RADIUS_KM = 30
ANNOUNCE_EVERY_SEC = 3600
RECONNECT_SEC = 60
SOUND = f"{FLIGHT_SCANER_DIR}/simple_contact.mp3"
STATE_PATH = "/tmp/ogn_watch_state.json"
IGNORED_DEST = ("OGNSDR", "OGADSB", "OGADSL", "OGNDSX")
AIRCRAFT_ID = re.compile(r"\bid([0-9A-F]{2})([0-9A-F]{6})\b")
TYPES = Etiquettes("planeur.type", {1: "planeur", 2: "avion remorqueur", 3: "hélicoptère", 4: "parachutiste",
         5: "avion largueur", 6: "deltaplane", 7: "parapente", 8: "avion léger", 9: "avion à réaction",
         11: "ballon", 12: "dirigeable", 13: "drone", 15: "objet fixe"},
                   "type d'aéronef du réseau des planeurs")

_memory = vd.Memory(STATE_PATH)


def _handle(line):
    try:
        packet = aprslib.parse(line)
    except (aprslib.ParseError, aprslib.UnknownFormat, ValueError):
        return
    if packet.get("to", "").startswith(IGNORED_DEST) or packet.get("latitude") is None:
        return
    m = AIRCRAFT_ID.search(packet.get("comment", "") or "")
    if not m:
        return
    kind = TYPES.get((int(m.group(1), 16) >> 2) & 0x0F) or dire("planeur.aeronef")
    address = m.group(2)
    lat, lon = packet["latitude"], packet["longitude"]
    if vd.distance_km(lat, lon) > RADIUS_KM:
        return
    vd.archive("ogn", packet)
    if not _memory.due(f"ogn|{address}", ANNOUNCE_EVERY_SEC):
        return
    altitude = packet.get("altitude")
    text = dire("planeur.signale_reseau_planeurs", capitalize=kind.capitalize(), where=vd.where(lat, lon))
    if altitude:
        text += dire("planeur.altitude_metres", round=round(altitude))
    speed = packet.get("speed")
    if speed:
        text += dire("planeur.kilometres_heure", round=round(speed))
    vd.announce(text + ".", SOUND, f"OGN_{address}", "ogn_watch")
    _memory.save()


def _run():
    lat, lon = vd.home()
    login = (f"user NEARFLD pass -1 vers nearfield-anchor 1.0 "
             f"filter r/{lat:.4f}/{lon:.4f}/{RADIUS_KM}\r\n")
    while True:
        try:
            with socket.create_connection(SERVER, timeout=60) as sock:
                sock.sendall(login.encode())
                buf = b""
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.decode("utf-8", errors="replace").strip()
                        if line and not line.startswith("#"):
                            _handle(line)
        except OSError as exc:
            print(f"[ogn_watch] connexion perdue : {exc}")
        time.sleep(RECONNECT_SEC)


def start():
    threading.Thread(target=_run, daemon=True, name="ogn").start()
