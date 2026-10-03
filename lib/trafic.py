"""Trafic routier autour de chez soi, a la demande (commande "trafic") :
flux ouvert DATEX II de Bison Fute (reseau routier national : evenements
des directions interdepartementales des routes). Pas d'ecoute radio pour
ca (choix utilisateur 2026-10-01 : utile ponctuellement, par exemple pour
prevenir quelqu'un qui vient en visite).
"""
import math
import os
import time
import xml.etree.ElementTree as ET

import requests

import reglages
from phrases import Etiquettes, dire, nombre
from phrases import dire

URL = ("https://tipi.bison-fute.gouv.fr/bison-fute-ouvert/publicationsDIR/"
       "Evenementiel-DIR/grt/RRN/content.xml")
CACHE_PATH = "/tmp/bison_fute.xml"
CACHE_SEC = 300
RADIUS_KM = 40
MAX_EVENTS = 6
XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
# type DATEX -> (gravite pour le tri, nom dit)
TYPES = {
    "Accident": (0, "accident"), "AbnormalTraffic": (0, "ralentissement ou bouchon"),
    "VehicleObstruction": (0, "véhicule arrêté ou obstacle"), "GeneralObstruction": (0, "obstacle sur la route"),
    "EnvironmentalObstruction": (0, "obstacle naturel, inondation ou chute"),
    "AnimalPresenceObstruction": (0, "animaux sur la route"), "PoorEnvironmentConditions": (0, "mauvaises conditions"),
    "RoadOrCarriagewayOrLaneManagement": (1, "voie ou route fermée"), "ReroutingManagement": (1, "déviation"),
    "SpeedManagement": (2, "limitation de vitesse"), "MaintenanceWorks": (2, "travaux"),
    "ConstructionWorks": (2, "chantier"), "GeneralNetworkManagement": (2, "gestion du réseau"),
    "GeneralInstructionOrMessageToRoadUsers": (2, "message aux usagers"),
}
DIRECTIONS = ["nord", "nord-est", "est", "sud-est", "sud", "sud-ouest", "ouest", "nord-ouest"]
TYPE_NAMES = Etiquettes("trafic.type", {k: name for k, (_, name) in TYPES.items()}, "type d'incident routier")
DIRECTION_VERS = Etiquettes("direction.vers", {d: (f"à l'{d}" if d[0] in "eo" else f"au {d}") for d in DIRECTIONS},
                            "où se trouve quelque chose, après une distance : à 12 kilomètres au nord")


def _tag(el):
    return el.tag.split("}")[-1]


def _fetch():
    try:
        if time.time() - os.path.getmtime(CACHE_PATH) < CACHE_SEC:
            return CACHE_PATH
    except OSError:
        pass
    resp = requests.get(URL, timeout=60)
    resp.raise_for_status()
    with open(CACHE_PATH, "wb") as f:
        f.write(resp.content)
    return CACHE_PATH


def _where(lat0, lon0, lat, lon):
    p0, p1 = math.radians(lat0), math.radians(lat)
    a = math.sin((p1 - p0) / 2) ** 2 + math.cos(p0) * math.cos(p1) * math.sin(math.radians(lon - lon0) / 2) ** 2
    km = 2 * 6371 * math.asin(math.sqrt(a))
    y = math.sin(math.radians(lon - lon0)) * math.cos(p1)
    x = math.cos(p0) * math.sin(p1) - math.sin(p0) * math.cos(p1) * math.cos(math.radians(lon - lon0))
    name = DIRECTIONS[int(((math.degrees(math.atan2(y, x)) + 360) % 360 + 22.5) // 45) % 8]
    return km, DIRECTION_VERS[name]


def events(radius_km=RADIUS_KM):
    """Evenements a moins de radius_km, les plus genants puis les plus proches."""
    lieu = reglages.get("lieu")
    lat0, lon0 = float(lieu["lat"]), float(lieu["lon"])
    root = ET.parse(_fetch()).getroot()
    found = []
    for rec in root.iter():
        if _tag(rec) != "situationRecord":
            continue
        kind = rec.attrib.get(XSI_TYPE, "").split(":")[-1]
        lat = lon = None
        road = town = description = None
        for sub in rec.iter():
            t, text = _tag(sub), (sub.text or "").strip()
            if t == "latitude" and lat is None:
                lat = float(text)
            elif t == "longitude" and lon is None:
                lon = float(text)
            elif t == "roadNumber" and not road:
                road = text
            elif t == "generalPublicComment" and not description:
                # <generalPublicComment><comment>...<value>texte</value></comment><commentType>
                ctype = next((c.text for c in sub if _tag(c) == "commentType"), "")
                if ctype == "description":
                    description = next((c.text.strip() for c in sub.iter() if _tag(c) == "value" and c.text), None)
            elif t == "name" and not town:
                # <name><descriptor>...<value>Ville</value></descriptor><tpegOtherPointDescriptorType>townName
                kind_text = next((c.text for c in sub if _tag(c) == "tpegOtherPointDescriptorType"), "")
                if kind_text == "townName":
                    town = next((c.text.strip() for c in sub.iter() if _tag(c) == "value" and c.text), None)
        if lat is None or lon is None:
            continue
        km, towards = _where(lat0, lon0, lat, lon)
        if km > radius_km:
            continue
        rank = TYPES.get(kind, (3, ""))[0]
        name = TYPE_NAMES[kind] if kind in TYPES else dire("trafic.evenement")
        found.append({"rang": rank, "km": km, "nom": name, "direction": towards, "route": road,
                      "ville": town, "description": description})
    found.sort(key=lambda e: (e["rang"], e["km"]))
    return found


def _road(road):
    """ "N0088" -> "N 88", "A0072" -> "A 72" """
    if not road:
        return ""
    letters = road.rstrip("0123456789")
    number = road[len(letters):].lstrip("0")
    return f"{letters} {number}".strip()


def summary(radius_km=RADIUS_KM):
    try:
        found = events(radius_km)
    except (requests.RequestException, ET.ParseError, OSError) as exc:
        print(f"[trafic] Bison Fute indisponible : {exc}")
        return dire("trafic.service_bison_fute_repond")
    if not found:
        return dire("trafic.aucun_incident_signale_grandes", radius_km=radius_km)
    serious = sum(1 for e in found if e["rang"] == 0)
    head = dire("trafic.resume", evenements=nombre(len(found), "événement"), rayon=radius_km,
                genants=nombre(serious, "gênant"))
    lines = []
    for e in found[:MAX_EVENTS]:
        text = e["nom"].capitalize()
        if e["route"]:
            text += (" " + dire("trafic.phrase", v1=_road(e['route'])))
        if e["ville"]:
            text += dire("trafic.vers", ville=e['ville'])
        text += dire("trafic.kilometres", round=round(e['km']), direction=e['direction'])
        boilerplate = (e["description"] or "").startswith(("La mesure est", "Arrêté"))
        if e["description"] and len(e["description"]) < 120 and not boilerplate:
            text += f" : {e['description'].rstrip('.')}"
        lines.append(text)
    more = len(found) - MAX_EVENTS
    tail = (" " + dire("trafic.autres_moins_genants", autres=nombre(more, "autre, moins gênant", "autres, moins gênants"))
            if more > 0 else "")
    return head + " " + ". ".join(lines) + "." + tail
