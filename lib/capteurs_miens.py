"""Capteurs sans fil de l'utilisateur (2026-10-07) et leur appairage.

Registre : reglage "capteurs" du NAS, {"miens": [...]}, une entree par capteur :
  cle          "modele|id|canal" (comme capteurs_watch._key)
  modele, id, genre (capteurs_catalogue), nom (choisi a l'appairage)
  plante_id    sonde de sol : la plante dont elle mesure la terre
  etalonnage   sonde de sol : {"sec": brut dans l'air, "mouille": brut dans l'eau}
  ajoute_le

Appairage, entre la console et le scanner (radio_watch), par fichiers dans /tmp :
  REQUEST_PATH   la console demande la radio des capteurs jusqu'a "jusqu_a"
  EVENTS_PATH    radio_watch y ecrit chaque trame entendue (rtl_433 sur 433,92
                 et 868,3 MHz en alternance), apres une ligne {"_debut": t}
La logique de reconnaissance (Pairing) ne depend d'aucun materiel : elle
se teste avec des trames simulees.
"""
import datetime
import glob
import json
import os
import time

import capteurs_catalogue as cat
import reglages

REQUEST_PATH = "/tmp/capteurs_appairage.json"
EVENTS_PATH = "/tmp/capteurs_appairage.jsonl"
PAIRING_SEC = 300
KNOWN_DAYS = 30                 # capteurs entendus ces 30 derniers jours : ceux des voisins
ARCHIVE_GLOB = "/media/nas/nd1/server/radio_log/donnees/capteurs_2*.jsonl"


def key(event):
    return "|".join(str(event.get(k, "")) for k in ("model", "id", "channel"))


def load():
    return list(reglages.get("capteurs").get("miens") or [])


def save(items):
    data = reglages.get("capteurs")
    data["miens"] = items
    return reglages.save("capteurs", data)


def mine(event_key):
    return next((c for c in load() if c["cle"] == event_key), None)


def known_keys(days=KNOWN_DAYS):
    """Capteurs deja entendus (archives du NAS) ou deja appairies : un capteur
    neuf qu'on vient d'allumer n'y est pas."""
    keys = {c["cle"] for c in load()}
    limit = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    for path in sorted(glob.glob(ARCHIVE_GLOB)):
        if os.path.basename(path)[len("capteurs_"):len("capteurs_") + 10] < limit:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        keys.add(key(json.loads(line)))
                    except ValueError:
                        continue
        except OSError:
            continue
    return keys


# --- protocole console <-> radio_watch ---

def request(seconds=PAIRING_SEC):
    with open(REQUEST_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"jusqu_a": time.time() + seconds}, f)
    os.replace(REQUEST_PATH + ".tmp", REQUEST_PATH)


def stop():
    try:
        os.remove(REQUEST_PATH)
    except OSError:
        pass


def requested():
    """Fin demandee de l'appairage, ou None."""
    try:
        with open(REQUEST_PATH, encoding="utf-8") as f:
            until = json.load(f).get("jusqu_a", 0)
    except (OSError, ValueError):
        return None
    return until if until > time.time() else None


def read_events(offset=0):
    """(trames, nouvel offset) ; started=True des que radio_watch a commence."""
    try:
        with open(EVENTS_PATH, encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            offset = f.tell()
    except OSError:
        return [], offset, False
    events, started = [], offset > 0
    for line in data.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if "_debut" in e:
            started = True
        else:
            events.append(e)
    return events, offset, started


# --- reconnaissance ---

class Pairing:
    """Suit les capteurs entendus pendant l'appairage. feed() rend des
    evenements : ("nouveau", candidat) puis ("geste", candidat) quand le geste
    demande a fait bouger ses mesures."""

    def __init__(self, known):
        self.known = set(known)
        self.candidates = []      # [{"cle", "genre", "premier", "dernier", "geste", "trames"}]

    def feed(self, event):
        k = key(event)
        if k in self.known or event.get("model") is None:
            return []
        if any(w in json.dumps(event).lower() for w in ("tpms", "pressure_kpa")):
            return []  # pneus des voitures qui passent
        c = next((c for c in self.candidates if c["cle"] == k), None)
        if c is None:
            c = {"cle": k, "genre": cat.kind(event), "premier": event, "dernier": event, "geste": False, "trames": 1}
            self.candidates.append(c)
            return [("nouveau", c)]
        c["dernier"], c["trames"] = event, c["trames"] + 1
        if not c["geste"] and cat.GESTURES.get(c["genre"]) and cat.gesture_passed(c["genre"], c["premier"], event):
            c["geste"] = True
            return [("geste", c)]
        return []


def entry_from(candidate, name, plant_id=None):
    e = candidate["dernier"]
    entry = {"cle": candidate["cle"], "modele": str(e.get("model")), "id": e.get("id"), "canal": e.get("channel"),
             "genre": candidate["genre"], "nom": name, "plante_id": plant_id,
             "ajoute_le": datetime.date.today().isoformat()}
    if candidate["genre"] == "sol" and candidate["geste"]:
        dry, wet = candidate["premier"].get("ad_raw"), e.get("ad_raw")
        if dry is not None and wet is not None:
            entry["etalonnage"] = {"sec": dry, "mouille": wet}  # air puis verre d'eau
    return entry


# --- lecture des releves (publies par capteurs_watch) ---

READINGS_PATH = "/tmp/capteurs_releves.json"


def latest(entry, max_age_sec=3 * 3600):
    """Dernieres valeurs de ce capteur ({grandeur: valeur, "t": ...}) ou None."""
    try:
        with open(READINGS_PATH, encoding="utf-8") as f:
            r = json.load(f).get(entry["cle"])
    except (OSError, ValueError):
        return None
    if not r or time.time() - r.get("t", 0) > max_age_sec:
        return None
    return dict(r.get("valeurs") or {}, t=r["t"])


def soil_for_plant(plant_id):
    """(sonde, humidite %) de la sonde rattachee a cette plante, ou None."""
    for entry in load():
        if entry.get("genre") == "sol" and entry.get("plante_id") == plant_id:
            values = latest(entry)
            if values:
                pct = cat.soil_percent(values, entry.get("etalonnage"))
                if pct is not None:
                    return entry, pct
    return None
