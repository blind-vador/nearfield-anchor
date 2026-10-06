"""Liste noire des stations DAB+ (2026-10-06) : une station listee n'est ni
annoncee ni decrite par Gemini (watchers/dab_watch.py). Partagee par le
scanner, le journal (fleche gauche sur un message DAB+) et la commande
"liste noire" de la console.

Liste de base (DEFAULT_BANNED) : grandes radios, Radio France, reseaux
commerciaux. Reglage "dab" sur le NAS :
  interdites  stations ajoutees par l'utilisateur
  retirees    entrees de la liste de base que l'utilisateur a enlevees
Comparaison sans accents ni casse, par mot ou groupe de mots entier
("rtl" interdit RTL et RTL2, pas RTLB). Reseau "ici" de Radio France :
"ici" suivi d'un lieu (ICI_PLACES), toujours interdit.
"""
import json
import re
import unicodedata

import reglages

DEFAULT_BANNED = [
    # Radio France
    "france inter", "france culture", "france musique", "franceinfo", "france info", "fip", "mouv",
    "france bleu",
    # grandes radios nationales, publiques ou non
    "rfi", "radio classique", "europe 1", "europe1", "rtl", "rmc", "bfm", "sud radio", "radio nova", "tsf jazz",
    "jazz radio", "skyrock", "fun radio", "virgin", "rfm", "nrj", "nostalgie", "cherie", "rire et chansons",
    "rire & chansons", "oui fm", "m radio", "latina", "africa radio", "radio fg", "fg", "voltage", "radio ado", "ado", "swigg",
    "chante france", "radio orient", "beur fm", "radio monaco", "melody",
    # reseaux commerciaux regionaux
    "radio scoop", "tonic radio", "alouette", "hit west", "contact fm", "radio espace", "forum", "vibration",
    "jordanne", "totem", "radio star",
]
ICI_PLACES = ("saint", "st ", "loire", "lyon", "rhone", "isere", "auvergne", "drome", "ardeche", "haute",
              "pays", "paris", "provence", "alsace", "nord", "sud")
SEEN_FILE = "/tmp/dab_watch.json"  # stations vues par ensemble (dab_watch._note_station)


def plain(text):
    return " ".join(unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().split())


def _settings():
    data = reglages.get("dab") or {}
    data.setdefault("interdites", [])
    data.setdefault("retirees", [])
    return data


def entries():
    """[(nom, "ajoutee" ou "base")] : ajouts de l'utilisateur d'abord, du plus
    recent au plus ancien, puis la liste de base sans les entrees retirees."""
    data = _settings()
    removed = {plain(x) for x in data["retirees"]}
    out = [(name, "ajoutee") for name in reversed(data["interdites"])]
    out += [(name, "base") for name in DEFAULT_BANNED if name not in removed]
    return out


def banned():
    """Formes simplifiees de toutes les entrees en vigueur (liste de base
    seule si le reglage est illisible : le scanner ne doit pas planter)."""
    try:
        return [plain(name) for name, _ in entries()]
    except Exception as exc:
        print(f"[dab_liste] reglage dab illisible : {exc!r}")
        return [plain(name) for name in DEFAULT_BANNED]


def _matches(plain_name, part):
    return re.search(rf"(^| ){re.escape(part)}(?![a-z])", plain_name) is not None


def kept(name):
    """Station a ecouter : tout ce qui n'est pas interdit."""
    p = plain(name)
    if p.startswith("ici ") and (p[4:] + " ").startswith(ICI_PLACES):  # reseau ici (Radio France)
        return False
    return not any(_matches(p, part) for part in banned() if part)


def blocking_entry(name):
    """Entree de la liste qui interdit deja cette station, ou None."""
    p = plain(name)
    return next((n for n, _ in entries() if plain(n) and _matches(p, plain(n))), None)


def add(name):
    """Ajoute une station. Rend ("ajoutee" | "deja" | "nas", entree)."""
    name = " ".join(name.split())
    already = blocking_entry(name)
    if already:
        return "deja", already
    data = _settings()
    p = plain(name)
    if p in {plain(x) for x in data["retirees"]}:  # entree de base remise
        data["retirees"] = [x for x in data["retirees"] if plain(x) != p]
    else:
        data["interdites"].append(name)
    return ("ajoutee" if reglages.save("dab", data) else "nas"), name


def remove(items):
    """Enleve des entrees [(nom, origine)]. False si le NAS ne repond pas."""
    data = _settings()
    gone = {plain(n) for n, origin in items if origin == "ajoutee"}
    data["interdites"] = [x for x in data["interdites"] if plain(x) not in gone]
    known = {plain(x) for x in data["retirees"]}
    data["retirees"] += [n for n, origin in items if origin == "base" and plain(n) not in known]
    return reglages.save("dab", data)


def restore(items):
    """Annule remove(items)."""
    data = _settings()
    back = {plain(n) for n, origin in items if origin == "base"}
    data["retirees"] = [x for x in data["retirees"] if plain(x) not in back]
    known = {plain(x) for x in data["interdites"]}
    data["interdites"] += [n for n, origin in reversed(items) if origin == "ajoutee" and plain(n) not in known]
    return reglages.save("dab", data)


def seen_stations():
    """{station: ensemble} des stations deja captees (pour reconnaitre un nom tape)."""
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {name: channel for channel, names in (data.get("stations") or {}).items() for name in names}
