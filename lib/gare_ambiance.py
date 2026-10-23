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

Volume (2026-10-22, commande "volume") : le son passe par un relais qui
applique au vol les curseurs "ambiance du hall" (lit normal) ou "ambiance
meteo" (pluie, orage, vent, neige) de la gare : un changement s'entend tout
de suite, sans attendre la boucle suivante.
"""
import audioop
import os
import random
import subprocess
import threading
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
CROSSFADE_SEC = 5.0                  # fondus croises entre boucles (2026-10-09)
RELAIS_OCTETS = RATE * 2 * 2 // 10   # 100 ms de stereo 16 bits
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


def _duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10).stdout.strip()
        return float(out)
    except Exception:
        return None


class _Bed:
    """Un lit d'ambiance en boucle a FONDUS CROISES (2026-10-09) : chaque
    segment a un fondu d'ouverture et de fermeture de CROSSFADE_SEC, et le
    segment suivant est lance CROSSFADE_SEC avant la fin du precedent -- les
    deux fondus se superposent (dmix mixe), donc aucune couture entre boucles.
    Un thread dedie enchaine les segments ; chaque segment se termine seul en
    fin de fichier, au plus deux se recouvrent."""
    def __init__(self, name, volume):
        self.name, self.volume = name, volume
        self._stop = threading.Event()
        self._thread = None
        self._procs = []  # (ffmpeg, aplay) en cours

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if not _files(self.name):
            return False
        if self.running():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"bed-{self.name}", daemon=True)
        self._thread.start()
        return True

    def _play_segment(self, path, dur):
        fade = min(CROSSFADE_SEC, max(0.5, dur / 3.0)) if dur else CROSSFADE_SEC
        af = f"volume={self.volume},afade=t=in:st=0:d={fade:.2f}"
        if dur:
            af += f",afade=t=out:st={max(0.0, dur - fade):.2f}:d={fade:.2f}"
        try:
            ff = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", path,
                 "-af", af, "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            ap = subprocess.Popen(
                ["aplay", "-q", "-D", "default", "-f", "S16_LE", "-r", str(RATE), "-c", "2"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            threading.Thread(target=self._relais, args=(ff, ap), daemon=True).start()
            self._procs.append((ff, ap))
            return fade
        except OSError as exc:
            print(f"[gare_ambiance] lit '{self.name}' : {exc!r}")
            return None

    def _relais(self, ff, ap):
        """ffmpeg -> aplay, au volume du curseur de la commande "volume"."""
        import volumes
        canal = "meteo" if self.name in WEATHER_SOURCES else "ambiance"
        try:
            while True:
                data = ff.stdout.read(RELAIS_OCTETS)
                if not data:
                    break
                f = volumes.facteur("trains", canal)
                if f != 1.0:
                    data = audioop.mul(data[: len(data) - len(data) % 2], 2, f)
                ap.stdin.write(data)
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            try:
                ap.stdin.close()
            except OSError:
                pass

    def _prune(self):
        """Recupere les segments termines (evite les zombies), garde les vivants."""
        alive = []
        for ff, ap in self._procs:
            if ap.poll() is None:
                alive.append((ff, ap))
            else:
                if ff.poll() is None:  # ffmpeg devrait avoir fini (EOF), au cas ou
                    try:
                        ff.terminate()
                    except Exception:
                        pass
                for p in (ff, ap):
                    try:
                        p.wait(timeout=1)
                    except Exception:
                        pass
        self._procs = alive

    def _run(self):
        print(f"[gare_ambiance] lit '{self.name}' demarre "
              f"(boucle a fondus croises {CROSSFADE_SEC:g}s, vol {self.volume})")
        while not self._stop.is_set():
            files = _files(self.name)
            if not files:
                break
            # variete, et jamais le meme fichier que le segment precedent (2026-10-22) :
            # au fondu croise, deux copies du meme son se chevauchaient
            autres = [f for f in files if f != getattr(self, "_dernier", None)]
            path = random.choice(autres or files)
            self._dernier = path
            dur = _duration(path)
            fade = self._play_segment(path, dur)
            self._prune()
            if fade is None:
                if self._stop.wait(5):  # echec de lancement : repli, on retente
                    break
                continue
            if not dur:
                if self._stop.wait(30):  # duree inconnue : pas de recouvrement precis
                    break
                continue
            # relance le segment suivant `fade` s AVANT la fin -> recouvrement
            if self._stop.wait(max(0.5, dur - fade)):
                break
        self._terminate_all()

    def _terminate_all(self):
        for ff, ap in self._procs:
            for p in (ap, ff):
                if p.poll() is None:
                    try:
                        p.terminate()
                        p.wait(timeout=2)
                    except Exception:
                        try:
                            p.kill()
                        except Exception:
                            pass
        self._procs = []

    def stop(self):
        if not self.running() and not self._procs:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=CROSSFADE_SEC + 2)
        self._terminate_all()
        print(f"[gare_ambiance] lit '{self.name}' coupe")


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
                elif bed.running():
                    bed.stop()
        except Exception as exc:
            print(f"[gare_ambiance] {exc!r}")
        time.sleep(POLL_SEC)
