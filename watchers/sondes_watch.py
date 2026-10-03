"""Ballons-sondes meteo captes en direct par la cle (400 a 406 MHz).

SondeHub (reseau ouvert de recepteurs) dit quel ballon est en vol a portee
et sur quelle frequence ; la fenetre n'occupe la cle que dans ce cas. Le
signal est decode par les decodeurs libres rs1729/RS selon le modele
(RS41 de Vaisala, M10/M20 de Meteomodem -- ceux de Meteo-France --, DFM) :
rtl_fm -> sox -> decodeur --ptu --json.

Annonces : premiere reception d'un ballon (distance, direction, altitude,
temperature mesuree, montee ou descente), eclatement du ballon, limite du
gel mesuree (passage par zero degre pendant la montee). Le profil mesure
est publie pour le bulletin meteo (PROFILE_PATH) ; tout est archive.
"""
import json
import os
import shutil
import subprocess
import threading
import time

import meteo_locale
import reglages
import veille_donnees as vd
from sound_paths import RADIO_SCANER_DIR
from phrases import dire

DECODER_DIR = "/home/vador/autopilot/bin"
DECODERS = {"RS41": "rs41mod", "M10": "m10m20mod", "M20": "m10m20mod", "DFM": "dfm09mod"}
SOUND = os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3")
STATE_PATH = "/tmp/sondes_watch_state.json"
PROFILE_PATH = "/tmp/sondes_profil.json"
MAX_RANGE_KM = 250          # au-dela, pas de reception possible avec une antenne de toit
MIN_ALTITUDE_M = 500        # au sol : plus de signal utile
CANDIDATES_CACHE_SEC = 60
REANNOUNCE_SEC = 20 * 60
BURST_DESCENT_MS = -8       # vitesse verticale qui signe l'eclatement

_memory = vd.Memory(STATE_PATH)
_cache = (0.0, [])


def _binary(name):
    path = shutil.which(name) or os.path.join(DECODER_DIR, name)
    return path if os.path.exists(path) else None


def _candidates():
    """Ballons en vol a portee, du plus proche au plus lointain (cache 60 s)."""
    global _cache
    if time.time() - _cache[0] < CANDIDATES_CACHE_SEC:
        return _cache[1]
    lieu = reglages.get("lieu")
    found = meteo_locale.sondes(lieu) or []
    lat0, lon0 = float(lieu["lat"]), float(lieu["lon"])
    usable = []
    for s in found:
        km = meteo_locale._distance_km(lat0, lon0, s["lat"], s["lon"])
        if km <= MAX_RANGE_KM and (s.get("alt") or 0) >= MIN_ALTITUDE_M and s.get("frequence"):
            usable.append(dict(s, km=km))
    usable.sort(key=lambda s: s["km"])
    _cache = (time.time(), usable)
    return usable


def _sonde_type(serial, info):
    kind = (info.get("type") or "").upper()
    for key in DECODERS:
        if kind.startswith(key):
            return key
    return "RS41"


def _profile(serial, frame):
    """Ajoute le point au profil du ballon ; rend l'altitude du zero degre
    si elle vient d'etre franchie en montee."""
    profiles = meteo_locale._read_json(PROFILE_PATH) or {}
    points = profiles.setdefault(serial, {"points": [], "gel": None})
    prev = points["points"][-1] if points["points"] else None
    point = {"t": time.time(), "alt": frame.get("alt"), "temp": frame.get("temp")}
    points["points"] = (points["points"] + [point])[-400:]
    freezing = None
    if (prev and prev.get("temp") is not None and point["temp"] is not None and points["gel"] is None
            and prev["temp"] > 0 >= point["temp"] and point["alt"] > prev["alt"]):
        ratio = prev["temp"] / (prev["temp"] - point["temp"])
        freezing = prev["alt"] + ratio * (point["alt"] - prev["alt"])
        points["gel"] = freezing
    profiles = {k: v for k, v in profiles.items() if time.time() - v["points"][-1]["t"] < 6 * 3600}
    meteo_locale._write_json(PROFILE_PATH, profiles)
    return freezing


def _describe(frame, lieu):
    parts = [vd.where(frame["lat"], frame["lon"])]
    if frame.get("alt") is not None:
        parts.append(dire("sonde.altitude_metres", round=round(frame['alt'])))
    if frame.get("temp") is not None:
        parts.append(dire("sonde.degres", fr_number=vd.fr_number(frame['temp'])))
    if frame.get("humidity") is not None:
        parts.append(dire("sonde.humidite_cent", round=round(frame['humidity'])))
    vel = frame.get("vel_v")
    if vel is not None:
        parts.append(dire("sonde.montee") if vel > 1 else dire("sonde.descente") if vel < -1 else "stable")
    return ", ".join(parts)


def _process(frame, serial):
    """Annonce ce qui le merite pour une trame decodee. Rend True si annonce."""
    lieu = reglages.get("lieu")
    vd.archive("sondes", dict(frame, serie=serial))
    if frame.get("lat") is None or frame.get("lon") is None:
        return False
    freezing = _profile(serial, frame)
    if freezing is not None:
        vd.announce(dire("sonde.ballon_sonde_vient_mesurer", int=int(round(freezing, -1))),
                    SOUND, f"sonde_gel_{serial}", "sondes_watch")
        return True
    vel = frame.get("vel_v") or 0
    if vel <= BURST_DESCENT_MS and _memory.due(f"eclatement|{serial}", 6 * 3600):
        vd.announce(dire("sonde.ballon_sonde_eclate_vers", round=round(frame.get('alt') or 0), where=vd.where(frame['lat'], frame['lon'])), SOUND, f"sonde_eclatement_{serial}",
                    "sondes_watch")
        return True
    if _memory.due(f"sonde|{serial}", REANNOUNCE_SEC):
        vd.announce(dire("sonde.ballon_sonde_capte_direct", v1=_describe(frame, lieu)), SOUND,
                    f"sonde_{serial}", "sondes_watch")
        return True
    return False


def _reader(proc, frames):
    for raw in proc.stdout:
        try:
            frames.append(json.loads(raw))
        except ValueError:
            continue


def scan_window(dwell_sec=45):
    """Ecoute le ballon en vol le plus proche, s'il y en a un a portee."""
    try:
        candidates = _candidates()
    except Exception as exc:  # SondeHub indisponible : la fenetre est sautee
        print(f"[sondes_watch] SondeHub : {exc!r}")
        return False
    if not candidates:
        return False
    target = candidates[0]
    kind = _sonde_type(target["serie"], target)
    decoder = _binary(DECODERS[kind])
    if decoder is None:
        print(f"[sondes_watch] decodeur {DECODERS[kind]} introuvable")
        return False
    freq = int(float(target["frequence"]) * 1e6)
    rtl = subprocess.Popen(["rtl_fm", "-p", "0", "-M", "fm", "-F9", "-s", "15k", "-f", str(freq), "-"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sox = subprocess.Popen(["sox", "-t", "raw", "-r", "15k", "-e", "s", "-b", "16", "-c", "1", "-",
                            "-r", "48000", "-b", "8", "-t", "wav", "-", "highpass", "20"],
                           stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    rtl.stdout.close()
    dec = subprocess.Popen([decoder, "--ptu", "--json"], stdin=sox.stdout,  # sans fichier : lit l'entree
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sox.stdout.close()
    frames = []
    threading.Thread(target=_reader, args=(dec, frames), daemon=True).start()
    print(f"[sondes_watch] ecoute de {target['serie']} ({kind}) sur {freq / 1e6:.3f} MHz, "
          f"a {round(target['km'])} km, {round(target.get('alt') or 0)} m")
    announced, count = False, 0
    try:
        deadline = time.time() + dwell_sec
        while time.time() < deadline:
            time.sleep(0.5)
            while frames:
                frame = frames.pop(0)
                count += 1
                serial = frame.get("id") or target["serie"]
                if _process(frame, serial):
                    announced = True
    finally:
        for p in (rtl, sox, dec):
            if p.poll() is None:
                p.terminate()
        for p in (rtl, sox, dec):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        _memory.save()
    print(f"[sondes_watch] {count} trame(s) decodee(s)")
    return count > 0
