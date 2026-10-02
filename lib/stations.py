"""Stations probables (idee de l'utilisateur 2026-10-02) : une capture notee
tout au vert (touche V, ou note detaillee sans defaut) devient une station
probable, gardee sur la carte SD du Pi.

- Recentrage : quand le scanner tombe pres d'une station probable, il
  mesure ou est vraiment le centre de l'emission (court releve I/Q, hors de
  la raie centrale du tuner) et s'y cale, puis affine la frequence gardee.
  Fini les ecoutes en bord de canal, sans grille officielle, sur toute bande.
- Interet : chaque capture pres de la station le fait monter ; il redescend
  lentement (demi-vie HALF_LIFE_DAYS) sans jamais s'eteindre (plancher) :
  la station est revisitee d'autant plus souvent qu'elle emet souvent, et
  au moins toutes les MAX_REVISIT_SEC ("on revient gratter").
- Habitudes : jour de la semaine et heure de chaque capture ; une habitude
  (au moins HABIT_MIN captures le meme jour et la meme heure, sur des dates
  differentes) fait revisiter la station a chaque tour a ce moment-la.
  Utile pour les pirates : "elle emet le dimanche vers 20 heures".
"""
import datetime
import fcntl
import json
import math
import os
import subprocess
import time

import numpy as np

STORE_PATH = "/home/vador/autopilot/stations_probables.json"
LOCK_PATH = "/tmp/stations_probables.lock"
TOLERANCE_HZ = {"wfm": 100_000, "am": 6_000}       # "meme station" a moins de... ; 12,5 kHz sinon
HALF_LIFE_DAYS = 14
FLOOR = 0.2
MIN_REVISIT_SEC = 10 * 60
MAX_REVISIT_SEC = 6 * 3600
HABIT_MIN = 2
CENTER_RATE = 1_024_000
CENTER_OFFSET_HZ = 250_000  # le signal loin de la raie parasite au centre du tuner
CENTER_SAMPLES = 262_144     # un quart de seconde
SEARCH_HZ = {"wfm": 120_000, "am": 8_000}           # 15 kHz sinon
WIDTH_HZ = {"wfm": 180_000, "am": 10_000}            # largeur typique, 12,5 kHz sinon
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


class _Locked:
    def __enter__(self):
        self.f = open(LOCK_PATH, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.data = load()
        return self.data

    def __exit__(self, *exc):
        if exc[0] is None:
            os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
            tmp = STORE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, STORE_PATH)
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def load():
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"stations": []}


def _tolerance(mode):
    return TOLERANCE_HZ.get(mode, 12_500)


def _near(data, freq_hz, mode=None):
    best = None
    for s in data["stations"]:
        if mode and s["mode"] != mode:
            continue
        gap = abs(s["freq_hz"] - freq_hz)
        if gap <= _tolerance(s["mode"]) and (best is None or gap < abs(best["freq_hz"] - freq_hz)):
            best = s
    return best


def interest(station, now=None):
    now = now or time.time()
    decay = math.log(2) / (HALF_LIFE_DAYS * 86400)
    return FLOOR + sum(math.exp(-decay * (now - t)) for t in station.get("captures", []))


def habits(station):
    """[(jour, heure, nombre)] : meme jour et meme heure, dates differentes."""
    slots = {}
    for t in station.get("captures", []):
        d = datetime.datetime.fromtimestamp(t)
        slots.setdefault((d.weekday(), d.hour), set()).add(d.date())
    return sorted(((wd, h, len(dates)) for (wd, h), dates in slots.items() if len(dates) >= HABIT_MIN),
                  key=lambda x: -x[2])


# --- creation et captures -------------------------------------------------------------

def is_all_green(note):
    """Note "tout au vert" : validation rapide, ou note detaillee sans defaut."""
    if note.get("validation_rapide"):
        return True
    r = note.get("reponses", {})
    return (r.get("lisibilite", 0) >= 4 and r.get("souffle") in ("aucun", "discret")
            and r.get("brouillage") == "aucun" and r.get("son") == "naturel" and r.get("stabilite") == "stable"
            and r.get("debut") == "complet" and r.get("fin") == "correcte" and r.get("merite") == "oui")


def register_from_note(note):
    """Appele par console/notation.py : rend True si une station est creee ou renforcee."""
    freq, mode = note.get("freq_hz"), note.get("mode")
    if not freq or not mode or note.get("source") == "airband" or not is_all_green(note):
        return False  # donnees, ou canal fixe RTLSDR-Airband : pas de recentrage a faire
    try:
        t = datetime.datetime.fromisoformat(note["capture_le"]).timestamp()
    except (KeyError, ValueError):
        t = time.time()
    with _Locked() as data:
        station = _near(data, freq, mode)
        if station is None:
            station = {"freq_hz": int(freq), "mode": mode, "bande": note.get("bande"), "label": note.get("label"),
                       "cree": time.time(), "centree": False, "captures": [], "visite": 0}
            data["stations"].append(station)
        if t not in station["captures"]:
            station["captures"] = sorted(station["captures"] + [t])[-500:]
    return True


def record_capture(freq_hz, mode):
    """Une capture du scanner pres d'une station probable : interet, habitude."""
    with _Locked() as data:
        station = _near(data, freq_hz, mode)
        if station:
            station["captures"] = (station["captures"] + [time.time()])[-500:]
            return True
    return False


# --- recentrage -------------------------------------------------------------------------

def measure_center(freq_hz, mode):
    """Centre reel de l'emission pres de freq_hz (Hz), ou None : court releve
    I/Q decale de CENTER_OFFSET_HZ, spectre moyenne, puis centre de gravite
    de l'energie au-dessus du bruit (porteuse la plus forte en AM)."""
    tune = int(freq_hz + CENTER_OFFSET_HZ)
    try:
        raw = subprocess.run(["rtl_sdr", "-f", str(tune), "-s", str(CENTER_RATE), "-g", "30",
                              "-n", str(CENTER_SAMPLES), "-"], capture_output=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if len(raw) < CENTER_SAMPLES:
        return None
    iq = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
    x = (iq[0::2] + 1j * iq[1::2])[: (len(iq) // 2 // 4096) * 4096].reshape(-1, 4096)
    spec = np.fft.fftshift((np.abs(np.fft.fft(x * np.hanning(4096), axis=1)) ** 2).mean(axis=0))
    bins = (np.arange(4096) - 2048) * CENTER_RATE / 4096 + tune  # frequence de chaque case
    search = SEARCH_HZ.get(mode, 15_000)
    window = np.abs(bins - freq_hz) <= search
    if window.sum() < 3:
        return None
    power_db = 10 * np.log10(spec[window] + 1e-9)
    floor = np.median(10 * np.log10(spec + 1e-9))
    above = power_db > floor + 6
    if not above.any():
        return None
    if mode == "am":
        return int(bins[window][np.argmax(power_db)])
    weights = 10 ** (power_db[above] / 10)
    return int((bins[window][above] * weights).sum() / weights.sum())


def center(freq_hz, mode):
    """Frequence ou ecouter : recentree si une station probable est proche,
    sinon freq_hz telle quelle. Affine la frequence gardee."""
    data = load()
    station = _near(data, freq_hz, mode)
    if station is None:
        return freq_hz
    measured = measure_center(freq_hz, mode)
    if measured is None or abs(measured - station["freq_hz"]) > _tolerance(mode):
        return station["freq_hz"] if station.get("centree") else freq_hz
    with _Locked() as data:
        s = _near(data, station["freq_hz"], mode)
        if s:
            # Premier centrage : on prend la mesure ; ensuite moyenne glissante.
            s["freq_hz"] = int(measured if not s.get("centree") else 0.7 * s["freq_hz"] + 0.3 * measured)
            s["centree"] = True
            freq = s["freq_hz"]
        else:
            freq = measured
    print(f"[stations] recentrage {freq_hz / 1e6:.4f} -> {freq / 1e6:.4f} MHz")
    return freq


# --- revisites ---------------------------------------------------------------------------

def bands(now=None):
    """Bandes "fm_channels" du scanner : les stations a revisiter maintenant,
    regroupees par mode. Delai entre deux visites selon l'interet ; a chaque
    tour pendant une heure d'habitude."""
    now = now or time.time()
    d = datetime.datetime.fromtimestamp(now)
    due = {}
    with _Locked() as data:
        for s in data["stations"]:
            habit_now = any(wd == d.weekday() and h == d.hour for wd, h, _ in habits(s))
            wait = max(MIN_REVISIT_SEC, min(MAX_REVISIT_SEC, MAX_REVISIT_SEC / (1 + 2 * interest(s, now))))
            if habit_now or now - s.get("visite", 0) >= wait:
                s["visite"] = now
                due.setdefault(s["mode"], []).append(s["freq_hz"])
    return [{"name": f"stations_{mode}", "name_spoken": "station probable", "type": "fm_channels",
             "channels": sorted(freqs), "mode": mode, "margin_db": 6.0, "span_hz": WIDTH_HZ.get(mode, 12_500)}
            for mode, freqs in due.items()]


def spoken_list():
    """Commande "stations" : les stations probables, de la plus suivie a la moins."""
    stations = sorted(load()["stations"], key=lambda s: -interest(s))
    if not stations:
        return ("Aucune station probable pour l'instant. Dans le journal radio, V sur une prise parfaite "
                "en crée une.")
    parts = []
    for s in stations[:12]:
        mhz = f"{s['freq_hz'] / 1e6:.4f}".rstrip("0").replace(".", ",")
        text = f"{mhz} mégahertz, {s.get('label') or s['mode']}, {len(s['captures'])} capture{'s' if len(s['captures']) > 1 else ''}"
        h = habits(s)
        if h:
            wd, hour, n = h[0]
            text += f", souvent le {JOURS[wd]} vers {hour} heures"
        if not s.get("centree"):
            text += ", pas encore recentrée"
        parts.append(text)
    return f"{len(stations)} station{'s' if len(stations) > 1 else ''} probable{'s' if len(stations) > 1 else ''} : " + " ; ".join(parts) + "."
