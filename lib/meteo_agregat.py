"""Agregat meteo : TOUTES les mesures disponibles, fondues en un etat present.

Demande utilisateur (2026-10-03) : on ne controle pas quand les capteurs
emettent ; un capteur muet pendant le balayage a pourtant parle avant. Le
bulletin part donc d'un agregat de toutes les sources, chacune gardee
pendant la duree ou elle reste representative :

  - aeroports (METAR, toutes les 30 min, mesures normalisees) ;
  - stations meteo radioamateur (APRS) et thermometres sans fil (433/868 MHz),
    captes pendant le balayage ou avant (historique 24 h, meteo_historique.json) ;
  - modele AROME / Open-Meteo, heure par heure, 6 h de passe.

Methode (assimilation simplifiee, comme les analyses de nowcasting type
INCA) : un releve n'est pas repris tel quel ; on garde son ECART au modele a
l'heure ou il a ete mesure, et on l'applique au modele de maintenant.
L'ecart d'un site (exposition, relief, erreur locale du modele) persiste
quelques heures : son poids decroit avec l'age (exp(-age / tau)), et au-dela
de la fenetre de la grandeur il est oublie :

  temperature  fenetre 6 h, tau 3 h      (ecarts persistants, cycle diurne suivi par le modele)
  humidite     fenetre 3 h, tau 1,5 h    (varie avec la temperature et les averses)
  pression     fenetre 3 h, tau 3 h      (champ lisse, ~1 hPa / 3 h en temps normal)
  vent         fenetre 1 h, tau 30 min   (tres local, moyenne 10 min de l'OMM : pas de recalage)
  pluie        fenetre 1 h               (en cours ou non)

Poids d'une source = qualite (aeroport 1, station 0,6, thermometre 0,4 : on
ignore ou il est pose) x age x distance (exp(-km / 25)). Mediane ponderee :
un appareil au soleil ou une station mal reglee ne tire pas tout le monde.
Ecarts invraisemblables au modele ecartes (meme seuils que meteo_locale).
"""
import math
import time
from datetime import datetime, timedelta, timezone

import meteo_locale as ml
from phrases import dire, nombre

WINDOWS = {  # grandeur : (fenetre s, tau s, ecart maxi au modele)
    "temp": (6 * 3600, 3 * 3600, None),
    "humidite": (3 * 3600, 1.5 * 3600, 35),
    "pression": (3 * 3600, 3 * 3600, 8),
    "vent": (3600, 1800, None),
}
MODEL_FIELDS = {"temp": "temperature_2m", "humidite": "relative_humidity_2m", "pression": "pressure_msl"}
QUALITY = {"aeroport": 1.0, "station": 0.6, "capteur": 0.4}
TEMP_MAX_GAP = {"aeroport": 10, "station": ml.PLAUSIBLE_STATION_C, "capteur": ml.PLAUSIBLE_SENSOR_C}
DISTANCE_SCALE_KM = 25
LOCAL_SENSOR_KM = 0.3          # thermometres sans fil : a portee radio, donc tout pres
RAIN_WINDOW_SEC = 3600
FRESH_SEC = 20 * 60


# --- modele ------------------------------------------------------------------------

def _model_times(fc):
    """Instants (epoch) des pas horaires du modele."""
    hourly = (fc or {}).get("hourly") or {}
    offset = timedelta(seconds=(fc or {}).get("utc_offset_seconds") or 0)
    out = []
    for t in hourly.get("time") or []:
        try:
            out.append((datetime.fromisoformat(t) - offset).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            out.append(None)
    return out


def model_at(fc, field, when, times=None):
    """Valeur du modele a l'instant when (interpolation lineaire entre deux heures)."""
    values = ((fc or {}).get("hourly") or {}).get(field) or []
    times = times if times is not None else _model_times(fc)
    pts = [(t, v) for t, v in zip(times, values) if t is not None and v is not None]
    if not pts:
        return None
    if when <= pts[0][0]:
        return pts[0][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= when <= t1:
            return v0 + (v1 - v0) * (when - t0) / (t1 - t0)
    return pts[-1][1]


# --- releves -------------------------------------------------------------------------

def _airports():
    import donnees_ouvertes
    import reglages
    avions = reglages.get("avions") or {}
    places = [avions[r] for r in ("proche", "grand") if avions.get(r, {}).get("oaci")]
    raws = donnees_ouvertes.metar_raw([p["oaci"] for p in places]) if places else {}
    found = []
    for p in places:
        if p["oaci"] in raws:
            v = donnees_ouvertes.metar_values(raws[p["oaci"]])
            if v["t"]:
                found.append({"type": "aeroport", "nom": p["nom"], "lat": p.get("lat"), "lon": p.get("lon"), **v})
    return found


def readings(lieu, history=None):
    """Tous les releves connus : [{type, nom, km, t, temp, humidite, pression, vent, rafale, pluie...}]."""
    lat0, lon0 = ml._home(lieu)
    history = ml.record_observations() if history is None else history
    out = []
    for key, entry in history.items():
        kind = "station" if key.startswith("aprs:") else "capteur"
        if kind == "station" and entry.get("lat") is not None:
            km = ml._distance_km(lat0, lon0, entry["lat"], entry["lon"])
            if km > ml.STATION_MAX_KM:
                continue
        else:
            km = LOCAL_SENSOR_KM
        rows = sorted(entry.get("releves") or [], key=lambda r: r["t"])
        for row in rows:
            out.append({"type": kind, "nom": entry.get("nom") or key, "cle": key, "km": km, "t": row["t"],
                        "temp": row.get("temp"), "humidite": row.get("humidite"), "pression": row.get("pression"),
                        "vent": row.get("vent"), "rafale": row.get("rafale"), "pluie_1h": row.get("pluie_1h"),
                        "pluie_cumul": row.get("pluie_cumul")})
    try:
        for a in _airports():
            km = ml._distance_km(lat0, lon0, a["lat"], a["lon"]) if a.get("lat") is not None else 20
            out.append({**a, "cle": f"metar:{a['nom']}", "km": km})
    except Exception as exc:  # aeroports injoignables : on fait sans
        print(f"[meteo_agregat] METAR : {exc!r}")
    return out


# --- fusion ------------------------------------------------------------------------

def _weighted_median(pairs):
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    acc = 0.0
    for value, w in pairs:
        acc += w
        if acc >= total / 2:
            return value
    return None


def _estimate(field, rows, fc, now, times):
    window, tau, max_gap = WINDOWS[field]
    model_now = model_at(fc, MODEL_FIELDS[field], now, times) if field in MODEL_FIELDS else None
    latest = {}  # derniere mesure de chaque source dans la fenetre
    for r in rows:
        if r.get(field) is None or now - r["t"] > window or r["t"] > now + 300:
            continue
        if r["cle"] not in latest or r["t"] > latest[r["cle"]]["t"]:
            latest[r["cle"]] = r
    pairs, used, rejected = [], [], 0
    for r in latest.values():
        age = max(0.0, now - r["t"])
        weight = QUALITY[r["type"]] * math.exp(-age / tau) * math.exp(-r["km"] / DISTANCE_SCALE_KM)
        if model_now is not None:
            model_then = model_at(fc, MODEL_FIELDS[field], r["t"], times)
            gap = r[field] - model_then
            limit = TEMP_MAX_GAP[r["type"]] if field == "temp" else max_gap
            if limit is not None and abs(gap) > limit:
                rejected += 1
                continue
            pairs.append((gap, weight))
        else:
            pairs.append((r[field], weight))
        used.append(r)
    if not pairs:
        return None
    center = _weighted_median(pairs)
    value = model_now + center if model_now is not None else center
    if field == "humidite":
        value = min(100.0, max(0.0, value))
    return {"valeur": value, "modele": model_now, "sources": used, "ecartes": rejected,
            "plus_ancien": max(now - r["t"] for r in used)}


def _rain(rows, now):
    """Pluie mesuree dans l'heure : [noms des sources]."""
    wet = []
    by_key = {}
    for r in rows:
        by_key.setdefault(r["cle"], []).append(r)
    for key, items in by_key.items():
        items.sort(key=lambda r: r["t"])
        recent = [r for r in items if now - r["t"] <= RAIN_WINDOW_SEC]
        if not recent:
            continue
        last = recent[-1]
        cumul = [r["pluie_cumul"] for r in items if r.get("pluie_cumul") is not None
                 and now - r["t"] <= RAIN_WINDOW_SEC + 1800]
        if (last.get("pluie") is True or (last.get("pluie_1h") or 0) >= 0.2
                or (len(cumul) >= 2 and cumul[-1] - cumul[0] >= 0.2)):
            wet.append(last["nom"])
    return wet


def agreger(lieu=None, fc=None, scan_started=None):
    """Etat present fondu : {temp, humidite, pression, vent : estimation ou None,
    pluie : [sources], releves : nombre, par_type, frais, balayage, aeroports}."""
    import reglages
    lieu = lieu or reglages.get("lieu")
    fc = fc if fc is not None else ml.forecast(lieu)
    now = time.time()
    rows = readings(lieu)
    times = _model_times(fc) if fc else []
    state = {field: _estimate(field, rows, fc, now, times) for field in WINDOWS}
    state["pluie"] = _rain(rows, now)
    used = {}
    for field in WINDOWS:
        for r in (state[field] or {}).get("sources", []):
            used[r["cle"]] = r
    state["par_type"] = {}
    for r in used.values():
        state["par_type"][r["type"]] = state["par_type"].get(r["type"], 0) + 1
    state["releves"] = len(used)
    state["frais"] = sum(1 for r in used.values() if now - r["t"] <= FRESH_SEC)
    state["balayage"] = (sum(1 for r in used.values() if r["t"] >= scan_started)
                         if scan_started is not None else None)
    state["aeroports"] = [r for r in rows if r["type"] == "aeroport"]
    return state


# --- en clair -------------------------------------------------------------------------

def _num(value, decimals=0):
    return ml._num(value, decimals)


def _age(seconds):
    minutes = round(seconds / 60)
    if minutes < 60:
        return nombre(max(1, minutes), "minute")
    return nombre(round(minutes / 60), "heure")


def composition(state):
    """ "d'apres 9 releves : 1 aeroport, 2 stations radioamateur, 6 thermometres ; ..." """
    if not state["releves"]:
        return dire("agregat.aucun_releve")
    kinds = state["par_type"]
    parts = [nombre(kinds[k], *names) for k, names in (
        ("aeroport", ("aéroport", "aéroports")), ("station", ("station radioamateur", "stations radioamateur")),
        ("capteur", ("thermomètre sans fil", "thermomètres sans fil"))) if kinds.get(k)]
    text = dire("agregat.composition", releves=nombre(state["releves"], "relevé"), detail=", ".join(parts))
    if state["balayage"]:
        text += " " + dire("agregat.dont_balayage", nombre=state["balayage"])
    oldest = max([(state[f] or {}).get("plus_ancien", 0) for f in ("temp", "humidite", "pression")])
    if oldest > FRESH_SEC:
        text += " " + dire("agregat.recales", age=_age(oldest))
    rejected = max((state[f] or {}).get("ecartes", 0) for f in ("temp", "humidite", "pression"))
    if rejected:
        text += " " + dire("agregat.ecartes", appareils=nombre(rejected, "relevé"))
    return text
