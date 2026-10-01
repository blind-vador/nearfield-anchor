"""Meteo locale : bulletin parle, signes avant-coureurs chez les voisins,
niveau de menace et contexte pour l'attention du scanner.

Sources ouvertes, sans cle :
  - Open-Meteo (modele AROME de Meteo-France en France) : temps present,
    heures passees et a venir, 3 jours ; pression, potentiel orageux (CAPE),
    rafales, niveau du gel ;
  - vigilances meteo et crues : etat publie par meteo_watch et crue_watch ;
  - stations meteo des radioamateurs (APRS) et capteurs sans fil voisins
    (rtl_433), captes par le scanner ;
  - SondeHub : ballons-sondes en vol (reseau ouvert de recepteurs).

L'historique des releves voisins (HISTORY_PATH, 24 h) sert a reperer les
signes avant-coureurs : chute brutale de temperature, pression qui
plonge, rafales, debut de pluie, avec la direction de la station -- l'ordre
dans lequel les stations reagissent dit d'ou arrive la perturbation.
"""
import json
import math
import os
import time
from datetime import datetime

import requests

import reglages

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
SONDEHUB_URL = "https://api.v2.sondehub.org/sondes"
TIMEOUT = 20
HISTORY_PATH = "/home/vador/autopilot/meteo_historique.json"
HISTORY_KEEP_SEC = 24 * 3600
CONTEXT_PATH = "/tmp/contexte_meteo.json"
APRS_WX_PATH = "/tmp/meteo_stations_aprs.json"
SENSORS_PATH = "/tmp/capteurs_releves.json"
METEO_STATE = "/tmp/meteo_watch_vigilance_state.json"
CRUE_STATE = "/tmp/crue_watch_vigilance_state.json"
STATION_MAX_KM = 60
SONDE_RADIUS_KM = 300
SONDE_MAX_AGE_SEC = 3600

# Signes avant-coureurs (sur l'historique d'une station)
TEMP_DROP_C = 4.0           # chute en moins de TEMP_DROP_WINDOW_SEC
TEMP_DROP_WINDOW_SEC = 45 * 60
PRESSURE_FALL_HPA = 3.0     # baisse en 3 h : depression qui arrive
GUST_JUMP_KMH = 25.0        # hausse du vent en moins de 30 min
PRECURSOR_MAX_AGE_SEC = 90 * 60

# Niveau de menace -> renfort d'attention des fenetres meteo du scanner
ATTENTION_BOOST = {"aprs": 0.6, "capteurs": 0.6, "sondes": 0.3}
SONDE_PROFILE_PATH = "/tmp/sondes_profil.json"   # ecrit par watchers/sondes_watch.py

WMO = {0: "ciel dégagé", 1: "ciel peu nuageux", 2: "éclaircies", 3: "ciel couvert",
       45: "brouillard", 48: "brouillard givrant", 51: "bruine légère", 53: "bruine",
       55: "bruine forte", 61: "pluie faible", 63: "pluie", 65: "forte pluie",
       66: "pluie verglaçante", 67: "forte pluie verglaçante", 71: "neige faible",
       73: "neige", 75: "forte neige", 77: "grains de neige", 80: "averses",
       81: "averses", 82: "fortes averses", 85: "averses de neige", 86: "fortes averses de neige",
       95: "orages", 96: "orages avec grêle", 99: "orages avec forte grêle"}
STORM_CODES = {95, 96, 99}
COLOR_NAMES = {2: "jaune", 3: "orange", 4: "rouge"}
DIRECTIONS = ["nord", "nord-est", "est", "sud-est", "sud", "sud-ouest", "ouest", "nord-ouest"]
LEVEL_NAMES = {0: "calme", 1: "à surveiller", 2: "perturbation en approche", 3: "danger imminent"}


# --- outils -----------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[meteo_locale] ecriture impossible ({path}) : {exc}")


def _home(lieu):
    return float(lieu["lat"]), float(lieu["lon"])


def _distance_km(lat0, lon0, lat, lon):
    p0, p1 = math.radians(lat0), math.radians(lat)
    a = (math.sin((p1 - p0) / 2) ** 2
         + math.cos(p0) * math.cos(p1) * math.sin(math.radians(lon - lon0) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(a))


def _direction(lat0, lon0, lat, lon):
    y = math.sin(math.radians(lon - lon0)) * math.cos(math.radians(lat))
    x = (math.cos(math.radians(lat0)) * math.sin(math.radians(lat))
         - math.sin(math.radians(lat0)) * math.cos(math.radians(lat)) * math.cos(math.radians(lon - lon0)))
    return DIRECTIONS[int(((math.degrees(math.atan2(y, x)) + 360) % 360 + 22.5) // 45) % 8]


def _where(lieu, lat, lon):
    lat0, lon0 = _home(lieu)
    km = round(_distance_km(lat0, lon0, lat, lon))
    towards = _direction(lat0, lon0, lat, lon)
    towards = f"à l'{towards}" if towards[0] in "eo" else f"au {towards}"
    return "tout près" if km < 1 else f"à {km} kilomètre{'s' if km > 1 else ''} {towards}"


def _num(value, decimals=0):
    text = f"{value:.{decimals}f}"
    return text.replace(".", ",").replace("-", "moins ")


def _wind_from(degrees):
    if degrees is None:
        return ""
    name = DIRECTIONS[int(((degrees % 360) + 22.5) // 45) % 8]
    return f"de l'{name}" if name[0] in "eo" else f"du {name}"


# --- donnees ouvertes --------------------------------------------------------

def forecast(lieu):
    lat, lon = _home(lieu)
    try:
        resp = requests.get(OPEN_METEO_URL, timeout=TIMEOUT, params={
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                       "wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl,precipitation",
            "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,"
                      "wind_gusts_10m,cape,pressure_msl,freezing_level_height",
            "past_hours": 3, "forecast_hours": 24,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                     "precipitation_probability_max,wind_gusts_10m_max",
            "forecast_days": 3})
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[meteo_locale] Open-Meteo indisponible : {exc}")
        return None


def sondes(lieu):
    """Ballons-sondes en vol (derniere position de moins d'une heure)."""
    lat, lon = _home(lieu)
    try:
        resp = requests.get(SONDEHUB_URL, timeout=TIMEOUT, params={
            "lat": lat, "lon": lon, "distance": SONDE_RADIUS_KM * 1000, "last": SONDE_MAX_AGE_SEC})
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    found = []
    for serial, s in data.items():
        try:
            when = datetime.fromisoformat(s["datetime"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if time.time() - when > SONDE_MAX_AGE_SEC or s.get("lat") is None:
            continue
        found.append({"serie": serial, "type": s.get("type"), "lat": s["lat"], "lon": s["lon"], "alt": s.get("alt"),
                      "temp": s.get("temp"), "humidite": s.get("humidity"), "vitesse_v": s.get("vel_v"),
                      "frequence": s.get("frequency"), "t": when})
    return found


# --- historique des voisins ------------------------------------------------------

def record_observations():
    """Ajoute a l'historique les releves voisins publies par le scanner
    (stations meteo APRS, capteurs rtl_433). Appele regulierement par
    meteo_watch ; un releve deja connu n'est pas duplique."""
    history = _read_json(HISTORY_PATH) or {}
    now = time.time()
    aprs = _read_json(APRS_WX_PATH) or {}
    for call, st in aprs.items():
        wx = st.get("meteo") or {}
        row = {"t": st.get("t"), "temp": wx.get("temperature"), "pression": wx.get("pressure"),
               "vent": (wx.get("wind_speed") or 0) * 3.6 if wx.get("wind_speed") is not None else None,
               "rafale": (wx.get("wind_gust") or 0) * 3.6 if wx.get("wind_gust") is not None else None,
               "pluie_1h": wx.get("rain_1h"), "humidite": wx.get("humidity")}
        entry = history.setdefault(f"aprs:{call}", {"lat": st.get("lat"), "lon": st.get("lon"), "nom": call,
                                                     "releves": []})
        if not entry["releves"] or entry["releves"][-1]["t"] != row["t"]:
            entry["releves"].append(row)
    sensors = _read_json(SENSORS_PATH) or {}
    for key, r in sensors.items():
        v = r.get("valeurs") or {}
        if v.get("temperature_C") is None:
            continue
        row = {"t": r.get("t"), "temp": v.get("temperature_C"), "humidite": v.get("humidity"),
               "rafale": v.get("wind_max_km_h"), "vent": v.get("wind_avg_km_h"), "pluie_cumul": v.get("rain_mm")}
        entry = history.setdefault(f"capteur:{key}", {"nom": r.get("nom"), "releves": []})
        if not entry["releves"] or entry["releves"][-1]["t"] != row["t"]:
            entry["releves"].append(row)
    for key in list(history):
        history[key]["releves"] = [r for r in history[key]["releves"] if r.get("t") and now - r["t"] < HISTORY_KEEP_SEC]
        if not history[key]["releves"]:
            del history[key]
    _write_json(HISTORY_PATH, history)
    return history


def _value_at(rows, field, before):
    """Derniere valeur de field au plus tard a l'instant before."""
    candidates = [r for r in rows if r.get(field) is not None and r["t"] <= before]
    return candidates[-1] if candidates else None


def precursors(lieu, history=None):
    """Signes avant-coureurs recents chez les voisins. Rend une liste de
    dicts {texte, gravite (1-2), t, direction}."""
    history = history if history is not None else (_read_json(HISTORY_PATH) or {})
    lat0, lon0 = _home(lieu)
    now = time.time()
    signs = []
    for key, entry in history.items():
        rows = sorted(entry.get("releves", []), key=lambda r: r["t"])
        if not rows or now - rows[-1]["t"] > PRECURSOR_MAX_AGE_SEC:
            continue
        last = rows[-1]
        located = entry.get("lat") is not None
        if located:
            if _distance_km(lat0, lon0, entry["lat"], entry["lon"]) > STATION_MAX_KM:
                continue
            where = _where(lieu, entry["lat"], entry["lon"])
            label = f"station météo radioamateur {where}"
            direction = _direction(lat0, lon0, entry["lat"], entry["lon"])
        else:
            label = f"capteur voisin {entry.get('nom') or ''}".strip()
            direction = None
        if last.get("temp") is not None:
            window = [r for r in rows if r.get("temp") is not None and last["t"] - r["t"] <= TEMP_DROP_WINDOW_SEC]
            peak = max(window, key=lambda r: r["temp"]) if window else None
            if peak and peak["temp"] - last["temp"] >= TEMP_DROP_C:
                minutes = max(1, round((last["t"] - peak["t"]) / 60))
                signs.append({"t": last["t"], "gravite": 2, "direction": direction, "type": "chute_temp",
                              "texte": f"chute de {_num(peak['temp'] - last['temp'])} degrés en {minutes} minutes "
                                       f"chez une {label}"})
        if last.get("pression") is not None:
            before = _value_at(rows, "pression", last["t"] - 3 * 3600 + 600)
            if before and before["t"] <= last["t"] - 2 * 3600 and before["pression"] - last["pression"] >= PRESSURE_FALL_HPA:
                signs.append({"t": last["t"], "gravite": 1, "direction": direction, "type": "pression",
                              "texte": f"pression en baisse de {_num(before['pression'] - last['pression'])} "
                                       f"hectopascals en 3 heures chez une {label}"})
        gust = last.get("rafale") if last.get("rafale") is not None else last.get("vent")
        if gust is not None:
            field = "rafale" if last.get("rafale") is not None else "vent"
            before = _value_at(rows, field, last["t"] - 30 * 60)
            if before and gust - before[field] >= GUST_JUMP_KMH:
                signs.append({"t": last["t"], "gravite": 2, "direction": direction, "type": "vent",
                              "texte": f"vent passé de {round(before[field])} à {round(gust)} kilomètres heure "
                                       f"chez une {label}"})
        if last.get("pluie_1h"):
            before = _value_at(rows, "pluie_1h", last["t"] - 30 * 60)
            if before and not before["pluie_1h"] and last["pluie_1h"] >= 0.5:
                signs.append({"t": last["t"], "gravite": 1, "direction": direction, "type": "pluie",
                              "texte": f"la pluie vient de commencer chez une {label}"})
    # Fiabilite apprise des observations de l'utilisateur (lib/meteo_obs.py) :
    # un type de signe rarement suivi d'un orage ou d'une forte pluie ici
    # perd du poids, un type souvent suivi en gagne.
    try:
        import meteo_obs
        reliability = meteo_obs.sign_reliability()
    except Exception:
        reliability = {}
    for s in signs:
        hits, total = reliability.get(s["type"], (0, 0))
        if total >= 5:
            rate = hits / total
            if rate < 0.2:
                s["gravite"] = 1
                s["texte"] += ", signe peu fiable ici"
            elif rate >= 0.6:
                s["gravite"] = 2
                s["texte"] += f", signe souvent suivi d'orage ici, {hits} fois sur {total}"
    signs.sort(key=lambda s: -s["t"])
    return signs


# --- analyse ------------------------------------------------------------------

def _hourly_index(fc):
    """Indice de l'heure courante dans les series horaires."""
    times = (fc.get("hourly") or {}).get("time") or []
    now = datetime.now().strftime("%Y-%m-%dT%H:00")
    return times.index(now) if now in times else min(3, max(0, len(times) - 1))


def pressure_trend(fc):
    """(pression actuelle, variation sur 3 h) ou (None, None)."""
    hourly = (fc or {}).get("hourly") or {}
    series = hourly.get("pressure_msl") or []
    i = _hourly_index(fc) if fc else 0
    if len(series) <= i or i < 3 or series[i] is None or series[i - 3] is None:
        return None, None
    return series[i], series[i] - series[i - 3]


def storm_outlook(fc, hours=6):
    """Ce que prevoit le modele pour les prochaines heures (dict)."""
    hourly = (fc or {}).get("hourly") or {}
    i = _hourly_index(fc) if fc else 0
    window = range(i, min(i + hours, len(hourly.get("time") or [])))

    def series(field):
        values = hourly.get(field) or []
        return [(values[k], k) for k in window if k < len(values) and values[k] is not None]
    cape = max(series("cape"), default=(0, i))
    gusts = max(series("wind_gusts_10m"), default=(0, i))
    rain = [k for v, k in series("precipitation_probability") if v >= 60]
    storms = [k for v, k in series("weather_code") if v in STORM_CODES]
    return {"cape": cape, "rafales": gusts, "pluie_des": rain[0] - i if rain else None,
            "orage_des": storms[0] - i if storms else None}


def threat(lieu, fc=None, signs=None):
    """(niveau 0-3, raisons parlees)."""
    reasons, level = [], 0
    vigil = _read_json(METEO_STATE) or {}
    worst = max([v for v in vigil.values() if isinstance(v, int)] or [1])
    if worst >= 2:
        level = max(level, {2: 1, 3: 2, 4: 3}[worst])
        reasons.append(f"vigilance {COLOR_NAMES[worst]} sur le département")
    if fc:
        out = storm_outlook(fc)
        if out["orage_des"] is not None:
            level = max(level, 3 if out["orage_des"] <= 1 else 2)
            reasons.append("orage prévu " + ("maintenant" if out["orage_des"] == 0
                                              else f"dans {out['orage_des']} heure{'s' if out['orage_des'] > 1 else ''}"))
        if out["cape"][0] >= 1500:
            level = max(level, 2)
            reasons.append("atmosphère très instable, potentiel orageux fort")
        elif out["cape"][0] >= 500:
            level = max(level, 1)
            reasons.append("atmosphère instable")
        if out["rafales"][0] >= 70:
            level = max(level, 2)
            reasons.append(f"rafales jusqu'à {round(out['rafales'][0])} kilomètres heure")
        elif out["rafales"][0] >= 50:
            level = max(level, 1)
            reasons.append(f"rafales jusqu'à {round(out['rafales'][0])} kilomètres heure")
        _, delta = pressure_trend(fc)
        if delta is not None and delta <= -PRESSURE_FALL_HPA:
            level = max(level, 1)
            reasons.append(f"pression en chute de {_num(-delta)} hectopascals en 3 heures")
    try:
        import meteo_obs
        observed = meteo_obs.recent()
    except Exception:
        observed = None
    if observed:
        thunder = observed.get("orage")
        if thunder in ("proche", "tout_pres"):
            level = 3
            reasons.append("tonnerre " + ("tout près" if thunder == "tout_pres" else "proche") + ", d'après ton observation")
        elif thunder == "loin":
            level = max(level, 2)
            reasons.append("tonnerre entendu au loin, d'après ton observation")
        if observed.get("precip") in ("violente", "grele"):
            level = max(level, 2)
            reasons.append(("grêle" if observed["precip"] == "grele" else "averse violente") + ", d'après ton observation")
        elif observed.get("precip") == "forte":
            level = max(level, 1)
            reasons.append("forte pluie, d'après ton observation")
    signs = precursors(lieu) if signs is None else signs
    if any(s["gravite"] >= 2 for s in signs):
        level = max(level, 2)
        reasons.append("signes avant-coureurs chez les voisins")
    elif signs:
        level = max(level, 1)
    return level, reasons


def published_level(max_age_sec=900):
    """Niveau de menace publie (0 si absent ou trop vieux)."""
    ctx = _read_json(CONTEXT_PATH) or {}
    if time.time() - ctx.get("updated", 0) > max_age_sec:
        return 0
    return int(ctx.get("niveau") or 0)


def publish_context(level, reasons):
    """Contexte pour l'attention du scanner (lib/attention.py)."""
    _write_json(CONTEXT_PATH, {"niveau": level, "raisons": reasons, "updated": time.time(),
                               "cibles": {k: v * level for k, v in ATTENTION_BOOST.items()}})


def approach_direction(signs):
    """D'ou arrive la perturbation : direction des stations qui ont reagi,
    de la plus ancienne a la plus recente."""
    located = sorted((s for s in signs if s["direction"]), key=lambda s: s["t"])
    if not located:
        return None
    first = located[0]["direction"]
    return f"de l'{first}" if first[0] in "eo" else f"du {first}"


# --- balayage meteo force (commande "meteo" de la console) ------------------

SCAN_REQUEST_PATH = "/tmp/radio_watch_meteo.json"
SCAN_DONE_PATH = "/tmp/radio_watch_meteo_fait.json"
SCAN_REQUEST_MAX_AGE_SEC = 600
# (fenetre du scanner, duree d'ecoute) : stations APRS, capteurs 433 puis 868 MHz
FORCED_SCAN = [("aprs", 60), ("capteurs", 45), ("capteurs", 45)]


def request_scan():
    """Demande un balayage au scanner ; rend l'instant de la demande."""
    asked = time.time()
    try:
        os.remove(SCAN_DONE_PATH)
    except OSError:
        pass
    _write_json(SCAN_REQUEST_PATH, {"t": asked})
    return asked


def scan_duration():
    return sum(sec for _, sec in FORCED_SCAN)


def scan_done(started, finished, stations, sensors):
    """Pour le scanner : signale la fin du balayage a la console."""
    _write_json(SCAN_DONE_PATH, {"debut": started, "fin": finished, "stations": stations, "capteurs": sensors})


def wait_scan(asked, timeout):
    """Attend la fin d'un balayage demande a l'instant asked. Rend le compte
    rendu ({debut, fin, stations, capteurs}) ou None si le scanner n'a pas
    repondu (pause thermique, mode manuel, scanner arrete...)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        done = _read_json(SCAN_DONE_PATH)
        if done and done.get("debut", 0) >= asked - 1:
            return done
        if scanner_busy() and scan_requested():
            return None  # pause thermique, manuel, accroche : inutile d'attendre
        time.sleep(2)
    return None


def scan_counts(started):
    aprs = _read_json(APRS_WX_PATH) or {}
    sensors = _read_json(SENSORS_PATH) or {}
    return (sum(1 for s in aprs.values() if s.get("t", 0) >= started),
            sum(1 for s in sensors.values() if s.get("t", 0) >= started))


SCANNER_STATE_PATH = "/tmp/radio_watch_etat.json"
# Etats ou le scanner ne peut pas faire le balayage (la console n'attend pas)
SCANNER_BUSY = {"pause_thermique": "le Pi est trop chaud, même pour l'écoute météo seule",
                "manuel": "le scanner est en mode manuel",
                "accroche": "le scanner est accroché sur une fréquence"}


def set_scanner_state(state):
    """Pour le scanner : son etat courant (actif, pause_thermique, manuel,
    accroche, pause_programmee)."""
    current = _read_json(SCANNER_STATE_PATH) or {}
    if current.get("etat") != state:
        _write_json(SCANNER_STATE_PATH, {"etat": state, "t": time.time()})


def scanner_busy():
    """Raison pour laquelle le scanner ne peut pas balayer, ou None."""
    state = (_read_json(SCANNER_STATE_PATH) or {}).get("etat")
    return SCANNER_BUSY.get(state)


def scan_requested():
    req = _read_json(SCAN_REQUEST_PATH)
    return bool(req) and time.time() - req.get("t", 0) <= SCAN_REQUEST_MAX_AGE_SEC


def take_scan_request():
    """Pour le scanner : vrai (une fois) si un balayage meteo est demande."""
    req = _read_json(SCAN_REQUEST_PATH)
    if not req:
        return False
    try:
        os.remove(SCAN_REQUEST_PATH)
    except OSError:
        pass
    return time.time() - req.get("t", 0) <= SCAN_REQUEST_MAX_AGE_SEC


# --- bulletin -------------------------------------------------------------------

def _neighbors_text(lieu):
    parts = []
    aprs = _read_json(APRS_WX_PATH) or {}
    lat0, lon0 = _home(lieu)
    near = sorted(((_distance_km(lat0, lon0, s["lat"], s["lon"]), c, s) for c, s in aprs.items()
                   if s.get("lat") is not None and (s.get("meteo") or {}).get("temperature") is not None
                   and time.time() - s.get("t", 0) < 3 * 3600), key=lambda p: p[0])
    for km, call, st in near[:2]:
        if km > STATION_MAX_KM:
            break
        wx = st["meteo"]
        text = f"station radioamateur {_where(lieu, st['lat'], st['lon'])} : {_num(wx['temperature'])} degrés"
        if wx.get("pressure"):
            text += f", pression {round(wx['pressure'])}"
        parts.append(text)
    sensors = _read_json(SENSORS_PATH) or {}
    temps = sorted(r["valeurs"]["temperature_C"] for r in sensors.values()
                   if (r.get("valeurs") or {}).get("temperature_C") is not None and time.time() - r.get("t", 0) < 2 * 3600)
    if temps:
        span = (f"{_num(temps[0], 1)} degrés" if len(temps) == 1 or temps[0] == temps[-1]
                else f"entre {_num(temps[0], 1)} et {_num(temps[-1], 1)} degrés")
        parts.append(f"capteurs sans fil des voisins : {span}")
    if not parts:
        return ""
    return "Relevés autour de toi : " + " ; ".join(parts) + "."


def _sondes_text(lieu, found):
    if not found:
        return ""
    lat0, lon0 = _home(lieu)
    s = min(found, key=lambda s: _distance_km(lat0, lon0, s["lat"], s["lon"]))
    km = _distance_km(lat0, lon0, s["lat"], s["lon"])
    text = (f"{len(found)} ballon{'s' if len(found) > 1 else ''}-sonde{'s' if len(found) > 1 else ''} en vol "
            f"dans un rayon de {SONDE_RADIUS_KM} kilomètres ; le plus proche, {_where(lieu, s['lat'], s['lon'])}")
    if s.get("alt"):
        text += f", à {round(s['alt'])} mètres d'altitude"
        if s.get("temp") is not None:
            text += f", il y mesure {_num(s['temp'])} degrés"
        if (s.get("vitesse_v") or 0) < -2:
            text += ", en descente"
    if km < 150 and s.get("frequence"):
        text += f", émet sur {_num(s['frequence'], 3)} mégahertz"
    return text + "."


def bulletin(lieu=None, with_sondes=True, scan=None, scan_failed=None):
    """Bulletin meteo local complet, en phrases. scan : compte rendu du
    balayage radio qui vient d'etre fait (wait_scan)."""
    lieu = lieu or reglages.get("lieu")
    record_observations()  # les releves tout frais du balayage
    fc = forecast(lieu)
    signs = precursors(lieu)
    level, reasons = threat(lieu, fc, signs)
    parts = [f"Météo de {lieu['commune']}."]
    if fc and fc.get("current"):
        c = fc["current"]
        text = f"En ce moment, {WMO.get(c.get('weather_code'), 'temps indéterminé')}, {_num(c['temperature_2m'])} degrés"
        if c.get("apparent_temperature") is not None and abs(c["apparent_temperature"] - c["temperature_2m"]) >= 2:
            text += f", ressenti {_num(c['apparent_temperature'])}"
        text += f", humidité {c.get('relative_humidity_2m')} pour cent."
        if c.get("wind_speed_10m") is not None:
            text += f" Vent {_wind_from(c.get('wind_direction_10m'))} à {round(c['wind_speed_10m'])} kilomètres heure"
            if (c.get("wind_gusts_10m") or 0) >= 30:
                text += f", rafales à {round(c['wind_gusts_10m'])}"
            text += "."
        parts.append(text)
        pressure, delta = pressure_trend(fc)
        if pressure is not None:
            trend = ("stable" if abs(delta) < 1 else
                     f"en {'hausse' if delta > 0 else 'baisse'} de {_num(abs(delta))} en 3 heures")
            parts.append(f"Pression {round(pressure)} hectopascals, {trend}.")
        out = storm_outlook(fc, hours=12)
        hourly = fc["hourly"]
        upcoming = []
        if out["orage_des"] is not None:
            upcoming.append("orage possible " + ("dès maintenant" if out["orage_des"] == 0
                                                  else f"dans {out['orage_des']} heure{'s' if out['orage_des'] > 1 else ''}"))
        if out["pluie_des"] is not None:
            upcoming.append("pluie probable " + ("dès maintenant" if out["pluie_des"] == 0
                                                  else f"dans {out['pluie_des']} heure{'s' if out['pluie_des'] > 1 else ''}"))
        if out["cape"][0] >= 500:
            upcoming.append("atmosphère " + ("très instable" if out["cape"][0] >= 1500 else "instable"))
        try:
            import meteo_obs
            threshold = meteo_obs.thunder_threshold()
            hourly_prob = hourly.get("precipitation_probability") or []
            i0 = _hourly_index(fc)
            prob = max([p for p in hourly_prob[i0:i0 + 6] if p is not None], default=None)
            calibrated = meteo_obs.calibrated_note(prob)
        except Exception:
            threshold, calibrated = None, ""
        if threshold is not None and out["cape"][0] >= threshold:
            upcoming.append("chez toi, le tonnerre a déjà grondé avec une telle instabilité")
        if out["rafales"][0] >= 50:
            upcoming.append(f"rafales jusqu'à {round(out['rafales'][0])} kilomètres heure")
        i = _hourly_index(fc)
        freezing = (hourly.get("freezing_level_height") or [None] * (i + 1))[i]
        if freezing is not None and freezing < 2500:
            upcoming.append(f"limite du gel vers {int(round(freezing, -2))} mètres")
        parts.append("Dans les 12 prochaines heures : " + (", ".join(upcoming) if upcoming else "rien de notable") + ".")
        if calibrated:
            parts.append(calibrated)
        daily = fc.get("daily") or {}
        days = []
        for k, label in enumerate(["Aujourd'hui", "Demain", "Après-demain"]):
            try:
                text = (f"{label}, {WMO.get(daily['weather_code'][k], '')}, de {_num(daily['temperature_2m_min'][k])} "
                        f"à {_num(daily['temperature_2m_max'][k])} degrés")
                if (daily["precipitation_sum"][k] or 0) >= 1:
                    text += f", {round(daily['precipitation_sum'][k])} millimètres de pluie"
                if (daily["wind_gusts_10m_max"][k] or 0) >= 60:
                    text += f", rafales à {round(daily['wind_gusts_10m_max'][k])}"
                days.append(text)
            except (KeyError, IndexError, TypeError):
                continue
        if days:
            parts.append(". ".join(days) + ".")
    else:
        parts.append("Les prévisions Open-Meteo ne répondent pas pour le moment.")
    vigil = _read_json(METEO_STATE) or {}
    active = [(lvl, name) for name, lvl in vigil.items() if isinstance(lvl, int) and lvl >= 2]
    if active:
        parts.append("Vigilance : " + ", ".join(f"{COLOR_NAMES[l]} {n}" for l, n in sorted(active, reverse=True)) + ".")
    crue = _read_json(CRUE_STATE) or {}
    if any(isinstance(l, int) and l >= 2 for l in crue.values()):
        parts.append("Vigilance crue en cours sur la rivière surveillée.")
    if scan:
        parts.append(f"Panel radio météo balayé : {scan.get('stations', 0)} station"
                     f"{'s' if scan.get('stations', 0) > 1 else ''} radioamateur et {scan.get('capteurs', 0)} "
                     f"capteur{'s' if scan.get('capteurs', 0) > 1 else ''} reçus à l'instant.")
    elif scan_failed:
        reason = scan_failed if isinstance(scan_failed, str) else "le scanner n'a pas répondu"
        parts.append(f"Pas de balayage radio cette fois, {reason} : relevés locaux des dernières heures.")
    neighbors = _neighbors_text(lieu)
    if neighbors:
        parts.append(neighbors)
    try:
        import meteo_obs
        last_obs = (meteo_obs.load() or [None])[-1]
    except Exception:
        last_obs = None
    if last_obs and time.time() - last_obs["t"] < 3 * 3600:
        minutes = round((time.time() - last_obs["t"]) / 60)
        when = "à l'instant" if minutes < 1 else f"il y a {minutes} minute{'s' if minutes > 1 else ''}"
        parts.append(f"Ta dernière observation, {when} : {meteo_obs.spoken(last_obs['reponses'])}.")
    if signs:
        text = "Signes avant-coureurs : " + " ; ".join(s["texte"] for s in signs[:3])
        origin = approach_direction(signs)
        if origin and len({s["direction"] for s in signs if s["direction"]}) > 1:
            text += f". Les premiers signes sont venus {origin}"
        parts.append(text + ".")
    profiles = _read_json(SONDE_PROFILE_PATH) or {}
    measured = [p for p in profiles.values() if p.get("gel") and time.time() - p["points"][-1]["t"] < 6 * 3600]
    if measured:
        parts.append(f"Limite du gel mesurée par un ballon-sonde capté ici : vers {int(round(measured[-1]['gel'], -2))} mètres.")
    if with_sondes:
        sondes_text = _sondes_text(lieu, sondes(lieu))
        if sondes_text:
            parts.append(sondes_text)
    if level >= 1:
        parts.append(f"Niveau de vigilance du Pi : {LEVEL_NAMES[level]}" +
                     (f", {', '.join(reasons)}" if reasons else "") + ".")
    publish_context(level, reasons)
    return " ".join(parts)
