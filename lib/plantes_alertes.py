"""Alertes pour les plantes (2026-10-07, etape 3) : ce qui demande d'agir
vite, d'apres les previsions Open-Meteo et la fiche de chaque plante.

  gel       plante dehors, minimale de la nuit a venir <= temp_gel_c (+ 2 C
            en pot : les racines n'ont pas la terre pour les proteger) ;
            annonce a partir de GEL_FROM_HOUR, pour laisser le temps d'agir
  rentrer   plante dehors a rentrer l'hiver (fiche), nuit a venir <= 5 C
            (si le gel n'est pas deja annonce)
  vent      plante dehors, rafales des 18 prochaines heures au-dela de ce
            que supporte la plante (fragile / moyen / robuste)
  chaleur   plante dehors, maximale des 24 prochaines heures >= temp_max_c
  soif      bilan d'eau depuis le dernier arrosage dit : evaporation (ET0
            FAO d'Open-Meteo, attenuee a l'interieur) moins la pluie si la
            plante la recoit ; au-dela de la reserve du pot ou de la terre.
            Sans arrosage dit, seule une plante sous la pluie est suivie
            (depuis la derniere vraie pluie)

Une alerte est redite toutes les REPEAT_SEC tant que la situation dure et
que l'utilisateur n'a pas dit "compris" (console : ack(), fichier ACK_PATH ;
une alerte nee apres le "compris" reste a dire). Silence 22 h - 7 h
(plantes_calendrier.QUIET_HOURS) : ce qui dure encore le matin est redit.
Appele par la boucle de plantes_calendrier (meteo_watch), toutes les 30 min.
"""
import datetime
import json
import os
import time

import requests

import reglages
from phrases import dire
from plantes import join_names

STATE_PATH = "/home/vador/autopilot/plantes_alertes.json"
ACK_PATH = "/home/vador/autopilot/plantes_acquit.json"
SOUND = "/media/nas/nd1/server/plantes/plante_alert1.mp3"  # Epidemic Sound (2026-10-18)
REPEAT_SEC = 30 * 60 - 60       # la boucle tourne toutes les 30 min : une minute de marge
GEL_FROM_HOUR = 17
SOIF_HOURS = (8, 21)
POT_FROST_MARGIN_C = 2
RENTRER_C = 5
GUSTS_KMH = {"fragile": 35, "moyen": 55, "robuste": 75}
MM_PER_HOT_DAY = 5              # evaporation d'une journee chaude d'ete (ET0)
INDOOR_ET_FACTOR = 0.3          # a l'interieur, sans soleil ni vent
GROUND_FACTOR = 3               # la pleine terre garde l'eau bien plus longtemps qu'un pot
FORECAST_CACHE_SEC = 3600
SOIL_THIRSTY = {"faible": 15, "moyen": 25, "fort": 35}   # % d'humidite du sol sous lequel la plante a soif


def soil_probe(plant):
    """(sonde, %) si une sonde de sol appairee mesure la terre de cette plante."""
    try:
        import capteurs_miens
        return capteurs_miens.soil_for_plant(plant["id"])
    except Exception as exc:
        print(f"[plantes_alertes] sonde : {exc!r}")
        return None
_forecast = {"t": 0, "data": None}


def forecast():
    """Previsions horaires et journalieres (7 jours passes + 2 a venir), cache 1 h."""
    if _forecast["data"] and time.time() - _forecast["t"] < FORECAST_CACHE_SEC:
        return _forecast["data"]
    lieu = reglages.get("lieu")
    resp = requests.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
        "latitude": lieu["lat"], "longitude": lieu["lon"], "timezone": "Europe/Paris",
        "hourly": "temperature_2m,wind_gusts_10m,relative_humidity_2m", "daily": "et0_fao_evapotranspiration,precipitation_sum",
        "past_days": 7, "forecast_days": 2})
    resp.raise_for_status()
    data = resp.json()
    _forecast.update(t=time.time(), data=data)
    return data


def _hours(fc, field, start, end):
    """[(datetime, valeur)] de start a end (exclus)."""
    out = []
    for stamp, value in zip(fc["hourly"]["time"], fc["hourly"][field]):
        t = datetime.datetime.fromisoformat(stamp)
        if start <= t < end and value is not None:
            out.append((t, value))
    return out


def _days(fc):
    """{date: (et0, pluie)}"""
    d = fc["daily"]
    return {datetime.date.fromisoformat(day): (et0 or 0.0, rain or 0.0)
            for day, et0, rain in zip(d["time"], d["et0_fao_evapotranspiration"], d["precipitation_sum"])}


def _num(value):
    return f"{value:.0f}".replace("-", "moins ")


def _when(t, now):
    if t.date() == now.date():
        return dire("plantes.quand_aujourdhui", heure=t.hour) if t.hour < 18 else dire("plantes.quand_ce_soir", heure=t.hour)
    return dire("plantes.quand_demain", heure=t.hour) if t.hour >= 6 else dire("plantes.quand_cette_nuit", heure=t.hour)


def water_deficit(plant, days, today):
    """(jours, millimetres manquants) depuis le dernier arrosage, ou None si inconnu."""
    exposed = not plant.get("dedans") and not plant.get("abri_pluie")
    start = None
    if plant.get("arrosee_le"):
        start = datetime.date.fromisoformat(plant["arrosee_le"])
    elif exposed:
        rainy = [d for d, (_, rain) in days.items() if d < today and rain >= 5]
        start = max(rainy) if rainy else None
    if start is None:
        return None
    deficit = 0.0
    for d in sorted(days):
        if start < d < today:
            et0, rain = days[d]
            et0 *= INDOOR_ET_FACTOR if plant.get("dedans") else 1
            deficit = max(0.0, deficit + et0 - (rain if exposed else 0))
    return (today - start).days, deficit


def conditions(plants, fc, now=None):
    """{cle: texte} des situations qui demandent d'agir maintenant. Gel,
    rentree, vent et chaleur : une alerte par situation, qui nomme toutes les
    plantes concernees (2026-10-07 : 5 plantes = pas 5 alertes identiques) ;
    soif : une par plante (chacune son arrosage)."""
    now = now or datetime.datetime.now()
    today = now.date()
    days = _days(fc)
    groups = {}  # (genre, date) -> {"noms": [...], valeurs}

    def add(kind, day, plant, **values):
        g = groups.setdefault((kind, str(day)), dict(values, noms=[]))
        g["noms"].append(plant["nom"])

    night_of = None
    night = []
    if now.hour >= GEL_FROM_HOUR or now.hour < 7:
        night_of = today if now.hour >= GEL_FROM_HOUR else today - datetime.timedelta(days=1)
        night = _hours(fc, "temperature_2m", max(now, datetime.datetime.combine(night_of, datetime.time(18))),
                       datetime.datetime.combine(night_of + datetime.timedelta(days=1), datetime.time(10)))
    gusts = _hours(fc, "wind_gusts_10m", now, now + datetime.timedelta(hours=18))
    heat = _hours(fc, "temperature_2m", now, now + datetime.timedelta(hours=24))
    out = {}
    for p in plants:
        fiche = p.get("fiche") or {}
        if not fiche:
            continue
        if not p.get("dedans"):
            if night:
                low_t, low = min(night, key=lambda x: x[1])
                try:
                    frost = float(fiche["temp_gel_c"]) + (POT_FROST_MARGIN_C if p.get("contenant") == "pot" else 0)
                except (KeyError, TypeError, ValueError):
                    frost = None
                if frost is not None and low <= frost:
                    add("gel", night_of, p, temp=_num(low), quand=_when(low_t, now))
                elif fiche.get("rentrer_l_hiver") and low <= RENTRER_C:
                    add("rentrer", night_of, p, temp=_num(low), quand=_when(low_t, now))
            limit = GUSTS_KMH.get(fiche.get("vent"))
            if limit and gusts:
                top_t, top = max(gusts, key=lambda x: x[1])
                if top >= limit:
                    add("vent", top_t.date(), p, vitesse=_num(top), quand=_when(top_t, now))
            try:
                hot = float(fiche["temp_max_c"])
            except (KeyError, TypeError, ValueError):
                hot = None
            if heat and hot is not None:
                top_t, top = max(heat, key=lambda x: x[1])
                if top >= hot:
                    add("chaleur", top_t.date(), p, temp=_num(top), quand=_when(top_t, now))
        if SOIF_HOURS[0] <= now.hour < SOIF_HOURS[1]:
            balance = water_deficit(p, days, today)
            try:
                reserve = max(1.0, float(fiche.get("jours_sans_eau_pot_ete") or 2)) * MM_PER_HOT_DAY
            except (TypeError, ValueError):
                reserve = 2 * MM_PER_HOT_DAY
            if p.get("contenant") == "pleine_terre":
                reserve *= GROUND_FACTOR
            probe = soil_probe(p)
            if probe:  # une vraie mesure : elle remplace l'estimation (2026-10-07)
                sensor, pct = probe
                if pct < SOIL_THIRSTY.get(fiche.get("eau"), 25):
                    out[f"{p['id']}|soif_sonde|{today}"] = dire("plantes.alerte_soif_sonde", nom=p["nom"],
                                                                sonde=sensor["nom"], pct=_num(pct), mot=p["nom"].lower())
            elif balance and balance[1] >= reserve:
                out[f"{p['id']}|soif|{p.get('arrosee_le')}"] = dire(
                    "plantes.alerte_soif", nom=p["nom"], jours=balance[0], mm=_num(balance[1]), mot=p["nom"].lower())
    for (kind, day), g in groups.items():
        values = {k: v for k, v in g.items() if k != "noms"}
        out[f"{kind}|{day}"] = dire(f"plantes.alerte_{kind}", noms=join_names(g["noms"]), **values)
    return out


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def ack():
    """ "compris" : rend les alertes en cours (textes) et les fait taire."""
    pending = [a["texte"] for a in active()]
    _save(ACK_PATH, {"t": time.time()})
    return pending


def active():
    """Alertes en cours pas encore acquittees."""
    acked = _load(ACK_PATH).get("t", 0)
    return [a for a in _load(STATE_PATH).get("alertes", {}).values() if a["premiere"] > acked]


def run_once(plants, quiet, announce, now=None):
    """Met a jour les alertes et dit ce qui doit l'etre. Rend les textes dits."""
    state = _load(STATE_PATH)
    alerts = state.get("alertes", {})
    try:
        current = conditions(plants, forecast(), now)
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        print(f"[plantes_alertes] previsions : {exc!r}")
        return []
    alerts = {k: v for k, v in alerts.items() if k in current}  # situation passee : on n'en parle plus
    acked = _load(ACK_PATH).get("t", 0)
    t = time.time()
    said = []
    for key, text in current.items():
        alert = alerts.setdefault(key, {"texte": text, "premiere": t, "derniere": 0})
        alert["texte"] = text  # chiffres mis a jour
        if quiet or alert["premiere"] <= acked or t - alert["derniere"] < REPEAT_SEC:
            continue
        spoken = text if not alert["derniere"] else dire("plantes.rappel", texte=text)
        announce(spoken + " " + dire("plantes.dis_compris"))
        alert["derniere"] = t
        said.append(spoken)
    state["alertes"] = alerts
    _save(STATE_PATH, state)
    return said
