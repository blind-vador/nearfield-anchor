"""Catalogue des capteurs sans fil que l'utilisateur peut appairer
(2026-10-07) : comment reconnaitre leur type dans une trame rtl_433 (par
les grandeurs qu'elle porte, quel que soit le fabricant), et quel geste
prouve que le capteur entendu est bien le sien (un voisin n'a pas sa main
dans le verre d'eau au meme moment).

Exemples de modeles 433 / 868 MHz decodes par rtl_433, tous lisibles sans
leur station ni passerelle :
  sol            Ecowitt WH51 / Froggit DP100 (Fineoffset-WH51)
  pluie          Ecowitt WH40 (Fineoffset-WH40), Bresser pluviometre
  station        Ecowitt WS69 / WH65, WS80, WS90 (Fineoffset-WH24/WH65/WS80/WS90),
                 Bresser 5-in-1, 6-in-1, 7-in-1 (vent, pluie, UV, lumiere)
  foudre         Ecowitt WH57 (Fineoffset-WH57), Acurite 6045M
  air            Ecowitt WH41 / WH43 (Fineoffset-WH0290, particules fines)
  co2            Ecowitt WH45 (Fineoffset-WH45)
  fuite          Ecowitt WH55 (Fineoffset-WH55)
  thermo_hygro   Ecowitt WN31 / WH31 (Fineoffset-WH31), WN32, LaCrosse TX141,
                 Nexus, inFactory, Bresser 3CH...
  thermo         Ecowitt WN34 (sonde de sol, d'eau)
"""
from phrases import Etiquettes

# (code, champs qui le trahissent) -- le premier qui correspond gagne
RULES = [
    ("sol", ("moisture",)),
    ("foudre", ("strike_count", "storm_dist", "storm_dist_km")),
    ("co2", ("co2_ppm",)),
    ("air", ("pm2_5_ug_m3", "pm10_ug_m3", "pm1_ug_m3")),
    ("fuite", ("leak", "water_leak", "detect_wet")),
    ("station", ("wind_avg_km_h", "wind_avg_m_s", "wind_max_km_h", "wind_max_m_s", "wind_dir_deg")),
    ("pluie", ("rain_mm", "rain_in", "rain_rate_mm_h")),
    ("thermo_hygro", ("humidity",)),
    ("thermo", ("temperature_C", "temperature_F")),
]
NAMES = Etiquettes("capteur.genre", {
    "sol": "sonde d'humidité du sol", "pluie": "pluviomètre", "station": "station météo, vent et pluie",
    "foudre": "détecteur d'orages", "air": "capteur de particules fines", "co2": "capteur de CO2",
    "fuite": "détecteur de fuite d'eau", "thermo_hygro": "thermomètre hygromètre", "thermo": "thermomètre",
    "autre": "capteur"}, "genre d'un capteur sans fil, à l'appairage")
# geste qui prouve que c'est le sien (phrase capteur.geste.<code>) ; None : pas de test simple
GESTURES = {"sol": "sol", "pluie": "pluie", "station": "station", "co2": "co2", "fuite": "fuite",
            "thermo_hygro": "main", "thermo": "main", "foudre": None, "air": None, "autre": None}


def kind(event):
    for code, fields in RULES:
        if any(event.get(f) is not None for f in fields):
            return code
    return "autre"


def _num(event, *fields):
    for f in fields:
        try:
            if event.get(f) is not None:
                return float(event[f])
        except (TypeError, ValueError):
            continue
    return None


def gesture_passed(code, first, now):
    """Le geste demande a-t-il fait bouger les mesures ? first : premiere
    trame entendue (avant le geste), now : trame actuelle."""
    if code == "sol":
        a, b = _num(first, "moisture"), _num(now, "moisture")
        raw_a, raw_b = _num(first, "ad_raw"), _num(now, "ad_raw")
        return (a is not None and b is not None and b - a >= 25) or \
            (raw_a is not None and raw_b is not None and abs(raw_b - raw_a) >= 100)
    if code in ("pluie", "station"):
        a, b = _num(first, "rain_mm", "rain_in"), _num(now, "rain_mm", "rain_in")
        if a is not None and b is not None and b > a:
            return True
        if code == "station":
            w = _num(now, "wind_max_km_h", "wind_avg_km_h")
            w = w if w is not None else (_num(now, "wind_max_m_s", "wind_avg_m_s") or 0) * 3.6
            return w >= 5
        return False
    if code == "co2":
        a, b = _num(first, "co2_ppm"), _num(now, "co2_ppm")
        return a is not None and b is not None and b - a >= 200
    if code == "fuite":
        return any(now.get(f) in (1, True, "1", "WET", "wet") for f in ("leak", "water_leak", "detect_wet"))
    if code in ("thermo_hygro", "thermo"):
        ta, tb = _num(first, "temperature_C"), _num(now, "temperature_C")
        ha, hb = _num(first, "humidity"), _num(now, "humidity")
        return (ta is not None and tb is not None and abs(tb - ta) >= 1.0) or \
            (ha is not None and hb is not None and abs(hb - ha) >= 5)
    return False


def soil_percent(event, calibration=None):
    """Humidite du sol en %, recalculee sur l'etalonnage sec / mouille fait a
    l'appairage (valeur brute ad_raw) quand il existe."""
    raw = _num(event, "ad_raw")
    if calibration and raw is not None:
        dry, wet = calibration.get("sec"), calibration.get("mouille")
        if dry is not None and wet is not None and abs(wet - dry) >= 50:
            return max(0.0, min(100.0, (raw - dry) / (wet - dry) * 100))
    return _num(event, "moisture")
