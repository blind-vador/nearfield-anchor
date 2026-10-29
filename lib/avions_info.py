"""Qui est cet avion, ou en est-il de son vol, a quoi ressemble-t-il ?
(2026-10-03, pour dire en clair les messages ACARS et VDL2.)

- identite : base publique adsbdb.com (gratuite, sans cle) : modele,
  compagnie, immatriculation, depuis l'adresse ICAO ou l'immatriculation ;
  trajet depuis l'indicatif. Cache sur la carte SD (30 jours pour l'avion,
  1 jour pour un trajet).
- etat et phase de vol : etat publie par adsb_watch (OpenSky + antenne,
  /tmp/avions_etat.json) : au sol, montee, croisiere, descente, approche.
- allure : description du MODELE par Gemini (une fois par type d'avion), et
  de la LIVREE d'apres une vraie photo de l'appareil (une fois par
  compagnie). Plafond d'appels Gemini par jour (GEMINI_PER_DAY).
"""
import chemins
import json
import os
import time

import requests

CACHE_PATH = chemins.base("avions_cache.json")
STATE_PATH = "/tmp/avions_etat.json"
STATE_MAX_AGE_SEC = 600
ADSBDB = "https://api.adsbdb.com/v0"
AIRCRAFT_TTL = 30 * 86400
ROUTE_TTL = 86400
MISS_TTL = 3 * 86400          # inconnu de la base : on ne redemande pas tout de suite
TIMEOUT = 8
GEMINI_PER_DAY = 40

_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save():
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_load(), f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        print(f"[avions_info] cache non ecrit : {exc}")


def _cached(section, key, ttl, fetch):
    store = _load().setdefault(section, {})
    entry = store.get(key)
    if entry and time.time() - entry["t"] < (ttl if entry["v"] is not None else MISS_TTL):
        return entry["v"]
    try:
        value = fetch()
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"[avions_info] {section} {key} : {exc!r}")
        return entry["v"] if entry else None
    store[key] = {"t": time.time(), "v": value}
    _save()
    return value


def _adsbdb(path, field):
    resp = requests.get(f"{ADSBDB}/{path}", timeout=TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return (resp.json().get("response") or {}).get(field) if isinstance(resp.json().get("response"), dict) else None


def aircraft(hex_addr=None, registration=None):
    """{type, icao_type, manufacturer, registration, registered_owner, ...} ou None."""
    key = (hex_addr or "").upper() or (registration or "").upper().lstrip(".")
    if not key:
        return None
    return _cached("avions", key, AIRCRAFT_TTL, lambda: _adsbdb(f"aircraft/{key}", "aircraft"))


def route(callsign):
    """{airline, origin, destination} ou None."""
    callsign = (callsign or "").strip().upper()
    if len(callsign) < 3:
        return None
    return _cached("trajets", callsign, ROUTE_TTL, lambda: _adsbdb(f"callsign/{callsign}", "flightroute"))


def state(hex_addr):
    """Etat ADS-B recent de l'appareil, ou None."""
    try:
        with open(STATE_PATH) as f:
            found = json.load(f).get((hex_addr or "").upper())
    except (OSError, ValueError):
        return None
    return found if found and time.time() - found.get("t", 0) < STATE_MAX_AGE_SEC else None


def phase(st):
    """Code de phase de vol d'apres l'etat ADS-B : sol, roulage, decollage,
    montee, croisiere, palier, descente, approche ; None si inconnu."""
    if not st:
        return None
    if st.get("sol"):
        return "roulage" if (st.get("vitesse") or 0) > 5 else "sol"
    alt, vz = st.get("altitude") or 0, st.get("vz") or 0
    if vz > 2.5:
        return "decollage" if alt < 1500 else "montee"
    if vz < -2.5:
        return "approche" if alt < 3000 else "descente"
    return "croisiere" if alt >= 7000 else "palier"


# --- allure (Gemini, en cache) ----------------------------------------------------------

TYPE_PROMPT = """Décris en une phrase courte, pour une personne aveugle qui écoute à voix haute, à quoi
ressemble l'avion {manufacturer} {type} (code {icao_type}) : sa taille approximative, le nombre et la
place de ses moteurs, un signe distinctif. Ton simple, sans chiffres inutiles, pas de markdown.
Langue de la réponse : {langue}."""

LIVERY_PROMPT = """Voici la photo d'un avion de la compagnie {owner}. Décris en une demi-phrase sa
livrée (couleurs du fuselage, de la dérive, du logo), pour une personne aveugle, en commençant par
"aux couleurs". Pas de markdown. Si la photo ne permet pas de le dire, réponds seulement : inconnu.
Langue de la réponse : {langue}."""


def _gemini_budget():
    day = time.strftime("%Y-%m-%d")
    used = _load().setdefault("gemini", {})
    if used.get("jour") != day:
        used.clear()
        used.update(jour=day, appels=0)
    if used["appels"] >= GEMINI_PER_DAY:
        return False
    used["appels"] += 1
    return True


def _langue():
    try:
        import phrases
        return {"fr": "français", "en": "anglais", "de": "allemand", "es": "espagnol", "it": "italien"}.get(
            phrases.langue(), phrases.langue())
    except Exception:
        return "français"


def type_look(ac):
    """Allure du modele (cache par type), ou None."""
    if not ac or not ac.get("icao_type"):
        return None
    store = _load().setdefault("allures", {})
    if ac["icao_type"] in store:
        return store[ac["icao_type"]]
    if not _gemini_budget():
        return None
    import gemini
    text, problem = gemini.ask(TYPE_PROMPT.format(manufacturer=ac.get("manufacturer", ""), type=ac.get("type", ""),
                                                  icao_type=ac["icao_type"], langue=_langue()), temperature=0.3)
    if problem or not text:
        print(f"[avions_info] allure {ac['icao_type']} : {problem}")
        return None
    store[ac["icao_type"]] = " ".join(text.split())
    _save()
    return store[ac["icao_type"]]


def livery(ac):
    """Livree de la compagnie, d'apres une vraie photo (cache par compagnie), ou None."""
    if not ac or not ac.get("url_photo"):
        return None
    owner_key = ac.get("registered_owner_operator_flag_code") or ac.get("registered_owner")
    if not owner_key:
        return None
    store = _load().setdefault("livrees", {})
    if owner_key in store:
        return store[owner_key]
    if not _gemini_budget():
        return None
    try:
        jpeg = requests.get(ac["url_photo"], timeout=TIMEOUT).content
    except requests.RequestException as exc:
        print(f"[avions_info] photo {ac.get('registration')} : {exc!r}")
        return None
    import gemini
    text, problem = gemini.ask(LIVERY_PROMPT.format(owner=ac.get("registered_owner", ""), langue=_langue()),
                               temperature=0.2, jpeg=jpeg)
    if problem or not text:
        print(f"[avions_info] livree {owner_key} : {problem}")
        return None
    text = " ".join(text.split()).rstrip(".")
    store[owner_key] = None if text.lower().startswith("inconnu") else text
    _save()
    return store[owner_key]


def card(hex_addr=None, registration=None, callsign=None):
    """Tout ce qu'on sait de l'appareil, pret pour la fiche du message."""
    ac = aircraft(hex_addr, registration)
    hex_addr = hex_addr or (ac or {}).get("mode_s")
    st = state(hex_addr) if hex_addr else None
    callsign = (st or {}).get("indicatif") or callsign
    rt = route(callsign)
    out = {"adresse": hex_addr, "indicatif": callsign}
    if ac:
        out.update(modele=spoken_model(ac),
                   immatriculation=ac.get("registration"), compagnie=ac.get("registered_owner"),
                   pays=ac.get("registered_owner_country_name"), allure=type_look(ac), livree=livery(ac))
    if rt:
        out.update(depart=_airport(rt.get("origin")), arrivee=_airport(rt.get("destination")),
                   compagnie_vol=(rt.get("airline") or {}).get("name"))
    if st:
        out.update(phase=phase(st), altitude_m=round(st["altitude"]) if st.get("altitude") is not None else None,
                   vitesse_kmh=round(st["vitesse"] * 3.6) if st.get("vitesse") is not None else None,
                   vitesse_verticale_m_s=round(st["vz"], 1) if st.get("vz") is not None else None)
    return {k: v for k, v in out.items() if v not in (None, "")}


def spoken_model(ac):
    """ "A320 251NSL" (code constructeur) -> "Airbus A320neo" : lisible a voix haute."""
    words = (ac.get("type") or "").split(" ")
    first = words[0]
    if not any(c.isdigit() for c in first) and len(words) > 1 and words[1][:1].isdigit():
        first += " " + words[1].split("-")[0]   # "CRJ 1000 NG" -> "CRJ 1000", "ERJ 170-100" -> "ERJ 170"
    if (ac.get("icao_type") or "").endswith("N") and first.startswith(("A31", "A32", "A33")):
        first += "neo"
    return f"{ac.get('manufacturer', '')} {first}".strip()


def _airport(a):
    if not a:
        return None
    return f"{a.get('municipality') or a.get('name')} ({a.get('country_name')})"
