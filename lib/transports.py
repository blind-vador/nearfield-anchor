"""Transports en commun de la commune choisie (2026-10-07) : bus, tram,
metro... quel que soit le reseau, d'apres le Point d'Acces National
transport.data.gouv.fr.

Decouverte (a chaque changement de ville, ou commande "transports
reseaux") : geo.api.gouv.fr donne l'intercommunalite, le departement et la
region de la commune ; l'API du PAN donne l'autorite organisatrice (AOM) et
tous les reseaux publies, avec la zone qu'ils couvrent. Sont gardes ceux qui
couvrent la commune, son intercommunalite ou son AOM (portee "locale",
actifs par defaut), son departement ou sa region (inactifs par defaut,
sauf s'il n'y a pas de reseau local) ; un reseau dont l'AOM de la commune est
proprietaire est toujours local (Ile-de-France Mobilites couvre la region
entiere). Les "agregats" regionaux sont listes
mais jamais actives d'office. Reglage "transports" du NAS.

Par reseau :
  GTFS      horaires theoriques : seules les lignes (routes.txt) servent,
            nom et mode ; copie sur la carte SD, refaite chaque semaine
  GTFS-RT   temps reel (lib/gtfs_rt.py) : retards, courses annulees,
            alertes de perturbation quand le reseau les publie
  SIRI Lite perturbations (general-message, situation-exchange), lib/siri_lite.py ;
            cle dans le dossier des cles quand le serveur en demande une
            (KEYED_HOSTS : PRIM d'Ile-de-France Mobilites -> cles/prim.txt).
            Le SIRI "classique" (XML sur abonnement, 85 reseaux) n'est que compte.
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
import siri_lite
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
# serveurs SIRI Lite qui demandent une cle : hote -> (fichier cles/<nom>.txt, en-tete HTTP)
KEYED_HOSTS = {"prim.iledefrance-mobilites.fr": ("prim", "apiKey")}
SIRI_SERVICES = ("general-message", "situation-exchange")
SIRI_CACHE_SEC = 120
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
        owners = {str(o.get("siren")) for o in d.get("legal_owners") or []}
        if aom.get("siren") and str(aom["siren"]) in owners:
            scope = "locale"  # reseau de l'autorite organisatrice de la commune, meme declare sur toute la region (IDFM)
        for area in [] if scope else d.get("covered_area") or []:
            kind = area.get("type")
            if str(area.get("insee")) in scope_of.get(kind, ()):
                scope = "locale" if kind in ("commune", "epci", "aom") else kind
                break
        if not scope:
            continue
        res = [r for r in d.get("resources") or [] if r.get("is_available", True)]
        gtfs = next((r["url"] for r in res if r.get("format") == "GTFS"), None)
        rt = [{"url": r["url"], "contenu": r.get("features") or []} for r in res if r.get("format") == "gtfs-rt"]
        siri_lite = []
        for r in res:
            url = r.get("url") or ""
            service = next((s for s in SIRI_SERVICES if s in url), None)
            if r.get("format") == "SIRI Lite" and service:
                host = requests.utils.urlparse(url).netloc
                siri_lite.append({"url": url, "service": service, "cle": KEYED_HOSTS.get(host, (None,))[0]})
        siri = len([r for r in res if r.get("format") == "SIRI"])
        page = next((PAGES[i] for i in local if i in PAGES), None) if scope == "locale" else None
        networks.append({"id": d["id"], "nom": d.get("title") or "?", "portee": scope, "gtfs": gtfs, "temps_reel": rt,
                         "siri": siri, "siri_lite": siri_lite, "page": page, "agregat": (d.get("title") or "").lower().startswith("agr"),
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

FULL_DOWNLOAD_MAX = 80 * 1024 * 1024   # sans lecture partielle, on ne telecharge pas plus


class _RangeFile(io.RawIOBase):
    """Fichier distant lu par morceaux (HTTP Range) : zipfile n'en lit que le
    sommaire et routes.txt (2026-10-07 : GTFS IDFM = 244 Mo, routes.txt
    quelques centaines de Ko)."""
    BLOCK = 256 * 1024

    def __init__(self, url, size):
        self.url, self.size, self.pos, self.cache = url, size, 0, {}

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def _block(self, n):
        if n not in self.cache:
            start = n * self.BLOCK
            end = min(self.size, start + self.BLOCK) - 1
            resp = requests.get(self.url, headers={"Range": f"bytes={start}-{end}"}, timeout=TIMEOUT)
            if resp.status_code != 206:
                raise OSError(f"lecture partielle refusee ({resp.status_code})")
            self.cache[n] = resp.content
        return self.cache[n]

    def readinto(self, b):
        if self.pos >= self.size:
            return 0
        n, off = divmod(self.pos, self.BLOCK)
        chunk = self._block(n)[off:off + len(b)]
        b[:len(chunk)] = chunk
        self.pos += len(chunk)
        return len(chunk)


def _read_routes(z):
    out = {}
    name = next((n for n in z.namelist() if n.endswith("routes.txt")), None)
    if name:
        for r in csv.DictReader(io.TextIOWrapper(z.open(name), "utf-8-sig")):
            out[r.get("route_id", "")] = {"nom": (r.get("route_short_name") or r.get("route_long_name") or "").strip(),
                                          "famille": group_of(r.get("route_type"))}
    return out

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
    head = requests.head(network["gtfs"], allow_redirects=True, timeout=TIMEOUT)
    size = int(head.headers.get("Content-Length") or 0)
    out = None
    if size and head.headers.get("Accept-Ranges", "").lower() == "bytes":
        try:
            with zipfile.ZipFile(_RangeFile(head.url, size)) as z:
                out = _read_routes(z)
        except (OSError, zipfile.BadZipFile, requests.RequestException) as exc:
            print(f"[transports] {network['nom']} lecture partielle : {exc!r}")
    if out is None:
        if size > FULL_DOWNLOAD_MAX:
            raise OSError(f"GTFS de {size // 1_000_000} Mo sans lecture partielle")
        with tempfile.TemporaryFile() as tmp:
            with requests.get(network["gtfs"], stream=True, timeout=TIMEOUT) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_content(1 << 16):
                    tmp.write(chunk)
                    if tmp.tell() > FULL_DOWNLOAD_MAX:
                        raise OSError("GTFS trop gros")
            tmp.seek(0)
            with zipfile.ZipFile(tmp) as z:
                out = _read_routes(z)
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


_siri = {}


def siri_messages(network):
    """Perturbations SIRI Lite du reseau ; ([messages], cle manquante ou None)."""
    out, missing = [], None
    for src in network.get("siri_lite") or []:
        headers = {}
        if src.get("cle"):
            key = reglages.cle(src["cle"])
            if not key:
                missing = src["cle"]
                continue
            headers[KEYED_HOSTS[requests.utils.urlparse(src["url"]).netloc][1]] = key
        cached = _siri.get(src["url"])
        if cached and time.time() - cached[0] < SIRI_CACHE_SEC:
            out += cached[1]
            continue
        try:
            resp = requests.get(src["url"], headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            msgs = siri_lite.parse(resp.content)
        except (requests.RequestException, ValueError, Exception) as exc:  # XML ou JSON mal formes
            print(f"[transports] {network['nom']} SIRI {src['service']} : {exc!r}")
            continue
        _siri[src["url"]] = (time.time(), msgs)
        out += msgs
    return out, missing


def items(network, feed=None):
    """Toutes les perturbations du reseau, de toutes ses sources :
    [{"id", "titre", "familles"}] ("general" : tout le reseau)."""
    lines = routes(network)
    by_name = {r["nom"].upper(): r["famille"] for r in lines.values() if r["nom"]}
    by_code = {}
    for rid, r in lines.items():
        by_code[rid] = by_code[rid.split(":")[-1]] = r["famille"]
    now = time.time()
    out = []
    if network.get("page") in PAGE_STATES:
        try:
            with open(PAGE_STATES[network["page"]], encoding="utf-8") as f:
                titles = json.load(f)
        except (OSError, ValueError):
            titles = []
        for t in titles if isinstance(titles, list) else []:
            out.append({"id": t, "titre": re.sub(r"^(Temps réel|Manifestation|Travaux)\s+", "", t),
                        "familles": _title_families(t, by_name)})
    for a in (feed or {}).get("alertes") or []:
        if a["fin"] and a["fin"] < now:
            continue
        fams = ({lines.get(r, {}).get("famille") for r in a["routes"]} | {group_of(t) for t in a["types"]}) - {None}
        title = a["titre"] or a["texte"][:120]
        out.append({"id": title, "titre": title, "familles": fams or {"general"}})
    msgs, _ = siri_messages(network)
    for m in msgs:
        if m["fin"] and m["fin"] < now:
            continue
        fams = {by_code.get(c) for c in m["lignes"]} - {None}
        title = m["titre"] or m["texte"][:120]
        out.append({"id": m["id"] or title, "titre": title, "familles": fams or {"general"}})
    seen, unique = set(), []
    for i in out:
        if i["titre"] and i["id"] not in seen:
            seen.add(i["id"])
            unique.append(i)
    return unique


def disruptions(network, group, feed=None):
    """Titres des perturbations qui touchent cette famille (ou tout le reseau)."""
    return [i["titre"] for i in items(network, feed) if group in i["familles"] or "general" in i["familles"]]


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


def _join(names):
    """ "A, B et C", le "et" dans la langue choisie."""
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " " + dire("transport.et") + " " + names[-1]


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
                parts.append(dire("transport.pire_ligne" if len(p["pires"]) == 1 else "transport.pires_lignes",
                                  lignes=_join(p["pires"])))
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
        elif rt and (rt["alertes"] or n.get("page")) or n.get("siri_lite"):
            parts.append(dire("transport.aucune_perturbation"))
        missing = next((s["cle"] for s in n.get("siri_lite") or [] if s.get("cle") and not reglages.cle(s["cle"])), None)
        if missing:
            parts.append(dire("transport.cle_manquante", fichier=f"{missing}.txt"))
        texts.append(dire("transport.reseau", famille=GROUPS[group].capitalize(), reseau=_short_name(n["nom"]))
                     + " " + " ".join(parts))
    if not texts:
        return dire("transport.pas_de_ce_mode", famille=GROUPS[group], commune=lieu.get("commune"))
    return " ".join(texts)


def _short_name(title):
    """ "Reseau urbain STAS" -> "STAS" ; "... Mobilites (IDFM)" -> "IDFM" """
    acronym = re.search(r"\(([A-Z][A-Za-z0-9 -]{1,12})\)\s*$", title)
    if acronym:
        return acronym.group(1)
    return re.sub(r"^(réseau|reseau)\s+(urbain|interurbain|départemental|regional|régional)?\s*(et\s+\w+\s+)?", "",
                  title, flags=re.I).strip(" -") or title


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
