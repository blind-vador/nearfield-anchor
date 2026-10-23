"""Veille helicopteres (demande utilisateur 2026-10-17) : un helicoptere qui
reste au-dessus de la ville est souvent le premier signe qu'il s'y passe
quelque chose. Donnees publiques seulement (ADS-B et MLAT d'OpenSky,
antenne du Pi) ; appele a chaque sondage par adsb_watch.

  - arrivee      un helicoptere entre dans le rayon : qui (immatriculation,
                 exploitant, modele quand la base les connait), ou, a quelle
                 altitude ;
  - stationnaire il reste LOITER_MIN_SEC dans un cercle de LOITER_SPREAD_KM :
                 "tourne au-dessus de ..." (redit toutes les REPEAT_SEC) ;
  - depart       il disparait apres avoir tourne : combien de temps.
Chaque position va au journal du jour (NAS, helicos_<jour>.jsonl).

Reconnaissance : categorie ADS-B 8, ou type d'appareil (base adsbdb, voir
lib/avions_info.py). Limite honnete : un helicoptere qui n'emet pas sa
position (transpondeur Mode S seul, sans MLAT) reste invisible ici.
"""
import math
import time

import audio_lock
from phrases import dire, nombre
from speak import speak_with_lead_sound
from sound_paths import FLIGHT_SCANER_DIR

RADIUS_KM = 20.0
LOITER_MIN_SEC = 300
LOITER_SPREAD_KM = 3.0
REPEAT_SEC = 900
GONE_SEC = 300
TRACK_SEC = 900
FAST_POLL_SEC = 30
import sons
SOUND = sons.chemin("helico", f"{FLIGHT_SCANER_DIR}/simple_contact.mp3")  # son propre (2026-10-23, sons.py)
CATEGORY_HELI = 8
HELI_MAKERS = ("helicop", "eurocopter", "robinson", "bell ", "bell-", "agusta", "leonardo", "sikorsky",
               "guimbal", "aerospatiale", "sud aviation", "md helicopters", "kaman", "enstrom", "schweizer")
HELI_TYPES = {"EC20", "EC25", "EC30", "EC35", "EC45", "EC55", "EC75", "H160", "H60", "AS32", "AS50", "AS55",
              "AS65", "AS3B", "SA16", "SA31", "SA33", "SA34", "SA36", "ALO2", "ALO3", "A109", "A119", "A129",
              "A139", "A169", "A189", "B06", "B06T", "B105", "B212", "B214", "B407", "B412", "B427", "B429",
              "B430", "B505", "R22", "R44", "R66", "S76", "S92", "NH90", "TIGR", "G2CA", "MD52", "MD60",
              "H269", "EN28", "EN48"}
DIRECTIONS = ["nord", "nord_est", "est", "sud_est", "sud", "sud_ouest", "ouest", "nord_ouest"]

_tracks = {}  # icao24 -> {"points": [(t, lat, lon, alt)], "first", "label", "loiter_said", "loitered"}
_kind_cache = {}  # icao24 -> bool


def _distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _direction(ref_lat, ref_lon, lat, lon):
    """Point cardinal de (lat, lon) vu depuis la maison."""
    y = math.sin(math.radians(lon - ref_lon)) * math.cos(math.radians(lat))
    x = (math.cos(math.radians(ref_lat)) * math.sin(math.radians(lat))
         - math.sin(math.radians(ref_lat)) * math.cos(math.radians(lat)) * math.cos(math.radians(lon - ref_lon)))
    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
    return dire(f"helico.direction_{DIRECTIONS[int((bearing + 22.5) // 45) % 8]}")


def _info(icao24):
    try:
        import avions_info
        return avions_info.aircraft(hex_addr=icao24) or {}
    except Exception as exc:
        print(f"[helicos] base appareils : {exc!r}")
        return {}


def is_helicopter(icao24, category):
    if category == CATEGORY_HELI:
        return True
    if icao24 not in _kind_cache:
        ac = _info(icao24)
        text = f"{ac.get('manufacturer') or ''} {ac.get('type') or ''}".lower() + " "
        _kind_cache[icao24] = ((ac.get("icao_type") or "").upper() in HELI_TYPES
                               or any(m in text for m in HELI_MAKERS))
    return _kind_cache[icao24]


def _label(icao24, callsign):
    ac = _info(icao24)
    parts = [p for p in (ac.get("registration") or (callsign or "").strip(), ac.get("registered_owner"),
                         " ".join(x for x in (ac.get("manufacturer"), ac.get("type")) if x)) if p]
    return ", ".join(parts) or dire("helico.inconnu", icao=icao24.upper())


def _say(text):
    lock = audio_lock.acquire("helicos")
    try:
        if not lock.dropped:
            speak_with_lead_sound(SOUND, text, label="helicos", source="helicos")
    finally:
        audio_lock.release(lock)


def _archive(record):
    try:
        import veille_donnees as vd
        vd.archive("helicos", record)
    except Exception as exc:
        print(f"[helicos] journal : {exc!r}")


def _spread(points):
    lat = sum(p[1] for p in points) / len(points)
    lon = sum(p[2] for p in points) / len(points)
    return lat, lon, max(_distance_km(lat, lon, p[1], p[2]) for p in points)


def update(states, ref_lat, ref_lon, idx, now=None):
    """Un sondage ADS-B. idx : indices du vecteur d'etat OpenSky
    (icao24, callsign, lat, lon, alt, sol, categorie). Rend l'ensemble des
    icao24 d'helicopteres (adsb_watch ne les annonce pas en double) et
    l'intervalle de sondage conseille."""
    now = now or time.time()
    seen = set()
    for st in states:
        icao24, lat, lon = st[idx["icao24"]], st[idx["lat"]], st[idx["lon"]]
        if lat is None or lon is None or st[idx["sol"]]:
            continue
        category = st[idx["categorie"]] if len(st) > idx["categorie"] else None
        dist = _distance_km(ref_lat, ref_lon, lat, lon)
        if dist > RADIUS_KM or not is_helicopter(icao24, category):
            continue
        seen.add(icao24)
        alt = st[idx["alt"]]
        track = _tracks.get(icao24)
        if track is None:
            track = _tracks[icao24] = {"points": [], "first": now, "label": _label(icao24, st[idx["callsign"]]),
                                       "loiter_said": 0.0, "loitered": False}
            text = dire("helico.arrivee", nom=track["label"], distance=format(dist, ".0f"),
                        direction=_direction(ref_lat, ref_lon, lat, lon))
            if alt:
                text += " " + dire("helico.altitude", altitude=format(alt, ".0f"))
            print(f"[helicos] {text}")
            _say(text)
        track["last"] = now
        track["points"] = [p for p in track["points"] if now - p[0] <= TRACK_SEC] + [(now, lat, lon, alt)]
        _archive({"icao24": icao24, "nom": track["label"], "lat": lat, "lon": lon, "alt_m": alt,
                  "distance_km": round(dist, 2)})
        recent = [p for p in track["points"] if now - p[0] <= LOITER_MIN_SEC + 60]
        if now - track["first"] >= LOITER_MIN_SEC and len(recent) >= 3:
            c_lat, c_lon, spread = _spread(recent)
            if spread <= LOITER_SPREAD_KM and now - track["loiter_said"] >= REPEAT_SEC:
                track["loiter_said"], track["loitered"] = now, True
                text = dire("helico.stationnaire", nom=track["label"],
                            distance=format(_distance_km(ref_lat, ref_lon, c_lat, c_lon), ".1f").replace(".", ","),
                            direction=_direction(ref_lat, ref_lon, c_lat, c_lon),
                            duree=nombre(int((now - track["first"]) // 60), "minute"))
                print(f"[helicos] {text}")
                _say(text)
    for icao24, track in list(_tracks.items()):
        if icao24 in seen or now - track.get("last", now) < GONE_SEC:
            continue
        if track["loitered"]:
            text = dire("helico.depart", nom=track["label"],
                        duree=nombre(int((track["last"] - track["first"]) // 60), "minute"))
            print(f"[helicos] {text}")
            _say(text)
        del _tracks[icao24]
    return seen, (FAST_POLL_SEC if _tracks else None)
