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
import chemins
import json
import math
import os
import time
from datetime import datetime

import requests

import reglages
from phrases import Etiquettes, dire, nombre

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
SONDEHUB_URL = "https://api.v2.sondehub.org/sondes"
TIMEOUT = 20
HISTORY_PATH = chemins.base("meteo_historique.json")
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

WMO = Etiquettes("meteo.temps", {0: "ciel dégagé", 1: "ciel peu nuageux", 2: "éclaircies", 3: "ciel couvert",
       45: "brouillard", 48: "brouillard givrant", 51: "bruine légère", 53: "bruine",
       55: "bruine forte", 61: "pluie faible", 63: "pluie", 65: "forte pluie",
       66: "pluie verglaçante", 67: "forte pluie verglaçante", 71: "neige faible",
       73: "neige", 75: "forte neige", 77: "grains de neige", 80: "averses",
       81: "averses", 82: "fortes averses", 85: "averses de neige", 86: "fortes averses de neige",
       95: "orages", 96: "orages avec grêle", 99: "orages avec forte grêle"},
                 "temps qu'il fait, d'après le code météo")
STORM_CODES = {95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}   # neige / averses de neige (codes WMO)
GEL_SEUIL_C = 1.0                        # <= : risque de gel (heads-up du flash)
COLOR_NAMES = Etiquettes("vigilance.couleur", {2: "jaune", 3: "orange", 4: "rouge"}, "couleur de vigilance météo")
DIRECTIONS = ["nord", "nord-est", "est", "sud-est", "sud", "sud-ouest", "ouest", "nord-ouest"]
LEVEL_NAMES = Etiquettes("vigilance.niveau", {0: "calme", 1: "à surveiller", 2: "perturbation en approche",
                                                3: "danger imminent"}, "niveau de vigilance du Pi")
# Les directions restent des codes (enregistres) ; dites avec leur preposition.
DIRECTION_VERS = Etiquettes("direction.vers", {d: (f"à l'{d}" if d[0] in "eo" else f"au {d}") for d in DIRECTIONS},
                            "où se trouve quelque chose, après une distance : à 12 kilomètres au nord")
DIRECTION_DE = Etiquettes("direction.de", {d: (f"de l'{d}" if d[0] in "eo" else f"du {d}") for d in DIRECTIONS},
                          "d'où vient le vent ou la perturbation")


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
    towards = DIRECTION_VERS[_direction(lat0, lon0, lat, lon)]
    return dire("meteo.tout_pres") if km < 1 else dire("meteo.a_distance", distance=nombre(km, "kilomètre"),
                                                       direction=towards)


def _num(value, decimals=0):
    text = f"{value:.{decimals}f}"
    return text.replace(".", ",").replace("-", dire("meteo.moins") + " ")


def _wind_from(degrees):
    if degrees is None:
        return ""
    return DIRECTION_DE[DIRECTIONS[int(((degrees % 360) + 22.5) // 45) % 8]]


# --- donnees ouvertes --------------------------------------------------------

def forecast(lieu):
    lat, lon = _home(lieu)
    try:
        resp = requests.get(OPEN_METEO_URL, timeout=TIMEOUT, params={
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                       "wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl,precipitation",
            "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,"
                      "wind_gusts_10m,cape,pressure_msl,freezing_level_height,relative_humidity_2m,wind_speed_10m",
            # 6 h de passe : les releves anciens sont recales sur l'evolution du modele (meteo_agregat)
            "past_hours": 6, "forecast_hours": 24,
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
    sensors, _ = outdoor_sensors(max_age_sec=HISTORY_KEEP_SEC)
    for key, r in sensors.items():
        v = r.get("valeurs") or {}
        row = {"t": r.get("t"), "temp": v.get("temperature_C"), "humidite": v.get("humidity"),
               "rafale": v.get("wind_max_km_h"), "vent": v.get("wind_avg_km_h"), "pluie_cumul": v.get("rain_mm")}
        entry = history.setdefault(f"capteur:{key}", {"nom": r.get("nom"), "releves": []})
        if not entry["releves"] or entry["releves"][-1]["t"] != row["t"]:
            entry["releves"].append(row)
    for key in list(history):
        if key.startswith("capteur:") and key[len("capteur:"):] not in sensors:
            del history[key]  # n'est plus (ou n'a jamais ete) un thermometre exterieur vraisemblable
            continue
        history[key]["releves"] = [r for r in history[key]["releves"] if r.get("t") and now - r["t"] < HISTORY_KEEP_SEC]
        if not history[key]["releves"]:
            del history[key]
    _write_json(HISTORY_PATH, history)
    return history


PLAUSIBLE_SENSOR_C = 8      # ecart maxi a la reference pour un capteur "air exterieur"
PLAUSIBLE_STATION_C = 12    # stations radioamateur : plus de tolerance (relief, exposition)


def _median(values):
    values = sorted(values)
    return values[len(values) // 2] if values else None


def outdoor_sensors(reference=None, max_age_sec=2 * 3600):
    """Capteurs sans fil qui mesurent vraisemblablement l'air dehors :
    type meteo (pas cuisson, piscine, frigo, sol -- capteurs_watch) et
    temperature proche de la reference (modele, sinon mediane des capteurs).
    Rend (gardes {cle: lecture}, nombre d'ecartes)."""
    sensors = _read_json(SENSORS_PATH) or {}
    now = time.time()
    try:  # thermometres d'interieur probables (lib/capteurs_interieurs.py) : pas l'air dehors
        import capteurs_interieurs
        indoor = capteurs_interieurs.indoor_keys()
    except Exception:
        indoor = set()
    weather = {k: r for k, r in sensors.items()
               if r.get("type", "meteo") == "meteo" and now - r.get("t", 0) < max_age_sec
               and (r.get("valeurs") or {}).get("temperature_C") is not None and k not in indoor}
    others = sum(1 for k, r in sensors.items() if k not in weather and now - r.get("t", 0) < max_age_sec
                 and (r.get("valeurs") or {}).get("temperature_C") is not None)
    ref = reference if reference is not None else _median(r["valeurs"]["temperature_C"] for r in weather.values())
    kept = {k: r for k, r in weather.items()
            if ref is None or abs(r["valeurs"]["temperature_C"] - ref) <= PLAUSIBLE_SENSOR_C}
    return kept, others + len(weather) - len(kept)


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
            label = dire("meteo.station_meteo_radioamateur", where=where)
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
                              "texte": dire("meteo.chute_degres_minutes_chez", v1=_num(peak['temp'] - last['temp']), minutes=minutes, label=label)})
        if last.get("pression") is not None:
            before = _value_at(rows, "pression", last["t"] - 3 * 3600 + 600)
            if before and before["t"] <= last["t"] - 2 * 3600 and before["pression"] - last["pression"] >= PRESSURE_FALL_HPA:
                signs.append({"t": last["t"], "gravite": 1, "direction": direction, "type": "pression",
                              "texte": dire("meteo.pression_baisse_hectopascals_3", v1=_num(before['pression'] - last['pression']), label=label)})
        gust = last.get("rafale") if last.get("rafale") is not None else last.get("vent")
        if gust is not None:
            field = "rafale" if last.get("rafale") is not None else "vent"
            before = _value_at(rows, field, last["t"] - 30 * 60)
            if before and gust - before[field] >= GUST_JUMP_KMH:
                signs.append({"t": last["t"], "gravite": 2, "direction": direction, "type": "vent",
                              "texte": dire("meteo.vent_passe_kilometres_heure", round=round(before[field]), round2=round(gust), label=label)})
        if last.get("pluie_1h"):
            before = _value_at(rows, "pluie_1h", last["t"] - 30 * 60)
            if before and not before["pluie_1h"] and last["pluie_1h"] >= 0.5:
                signs.append({"t": last["t"], "gravite": 1, "direction": direction, "type": "pluie",
                              "texte": dire("meteo.pluie_vient_commencer_chez", label=label)})
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
                s["texte"] += dire("meteo.signe_souvent_suivi_orage", hits=hits, total=total)
    signs.sort(key=lambda s: -s["t"])
    return signs


# --- analyse ------------------------------------------------------------------

def _hourly_index(fc):
    """Indice de l'heure courante dans les series horaires."""
    times = (fc.get("hourly") or {}).get("time") or []
    now = datetime.now().strftime("%Y-%m-%dT%H:00")
    return times.index(now) if now in times else min(6, max(0, len(times) - 1))


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


def apercu_flash(lieu, fc=None):
    """Preventions courtes pour le flash horaire (pas des alarmes),
    tirees du modele au point du lieu choisi dans les reglages : gel de
    la nuit a venir (minimale horaire sur ~15 h <= seuil) et neige
    attendue (codes neige dans les prochaines heures). Deterministe :
    tout vient de forecast(lieu). Rend {"gel": mini_c|None, "neige": bool}."""
    fc = fc or forecast(lieu)
    out = {"gel": None, "neige": False}
    if not fc:
        return out
    hourly = fc.get("hourly") or {}
    i = _hourly_index(fc)
    temps = hourly.get("temperature_2m") or []
    nuit = [t for t in temps[i:i + 15] if t is not None]
    if nuit and min(nuit) <= GEL_SEUIL_C:
        out["gel"] = min(nuit)
    codes = hourly.get("weather_code") or []
    if any(c in SNOW_CODES for c in codes[i:i + 12] if c is not None):
        out["neige"] = True
    return out


FOUDRE_LOCALE_PATH = "/tmp/foudre_locale.json"   # ecrit par capteurs_watch._foudre
FOUDRE_LOCALE_MAX_AGE_SEC = 900                  # 15 min : impacts encore d'actualite
FOUDRE_LOCALE_PROCHE_KM = 5                       # danger imminent
FOUDRE_LOCALE_MOYENNE_KM = 15                     # perturbation en approche


def local_strikes(max_age_sec=FOUDRE_LOCALE_MAX_AGE_SEC):
    """Derniers impacts vus par un capteur de foudre pres du Pi (Acurite 6045,
    FineOffset WH57/WH31L -- voisin ou perso). Mesure PHYSIQUE locale, donc
    pres de la ville choisie : deterministe, aucune source distante. None si
    rien de recent."""
    d = _read_json(FOUDRE_LOCALE_PATH)
    if not d or time.time() - d.get("t", 0) > max_age_sec:
        return None
    return {"km": d.get("storm_dist_km"), "count": d.get("strike_count"),
            "source": d.get("source")}


def threat(lieu, fc=None, signs=None):
    """(niveau 0-3, raisons parlees)."""
    reasons, level = [], 0
    vigil = _read_json(METEO_STATE) or {}
    worst = max([v for v in vigil.values() if isinstance(v, int)] or [1])
    if worst >= 2:
        level = max(level, {2: 1, 3: 2, 4: 3}[worst])
        reasons.append(dire("meteo.vigilance_departement", v1=COLOR_NAMES[worst]))
    if fc:
        out = storm_outlook(fc)
        if out["orage_des"] is not None:
            level = max(level, 3 if out["orage_des"] <= 1 else 2)
            reasons.append((dire("meteo.orage_prevu") + " ") + (dire("meteo.maintenant_seul") if out["orage_des"] == 0
                                              else dire("meteo.heure", orage_des=out['orage_des'], pluriel='s' if out['orage_des'] > 1 else '')))
        if out["cape"][0] >= 1500:
            level = max(level, 2)
            reasons.append(dire("meteo.atmosphere_tres_instable_potentiel"))
        elif out["cape"][0] >= 500:
            level = max(level, 1)
            reasons.append(dire("meteo.atmosphere_instable"))
        if out["rafales"][0] >= 70:
            level = max(level, 2)
            reasons.append(dire("meteo.rafales_jusqu_kilometres_heure", round=round(out['rafales'][0])))
        elif out["rafales"][0] >= 50:
            level = max(level, 1)
            reasons.append(dire("meteo.rafales_jusqu_kilometres_heure_2", round=round(out['rafales'][0])))
        _, delta = pressure_trend(fc)
        if delta is not None and delta <= -PRESSURE_FALL_HPA:
            level = max(level, 1)
            reasons.append(dire("meteo.pression_chute_hectopascals_3", v1=_num(-delta)))
    try:
        import meteo_obs
        observed = meteo_obs.recent()
    except Exception:
        observed = None
    if observed:
        thunder = observed.get("orage")
        if thunder in ("proche", "tout_pres"):
            level = 3
            reasons.append(dire("meteo.obs_tonnerre_tout_pres" if thunder == "tout_pres" else "meteo.obs_tonnerre_proche"))
        elif thunder == "loin":
            level = max(level, 2)
            reasons.append(dire("meteo.tonnerre_entendu_au_loin"))
        if observed.get("precip") in ("violente", "grele"):
            level = max(level, 2)
            reasons.append(dire("meteo.obs_grele" if observed["precip"] == "grele" else "meteo.obs_averse_violente"))
        elif observed.get("precip") == "forte":
            level = max(level, 1)
            reasons.append(dire("meteo.forte_pluie_apres_ton"))
    foudre = local_strikes()
    if foudre:
        km = foudre.get("km")
        if km is None:
            level = max(level, 2)
            reasons.append(dire("meteo.foudre_capteur_detectee"))
        elif km <= FOUDRE_LOCALE_PROCHE_KM:
            level = 3
            reasons.append(dire("meteo.foudre_capteur_tout_pres", km=round(km)))
        elif km <= FOUDRE_LOCALE_MOYENNE_KM:
            level = max(level, 2)
            reasons.append(dire("meteo.foudre_capteur_proche", km=round(km)))
        else:
            level = max(level, 1)
            reasons.append(dire("meteo.foudre_capteur_loin", km=round(km)))
    try:
        import donnees_ouvertes
        storms = donnees_ouvertes.airport_storms()
    except Exception:
        storms = []
    if storms:
        level = max(level, 2)
        reasons.append(dire("meteo.orage_aeroports", aeroports=(" " + dire("meteo.et_de") + " ").join(storms)))
    signs = precursors(lieu) if signs is None else signs
    if any(s["gravite"] >= 2 for s in signs):
        level = max(level, 2)
        reasons.append(dire("meteo.signes_avant_coureurs_chez"))
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
    return DIRECTION_DE.get(first, first)


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
SCANNER_BUSY = Etiquettes("scanner_occupe", {"pause_thermique": "le Pi est trop chaud, même pour l'écoute météo seule",
                                             "manuel": "le scanner est en mode manuel",
                                             "accroche": "le scanner est accroché sur une fréquence",
                                             "appairage": "le scanner appaire un de tes capteurs"},
                          "pourquoi le scanner ne peut pas balayer les stations météo")


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

LAPSE_C_PER_KM = 6.5        # refroidissement moyen de l'air avec l'altitude (atmosphere standard)
TROPOPAUSE_M = 11000         # au-dela : haute atmosphere, toujours tres froide
ALOFT_ANOMALY_C = 4


def aloft_meaning(alt_m, temp_c, fc):
    """Ce que veut dire une temperature mesuree en altitude, en clair.
    Compare a ce qu'on attend en partant de la temperature au sol (6,5
    degres de moins par kilometre en moyenne)."""
    if alt_m >= TROPOPAUSE_M:
        return dire("meteo.haute_atmosphere_au_dessus")
    current = (fc or {}).get("current") or {}
    ground = current.get("temperature_2m")
    if ground is None:
        return ""
    elevation = (fc or {}).get("elevation") or 0
    expected = ground - LAPSE_C_PER_KM * (alt_m - elevation) / 1000
    gap = temp_c - expected
    if gap <= -ALOFT_ANOMALY_C:
        return (dire("meteo.plus_froid_habitude_cette"))
    if gap >= ALOFT_ANOMALY_C:
        return dire("meteo.plus_doux_habitude_haut")
    return dire("meteo.normal_cette_altitude_air")


def _sondes_text(lieu, found, fc=None):
    if not found:
        return ""
    lat0, lon0 = _home(lieu)
    s = min(found, key=lambda s: _distance_km(lat0, lon0, s["lat"], s["lon"]))
    text = (dire("meteo.ballon_sonde_ballon_meteo", v1=_where(lieu, s['lat'], s['lon'])))
    if s.get("alt"):
        text += dire("meteo.kilometres_altitude", v1=format(round(s['alt'] / 100) / 10, ".1f")).replace(".", ",")
        if s.get("temp") is not None:
            text += (" " + dire("meteo.fait_degres", v1=_num(s['temp'])))
            meaning = aloft_meaning(s["alt"], s["temp"], fc)
            if meaning:
                text += f" ; {meaning}"
        if (s.get("vitesse_v") or 0) < -2:
            text += dire("meteo.redescend_parachute")
    if len(found) > 1:
        text += dire("meteo.ballons_vol_tout_rayon", len=len(found), SONDE_RADIUS_KM=SONDE_RADIUS_KM)
    return text + "."


def bulletin(lieu=None, with_sondes=True, scan=None, scan_failed=None):
    """Bulletin meteo local complet, en phrases. scan : compte rendu du
    balayage radio qui vient d'etre fait (wait_scan). L'etat present vient
    de l'AGREGAT de toutes les mesures (lib/meteo_agregat.py : aeroports,
    stations, thermometres, captes pendant le balayage ou avant, recales sur
    le modele) ; le modele seul ne sert que de repli."""
    import meteo_agregat
    lieu = lieu or reglages.get("lieu")
    fc = forecast(lieu)
    state = meteo_agregat.agreger(lieu, fc, scan_started=(scan or {}).get("debut"))  # enregistre aussi l'historique
    signs = precursors(lieu)
    level, reasons = threat(lieu, fc, signs)
    parts = [dire("meteo.meteo", commune=lieu['commune'])]
    if fc and fc.get("current"):
        c = fc["current"]
        sky = WMO.get(c.get('weather_code'), dire('meteo.temps_indetermine'))
        measured = state["temp"]
        if measured:
            temp = measured["valeur"]
            model_temp = measured["modele"] if measured["modele"] is not None else c["temperature_2m"]
            key = "meteo.moment_mesure_modele" if abs(temp - model_temp) >= 1.5 else "meteo.moment_mesure"
            text = dire(key, ciel=sky, temperature=_num(temp), modele=_num(model_temp))
        else:
            temp, model_temp = c["temperature_2m"], c["temperature_2m"]
            text = dire("meteo.moment_degres", get=sky, v1=_num(temp))
        if c.get("apparent_temperature") is not None and abs(c["apparent_temperature"] - c["temperature_2m"]) >= 2:
            text += dire("meteo.ressenti", ressenti=_num(c['apparent_temperature'] + temp - model_temp))
        humidity = round(state["humidite"]["valeur"]) if state["humidite"] else c.get("relative_humidity_2m")
        text += dire("meteo.humidite_cent", humidity=humidity) + (dire("meteo.air_presque_sature_eau") if (humidity or 0) >= 95 else ".")
        wind = state["vent"]["valeur"] if state["vent"] else c.get("wind_speed_10m")
        if wind is not None:
            # direction : l'aeroport le plus proche qui a vraiment du vent (VRB ou calme : pas de direction)
            fresh_airport = sorted((a for a in state["aeroports"] if a.get("direction") is not None
                                    and (a.get("vent") or 0) >= 4 and time.time() - a["t"] < 3600), key=lambda a: a["km"])
            heading = fresh_airport[0]["direction"] if state["vent"] and fresh_airport else c.get('wind_direction_10m')
            text += (" " + dire("meteo.vent_kilometres_heure", v1=_wind_from(heading), round=round(wind)))
            gusts = max([r.get("rafale") or 0 for r in (state["vent"] or {}).get("sources", [])] + [c.get("wind_gusts_10m") or 0])
            if gusts >= 30:
                text += dire("meteo.rafales", round=round(gusts))
            text += "."
        parts.append(text)
        if state["pluie"]:
            parts.append(dire("agregat.pluie_mesuree", sources=", ".join(state["pluie"][:3])))
        parts.append(meteo_agregat.composition(state))
        pressure, delta = pressure_trend(fc)
        if pressure is not None and state["pression"]:
            pressure = state["pression"]["valeur"]
        if pressure is not None:
            trend = (dire("donnees.stable") if abs(delta) < 1 else
                     dire("meteo.3_heures", v1=dire('meteo.mot_hausse') if delta > 0 else dire('meteo.mot_baisse'), v3=_num(abs(delta))))
            if delta <= -2:
                meaning = dire("meteo.baisse_comme_celle_ci")
            elif delta >= 2:
                meaning = dire("meteo.hausse_annonce_plutot_amelioration")
            elif pressure >= 1020:
                meaning = dire("meteo.haute_pression_temps_plutot")
            elif pressure <= 1005:
                meaning = dire("meteo.basse_pression_temps_souvent")
            else:
                meaning = dire("meteo.pression_moyenne")
            parts.append(dire("meteo.pression_hectopascals", round=round(pressure), trend=trend, meaning=meaning))
        out = storm_outlook(fc, hours=12)
        hourly = fc["hourly"]
        upcoming = []
        if out["orage_des"] is not None:
            upcoming.append(dire("meteo.orage_possible") + " " + (dire("meteo.maintenant") if out["orage_des"] == 0
                                                  else dire("meteo.heure_2", orage_des=out['orage_des'], pluriel='s' if out['orage_des'] > 1 else '')))
        if out["pluie_des"] is not None:
            upcoming.append(dire("meteo.pluie_probable") + " " + (dire("meteo.maintenant_2") if out["pluie_des"] == 0
                                                  else dire("meteo.heure_3", pluie_des=out['pluie_des'], pluriel='s' if out['pluie_des'] > 1 else '')))
        if out["cape"][0] >= 1500:
            upcoming.append(dire("meteo.air_tres_instable_orages"))
        elif out["cape"][0] >= 500:
            upcoming.append(dire("meteo.air_instable_averses_peuvent"))
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
            upcoming.append(dire("meteo.chez_toi_tonnerre_deja"))
        if out["rafales"][0] >= 50:
            upcoming.append(dire("meteo.rafales_jusqu_kilometres_heure_3", round=round(out['rafales'][0])))
        i = _hourly_index(fc)
        freezing = (hourly.get("freezing_level_height") or [None] * (i + 1))[i]
        if freezing is not None and freezing < 2500:
            upcoming.append(dire("meteo.gele_au_dessus_metres", int=int(round(freezing, -2))))
        parts.append((dire("meteo.12_prochaines_heures") + " ") + (", ".join(upcoming) if upcoming else dire("meteo.rien_notable")) + ".")
        if calibrated:
            parts.append(calibrated)
        daily = fc.get("daily") or {}
        days = []
        for k, label in enumerate([dire("meteo.aujourdhui"), dire("meteo.demain"), dire("meteo.apres_demain")]):
            try:
                text = (dire("meteo.degres_2", label=label, get=WMO.get(daily['weather_code'][k], ''), v1=_num(daily['temperature_2m_min'][k]), v3=_num(daily['temperature_2m_max'][k])))
                if (daily["precipitation_sum"][k] or 0) >= 1:
                    text += dire("meteo.millimetres_pluie", round=round(daily['precipitation_sum'][k]))
                if (daily["wind_gusts_10m_max"][k] or 0) >= 60:
                    text += dire("meteo.rafales_2", round=round(daily['wind_gusts_10m_max'][k]))
                days.append(text)
            except (KeyError, IndexError, TypeError):
                continue
        if days:
            parts.append(". ".join(days) + ".")
    else:
        parts.append(dire("meteo.previsions_open_meteo_repondent"))
    vigil = _read_json(METEO_STATE) or {}
    active = [(lvl, name) for name, lvl in vigil.items() if isinstance(lvl, int) and lvl >= 2]
    if active:
        parts.append((dire("meteo.vigilance") + " ") + ", ".join(f"{COLOR_NAMES[l]} {n}" for l, n in sorted(active, reverse=True)) + ".")
    crue = _read_json(CRUE_STATE) or {}
    if any(isinstance(l, int) and l >= 2 for l in crue.values()):
        parts.append(dire("meteo.vigilance_crue_cours_riviere"))
    if not scan and scan_failed:  # balayage reussi : deja dit par la composition de l'agregat
        reason = scan_failed if isinstance(scan_failed, str) else dire("meteo.scanner_repondu")
        parts.append(dire("meteo.balayage_radio_cette_fois", reason=reason))
    try:
        import donnees_ouvertes
        rivers = donnees_ouvertes.rivers_text()
        found = donnees_ouvertes.quakes() or []
        quake = (donnees_ouvertes.quake_sentence(found[0])
                 if found and found[0]["km"] <= donnees_ouvertes.QUAKE_NEAR_KM else "")
        air = donnees_ouvertes.air_text()
    except Exception as exc:
        print(f"[meteo_locale] donnees ouvertes : {exc!r}")
        rivers = quake = air = ""
    for text in (rivers, quake, air):
        if text:
            parts.append(text)
    try:
        import meteo_obs
        last_obs = (meteo_obs.load() or [None])[-1]
    except Exception:
        last_obs = None
    if last_obs and time.time() - last_obs["t"] < 3 * 3600:
        minutes = round((time.time() - last_obs["t"]) / 60)
        when = dire("meteo.instant") if minutes < 1 else dire("meteo.minute", minutes=minutes, pluriel='s' if minutes > 1 else '')
        parts.append(dire("meteo.ta_derniere_observation", when=when, spoken=meteo_obs.spoken(last_obs['reponses'])))
    if signs:
        text = (dire("meteo.signes_avant_coureurs") + " ") + " ; ".join(s["texte"] for s in signs[:3])
        origin = approach_direction(signs)
        if origin and len({s["direction"] for s in signs if s["direction"]}) > 1:
            text += dire("meteo.premiers_signes_sont_venus", origin=origin)
        parts.append(text + ".")
    profiles = _read_json(SONDE_PROFILE_PATH) or {}
    measured = [p for p in profiles.values() if p.get("gel") and time.time() - p["points"][-1]["t"] < 6 * 3600]
    if measured:
        parts.append(dire("meteo.ballon_sonde_capte_ici", int=int(round(measured[-1]['gel'], -2))))
    if with_sondes:
        sondes_text = _sondes_text(lieu, sondes(lieu), fc)
        if sondes_text:
            parts.append(sondes_text)
    if level >= 1:
        parts.append(dire("meteo.niveau_vigilance_pi", v1=LEVEL_NAMES[level]) +
                     (f", {', '.join(reasons)}" if reasons else "") + ".")
    publish_context(level, reasons)
    return " ".join(parts)
