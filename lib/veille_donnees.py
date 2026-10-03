"""Outils communs aux veilles de donnees radio (APRS, capteurs 433/868 MHz,
VDL2) : annonce journalisee, anti-repetition, archive, distances.

Meme discipline que l'ACARS (watchers/acars_watch.py) : chaque veille est
une fenetre d'ecoute placee dans le cycle du scanner par le systeme
d'attention, jamais en parallele d'une autre ecoute (un seul dongle).
"""
import json
import math
import os
import time
from datetime import datetime

import audio_lock
import radio_log
import reglages
import sound_player
from sound_paths import RADIO_LOG_DIR
from phrases import Etiquettes, dire, nombre
from speak import speak_with_lead_sound

ARCHIVE_DIR = os.path.join(RADIO_LOG_DIR, "donnees")
EARTH_RADIUS_KM = 6371.0
DIRECTIONS = ["nord", "nord-est", "est", "sud-est", "sud", "sud-ouest", "ouest", "nord-ouest"]
DIRECTION_VERS = Etiquettes("direction.vers", {d: (f"à l'{d}" if d[0] in "eo" else f"au {d}") for d in DIRECTIONS},
                            "où se trouve quelque chose, après une distance : à 12 kilomètres au nord")
SPELL = Etiquettes("epeler", {"-": "tiret", "/": "barre"}, "signe lu dans un indicatif épelé")


def announce(text, sound, label_for_log, tag):
    """Annonce parlee avec son d'accompagnement, captee dans le journal
    audio (comme une reception radio), sous le verrou audio partage."""
    print(f"[{tag}] annonce : {text}")
    log_session = radio_log.RadioLogSession(label_for_log)
    sound_player.set_recording_session(log_session)
    lock = audio_lock.acquire("donnees")
    try:
        speak_with_lead_sound(sound, text, label=tag)
    finally:
        audio_lock.release(lock)
        sound_player.set_recording_session(None)
        log_session.finalize()


def archive(kind, record):
    """Ajoute une ligne JSON au fichier du jour (NAS). Jamais bloquant."""
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        path = os.path.join(ARCHIVE_DIR, f"{kind}_{datetime.now():%Y-%m-%d}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(record, t=time.time()), ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        print(f"[veille_donnees] archive {kind} impossible : {exc}")


class Memory:
    """Etat persistant d'une veille (fichier JSON dans /tmp) : dernieres
    annonces, stations connues..."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[veille_donnees] etat non ecrit ({self.path}) : {exc}")

    def due(self, key, every_sec):
        """Vrai (et note l'instant) si rien n'a ete annonce pour key depuis every_sec."""
        stamps = self.data.setdefault("_annonces", {})
        now = time.time()
        if now - stamps.get(key, 0) < every_sec:
            return False
        stamps[key] = now
        # menage : au-dela d'une semaine, on oublie
        for k in [k for k, t in stamps.items() if now - t > 7 * 86400]:
            del stamps[k]
        return True


def home():
    lieu = reglages.get("lieu")
    return float(lieu["lat"]), float(lieu["lon"])


def distance_km(lat, lon):
    lat0, lon0 = home()
    p0, p1 = math.radians(lat0), math.radians(lat)
    dp, dl = p1 - p0, math.radians(lon - lon0)
    a = math.sin(dp / 2) ** 2 + math.cos(p0) * math.cos(p1) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def direction(lat, lon):
    lat0, lon0 = home()
    y = math.sin(math.radians(lon - lon0)) * math.cos(math.radians(lat))
    x = (math.cos(math.radians(lat0)) * math.sin(math.radians(lat))
         - math.sin(math.radians(lat0)) * math.cos(math.radians(lat)) * math.cos(math.radians(lon - lon0)))
    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
    return DIRECTIONS[int((bearing + 22.5) // 45) % 8]


def where(lat, lon):
    """ "a 8 kilometres au nord-est" """
    km = distance_km(lat, lon)
    if km < 1:
        return dire("donnees.moins_un_kilometre")
    return dire("meteo.a_distance", distance=nombre(round(km), "kilomètre"), direction=DIRECTION_VERS[direction(lat, lon)])


def spell(code):
    """Indicatif lu caractere par caractere : "F4ABC-9" -> "F 4 A B C tiret 9"."""
    return " ".join(SPELL.get(c, c) for c in code.upper())


def fr_number(value, decimals=0):
    text = f"{value:.{decimals}f}"
    return text.replace(".", ",")
