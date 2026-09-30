"""Reception ADS-B directe (1090 MHz) par la cle du scanner, en fenetre
d'ecoute : les avions et helicopteres a portee de l'antenne, sans passer
par OpenSky ni son quota.

dump1090-mutability (paquet Debian ; son service automatique est
DESACTIVE, il s'accaparait la cle) tourne DWELL secondes et ecrit
aircraft.json ; les appareils avec position sont publies dans LOCAL_PATH,
que adsb_watch fusionne avec les donnees OpenSky a chaque sondage (une
seule logique d'annonce, pas de doublon).
"""
import json
import os
import shutil
import subprocess
import tempfile
import time

import veille_donnees as vd

BINARY = "dump1090-mutability"
LOCAL_PATH = "/tmp/adsb_local.json"
KEEP_SEC = 600
FEET_TO_M = 0.3048
FPM_TO_MS = 0.00508
LOW_ALT_M = 2000
NEAR_KM = 20


def _load():
    try:
        with open(LOCAL_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def scan_window(dwell_sec=30):
    if shutil.which(BINARY) is None:
        print("[adsb_local] dump1090-mutability introuvable")
        return False
    json_dir = tempfile.mkdtemp(prefix="dump1090_")
    proc = subprocess.Popen([BINARY, "--device-index", "0", "--gain", "-10", "--quiet",
                             "--write-json", json_dir, "--write-json-every", "2"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + dwell_sec
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.5)
        try:
            with open(os.path.join(json_dir, "aircraft.json"), encoding="utf-8") as f:
                aircraft = json.load(f).get("aircraft") or []
        except (OSError, ValueError):
            aircraft = []
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(json_dir, ignore_errors=True)
    now = time.time()
    known = {k: v for k, v in _load().items() if now - v.get("t", 0) < KEEP_SEC}
    positioned, low_near = 0, 0
    for a in aircraft:
        if a.get("lat") is None or a.get("lon") is None or a.get("seen_pos", 99) > dwell_sec:
            continue
        positioned += 1
        altitude = a.get("altitude")
        on_ground = altitude == "ground"
        entry = {"icao24": a["hex"].lower(), "callsign": (a.get("flight") or "").strip(),
                 "lat": a["lat"], "lon": a["lon"], "on_ground": on_ground,
                 "alt_m": None if on_ground or altitude is None else altitude * FEET_TO_M,
                 "vrate": (a.get("vert_rate") or 0) * FPM_TO_MS, "category": a.get("category"),
                 "t": now - a.get("seen_pos", 0)}
        known[entry["icao24"]] = entry
        if not on_ground and (entry["alt_m"] or 0) < LOW_ALT_M and vd.distance_km(a["lat"], a["lon"]) < NEAR_KM:
            low_near += 1
        vd.archive("adsb_local", entry)
    try:
        tmp = LOCAL_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(known, f)
        os.replace(tmp, LOCAL_PATH)
    except OSError:
        pass
    if aircraft:
        print(f"[adsb_local] {len(aircraft)} appareil(s), {positioned} avec position")
    # Pique l'attention seulement pour ce qui vole bas et pres (helicopteres,
    # approches) : les avions de ligne en croisiere sont toujours la.
    return low_near > 0


def as_states(max_age_sec=KEEP_SEC):
    """Appareils recents au format "state vector" d'OpenSky (pour adsb_watch)."""
    now = time.time()
    states = []
    for e in _load().values():
        if now - e.get("t", 0) > max_age_sec:
            continue
        s = [None] * 18
        s[0], s[1] = e["icao24"], e["callsign"] or None
        s[5], s[6], s[7], s[8] = e["lon"], e["lat"], e["alt_m"], e["on_ground"]
        s[11] = e["vrate"]
        states.append(s)
    return states
