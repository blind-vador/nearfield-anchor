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

from phrases import Etiquettes, dire

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
VERDICTS = Etiquettes("station.verdict", {
    "bruit": "bruit large, pas une émission : parasite local probable (ordinateur, alimentation, câble USB)",
    "artefact": "artefact probable : une émission forte qui sature la clé, pas une station sur cette fréquence",
    "vraie": "vraie émission probable sur cette fréquence",
    "indecis": "indécis"}, "verdict du test vraie station ou artefact")


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

def _no_interference(r):
    """Aucun brouillage : ancien champ unique, ou les quatre champs du 2026-10-02."""
    if "brouillage" in r:
        return r["brouillage"] == "aucun"
    return all(r.get(k, "aucun") == "aucun" for k in ("sifflement", "bourdonnement", "autre_station", "crachotements"))


def is_all_green(note):
    """Note "tout au vert" : validation rapide, ou note detaillee sans defaut."""
    if note.get("validation_rapide"):
        return True
    r = note.get("reponses", {})
    return (r.get("lisibilite", 0) >= 4 and r.get("souffle") in ("aucun", "discret")
            and _no_interference(r) and r.get("son") == "naturel" and r.get("stabilite") == "stable"
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
        if is_digital(note):  # station DMR/numerique : le mode manuel doit decoder, pas ecouter l'analogique
            station["numerique"] = True
        if t not in station["captures"]:
            station["captures"] = sorted(station["captures"] + [t])[-500:]
    return True


def is_digital(entry):
    """Station ou note en voix numerique (DMR...) : reconnue par la bande
    "dmr" ou le drapeau metrics "numerique" (2026-10-08)."""
    return bool(entry.get("numerique") or entry.get("bande") == "dmr"
                or (entry.get("metrics") or {}).get("numerique"))


def record_capture(freq_hz, mode):
    """Une capture du scanner pres d'une station probable : interet, habitude."""
    with _Locked() as data:
        station = _near(data, freq_hz, mode)
        if station:
            station["captures"] = (station["captures"] + [time.time()])[-500:]
            return True
    return False


# --- recentrage -------------------------------------------------------------------------

def _snapshot(freq_hz, gain=30):
    """Spectre moyen (dB) autour de freq_hz : (frequences, niveaux, plancher)
    ou None. Releve I/Q en flux, decale de CENTER_OFFSET_HZ (loin de la raie
    parasite au centre du tuner), jamais plus de 5 s."""
    tune = int(freq_hz + CENTER_OFFSET_HZ)
    try:
        proc = subprocess.Popen(["rtl_sdr", "-f", str(tune), "-s", str(CENTER_RATE), "-g", str(gain),
                                 "-n", str(CENTER_RATE // 2 + CENTER_SAMPLES + CENTER_RATE // 4), "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    raw, deadline = b"", time.time() + 5
    skip = CENTER_RATE  # la premiere demi-seconde (reglage du tuner) est jetee
    try:
        os.set_blocking(proc.stdout.fileno(), False)
        while len(raw) < skip + 2 * CENTER_SAMPLES and time.time() < deadline and proc.poll() is None:
            chunk = proc.stdout.read(65536)
            if chunk:
                raw += chunk
            else:
                time.sleep(0.02)
    finally:
        from lecture_flux import arreter
        arreter(proc, "rtl_sdr (recentrage)")
    raw = raw[skip:]
    if len(raw) < 2 * CENTER_SAMPLES:
        return None
    iq = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
    x = (iq[0::2] + 1j * iq[1::2])[: (len(iq) // 2 // 4096) * 4096].reshape(-1, 4096)
    spec = np.fft.fftshift((np.abs(np.fft.fft(x * np.hanning(4096), axis=1)) ** 2).mean(axis=0))
    bins = (np.arange(4096) - 2048) * CENTER_RATE / 4096 + tune
    db = 10 * np.log10(spec + 1e-9)
    return bins, db, float(np.median(db))


def measure_center(freq_hz, mode):
    """Centre reel de l'emission pres de freq_hz (Hz), ou None : centre de
    gravite de l'energie au-dessus du bruit (porteuse la plus forte en AM)."""
    snap = _snapshot(freq_hz)
    if snap is None:
        return None
    bins, db, floor = snap
    window = np.abs(bins - freq_hz) <= SEARCH_HZ.get(mode, 15_000)
    if window.sum() < 3:
        return None
    power_db = db[window]
    above = power_db > floor + 6
    if not above.any():
        return None
    if mode == "am":
        return int(bins[window][np.argmax(power_db)])
    weights = 10 ** (power_db[above] / 10)
    return int((bins[window][above] * weights).sum() / weights.sum())


# --- vraie station ou artefact de la cle ? -----------------------------------------------

GAIN_STEP_DB = 10


def analyse(freq_hz):
    """Juste apres une capture en ondes courtes (CB, 10 m) : largeur du
    signal et baisse quand on retire GAIN_STEP_DB de gain.
    - une emission AM fait environ 10 kHz de large, une radio FM qui deborde
      sur ces frequences (cle saturee) 150 a 200 kHz ;
    - un vrai signal baisse d'autant que le gain ; un produit de saturation
      (intermodulation) baisse environ trois fois plus.
    Rend {largeur_khz, baisse_db, verdict} ou None (signal deja parti)."""
    high = _snapshot(freq_hz, gain=30)
    low = _snapshot(freq_hz, gain=30 - GAIN_STEP_DB)
    if high is None or low is None:
        return None
    bins, db, floor = high
    near = np.abs(bins - freq_hz) <= 120_000
    peak_i = np.flatnonzero(near)[np.argmax(db[near])]
    peak = db[peak_i]
    if peak < floor + 10:
        return None  # plus rien a mesurer
    above = db > peak - 20  # largeur a -20 dB de la crete, d'un seul tenant
    lo = hi = peak_i
    while lo > 0 and above[lo - 1]:
        lo -= 1
    while hi < len(db) - 1 and above[hi + 1]:
        hi += 1
    width_khz = round(float(bins[hi] - bins[lo]) / 1000, 1)
    drop = round(float(peak - low[1][peak_i]), 1)
    if width_khz > 400:
        code = "bruit"
    elif width_khz > 60 or drop >= 2 * GAIN_STEP_DB:
        code = "artefact"
    elif width_khz <= 20 and drop <= 1.5 * GAIN_STEP_DB:
        code = "vraie"
    else:
        code = "indecis"
    # le code sert aux decisions ; le texte, dit dans la langue du moment, a l'ecoute
    return {"largeur_khz": width_khz, "baisse_db": drop, "code": code, "verdict": VERDICTS[code]}


def note_analysis(freq_hz, mode, result):
    """Garde le verdict sur la station probable proche, s'il y en a une."""
    if not result:
        return
    with _Locked() as data:
        s = _near(data, freq_hz, mode)
        if s:
            s["analyse"] = dict(result, t=time.time())


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

def active(station):
    """La station est-elle revisitee par le scanner ? Reglage propre a la
    station (touche O du mode manuel, 2026-10-17) sinon l'interet de sa
    bande : "désactivé" (commande interets) la saute entierement."""
    if "actif" in station:
        return bool(station["actif"])
    try:
        import interets
        return not interets.disabled(station.get("bande") or "")
    except Exception:
        return True


def set_active(freq_hz, mode, on):
    """Active ou desactive la station la plus proche ; rend la station ou None."""
    with _Locked() as data:
        s = _near(data, freq_hz, mode)
        if s is not None:
            s["actif"] = bool(on)
            return dict(s)
    return None


def add(freq_hz, mode, nom=None, bande=None, label=None, fenetre=None, captures=()):
    """Station entree a la main (touche A du mode manuel, 2026-10-17) ou
    reprise d'une fenetre RTLSDR-Airband (fenetre : son nom ; le scanner
    l'ecoute deja par la fenetre, bands() ne la double pas).
    Rend (station, creee) ; une station deja proche est seulement completee."""
    with _Locked() as data:
        s = _near(data, freq_hz, mode)
        created = s is None
        if created:
            s = {"freq_hz": int(freq_hz), "mode": mode, "bande": bande, "label": label,
                 "cree": time.time(), "centree": False, "captures": [], "visite": 0, "manuelle": True}
            data["stations"].append(s)
        if nom and not s.get("nom"):
            s["nom"] = nom
        if fenetre:
            s["fenetre"] = fenetre
        if captures:
            s["captures"] = sorted(set(s["captures"]) | set(captures))[-500:]
        return dict(s), created


def remove(freq_hz, mode):
    """Retire la station la plus proche ; rend True si elle existait."""
    with _Locked() as data:
        s = _near(data, freq_hz, mode)
        if s is None:
            return False
        data["stations"].remove(s)
        return True


def window_disabled(name):
    """Fenetre RTLSDR-Airband dont la station a ete desactivee (touche O)."""
    return any(s.get("fenetre") == name and s.get("actif") is False for s in load()["stations"])


def rename(freq_hz, mode, name):
    """Nom humain de la station (touche F3 du mode manuel, 2026-10-17) ;
    name vide : le nom est retire. Rend la station ou None."""
    with _Locked() as data:
        s = _near(data, freq_hz, mode)
        if s is None:
            return None
        if name:
            s["nom"] = name
        else:
            s.pop("nom", None)
        return dict(s)


def name_near(freq_hz, mode):
    """Nom humain de la station probable proche, ou None."""
    s = _near(load(), freq_hz, mode)
    return s.get("nom") if s else None


def _interest_weight(station):
    try:
        import interets
        return interets.weight(station.get("bande") or "")
    except Exception:
        return 1.0


def bands(now=None):
    """Bandes "fm_channels" du scanner : les stations a revisiter maintenant,
    regroupees par mode. Delai entre deux visites selon l'interet ; a chaque
    tour pendant une heure d'habitude."""
    now = now or time.time()
    d = datetime.datetime.fromtimestamp(now)
    due = {}
    with _Locked() as data:
        for s in data["stations"]:
            if not active(s):
                continue  # desactivee (touche O) ou bande "désactivé" (interets)
            if s.get("fenetre"):
                continue  # deja ecoutee par sa fenetre RTLSDR-Airband
            habit_now = any(wd == d.weekday() and h == d.hour for wd, h, _ in habits(s))
            # L'interet de la bande (commande interets) espace aussi les visites :
            # "aucun" ou "faible" reviennent bien moins souvent (au plus MAX_REVISIT_SEC).
            weight = max(_interest_weight(s), 0.05) if "actif" not in s else 1.0
            wait = max(MIN_REVISIT_SEC, min(MAX_REVISIT_SEC,
                                            MAX_REVISIT_SEC / (1 + 2 * interest(s, now)) / weight))
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
        return (dire("station.aucune_station_probable_instant"))
    parts = []
    for s in stations[:12]:
        mhz = f"{s['freq_hz'] / 1e6:.4f}".rstrip("0").replace(".", ",")
        text = dire("station.megahertz_capture", mhz=mhz, v1=s.get('nom') or s.get('label') or s['mode'], len=len(s['captures']), pluriel='s' if len(s['captures']) > 1 else '')
        h = habits(s)
        if h:
            wd, hour, n = h[0]
            text += dire("station.souvent_vers_heures", v1=JOURS[wd], hour=hour)
        if not active(s):
            text += dire("station.desactivee_suffixe")
        if not s.get("centree"):
            text += dire("station.encore_recentree")
        if s.get("analyse", {}).get("code", s.get("analyse", {}).get("verdict", "")[:8]) == "artefact":
            text += dire("station.sans_doute_artefact_cle")
        parts.append(text)
    return (dire("station.station_probable", len=len(stations), pluriel='s' if len(stations) > 1 else '', pluriel2='s' if len(stations) > 1 else '') + " ") + " ; ".join(parts) + "."
