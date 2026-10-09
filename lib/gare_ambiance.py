"""Habillage sonore de la gare (2026-10-08) : un lit d'ambiance en boucle,
a VOLUME FAIBLE, mixe en fond sous tout le reste (aplay sur "default", dmix).

- Quand la gare est OUVERTE (heures d'ouverture), le lit "normal" tourne en
  continu, tres bas -- juste de quoi habiller.
- PAR-DESSUS, une ambiance meteo (pluie / neige / vent) tourne en boucle si
  ce temps est REEL, d'apres la meteo en ligne ultralocale (Open-Meteo,
  position de la commune choisie), affinee par les releves de capteurs quand
  l'agregat local en a (lib/meteo_agregat).
- Gare fermee : tout est coupe.

Fichiers fournis par l'utilisateur dans server/train_scaner/ambi/<nom>/ :
normal, pluie, neige, vent. Dossier vide = pas de son, sans erreur. Chaque
lit est un ffmpeg (-stream_loop -1) decode en PCM, joue par aplay sur
"default" : il boucle et se mixe sans prendre le verrou audio (il doit
rester en fond, sous les annonces et le scanner).
"""
import os
import random
import subprocess
import time

import reglages
from sound_paths import TRAIN_SCANER_DIR

AMBI_DIR = os.path.join(TRAIN_SCANER_DIR, "ambi")
# Heures d'ouverture de la gare (locale) : lit coupe la nuit profonde.
OPEN_HOUR, CLOSE_HOUR = 5, 1          # ouverte de 5 h a 1 h du matin
RATE = 48000
NORMAL_VOLUME = 0.15                  # fond discret du hall (2026-10-09)
WEATHER_VOLUME = 0.18
WEATHER_SOURCES = ("pluie", "orage", "neige", "vent")
WEATHER_CACHE_SEC = 600
POLL_SEC = 60
_weather = {"t": 0, "actifs": set()}


def _station_open(now=None):
    h = (now or time.localtime()).tm_hour
    return h >= OPEN_HOUR or h < CLOSE_HOUR


def _files(name):
    folder = os.path.join(AMBI_DIR, name)
    try:
        return [os.path.join(folder, f) for f in os.listdir(folder)
                if f.lower().endswith((".mp3", ".wav", ".ogg", ".flac", ".m4a"))]
    except OSError:
        return []


def active_weather():
    """Ensemble des ambiances meteo reelles maintenant : sous-ensemble de
    {pluie, neige, vent}. Meteo en ligne (Open-Meteo), nuancee par l'agregat
    local (capteurs) quand il existe. Cache WEATHER_CACHE_SEC."""
    if time.time() - _weather["t"] < WEATHER_CACHE_SEC:
        return _weather["actifs"]
    actifs = set()
    lieu = reglages.get("lieu")
    try:
        import requests
        cur = requests.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
            "latitude": lieu["lat"], "longitude": lieu["lon"], "timezone": "Europe/Paris",
            "current": "rain,showers,snowfall,wind_speed_10m,wind_gusts_10m,weather_code"}).json().get("current") or {}
        code = cur.get("weather_code")
        if code in STORM_CODES:
            actifs.add("orage")  # orage distinct de la pluie
        if (cur.get("rain") or 0) > 0.1 or (cur.get("showers") or 0) > 0.1 or code in RAIN_CODES:
            actifs.add("pluie")  # peut se superposer a l'orage (il pleut pendant l'orage)
        if (cur.get("snowfall") or 0) > 0.02 or code in SNOW_CODES:
            actifs.add("neige")
        gust = cur.get("wind_gusts_10m") or 0
        if gust >= 40 or (cur.get("wind_speed_10m") or 0) >= 28:
            actifs.add("vent")
    except Exception as exc:
        print(f"[gare_ambiance] meteo en ligne : {exc!r}")
    actifs |= _sensor_weather()
    _weather.update(t=time.time(), actifs=actifs)
    return actifs


RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}
STORM_CODES = {95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}


def _sensor_weather():
    """Nuance par les capteurs SI l'agregat local les a consultes et montre une
    variation (pluie qui tombe, rafales) -- meteo ultralocale (2026-10-08)."""
    try:
        import meteo_agregat
        state = meteo_agregat.agreger()
        out = set()
        # agreger : "pluie" = liste des sources qui mesurent de la pluie dans
        # l'heure (non vide => il pleut vraiment, pres d'ici).
        if state.get("pluie"):
            out.add("pluie")
        # "vent" = {"valeur": vitesse} ou None (pas de rafale dans l'agregat).
        vent = (state.get("vent") or {}).get("valeur")
        if isinstance(vent, (int, float)) and vent >= 28:
            out.add("vent")
        return out
    except Exception:
        return set()


class _Bed:
    """Un lit d'ambiance : ffmpeg(-stream_loop -1) -> aplay sur 'default'."""
    def __init__(self, name, volume):
        self.name, self.volume = name, volume
        self.ff = self.ap = self.path = None

    def running(self):
        return self.ff is not None and self.ff.poll() is None and self.ap is not None and self.ap.poll() is None

    def start(self):
        files = _files(self.name)
        if not files:
            return False
        self.path = random.choice(files)
        try:
            self.ff = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-stream_loop", "-1", "-i", self.path,
                 "-af", f"volume={self.volume}", "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.ap = subprocess.Popen(
                ["aplay", "-q", "-D", "default", "-f", "S16_LE", "-r", str(RATE), "-c", "2"],
                stdin=self.ff.stdout, stderr=subprocess.DEVNULL)
            self.ff.stdout.close()  # aplay tient le tube
            print(f"[gare_ambiance] lit '{self.name}' demarre ({os.path.basename(self.path)}, vol {self.volume})")
            return True
        except OSError as exc:
            print(f"[gare_ambiance] lit '{self.name}' : {exc!r}")
            return False

    def stop(self):
        for p in (self.ap, self.ff):
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=3)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        if self.ff is not None:
            print(f"[gare_ambiance] lit '{self.name}' coupe")
        self.ff = self.ap = None


def watch():
    beds = {name: _Bed(name, WEATHER_VOLUME if name in WEATHER_SOURCES else NORMAL_VOLUME)
            for name in ("normal",) + WEATHER_SOURCES}
    while True:
        try:
            if _station_open():
                wanted = {"normal"} | (active_weather() & set(WEATHER_SOURCES))
            else:
                wanted = set()
            for name, bed in beds.items():
                if name in wanted:
                    if not bed.running():
                        bed.start()  # (re)demarre ; dossier vide -> rien, sans erreur
                elif bed.running() or bed.ff is not None:
                    bed.stop()
        except Exception as exc:
            print(f"[gare_ambiance] {exc!r}")
        time.sleep(POLL_SEC)
