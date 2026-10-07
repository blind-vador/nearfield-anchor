"""Transports en commun de la commune choisie (2026-10-07) : bus, tram,
metro... quel que soit le reseau, d'apres le Point d'Acces National
transport.data.gouv.fr.

Decouverte (a chaque changement de ville, ou commande "transports
reseaux") : geo.api.gouv.fr donne l'intercommunalite, le departement et la
region de la commune ; l'API du PAN donne l'autorite organisatrice (AOM) et
tous les reseaux publies, avec la zone qu'ils couvrent. Sont gardes ceux qui
couvrent la commune, son intercommunalite ou son AOM (portee "locale",
actifs par defaut), son departement ou sa region (inactifs par defaut,
sauf s'il n'y a pas de reseau local). Les "agregats" regionaux sont listes
mais jamais actives d'office. Reglage "transports" du NAS.

Par reseau :
  GTFS      horaires theoriques : seules les lignes (routes.txt) servent,
            nom et mode ; copie sur la carte SD, refaite chaque semaine
  GTFS-RT   temps reel (lib/gtfs_rt.py) : retards, courses annulees,
            alertes de perturbation quand le reseau les publie
  SIRI      liste mais pas encore lu (85 reseaux en publient)
  page      perturbations publiees seulement sur le site de l'exploitant :
            STAS (page lue par watchers/stas_watch.py)
"""
import csv
import io
import json
import os
import re
import tempfile
import time
import zipfile

import requests

import gtfs_rt
import reglages
from phrases import Etiquettes, dire, nombre

PAN = "https://transport.data.gouv.fr/api"
GEO = "https://geo.api.gouv.fr/communes/{insee}"
TIMEOUT = 30
ROUTES_MAX_AGE = 7 * 86400
DATASETS_MAX_AGE = 7 * 86400
DATASETS_CACHE = os.path.join(reglages.CACHE_DIR, "transports_reseaux_pan.json")
# reseaux dont les perturbations ne sont que sur le site de l'exploitant
PAGES = {"244200770": "stas"}     # Saint-Etienne Metropole -> watchers/stas_watch.py
PAGE_STATES = {"stas": "/tmp/stas_watch_state.json"}
LATE_SEC = 300
MAX_DELAY_SEC = 3600              # au-dela : donnee aberrante, ignoree

# Familles de modes, telles que l'utilisateur les demande ("les bus" = bus et trolleybus)
GROUPS = Etiquettes("transport.mode", {
    "bus": "bus", "tram": "tram", "metro": "métro", "train": "train urbain", "bateau": "navette fluviale",
    "cable": "télécabine ou téléphérique", "funiculaire": "funiculaire"}, "famille de transport en commun")


def group_of(route_type):
    """route_type GTFS (simple ou etendu) -> famille."""
    try:
        t = int(route_type)
    except (TypeError, ValueError):
        return None
    simple = {0: "tram", 1: "metro", 2: "train", 3: "bus", 4: "bateau", 5: "tram", 6: "cable", 7: "funiculaire",
              11: "bus", 12: "metro"}
    if t in simple:
        return simple[t]
    for lo, hi, g in ((100, 199, "train"), (200, 299, "bus"), (400, 499, "metro"), (700, 899, "bus"),
                      (900, 999, "tram"), (1000, 1299, "bateau"), (1300, 1399, "cable"), (1400, 1499, "funiculaire")):
        if lo <= t <= hi:
            return g
    return None


def _get(url, **params):
    resp = requests.get(url, params=params or None, timeout=TIMEOUT, headers={"User-Agent": "nearfield-anchor"})
    resp.raise_for_status()
    return resp


def _datasets():
    """Liste nationale des jeux de donnees (2,5 Mo), copie de 7 jours sur la carte SD."""
    try:
        if time.time() - os.path.getmtime(DATASETS_CACHE) < DATASETS_MAX_AGE:
            with open(DATASETS_CACHE, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    data = [d for d in _get(f"{PAN}/datasets").json() if d.get("type") == "public-transit"]
    os.makedirs(os.path.dirname(DATASETS_CACHE), exist_ok=True)
    with open(DATASETS_CACHE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(DATASETS_CACHE + ".tmp", DATASETS_CACHE)
    return data


def discover(lieu):
    """Cherche les reseaux de la commune et les enregistre. Rend le reglage."""
    insee = str(lieu["code_insee"])
    geo = _get(GEO.format(insee=insee), fields="codeRegion,codeDepartement,codeEpci").json()
    try:
        aom = _get(f"{PAN}/aoms/{insee}").json()
    except requests.RequestException:
        aom = {}
    local = {insee, str(geo.get("codeEpci") or ""), str(aom.get("siren") or "")} - {""}
    scope_of = {"commune": local, "epci": local, "aom": local,
                "departement": {str(geo.get("codeDepartement") or lieu.get("departement") or "")},
                "region": {str(geo.get("codeRegion") or "")}}
    previous = {n["id"]: n for n in (reglages.get("transports").get("reseaux") or [])}
    networks = []
    for d in _datasets():
        scope = None
        for area in d.get("covered_area") or []:
            kind = area.get("type")
            if str(area.get("insee")) in scope_of.get(kind, ()):
                scope = "locale" if kind in ("commune", "epci", "aom") else kind
                break
        if not scope:
            continue
        res = [r for r in d.get("resources") or [] if r.get("is_available", True)]
        gtfs = next((r["url"] for r in res if r.get("format") == "GTFS"), None)
        rt = [{"url": r["url"], "contenu": r.get("features") or []} for r in res if r.get("format") == "gtfs-rt"]
        siri = len([r for r in res if r.get("format") in ("SIRI", "SIRI Lite")])
        page = next((PAGES[i] for i in local if i in PAGES), None) if scope == "locale" else None
        networks.append({"id": d["id"], "nom": d.get("title") or "?", "portee": scope, "gtfs": gtfs, "temps_reel": rt,
                         "siri": siri, "page": page, "agregat": (d.get("title") or "").lower().startswith("agr"),
                         "actif": False})
    has_local = any(n["portee"] == "locale" for n in networks)
    for n in networks:
        if n["id"] in previous:
            n["actif"] = previous[n["id"]].get("actif", False)
        else:
            n["actif"] = not n["agregat"] and (n["portee"] == "locale" or (not has_local and n["portee"] == "departement"))
    order = {"locale": 0, "departement": 1, "region": 2}
    networks.sort(key=lambda n: (order.get(n["portee"], 3), n["agregat"], n["nom"]))
    data = {"commune": lieu.get("commune"), "code_insee": insee, "aom": aom.get("nom"), "reseaux": networks,
            "decouvert_le": time.strftime("%Y-%m-%d")}
    reglages.save("transports", data)
    return data


def settings(lieu=None):
    """Reglage des transports pour la commune choisie ; None s'il est a refaire."""
    lieu = lieu or reglages.get("lieu")
    data = reglages.get("transports")
    if str(data.get("code_insee")) != str(lieu.get("code_insee")):
        return None
    return data


def active(lieu=None):
    data = settings(lieu)
    return [n for n in (data or {}).get("reseaux") or [] if n.get("actif")]


def page_active(name):
    """Le reseau qui publie sur cette page est-il actif pour la commune choisie ?"""
    try:
        return any(n.get("page") == name for n in active())
    except Exception:
        return False


# --- lignes d'un reseau (GTFS routes.txt) ---

def routes(network):
    """{route_id: {"nom", "famille"}}, copie de la carte SD refaite chaque semaine."""
    path = os.path.join(reglages.CACHE_DIR, f"transports_lignes_{network['id']}.json")
    try:
        if time.time() - os.path.getmtime(path) < ROUTES_MAX_AGE:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    if not network.get("gtfs"):
        return {}
    out = {}
    with tempfile.TemporaryFile() as tmp:
        with requests.get(network["gtfs"], stream=True, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(1 << 16):
                tmp.write(chunk)
        tmp.seek(0)
        with zipfile.ZipFile(tmp) as z:
            name = next((n for n in z.namelist() if n.endswith("routes.txt")), None)
            if name:
                for r in csv.DictReader(io.TextIOWrapper(z.open(name), "utf-8-sig")):
                    out[r.get("route_id", "")] = {"nom": (r.get("route_short_name") or r.get("route_long_name") or "").strip(),
                                                  "famille": group_of(r.get("route_type"))}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    return out


def families(network):
    return {r["famille"] for r in routes(network).values() if r["famille"]}


# --- perturbations ---

LINE_RE = re.compile(r"lignes?\s+([A-Za-z0-9]+(?:\s*[-/,&]\s*[A-Za-z0-9]+)*)", re.I)


def _title_families(title, by_name):
    m = LINE_RE.search(title)
    if not m:
        return {"general"}
    names = re.split(r"\s*[-/,&]\s*", m.group(1))
    fams = {by_name.get(n.upper()) for n in names} - {None}
    return fams or {"general"}


def disruptions(network, group, feed=None):
    """Titres des perturbations qui touchent cette famille (ou tout le reseau)."""
    lines = routes(network)
    by_name = {r["nom"].upper(): r["famille"] for r in lines.values() if r["nom"]}
    out = []
    if network.get("page") in PAGE_STATES:
        try:
            with open(PAGE_STATES[network["page"]], encoding="utf-8") as f:
                titles = json.load(f)
        except (OSError, ValueError):
            titles = []
        for t in titles if isinstance(titles, list) else []:
            fams = _title_families(t, by_name)
            if group in fams or "general" in fams:
                out.append(re.sub(r"^(Temps réel|Manifestation|Travaux)\s+", "", t))
    for a in (feed or {}).get("alertes") or []:
        fams = {lines.get(r, {}).get("famille") for r in a["routes"]} | {group_of(t) for t in a["types"]}
        fams -= {None}
        if a["fin"] and a["fin"] < time.time():
            continue
        if group in fams or not fams:
            out.append(a["titre"] or a["texte"][:120])
    return [t for t in out if t]


FEED_CACHE_SEC = 60
_feeds = {}


def feed(network):
    """Temps reel du reseau (tous ses flux GTFS-RT reunis), ou None. Garde
    une minute : le flash interroge chaque famille du meme reseau."""
    cached = _feeds.get(network["id"])
    if cached and time.time() - cached[0] < FEED_CACHE_SEC:
        return cached[1]
    merged = {"horodatage": None, "trajets": [], "alertes": []}
    found = False
    for rt in network.get("temps_reel") or []:
        try:
            data = gtfs_rt.parse(_get(rt["url"]).content)
        except (requests.RequestException, ValueError, IndexError) as exc:
            print(f"[transports] {network['nom']} temps reel : {exc!r}")
            continue
        found = True
        merged["trajets"] += data["trajets"]
        merged["alertes"] += data["alertes"]
        if data["horodatage"]:
            merged["horodatage"] = max(merged["horodatage"] or 0, data["horodatage"])
    result = merged if found else None
    _feeds[network["id"]] = (time.time(), result)
    return result


def punctuality(network, group, data):
    """{"suivis", "moyenne", "en_retard", "annules", "pires": [noms]} ou None."""
    lines = routes(network)
    trips = [t for t in (data or {}).get("trajets") or [] if lines.get(t["route_id"], {}).get("famille") == group]
    if not trips:
        return None
    delays = [t for t in trips if t["retard"] is not None and abs(t["retard"]) <= MAX_DELAY_SEC and not t["annule"]]
    per_line = {}
    for t in delays:
        per_line.setdefault(t["route_id"], []).append(max(0, t["retard"]))
    worst = sorted(((sum(v) / len(v), k) for k, v in per_line.items() if len(v) >= 2), reverse=True)
    return {"suivis": len(delays),
            "moyenne": sum(max(0, t["retard"]) for t in delays) / len(delays) if delays else 0,
            "en_retard": sum(1 for t in delays if t["retard"] >= LATE_SEC),
            "annules": sum(1 for t in trips if t["annule"]),
            "pires": [lines[k]["nom"] for m, k in worst[:2] if m >= 240 and lines.get(k, {}).get("nom")]}


def _minutes(sec):
    m = round(sec / 60)
    return dire("transport.moins_d_une_minute") if m < 1 else nombre(m, "minute")


def status(group, lieu=None, short=False):
    """Comment roulent les <famille> : phrase(s) a dire."""
    lieu = lieu or reglages.get("lieu")
    data = settings(lieu)
    if data is None:
        return dire("transport.pas_encore_cherche", commune=lieu.get("commune"))
    nets = [n for n in data.get("reseaux") or [] if n.get("actif")]
    if not nets:
        return dire("transport.aucun_reseau_actif", commune=lieu.get("commune"))
    texts = []
    for n in nets:
        try:
            fams = families(n)
        except (requests.RequestException, zipfile.BadZipFile, OSError) as exc:
            print(f"[transports] {n['nom']} lignes : {exc!r}")
            continue
        if group not in fams:
            continue
        rt = feed(n) if n.get("temps_reel") else None
        parts = []
        p = punctuality(n, group, rt) if rt else None
        if p and p["suivis"]:
            parts.append(dire("transport.ponctualite", suivis=p["suivis"], moyenne=_minutes(p["moyenne"]),
                              en_retard=p["en_retard"]))
            if p["pires"]:
                parts.append(dire("transport.pires_lignes", lignes=" et ".join(p["pires"])))
        if p and p["annules"]:
            parts.append(dire("transport.annules", nombre=nombre(p["annules"], "course annulée", "courses annulées")))
        if rt and rt["horodatage"] and time.time() - rt["horodatage"] > 900:
            parts.append(dire("transport.temps_reel_ancien", minutes=round((time.time() - rt["horodatage"]) / 60)))
        if not rt:
            parts.append(dire("transport.pas_de_temps_reel"))
        dis = disruptions(n, group, rt)
        if dis:
            shown = dis[:2] if short else dis[:4]
            parts.append(dire("transport.perturbations", nombre=nombre(len(dis), "perturbation"),
                              liste=" ; ".join(shown)))
        elif rt and (rt["alertes"] or n.get("page")):
            parts.append(dire("transport.aucune_perturbation"))
        texts.append(dire("transport.reseau", famille=GROUPS[group].capitalize(), reseau=_short_name(n["nom"]))
                     + " " + " ".join(parts))
    if not texts:
        return dire("transport.pas_de_ce_mode", famille=GROUPS[group], commune=lieu.get("commune"))
    return " ".join(texts)


def _short_name(title):
    """ "Reseau urbain STAS" -> "STAS" """
    return re.sub(r"^(réseau|reseau)\s+(urbain|interurbain|départemental|regional|régional)?\s*(et\s+\w+\s+)?", "",
                  title, flags=re.I).strip() or title


def available_groups(lieu=None):
    out = []
    for n in active(lieu):
        try:
            out += [g for g in families(n) if g not in out]
        except (requests.RequestException, zipfile.BadZipFile, OSError):
            continue
    return [g for g in GROUPS if g in out]


def flash_text(lieu, complet=False):
    """Rubrique transports du flash : chaque famille du reseau, en bref."""
    if settings(lieu) is None:
        return ""
    texts = [status(g, lieu, short=not complet) for g in available_groups(lieu)]
    return " ".join(t for t in texts if t)
