"""Tableaux des departs et arrivees (commande "tableau gare" / "tableau
aeroport") et listes de gares et d'aeroports avec favoris, partagees avec
les commandes de reglage "gare" et "aeroport".

  trains   API SNCF (Navitia), cle cles/sncf.txt, quota 5000 requetes/jour
  vols     AirLabs /schedules, cle cles/airlabs.txt, offre gratuite
           ~1000 requetes/mois : chaque tableau ouvert est garde
           FLIGHT_CACHE_SEC en memoire pour l'epargner

Favoris : favoris.json des reglages (identifiants UIC et OACI), affiches en
tete de liste ; l'etoile les ajoute ou les retire.
"""
import sys
import time
from datetime import datetime

import requests

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import arbre  # noqa: E402
import matcher  # noqa: E402
import reglages  # noqa: E402
import sources  # noqa: E402
from phrases import dire  # noqa: E402

NAVITIA_URL = "https://api.sncf.com/v1/coverage/sncf/stop_areas/stop_area:SNCF:{uic}/{kind}"
AIRLABS_URL = "https://airlabs.co/api/v9/schedules"
NAVITIA_DT = "%Y%m%dT%H%M%S"
AIRLABS_DT = "%Y-%m-%d %H:%M"
TRAIN_COUNT = 20
FLIGHT_MAX = 30
FLIGHT_CACHE_SEC = 300
TIMEOUT = 15

TRAIN_UNIT = ("train", "trains")
FLIGHT_UNIT = ("vol", "vols")
TRAIN_MODE_LABELS = {"REGIONAURA": "TER", "TGV INOUI": "TGV Inouï", "OUIGO": "Ouigo"}
FLIGHT_STATUS = ("scheduled", "active", "landed", "cancelled", "diverted")  # phrases tableaux.vol_<statut>

_flight_cache = {}


def _spoken_time(dt):
    return f"{dt.hour} heure" if dt.minute == 0 else f"{dt.hour} heure {dt.minute}"


def _clean_place(name):
    """ "Lyon Perrache (Lyon)" -> "Lyon Perrache" (doublon a l'oral)."""
    return name.split(" (", 1)[0] if name and " (" in name else (name or "")


# --- Trains ---------------------------------------------------------------

def _no_service_ids(data):
    """Identifiants (perturbation et trajet) des trains supprimes."""
    ids = set()
    for disr in data.get("disruptions") or []:
        if (disr.get("severity") or {}).get("effect") != "NO_SERVICE":
            continue
        ids.add(disr.get("id"))
        for obj in disr.get("impacted_objects") or []:
            pt = obj.get("pt_object") or {}
            if pt.get("embedded_type") == "trip" and pt.get("id"):
                ids.add(pt["id"])
                ids.add("vehicle_journey:" + pt["id"])
    return ids


def _train_row(entry, departures, origins, cancelled):
    info = entry.get("display_informations") or {}
    std = entry.get("stop_date_time") or {}
    field = "departure_date_time" if departures else "arrival_date_time"
    real = datetime.strptime(std.get(field) or std[f"base_{field}"], NAVITIA_DT)
    base = datetime.strptime(std.get(f"base_{field}") or std[field], NAVITIA_DT)
    mode = info.get("commercial_mode") or "Train"
    mode = TRAIN_MODE_LABELS.get(mode.upper(), mode)
    number = info.get("trip_short_name") or info.get("headsign") or ""
    if departures:
        place = dire("tableaux.vers", v1=_clean_place(info.get('direction')))
    else:
        origin = next((origins.get(l.get("id")) for l in info.get("links") or []
                       if l.get("category") == "origin"), None)
        place = dire("tableaux.phrase", v1=_clean_place(origin)) if origin else "origine inconnue"
    links = (entry.get("links") or []) + (info.get("links") or [])
    if any(l.get("id") in cancelled for l in links):
        status = "supprimé"
    else:
        delay = int((real - base).total_seconds() // 60)
        status = f"retard {delay} minute{'s' if delay > 1 else ''}" if delay > 0 else dire("tableaux.heure")
    label = f"{_spoken_time(base)}, {mode} {number}".rstrip() + f", {place}, {status}"
    return arbre.Node(label)


def train_board(uic, departures):
    key = reglages.cle("sncf")
    if not key:
        raise arbre.LoadError(dire("tableaux.cle_sncf_absente_colle"))
    kind = "departures" if departures else "arrivals"
    try:
        resp = requests.get(NAVITIA_URL.format(uic=uic, kind=kind), auth=(key, ""), timeout=TIMEOUT,
                            params={"count": TRAIN_COUNT, "data_freshness": "realtime"})
    except requests.RequestException:
        raise arbre.LoadError(dire("tableaux.api_sncf_repond"))
    if resp.status_code in (401, 403):
        raise arbre.LoadError(dire("tableaux.api_sncf_refuse_cle"))
    if resp.status_code == 404:
        raise arbre.LoadError(dire("tableaux.api_sncf_connait_tableau"))
    if not resp.ok:
        raise arbre.LoadError(dire("tableaux.api_sncf_repond_erreur", status_code=resp.status_code))
    data = resp.json()
    origins = {o["id"]: o.get("name") for o in data.get("origins") or [] if "id" in o}
    cancelled = _no_service_ids(data)
    rows = []
    for entry in data.get(kind) or []:
        try:
            rows.append(_train_row(entry, departures, origins, cancelled))
        except (KeyError, ValueError):
            continue
    return rows


def station_node(gare, prefix="", board=True):
    label = f"{prefix}{gare['nom']}"
    if gare.get("departement_nom"):
        label += f", {gare['departement_nom']}"
    if not board:
        return arbre.Node(label, item=gare, fav=gare["uic"])
    uic = gare["uic"]
    return arbre.Node(label, item=gare, fav=uic, children=[
        arbre.Node("Départs", loader=lambda: train_board(uic, True), unit=TRAIN_UNIT,
                   empty=dire("tableaux.aucun_depart_annonce")),
        arbre.Node("Arrivées", loader=lambda: train_board(uic, False), unit=TRAIN_UNIT,
                   empty=dire("tableaux.aucune_arrivee_annoncee")),
    ])


# --- Vols -----------------------------------------------------------------

def _spoken_flight(code, airline=None):
    """ "AF1234" -> "A F 1234" : compagnie epelee, numero lu comme un nombre.
    Le code compagnie IATA fait 2 caracteres et peut contenir un chiffre
    (U2 easyJet, V7 Volotea) : on ne le devine pas en coupant aux chiffres."""
    airline = airline if airline and code.startswith(airline) else code[:2]
    return f"{' '.join(airline)} {code[len(airline):]}".strip()


def _flight_rows(rows, departures):
    names = sources.iata_names()
    out = []
    now = datetime.now()
    for r in rows:
        if r.get("cs_flight_iata"):
            continue  # vol en partage de code : doublon du vol reellement opere
        field = "dep" if departures else "arr"
        try:
            sched = datetime.strptime(r[f"{field}_time"], AIRLABS_DT)
        except (KeyError, TypeError, ValueError):
            continue
        if (now - sched).total_seconds() > 3600:
            continue
        other = r.get("arr_iata" if departures else "dep_iata") or ""
        place = names.get(other) or " ".join(other) or "destination inconnue"
        status = dire(f"tableaux.vol_{r['status']}") if r.get("status") in FLIGHT_STATUS else ""
        delay = r.get(f"{field}_delayed") or r.get("delayed")
        if status not in ("annulé", "atterri") and isinstance(delay, (int, float)) and delay > 0:
            status = f"retard {int(delay)} minutes"
        code = r.get("flight_iata") or r.get("flight_icao") or ""
        label = (f"{_spoken_time(sched)}, vol {_spoken_flight(code, r.get('airline_iata'))}, "
                 f"{'vers' if departures else 'de'} {place}" + (f", {status}" if status else ""))
        out.append((sched, arbre.Node(label)))
    out.sort(key=lambda p: p[0])
    return [node for _, node in out[:FLIGHT_MAX]]


def flight_board(iata, departures):
    key = reglages.cle("airlabs")
    if not key:
        raise arbre.LoadError(dire("tableaux.cle_airlabs_absente_colle"))
    cache_key = (iata, departures)
    cached = _flight_cache.get(cache_key)
    if cached and time.time() - cached[0] < FLIGHT_CACHE_SEC:
        rows = cached[1]
    else:
        try:
            resp = requests.get(AIRLABS_URL, timeout=TIMEOUT, params={
                "api_key": key, ("dep_iata" if departures else "arr_iata"): iata})
            data = resp.json()
        except (requests.RequestException, ValueError):
            raise arbre.LoadError(dire("tableaux.airlabs_repond"))
        if data.get("error"):
            message = (data["error"] or {}).get("message") or "erreur inconnue"
            raise arbre.LoadError(dire("tableaux.airlabs_refuse_demande", message=message))
        rows = data.get("response") or []
        _flight_cache[cache_key] = (time.time(), rows)
    return _flight_rows(rows, departures)


def airport_node(a, prefix="", board=True):
    label = f"{prefix}{a['nom']}"
    if a.get("ville") and a["ville"] not in a["nom"]:
        label += f", {a['ville']}"
    if not board:
        return arbre.Node(f"{label}, {' '.join(a['oaci'])}", item=a, fav=a["oaci"])
    iata = a.get("iata")
    if not iata:
        return arbre.Node(dire("tableaux.pas_vols_commerciaux", aeroport=label), item=a, fav=a["oaci"])
    return arbre.Node(label, item=a, fav=a["oaci"], children=[
        arbre.Node("Départs", loader=lambda: flight_board(iata, True), unit=FLIGHT_UNIT,
                   empty=dire("tableaux.aucun_depart_annonce_2")),
        arbre.Node("Arrivées", loader=lambda: flight_board(iata, False), unit=FLIGHT_UNIT,
                   empty=dire("tableaux.aucune_arrivee_annoncee_2")),
    ])


# --- Listes avec favoris ----------------------------------------------------

def _toggle(kind, node):
    favoris = reglages.get("favoris")
    ids = list(favoris.get(kind) or [])
    name = node.item["nom"]
    if node.fav in ids:
        ids.remove(node.fav)
        text = dire("tableaux.retire_favoris", name=name)
    else:
        ids.append(node.fav)
        text = dire("tableaux.ajoute_aux_favoris", name=name)
    favoris[kind] = ids
    if not reglages.save("favoris", favoris):
        return dire("tableaux.disque_nas_inaccessible_favori")
    return text


def station_navigator(board, select=None):
    """None si la liste des gares est indisponible."""
    items = sources.gares()
    if not items:
        return None
    by_uic = {g["uic"]: g for g in items}
    make = (lambda g: station_node(g, board=board))
    letters, index = arbre.alphabet(items, make, ("gare", "gares"))

    def roots():
        head = []
        followed = reglages.get("trains")
        if board:
            gare = by_uic.get(str(followed["uic"])) or {"nom": followed["gare"], "uic": str(followed["uic"])}
            head.append(station_node(gare, dire("tableaux.gare_suivie") + " ", board))
        shown = {node.fav for node in head}
        for uic in reglages.get("favoris").get("gares") or []:
            if uic in by_uic and uic not in shown:
                head.append(station_node(by_uic[uic], dire("tableaux.favori") + " ", board))
        return head + letters

    return arbre.Navigator(roots, index, select=select,
                           toggle_favorite=lambda node: _toggle("gares", node))


def airport_navigator(board, select=None):
    items = sources.aeroports()
    if not items:
        return None
    if board:
        items = [a for a in items if a.get("iata") and a.get("commercial")]
    by_oaci = {a["oaci"]: a for a in sources.aeroports() or []}
    letters, index = arbre.alphabet(items, lambda a: airport_node(a, board=board), ("aéroport", "aéroports"),
                                    aliases=lambda a: (a.get("iata"), a["oaci"]))

    def roots():
        head = []
        if board:
            avions = reglages.get("avions")
            for role, prefix in (("proche", dire("tableaux.aeroport_proche") + " "), ("grand", dire("tableaux.grand_aeroport") + " ")):
                a = by_oaci.get(avions[role]["oaci"])
                if a:
                    head.append(airport_node(a, prefix, board))
        shown = {node.fav for node in head}
        for oaci in reglages.get("favoris").get("aeroports") or []:
            if oaci in by_oaci and oaci not in shown:
                head.append(airport_node(by_oaci[oaci], dire("tableaux.favori_2") + " ", board))
        return head + letters

    return arbre.Navigator(roots, index, select=select,
                           toggle_favorite=lambda node: _toggle("aeroports", node))




def run(line, key_source, say, out):
    words = set(matcher.normalize(line).split())
    if words & {"gare", "gares", "train", "trains", "sncf"}:
        kind = "gare"
    elif words & {"aeroport", "aeroports", "avion", "avions", "vol", "vols", "aerodrome"}:
        kind = "aeroport"
    else:
        index = arbre.run_list([dire("tableaux.tableau_gares"), dire("tableaux.tableau_aeroports")], key_source, say, out,
                               dire("tableaux.quel_tableau"))
        if index is None:
            return dire("tableaux.tableau_ferme")
        kind = ("gare", "aeroport")[index]
    say(dire("tableaux.chargement_liste"))
    if kind == "gare":
        nav = station_navigator(board=True)
        title = dire("tableaux.tableau_gares_departs_arrivees")
    else:
        nav = airport_navigator(board=True)
        title = dire("tableaux.tableau_aeroports_departs_arrivees")
    if nav is None:
        return dire("tableaux.liste_indisponible_reseau_copie")
    arbre.run(nav, key_source, say, out, title, dire("tableaux.board_keys"), "tableau")
    return dire("tableaux.tableau_ferme_2")
