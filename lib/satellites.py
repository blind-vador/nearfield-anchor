"""Passages de satellites au-dessus de chez soi (orbites ouvertes Celestrak,
frequences actives SatNOGS, calcul pyephem), pour le scanner :

  meteo   Meteor-M2 3 et M2-4 : images LRPT des nuages, 137,1 ou 137,9 MHz ;
  iss     Station spatiale internationale : relais APRS 145,825 MHz ;
  fm      satellites radioamateurs a relais FM (liste SatNOGS du jour).

Un passage n'est retenu qu'au-dessus de MIN_ELEVATION de sa famille
(un passage bas ne donne rien avec une antenne verticale, et la cle est
precieuse pour le reste).
"""
import datetime
import json
import os
import time
import urllib.request

import ephem

import reglages

TLE_DIR = "/home/vador/autopilot/tle"
TLE_GROUPS = ["weather", "stations", "amateur"]
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
SATNOGS_URL = "https://db.satnogs.org/api/transmitters/?format=json&alive=true"
REFRESH_SEC = 24 * 3600
STATE_PATH = "/tmp/satellites_passages.json"

METEOR = {"METEOR-M2 3": "Meteor M2 3", "METEOR-M2 4": "Meteor M2 4"}
METEOR_FREQS = [137_100_000, 137_900_000]
ISS = "ISS (ZARYA)"
ISS_APRS_HZ = 145_825_000
MIN_ELEVATION = {"meteo": 25, "iss": 20, "fm": 55}
FM_MAX_PER_DAY = 4   # petits satellites radioamateurs : seulement les plus beaux passages
FM_BANDS = [(144e6, 146e6), (435e6, 438e6)]


def _fetch(url, path):
    try:
        if time.time() - os.path.getmtime(path) < REFRESH_SEC:
            return
    except OSError:
        pass
    try:
        data = urllib.request.urlopen(url, timeout=60).read()
        if data:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
    except OSError as exc:
        print(f"[satellites] {url} indisponible : {exc}")


def _orbits():
    sats = {}
    for group in TLE_GROUPS:
        path = os.path.join(TLE_DIR, f"{group}.txt")
        _fetch(TLE_URL.format(group=group), path)
        try:
            lines = [l.rstrip() for l in open(path) if l.strip()]
        except OSError:
            continue
        for i in range(0, len(lines) - 2, 3):
            try:
                sats[lines[i].strip()] = (ephem.readtle(lines[i], lines[i + 1], lines[i + 2]),
                                          int(lines[i + 2].split()[1]))
            except (ValueError, IndexError):
                continue
    return sats


def _fm_transmitters():
    """{numero NORAD: (frequence Hz, description)} des relais FM actifs."""
    path = os.path.join(TLE_DIR, "satnogs.json")
    _fetch(SATNOGS_URL, path)
    try:
        data = json.load(open(path))
    except (OSError, ValueError):
        return {}
    found = {}
    for t in data:
        f = t.get("downlink_low") or 0
        if (t.get("status") == "active" and t.get("mode") == "FM" and t.get("type") == "Transceiver"
                and any(low <= f <= high for low, high in FM_BANDS)):
            found.setdefault(t["norad_cat_id"], (int(f), t.get("description") or ""))
    return found


def _observer():
    lieu = reglages.get("lieu")
    obs = ephem.Observer()
    obs.lat, obs.lon = str(lieu["lat"]), str(lieu["lon"])
    obs.elevation = 500
    obs.horizon = "5"
    return obs


def upcoming(hours=24):
    """Passages retenus dans les prochaines heures, tries par debut :
    [{nom, famille, debut, fin, elevation_max, frequences}] (heures UTC en
    secondes epoch)."""
    sats = _orbits()
    fm = _fm_transmitters()
    candidates = []
    for name, label in METEOR.items():
        if name in sats:
            candidates.append((label, "meteo", sats[name][0], METEOR_FREQS))
    if ISS in sats:
        candidates.append(("internationale", "iss", sats[ISS][0], [ISS_APRS_HZ]))
    for name, (body, norad) in sats.items():
        if norad in fm and norad != sats.get(ISS, (None, 0))[1]:
            candidates.append((name.split(" (")[0].title(), "fm", body, [fm[norad][0]]))
    now = datetime.datetime.utcnow()
    end = now + datetime.timedelta(hours=hours)
    passes = []
    for label, family, body, freqs in candidates:
        obs = _observer()
        obs.date = now
        for _ in range(12):
            try:
                rise, _, peak_t, peak_el, setting, _ = obs.next_pass(body)
            except (ValueError, TypeError):
                break
            if rise is None or setting is None or ephem.Date(rise).datetime() > end:
                break
            elevation = float(peak_el) * 180 / 3.14159265
            if elevation >= MIN_ELEVATION[family]:
                passes.append({"nom": label, "famille": family, "elevation_max": round(elevation),
                               "debut": ephem.Date(rise).datetime().replace(tzinfo=datetime.timezone.utc).timestamp(),
                               "fin": ephem.Date(setting).datetime().replace(tzinfo=datetime.timezone.utc).timestamp(),
                               "frequences": freqs})
            obs.date = ephem.Date(setting + ephem.minute)
    fm_best = sorted((p for p in passes if p["famille"] == "fm"), key=lambda p: -p["elevation_max"])
    dropped = {id(p) for p in fm_best[FM_MAX_PER_DAY:]}
    passes = [p for p in passes if id(p) not in dropped]
    passes.sort(key=lambda p: p["debut"])
    return passes


def due(lead_sec=90):
    """Le passage qui commence (ou a commence) dans lead_sec et n'est pas
    encore traite ; en cas de chevauchement, le plus haut dans le ciel."""
    state = {}
    try:
        state = json.load(open(STATE_PATH))
    except (OSError, ValueError):
        pass
    if time.time() - state.get("calcule", 0) > 3600:
        state = {"calcule": time.time(), "passages": upcoming(), "faits": state.get("faits", [])}
        json.dump(state, open(STATE_PATH, "w"))
    now = time.time()
    ready = [p for p in state["passages"]
             if p["debut"] - lead_sec <= now < p["fin"] - 120 and f"{p['nom']}|{int(p['debut'])}" not in state["faits"]]
    if not ready:
        return None
    best = max(ready, key=lambda p: p["elevation_max"])
    for p in ready:  # les autres chevauchements sont abandonnes
        state["faits"].append(f"{p['nom']}|{int(p['debut'])}")
    state["faits"] = state["faits"][-200:]
    json.dump(state, open(STATE_PATH, "w"))
    return best


def spoken_list(hours=12):
    """Commande "satellites" : prochains passages ecoutes."""
    passes = upcoming(hours)
    if not passes:
        return f"Aucun passage de satellite assez haut dans les {hours} prochaines heures."
    families = {"meteo": "satellite météo", "iss": "station spatiale", "fm": "satellite radioamateur"}
    hour = lambda h: "minuit" if h == 0 else "midi" if h == 12 else f"{h} heures"
    lines = []
    for p in passes[:8]:
        start = datetime.datetime.fromtimestamp(p["debut"])
        minutes = round((p["fin"] - p["debut"]) / 60)
        lines.append(f"{hour(start.hour)} {start.minute:02d}, {families[p['famille']]} {p['nom']}, "
                     f"{p['elevation_max']} degrés au plus haut, {minutes} minutes")
    return f"{len(passes)} passages dans les {hours} prochaines heures : " + " ; ".join(lines) + "."
