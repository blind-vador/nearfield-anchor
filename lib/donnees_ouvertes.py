"""Donnees ouvertes "quasi vitales" autour de chez soi, sans cle :

  - METAR (aviationweather.gov) : meteo officielle des aeroports proche et
    grand (avions.json), traduite en clair ; orage ou cumulonimbus signales ;
  - hauteur des rivieres en temps reel (Hub'Eau v2, stations a moins de
    RIVER_RADIUS_KM) ; montee rapide signalee ;
  - seismes (EMSC, portail europeen) ;
  - qualite de l'air et pollens (Open-Meteo, modele europeen CAMS).

Textes pour le bulletin meteo et la commande "environnement" ; watch()
est appele regulierement par meteo_watch pour les annonces (seisme,
riviere qui monte vite).
"""
import json
import math
import re
import time
from datetime import datetime, timezone

import requests

import reglages

TIMEOUT = 20
METAR_URL = "https://aviationweather.gov/api/data/metar"
HUBEAU_STATIONS = "https://hubeau.eaufrance.fr/api/v2/hydrometrie/referentiel/stations"
HUBEAU_OBS = "https://hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr"
EMSC_URL = "https://www.seismicportal.eu/fdsnws/event/1/query"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
STATE_PATH = "/tmp/donnees_ouvertes.json"

RIVER_RADIUS_KM = 20
RIVER_MAX = 4
RIVER_RISE_CM_1H = 20          # montee rapide : annonce
QUAKE_RADIUS_DEG = 5           # ~550 km de rayon de recherche
QUAKE_NEAR_KM = 150
QUAKE_NEAR_MAG = 2.5           # annonce si plus fort, a moins de QUAKE_NEAR_KM
QUAKE_FAR_MAG = 4.5            # ou plus fort que ca, n'importe ou dans le rayon
DIRECTIONS = ["nord", "nord-est", "est", "sud-est", "sud", "sud-ouest", "ouest", "nord-ouest"]

CLOUDS = {"FEW": "quelques nuages", "SCT": "nuages épars", "BKN": "ciel nuageux", "OVC": "ciel couvert"}
WEATHER = {"RA": "pluie", "DZ": "bruine", "SN": "neige", "GR": "grêle", "GS": "grésil", "FG": "brouillard",
           "BR": "brume", "HZ": "brume sèche", "TS": "orage", "SH": "averses", "FZ": "verglaçante",
           "SQ": "grains", "FU": "fumée", "PL": "granules de glace", "IC": "cristaux de glace"}


# --- outils -----------------------------------------------------------------------

def _get(url, **params):
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


def _home():
    lieu = reglages.get("lieu")
    return float(lieu["lat"]), float(lieu["lon"])


def _distance(lat0, lon0, lat, lon):
    p0, p1 = math.radians(lat0), math.radians(lat)
    a = math.sin((p1 - p0) / 2) ** 2 + math.cos(p0) * math.cos(p1) * math.sin(math.radians(lon - lon0) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def _towards(lat0, lon0, lat, lon):
    y = math.sin(math.radians(lon - lon0)) * math.cos(math.radians(lat))
    x = (math.cos(math.radians(lat0)) * math.sin(math.radians(lat))
         - math.sin(math.radians(lat0)) * math.cos(math.radians(lat)) * math.cos(math.radians(lon - lon0)))
    name = DIRECTIONS[int(((math.degrees(math.atan2(y, x)) + 360) % 360 + 22.5) // 45) % 8]
    return f"à l'{name}" if name[0] in "eo" else f"au {name}"


def _num(value, decimals=0):
    return f"{value:.{decimals}f}".replace(".", ",").replace("-", "moins ")


def _state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


# --- METAR ------------------------------------------------------------------------------

def metar_raw(codes):
    try:
        text = _get(METAR_URL, ids=",".join(codes), format="raw").text
    except requests.RequestException:
        return {}
    found = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) > 2 and parts[0] in ("METAR", "SPECI"):
            found[parts[1]] = line
        elif parts and len(parts[0]) == 4 and parts[0].isalpha():
            found[parts[0]] = line
    return found


def metar_meaning(raw):
    """METAR -> (phrase en clair, orage signale ?)."""
    tokens = raw.split()
    parts, storm = [], False
    when = next((t for t in tokens if re.fullmatch(r"\d{6}Z", t)), None)
    if when:
        utc = datetime.now(timezone.utc).replace(hour=int(when[2:4]), minute=int(when[4:6]), second=0, microsecond=0)
        local = utc.astimezone()
        parts.append(f"relevé de {local.hour} heures" + (f" {local.minute}" if local.minute else ""))
    wind = next((re.fullmatch(r"(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT", t) for t in tokens
                 if re.fullmatch(r"(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT", t)), None)
    if wind:
        speed = round(int(wind.group(2)) * 1.852)
        if speed < 4:
            parts.append("vent calme")
        else:
            if wind.group(1) == "VRB":
                origin = "de direction variable"
            else:
                name = DIRECTIONS[int(((int(wind.group(1)) % 360) + 22.5) // 45) % 8]
                origin = f"de l'{name}" if name[0] in "eo" else f"du {name}"
            text = f"vent {origin} à {speed} kilomètres heure"
            if wind.group(4):
                text += f", rafales à {round(int(wind.group(4)) * 1.852)}"
            parts.append(text)
    if "CAVOK" in tokens:
        parts.append("temps clair, bonne visibilité")
    else:
        vis = next((t for t in tokens if re.fullmatch(r"\d{4}", t)), None)
        if vis:
            parts.append("visibilité de plus de 10 kilomètres" if vis == "9999"
                         else f"visibilité {int(vis)} mètres" if int(vis) < 1000
                         else f"visibilité {_num(int(vis) / 1000, 1)} kilomètres")
    for t in tokens:
        m = re.fullmatch(r"(\+|-|VC)?((?:TS|SH|FZ|BL|DR|MI|BC|PR)?(?:RA|DZ|SN|SG|GR|GS|PL|IC|FG|BR|HZ|FU|SQ)*)", t)
        if not m or not m.group(2) or t in ("CAVOK", "NOSIG", "AUTO"):
            continue
        codes = re.findall(r"TS|SH|FZ|RA|DZ|SN|GR|GS|FG|BR|HZ|SQ|FU|PL|IC", m.group(2))
        if not codes:
            continue
        storm = storm or "TS" in codes
        intensity = m.group(1) or ""
        precip = [WEATHER[c] for c in codes if c not in ("TS", "SH", "FZ")]
        if intensity == "+":
            precip = [f"forte {p}" for p in precip]
        elif intensity == "-":
            precip = [f"{p} faible" for p in precip]
        if "FZ" in codes:
            precip = [f"{p} verglaçante" for p in precip]
        if "TS" in codes:
            text = "orage" + (" avec " + " et ".join(precip) if precip else "")
        elif "SH" in codes:
            text = "averses" + (" de " + " et ".join(precip) if precip else "")
        else:
            text = " et ".join(precip)
        if intensity == "VC":
            text += " à proximité"
        parts.append(text)
    layers = [re.fullmatch(r"(FEW|SCT|BKN|OVC|///|VV)(\d{3}|///)(CB|TCU|///)?", t) for t in tokens]
    layers = [m for m in layers if m]
    covered = [m for m in layers if m.group(1) in ("BKN", "OVC") and m.group(2).isdigit()]
    if covered:
        word = "ciel couvert" if any(m.group(1) == "OVC" for m in layers) else "ciel nuageux"
        parts.append(f"{word}, nuages les plus bas à {int(round(int(covered[0].group(2)) * 30.48, -1))} mètres")
    elif any(m.group(1) in ("FEW", "SCT") for m in layers):
        parts.append("quelques nuages")
    if any(m.group(3) == "CB" for m in layers):
        storm = True
        parts.append("cumulonimbus, des nuages d'orage")
    elif any(m.group(3) == "TCU" for m in layers):
        parts.append("cumulus bourgeonnants, des nuages qui grossissent et peuvent tourner à l'averse")
    temp = next((re.fullmatch(r"(M?\d{2})/(M?\d{2})?", t) for t in tokens if re.fullmatch(r"(M?\d{2})/(M?\d{2})?", t)), None)
    if temp:
        parts.append(f"{_num(int(temp.group(1).replace('M', '-')))} degrés")
    qnh = next((t for t in tokens if re.fullmatch(r"Q\d{4}", t)), None)
    if qnh:
        parts.append(f"pression {int(qnh[1:])}")
    # doublons (meme phenomene sur plusieurs couches) et ordre conserve
    seen, clean = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            clean.append(p)
    return ", ".join(clean), storm


def airports_text():
    avions = reglages.get("avions")
    airports = [(avions["proche"]["oaci"], avions["proche"]["nom"]), (avions["grand"]["oaci"], avions["grand"]["nom"])]
    raws = metar_raw([c for c, _ in airports])
    lines, storms = [], []
    for code, name in airports:
        if code not in raws:
            continue
        text, storm = metar_meaning(raws[code])
        lines.append(f"Aéroport de {name}, {text}")
        if storm:
            storms.append(name)
    return (". ".join(lines) + "." if lines else ""), storms


# --- rivieres ----------------------------------------------------------------------------

def _river_stations():
    state = _state()
    cached = state.get("stations_riviere")
    if cached and time.time() - state.get("stations_t", 0) < 86400:
        return cached
    lat, lon = _home()
    data = _get(HUBEAU_STATIONS, latitude=lat, longitude=lon, distance=RIVER_RADIUS_KM, size=30,
                format="json").json().get("data", [])
    stations = [{"code": s["code_station"], "nom": s["libelle_station"].replace(" [HS]", ""),
                 "km": _distance(lat, lon, s["latitude_station"], s["longitude_station"])
                 if s.get("latitude_station") is not None else 99}
                for s in data if s.get("en_service")]
    stations.sort(key=lambda s: s["km"])
    state.update(stations_riviere=stations, stations_t=time.time())
    _save_state(state)
    return stations


def rivers():
    """[{nom, hauteur_mm, variation_1h_mm, variation_2h_mm, debit_m3s, retenue}] (stations qui repondent)."""
    found, rivers_seen = [], set()
    for st in _river_stations():
        if len(found) >= RIVER_MAX:
            break
        river = st["nom"].split(" à ")[0].strip("[] ")
        if river in rivers_seen:
            continue  # une station par riviere : la plus proche
        try:
            obs = _get(HUBEAU_OBS, code_entite=st["code"], size=200, sort="desc").json().get("data", [])
        except requests.RequestException:
            continue
        heights = [(o["date_obs"], o["resultat_obs"]) for o in obs if o.get("grandeur_hydro") == "H"]
        flows = [o["resultat_obs"] for o in obs if o.get("grandeur_hydro") == "Q"]
        if not heights:
            continue
        def at(hours):
            now = datetime.fromisoformat(heights[0][0].replace("Z", "+00:00")).timestamp()
            for date, value in heights:
                if now - datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp() >= hours * 3600:
                    return value
            return None
        h = heights[0][1]
        rivers_seen.add(river)
        found.append({"nom": st["nom"], "hauteur_mm": h, "variation_1h_mm": (h - at(1)) if at(1) is not None else None,
                      "variation_2h_mm": (h - at(2)) if at(2) is not None else None,
                      "debit_m3s": flows[0] / 1000 if flows else None, "retenue": h > 100000})
    return found


def rivers_text(found=None):
    found = rivers() if found is None else found
    lines = []
    for r in found:
        delta = r["variation_2h_mm"]
        trend = ("stable" if delta is None or abs(delta) < 20 else
                 f"en {'hausse' if delta > 0 else 'baisse'} de {_num(abs(delta) / 10)} centimètres en 2 heures")
        if r["retenue"]:
            lines.append(f"{r['nom']} : niveau de la retenue {_num(r['hauteur_mm'] / 1000, 2)} mètres d'altitude, {trend}")
        else:
            text = f"{r['nom']} : {_num(r['hauteur_mm'] / 10)} centimètres, {trend}"
            if r["debit_m3s"] is not None and r["debit_m3s"] >= 0.05:
                text += f", débit {_num(r['debit_m3s'], 1)} mètre{'s' if r['debit_m3s'] >= 2 else ''} cube par seconde"
            lines.append(text)
    return ("Rivières : " + ". ".join(lines) + ".") if lines else ""


# --- seismes ------------------------------------------------------------------------------

def quakes(hours=24):
    lat, lon = _home()
    start = datetime.now(timezone.utc).timestamp() - hours * 3600
    try:
        data = _get(EMSC_URL, lat=lat, lon=lon, maxradius=QUAKE_RADIUS_DEG, format="json", limit=50,
                    starttime=datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")).json()
    except (requests.RequestException, ValueError):
        return None
    found = []
    for f in data.get("features", []):
        p = f["properties"]
        km = _distance(lat, lon, p["lat"], p["lon"])
        found.append({"id": p.get("unid") or f.get("id"), "mag": p.get("mag"), "km": km,
                      "direction": _towards(lat, lon, p["lat"], p["lon"]), "region": p.get("flynn_region", ""),
                      "t": datetime.fromisoformat(p["time"].replace("Z", "+00:00")).timestamp(),
                      "profondeur": p.get("depth")})
    found.sort(key=lambda q: -q["t"])
    return found


def quake_sentence(q):
    hours = int((time.time() - q["t"]) // 3600)
    when = "il y a moins d'une heure" if hours < 1 else f"il y a {hours} heure{'s' if hours > 1 else ''}"
    felt = (" Probablement ressenti dans la région." if q["mag"] >= 3.5 and q["km"] < 100
            else " Trop faible ou trop loin pour être ressenti ici." if q["mag"] < 3 or q["km"] > 200 else "")
    return (f"Séisme de magnitude {_num(q['mag'], 1)}, {when}, à {round(q['km'])} kilomètres {q['direction']}."
            + felt)


def quakes_text(found=None):
    found = quakes() if found is None else found
    if found is None:
        return ""
    near = [q for q in found if q["km"] <= QUAKE_NEAR_KM]
    if not near:
        return "Aucun séisme à moins de 150 kilomètres depuis hier."
    return quake_sentence(near[0]) + (f" {len(near) - 1} autre{'s' if len(near) > 2 else ''} plus faible"
                                      f"{'s' if len(near) > 2 else ''} depuis hier." if len(near) > 1 else "")


# --- air et pollens -------------------------------------------------------------------------

AQI_WORDS = [(20, "bonne"), (40, "correcte"), (60, "moyenne"), (80, "médiocre"), (100, "mauvaise"), (999, "très mauvaise")]
POLLENS = {"alder_pollen": "aulne", "birch_pollen": "bouleau", "grass_pollen": "graminées",
           "mugwort_pollen": "armoise", "ragweed_pollen": "ambroisie"}
POLLEN_HIGH = 50   # grains par metre cube


def air_text():
    lat, lon = _home()
    try:
        cur = _get(AIR_URL, latitude=lat, longitude=lon, timezone="auto",
                   current="european_aqi," + ",".join(POLLENS)).json()["current"]
    except (requests.RequestException, ValueError, KeyError):
        return ""
    aqi = cur.get("european_aqi")
    if aqi is None:
        return ""
    word = next(w for limit, w in AQI_WORDS if aqi <= limit)
    text = f"Qualité de l'air {word}."
    high = [name for key, name in POLLENS.items() if (cur.get(key) or 0) >= POLLEN_HIGH]
    if high:
        text += " Pollens abondants : " + ", ".join(high) + "."
    return text


# --- veille (meteo_watch) -------------------------------------------------------------------

def watch():
    """Phrases a annoncer depuis le dernier passage : seisme notable, riviere
    qui monte vite. Rend aussi les orages signales par les aeroports."""
    state = _state()
    announced = set(state.get("annonces", []))
    sentences = []
    for q in quakes(hours=2) or []:
        notable = (q["km"] <= QUAKE_NEAR_KM and q["mag"] >= QUAKE_NEAR_MAG) or q["mag"] >= QUAKE_FAR_MAG
        if notable and f"seisme|{q['id']}" not in announced:
            announced.add(f"seisme|{q['id']}")
            sentences.append(quake_sentence(q))
    for r in rivers():
        rise = r["variation_1h_mm"]
        key = f"riviere|{r['nom']}|{int(time.time() // 7200)}"
        if not r["retenue"] and rise is not None and rise >= RIVER_RISE_CM_1H * 10 and key not in announced:
            announced.add(key)
            sentences.append(f"{r['nom']} monte vite : plus {_num(rise / 10)} centimètres en une heure, "
                             f"{_num(r['hauteur_mm'] / 10)} centimètres maintenant.")
    _, storms = airports_text()
    state["annonces"] = list(announced)[-300:]
    state["orages_aeroports"] = {"t": time.time(), "aeroports": storms}
    _save_state(state)
    return sentences


def airport_storms(max_age_sec=1800):
    data = _state().get("orages_aeroports") or {}
    return data.get("aeroports", []) if time.time() - data.get("t", 0) < max_age_sec else []


def summary():
    """Commande "environnement"."""
    parts = []
    airports, _ = airports_text()
    for text in (airports, rivers_text(), quakes_text(), air_text()):
        if text:
            parts.append(text)
    return " ".join(parts) or "Aucune donnée ouverte disponible pour le moment."
