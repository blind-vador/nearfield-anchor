"""Listes publiques, sans cle, pour les commandes ville, gare et aeroport.

  communes     geo.api.gouv.fr (recherche en direct, triee par population)
  gares        open data SNCF, gares de voyageurs (code UIC pour l'API SNCF)
  aeroports    OurAirports, aerodromes francais a code OACI "LF"

Gares et aeroports sont gardes en cache dans le dossier listes des
reglages (NAS), rafraichis au bout de LIST_MAX_AGE_SEC : pas de
telechargement a chaque ouverture de l'arborescence.
"""
import csv
import io
import json
import os
import sys
import time

import requests

sys.path.insert(0, "/home/vador/autopilot/pi_console")
import reglages  # noqa: E402

LIST_DIR = os.path.join(reglages.SETTINGS_DIR, "listes")
LIST_MAX_AGE_SEC = 30 * 24 * 3600
TIMEOUT = 30

COMMUNES_URL = "https://geo.api.gouv.fr/communes"
DEPARTEMENTS_URL = "https://geo.api.gouv.fr/departements"
GARES_URL = ("https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/"
             "gares-de-voyageurs/exports/json")
AEROPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport"}
# OurAirports nomme en anglais : "Lyon Saint-Exupéry Airport" -> "Lyon Saint-Exupéry".
NAME_SUFFIXES = (" International Airport", " Airport", " Airfield", " Airstrip", " Air Base")


def _cached_list(name, build):
    """Liste `name` depuis le cache du NAS, ou reconstruite par build()."""
    path = os.path.join(LIST_DIR, f"{name}.json")
    try:
        if time.time() - os.path.getmtime(path) < LIST_MAX_AGE_SEC:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    try:
        items = build()
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"[sources] {name} non telecharge : {exc}", file=sys.stderr)
        try:  # cache perime plutot que rien
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    try:
        os.makedirs(LIST_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[sources] {name} non mis en cache : {exc}", file=sys.stderr)
    return items


def departements():
    """{code: nom}"""
    def build():
        resp = requests.get(DEPARTEMENTS_URL, params={"fields": "nom,code"}, timeout=TIMEOUT)
        resp.raise_for_status()
        return {d["code"]: d["nom"] for d in resp.json()}
    return _cached_list("departements", build) or {}


def _departement_code(insee):
    insee = insee or ""
    return insee[:3] if insee.startswith("97") else insee[:2]


def communes(query):
    """Communes dont le nom ressemble a `query`, les plus peuplees d'abord.
    None si le service ne repond pas."""
    try:
        resp = requests.get(COMMUNES_URL, timeout=TIMEOUT, params={
            "nom": query, "boost": "population", "limit": 15,
            "fields": "nom,code,codeDepartement,departement,centre,population"})
        resp.raise_for_status()
        found = resp.json()
    except (requests.RequestException, ValueError):
        return None
    items = []
    for c in found:
        if not c.get("centre"):
            continue
        lon, lat = c["centre"]["coordinates"]
        dep = c.get("departement") or {}
        items.append({
            "nom": c["nom"], "code_insee": c["code"],
            "departement": c.get("codeDepartement", ""), "departement_nom": dep.get("nom", ""),
            "lat": lat, "lon": lon, "population": c.get("population") or 0,
        })
    return items


def gares():
    def build():
        resp = requests.get(GARES_URL, timeout=TIMEOUT,
                            params={"select": "nom,codes_uic,codeinsee,position_geographique"})
        resp.raise_for_status()
        noms = departements()
        items = []
        for g in resp.json():
            uic = (g.get("codes_uic") or "").split(";")[0].strip()
            if not uic or not g.get("nom"):
                continue
            pos = g.get("position_geographique") or {}
            dep = _departement_code(g.get("codeinsee"))
            items.append({"nom": g["nom"], "uic": uic, "departement": dep,
                          "departement_nom": noms.get(dep, ""),
                          "lat": pos.get("lat"), "lon": pos.get("lon")})
        return items
    return _cached_list("gares", build)


def _airport_name(name):
    for suffix in NAME_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def aeroports():
    def build():
        resp = requests.get(AEROPORTS_URL, timeout=TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        items = []
        for a in csv.DictReader(io.StringIO(resp.text)):
            code = a["icao_code"] or a["gps_code"]
            if a["iso_country"] != "FR" or a["type"] not in AIRPORT_TYPES or not code.startswith("LF"):
                continue
            items.append({"nom": _airport_name(a["name"]), "oaci": code,
                          "iata": a["iata_code"], "commercial": a["scheduled_service"] == "yes",
                          "ville": a["municipality"], "type": a["type"],
                          "lat": float(a["latitude_deg"]), "lon": float(a["longitude_deg"])})
        return items
    # v2 : ajout des codes IATA et du drapeau vols commerciaux (tableaux).
    return _cached_list("aeroports_v2", build)


def iata_names():
    """{code IATA: nom de l'aeroport} pour dire les destinations des vols
    (le nom plutot que la commune : "Lyon Saint-Exupéry", pas
    "Colombier-Saugnieu"). Monde entier, garde en cache."""
    def build():
        resp = requests.get(AEROPORTS_URL, timeout=TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        names = {}
        for a in csv.DictReader(io.StringIO(resp.text)):
            if a["iata_code"] and a["type"] in AIRPORT_TYPES:
                names[a["iata_code"]] = _airport_name(a["name"])
        return names
    return _cached_list("iata", build) or {}
