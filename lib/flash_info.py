"""Flash d'info locale, construit autour de la commune et du departement
choisis (commande "ville", lieu.json -- voir reglages.py).

Trois parties :
  1. presse du jour : Google Actualites (recherche sur la commune), le fil
     France 3 du departement, et la veille terrain (sites militants,
     Mastodon, Telegram : sources independantes, 2026-10-18) ; articles
     regroupes par sujet, un sujet repris par plusieurs medias monte a la une ;
  2. redaction du flash par Gemini (cle cles/gemini.txt) : ligne editoriale
     materialiste choisie par l'utilisateur (voir PROMPT), consigne stricte
     de ne rien ajouter aux faits fournis ; sans Gemini, lecture des
     meilleurs titres ;
  3. services, toujours frais car calcules a chaque demande : meteo du
     jour (Open-Meteo), vigilances meteo et crues (etat des veilles), trains
     de la gare suivie, bus et tram (Loire uniquement).

La partie presse n'est regeneree que si la peche a change : on compare
les sujets retenus a ceux du dernier flash (demande utilisateur,
2026-09-30 : "on compare la peche avant de regenerer").
"""
import concurrent.futures
import email.utils
import html
import json
import os
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests

import reglages
from phrases import Etiquettes, dire

CACHE_PATH = os.path.join(reglages.CACHE_DIR, "flash_info.json")
TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (X11; Linux armv7l) nearfield-anchor"
MAX_AGE_SEC = 24 * 3600
SAME_STORY = 0.5          # similarite des mots de titre au dela de laquelle deux articles = un sujet
TOP_STORIES = 12          # sujets confies a Gemini
DETAIL_STORIES = 30       # sujets gardes pour "flash details"

GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
FRANCE3_URL = "https://france3-regions.francetvinfo.fr/{region}/{departement}/rss"
DEPARTEMENT_URL = "https://geo.api.gouv.fr/departements/{code}"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
NAVITIA_URL = "https://api.sncf.com/v1/coverage/sncf/stop_areas/stop_area:SNCF:{uic}/departures"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]

APRS_WX_PATH = "/tmp/meteo_stations_aprs.json"
APRS_WX_MAX_KM = 20
APRS_WX_MAX_AGE_SEC = 2 * 3600
METEO_STATE = "/tmp/meteo_watch_vigilance_state.json"
CRUE_STATE = "/tmp/crue_watch_vigilance_state.json"
CRUE_NAMES = Etiquettes("crue.troncon", {"LC126": "la Loire forézienne"}, "nom d'un tronçon de rivière surveillé")
COLOR_NAMES = Etiquettes("vigilance.couleur", {2: "jaune", 3: "orange", 4: "rouge"}, "couleur de vigilance météo")
TRAIN_WINDOW_SEC = 3 * 3600

STOP_WORDS = set("""le la les l un une des de du d a au aux et en dans sur pour par
avec sans ce cette ces se sa son ses leur leurs qui que quoi est sont ont a ete plus
pas ne apres avant entre sous vers chez il elle ils elles on nous vous""".split())

WMO = Etiquettes("meteo.temps", {0: "ciel dégagé", 1: "ciel peu nuageux", 2: "éclaircies", 3: "ciel couvert",
       45: "brouillard", 48: "brouillard givrant", 51: "bruine légère", 53: "bruine",
       55: "bruine forte", 61: "pluie faible", 63: "pluie", 65: "forte pluie",
       66: "pluie verglaçante", 67: "forte pluie verglaçante", 71: "neige faible",
       73: "neige", 75: "forte neige", 77: "grains de neige", 80: "averses",
       81: "averses", 82: "fortes averses", 85: "averses de neige", 86: "fortes averses de neige",
       95: "orages", 96: "orages avec grêle", 99: "orages avec forte grêle"},
                 "temps qu'il fait, d'après le code météo")

# Ligne editoriale (choix de l'utilisateur, 2026-10-18) : lecture materialiste,
# pas de "journalisme de prefecture". La regle des faits reste entiere : rien
# d'invente, l'analyse est donnee comme analyse. Changer PROMPT_VERSION
# reecrit le flash suivant meme si la peche n'a pas change.
PROMPT_VERSION = 3  # 3 (2026-10-23) : le sport a part, precede de son jingle
PROMPT = """Tu es journaliste dans une radio locale libre française. Rédige le flash
d'information locale de {commune} ({departement}), à lire à voix haute par une
synthèse vocale, à partir UNIQUEMENT des sujets ci-dessous.

Ligne éditoriale :
- Grille de lecture matérialiste, marxiste et trotskiste : pour chaque sujet
  important, demande-toi quels rapports de classe, quels intérêts économiques,
  quels rapports de production et quel rapport de force entre travailleurs,
  patronat et État sont en jeu ; qui y gagne, qui en paie le prix.
- Critique rationaliste et systémique : cherche les causes structurelles
  (logique du profit, politiques publiques, institutions) plutôt que les
  explications individuelles, morales ou de fatalité. Pas de complotisme :
  des mécanismes, pas des intentions cachées.
- Pas de journalisme de préfecture : ne reprends jamais tel quel le récit
  des autorités (préfecture, police, gouvernement, mairie, direction d'une
  entreprise). Attribue-le : « selon la préfecture », « d'après la police ».
  Refuse leur vocabulaire euphémisant ou criminalisant quand un mot plus
  exact existe. Quand une source indépendante ou militante (type
  "independant") raconte autre chose, donne les deux versions et dis
  laquelle est vérifiée.
- Donne la parole aux premiers concernés : grévistes, manifestants,
  habitants, syndicats, collectifs, quand les sujets les mentionnent.

Règles sur les faits (elles passent avant tout) :
- N'ajoute aucun fait, chiffre, nom ou lieu absent des sujets fournis. Si un
  sujet est flou, reste flou ou laisse-le de côté.
- L'analyse doit se voir comme telle (« derrière cette décision… », « ce qui
  se joue, c'est… ») et rester courte : une phrase ou deux par sujet important.
- Les sujets "independant" viennent de sites militants, de Mastodon ou de
  Telegram : attribue-les (« selon des témoignages relayés par… ») et ne les
  présente pas comme vérifiés s'ils ne sont confirmés par aucune autre source.
- Commence directement par le sujet le plus important (l'annonce de l'heure et
  de la ville est déjà faite). "medias" indique combien de médias en parlent ;
  une mobilisation sociale ou une répression compte autant qu'un sujet repris
  partout.
- Laisse de côté les titres racoleurs ou vagues qui n'apportent aucun fait
  concret (classements, "au top", "on vous dit tout"...).
- Mets le sport à part, dans "sport" (chaîne vide s'il n'y en a pas) ;
  regroupe la culture et les sorties à la fin de "flash".
- 180 à 300 mots, phrases courtes, style radio, en français. Pas de markdown,
  pas de liste, pas de titre, pas de formule de fin.
- Écris les nombres en chiffres, sans abréviation (pas de "M€", écris
  "millions d'euros").

Réponds en JSON : {{"flash": "texte du flash", "sport": "le sport, ou vide", "rubriques": {{"id du sujet":
"rubrique"}}}} avec pour chaque sujet une rubrique parmi : À la une, Luttes
et mobilisations, Faits divers et justice, Politique et société, Économie,
Transports, Sport, Culture et sorties, Autres.

Sujets :
{sujets}
"""
INDEPENDENT_STORIES = 6   # sujets de la veille terrain toujours confies a Gemini, en plus du haut du classement


# --- Jingles (2026-10-23) ----------------------------------------------------
# Reperes poses dans le texte du flash complet : console/lecture_longue.py joue
# le son de l'evenement (lib/sons.py) a cet endroit ; sans_sons() les retire
# pour une lecture ordinaire.
MARQUE_SON = re.compile(r"\s*⟦son:(\w+)⟧\s*")


def son(evenement):
    return f"⟦son:{evenement}⟧"


def sans_sons(text):
    return MARQUE_SON.sub(" ", text).strip()


# --- Collecte --------------------------------------------------------------

def _norm(text):
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def _words(title):
    return {w for w in _norm(title).split() if w not in STOP_WORDS and len(w) > 1}


def _strip_html(text):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def _parse_rss(xml_text, google):
    items = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = html.unescape(it.findtext("title") or "").strip()
        source = (it.findtext("source") or "").strip()
        if google and source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        try:
            published = email.utils.parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except (TypeError, ValueError):
            published = time.time()
        description = "" if google else _strip_html(it.findtext("description"))
        if title:
            items.append({"titre": title, "source": source or "France 3", "date": published,
                          "resume": description, "lien": it.findtext("link") or ""})
    return items


def _get(url, **params):
    resp = requests.get(url, params=params or None, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp


def _slug(text):
    return "-".join(_norm(text).split())


_france3_cache = {}


def _france3_url(lieu):
    code = str(lieu["departement"])
    if code not in _france3_cache:
        dep = _get(DEPARTEMENT_URL.format(code=code), fields="nom,region").json()
        _france3_cache[code] = FRANCE3_URL.format(region=_slug(dep["region"]["nom"]),
                                                  departement=_slug(dep["nom"]))
    return _france3_cache[code]


def _flat(text):
    """Minuscules sans accents, tirets gardes (noms composes)."""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def _about_homonym(title, commune):
    """Vrai si le titre ne cite la commune que dans le nom d'une AUTRE
    commune ("Saint-Étienne-de-Montluc" pour Saint-Étienne) : la recherche
    Google les ramene, mais ce sont souvent des communes d'autres regions."""
    flat, name = _flat(title), re.escape(_flat(commune))
    if not re.search(name, flat):
        return False
    return not re.search(rf"(?<![\w-]){name}(?![\w-])", flat)


def _fetch_google(lieu):
    query = f'"{lieu["commune"]}" when:1d'
    xml_text = _get(GOOGLE_NEWS_URL, q=query, hl="fr", gl="FR", ceid="FR:fr").text
    return [a for a in _parse_rss(xml_text, google=True) if not _about_homonym(a["titre"], lieu["commune"])]


def _fetch_france3(lieu):
    return _parse_rss(_get(_france3_url(lieu)).text, google=False)


def _fetch_terrain(lieu):
    """Veille terrain (watchers/veille_terrain.py, journal du jour et de la
    veille) : sites militants, Mastodon, Telegram. Seulement ce qu'elle a
    juge digne d'etre dit (parle du coin, ou site local)."""
    import veille_donnees as vd
    rows = []
    for day in (datetime.now(), datetime.now() - timedelta(days=1)):
        path = os.path.join(vd.ARCHIVE_DIR, f"terrain_{day:%Y-%m-%d}.jsonl")
        try:
            with open(path, encoding="utf-8") as f:
                rows += [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            continue
    articles = []
    for r in rows:
        if not (r.get("annonce") or r.get("local")):
            continue
        text = r.get("texte") or ""
        title = r.get("titre") or (text[:140].rsplit(" ", 1)[0] if len(text) > 140 else text)
        if title:
            articles.append({"titre": title, "source": r.get("source", "veille terrain"), "date": r.get("t", 0),
                             "resume": text[:400], "lien": r.get("lien") or "", "independant": True})
    return articles


def fetch_articles(lieu):
    """Articles des dernieres 24 heures, toutes sources ; les sources en
    panne sont ignorees (liste des pannes rendue a part)."""
    articles, failed = [], []
    fetchers = {"Google Actualités": _fetch_google, "France 3": _fetch_france3, "Veille terrain": _fetch_terrain}
    with concurrent.futures.ThreadPoolExecutor(len(fetchers)) as pool:
        futures = {pool.submit(fn, lieu): name for name, fn in fetchers.items()}
        for future in concurrent.futures.as_completed(futures):
            try:
                articles.extend(future.result())
            except Exception as exc:  # reseau, XML, format : la source est sautee
                print(f"[flash_info] {futures[future]} indisponible : {exc!r}")
                failed.append(futures[future])
    cutoff = time.time() - MAX_AGE_SEC
    return [a for a in articles if a["date"] >= cutoff], failed


def cluster(articles):
    """Regroupe les articles par sujet. Rend les sujets tries : nombre de
    medias, puis fraicheur."""
    stories = []
    for art in sorted(articles, key=lambda a: -a["date"]):
        words = _words(art["titre"])
        best, best_sim = None, 0.0
        for story in stories:
            union = words | story["mots"]
            sim = len(words & story["mots"]) / len(union) if union else 0.0
            if sim > best_sim:
                best, best_sim = story, sim
        if best is not None and best_sim >= SAME_STORY:
            best["articles"].append(art)
            best["mots"] |= words
        else:
            stories.append({"mots": set(words), "articles": [art]})
    for story in stories:
        arts = story["articles"]
        story["medias"] = len({a["source"] for a in arts})
        story["independant"] = any(a.get("independant") for a in arts)
        story["sources"] = sorted({a["source"] for a in arts})
        story["date"] = max(a["date"] for a in arts)
        # titre le plus informatif : celui d'un article avec resume, sinon le plus long
        lead = max(arts, key=lambda a: (bool(a["resume"]), len(a["titre"])))
        story["titre"] = lead["titre"]
        story["resume"] = lead["resume"]
        story["cle"] = _norm(lead["titre"])
    stories.sort(key=lambda s: (-s["medias"], -s["date"]))
    return stories


# --- Redaction ------------------------------------------------------------

def _gemini(prompt):
    """(dict JSON, None) ou (None, raison)."""
    key = reglages.cle("gemini")
    if not key:
        return None, dire("flash.cle_gemini")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3}}
    reason = dire("flash.gemini_repond")
    for model in GEMINI_MODELS:
        try:
            resp = requests.post(GEMINI_URL.format(model=model), json=body, timeout=60,
                                 headers={"x-goog-api-key": key})
        except requests.RequestException:
            return None, dire("flash.acces_gemini")
        if resp.status_code in (401, 403):
            return None, dire("flash.google_refuse_cle_gemini")
        if not resp.ok:
            print(f"[flash_info] Gemini {model} : HTTP {resp.status_code}")
            reason = dire("flash.quota_gemini_epuise") if resp.status_code == 429 else reason
            continue
        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            if isinstance(data.get("flash"), str) and data["flash"].strip():
                return data, None
        except (KeyError, IndexError, ValueError, AttributeError):
            pass
        reason = dire("flash.reponse_gemini_inutilisable")
    return None, reason


def _hours_ago(ts):
    hours = int((time.time() - ts) // 3600)
    return dire("flash.moins_heure") if hours < 1 else f"{hours} heure{'s' if hours > 1 else ''}"


def _selection(stories):
    """Haut du classement, plus les sujets de la veille terrain qui n'y sont
    pas : un seul site militant n'atteint jamais le nombre de medias de la
    presse, il ne doit pas disparaitre pour autant."""
    top = stories[:TOP_STORIES]
    extra = [s for s in stories[TOP_STORIES:] if s.get("independant")][:INDEPENDENT_STORIES]
    return top + extra


def write_news(lieu, stories):
    """(texte, {cle: rubrique}, remarque ou None)."""
    top = _selection(stories)
    if not top:
        return dire("flash.information_locale_marquante_depuis"), {}, None
    sujets = "\n".join(
        json.dumps({"id": str(i), "titre": s["titre"], "resume": s["resume"][:400],
                    "medias": s["medias"], "sources": s.get("sources", [])[:5],
                    "type": "independant" if s.get("independant") else "presse",
                    "il_y_a": _hours_ago(s["date"])}, ensure_ascii=False)
        for i, s in enumerate(top))
    data, problem = _gemini(PROMPT.format(commune=lieu["commune"],
                                          departement=lieu["departement_nom"], sujets=sujets))
    if data is None:
        titles = ". ".join(s["titre"].rstrip(".") for s in top[:5])
        return dire("flash.titres", titles=titles), {}, problem
    rubriques = {}
    for i, s in enumerate(top):
        rubrique = (data.get("rubriques") or {}).get(str(i))
        if isinstance(rubrique, str):
            rubriques[s["cle"]] = rubrique
    text = " ".join(data["flash"].replace("*", "").split())
    sport = " ".join(str(data.get("sport") or "").replace("*", "").split())
    if sport:
        text += f" {son('jingle_sport')} {sport}"
    return text, rubriques, None


# --- Services (toujours frais) --------------------------------------------

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def weather_text(lieu):
    evening = datetime.now().hour >= 18
    try:
        daily = _get(OPEN_METEO_URL, latitude=lieu["lat"], longitude=lieu["lon"], timezone="auto",
                     forecast_days=2,
                     daily="weather_code,temperature_2m_max,temperature_2m_min,"
                           "precipitation_sum,wind_gusts_10m_max").json()["daily"]
    except (requests.RequestException, ValueError, KeyError):
        return ""
    day = 1 if evening else 0
    when = dire("meteo.demain") if evening else dire("meteo.aujourdhui")
    try:
        sky = WMO.get(daily["weather_code"][day], "")
        tmin, tmax = round(daily["temperature_2m_min"][day]), round(daily["temperature_2m_max"][day])
        rain, gusts = daily["precipitation_sum"][day], daily["wind_gusts_10m_max"][day]
    except (KeyError, IndexError, TypeError):
        return ""
    text = dire("flash.meteo_degres", when=when, commune=lieu['commune'], sky=sky, tmin=tmin, tmax=tmax)
    if rain and rain >= 1:
        text += (" " + dire("flash.millimetre_pluie_attendus", round=round(rain), pluriel='s' if round(rain) > 1 else ''))
    if gusts and gusts >= 50:
        text += (" " + dire("flash.rafales_jusqu_kilometres_heure", round=round(gusts)))
    return text


def nearby_station_text(lieu):
    """Releve de la station meteo radioamateur (APRS) la plus proche, si recente."""
    import math
    stations = _read_json(APRS_WX_PATH) or {}
    best = None
    for call, st in stations.items():
        wx = st.get("meteo") or {}
        if wx.get("temperature") is None or time.time() - st.get("t", 0) > APRS_WX_MAX_AGE_SEC:
            continue
        dlat = math.radians(st["lat"] - float(lieu["lat"]))
        dlon = math.radians(st["lon"] - float(lieu["lon"])) * math.cos(math.radians(float(lieu["lat"])))
        km = 6371 * math.hypot(dlat, dlon)
        if km <= APRS_WX_MAX_KM and (best is None or km < best[0]):
            best = (km, wx)
    if best is None:
        return ""
    km, wx = best
    where = dire("flash.tout_pres") if km < 1 else dire("flash.kilometre", round=round(km), pluriel='s' if round(km) > 1 else '')
    temp = f"{wx['temperature']:.0f}".replace("-", dire("meteo.moins") + " ")
    return dire("flash.releve_station_meteo_radioamateur", where=where, temp=temp)


def alerts_text(lieu):
    parts = []
    meteo = _read_json(METEO_STATE) or {}
    active = [(level, name) for name, level in meteo.items() if isinstance(level, int) and level >= 2]
    if active:
        by_color = {}
        for level, name in sorted(active, reverse=True):
            by_color.setdefault(COLOR_NAMES.get(level, "jaune"), []).append(name)
        parts.append(" ".join(dire("flash.vigilance_departement", color=color, join=' et '.join(names), departement_nom=lieu['departement_nom'])
                              for color, names in by_color.items()))
    crue = _read_json(CRUE_STATE) or {}
    for code, level in crue.items():
        if isinstance(level, int) and level >= 2:
            parts.append(dire("flash.vigilance_crue", get=COLOR_NAMES.get(level, 'jaune'), get2=CRUE_NAMES.get(code, 'le tronçon surveillé')))
    return " ".join(parts)


def trains_text():
    gare = reglages.get("trains")
    key = reglages.cle("sncf")
    if not key:
        return ""
    try:
        resp = requests.get(NAVITIA_URL.format(uic=gare["uic"]), auth=(key, ""), timeout=TIMEOUT,
                            params={"duration": TRAIN_WINDOW_SEC, "count": 50, "data_freshness": "realtime"})
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return ""
    cancelled = set()
    for disr in data.get("disruptions") or []:
        if (disr.get("severity") or {}).get("effect") == "NO_SERVICE":
            cancelled.add(disr.get("id"))
    late, gone = [], []
    for dep in data.get("departures") or []:
        info = dep.get("display_informations") or {}
        std = dep.get("stop_date_time") or {}
        direction = (info.get("direction") or "").split(" (", 1)[0]
        links = (dep.get("links") or []) + (info.get("links") or [])
        try:
            base = datetime.strptime(std["base_departure_date_time"], "%Y%m%dT%H%M%S")
            real = datetime.strptime(std["departure_date_time"], "%Y%m%dT%H%M%S")
        except (KeyError, ValueError):
            continue
        hour = f"{base.hour} heure {base.minute}" if base.minute else f"{base.hour} heure"
        if any(l.get("id") in cancelled for l in links):
            gone.append(dire("flash.vers", hour=hour, direction=direction))
        elif (real - base) >= timedelta(minutes=5):
            late.append(dire("flash.vers_minutes_retard", hour=hour, direction=direction, int=int((real - base).total_seconds() // 60)))
    name = gare["gare"]
    if not late and not gone:
        return dire("flash.trafic_normal_gare_3", name=name)
    parts = []
    if gone:
        parts.append((dire("flash.supprime") + " ") + ", ".join(gone[:3]))
    if late:
        parts.append((dire("flash.retard") + " ") + ", ".join(late[:3]))
    return (dire("flash.gare", name=name) + " ") + " ; ".join(parts) + "."


# --- Assemblage -------------------------------------------------------------

def _load_cache():
    return _read_json(CACHE_PATH) or {}


def _save_cache(data):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        print(f"[flash_info] cache non ecrit : {exc}")


def _spoken_now():
    now = datetime.now()
    return f"{now.hour} heures {now.minute}" if now.minute else f"{now.hour} heures"


def _press(lieu):
    """Partie presse : (texte, rubriques, sujets, regeneree ?, remarque)."""
    articles, failed = fetch_articles(lieu)
    stories = cluster(articles)
    # La peche : tous les titres des sujets retenus. Un nouveau titre (meme
    # sur un sujet deja connu) = flash reecrit ; sinon, on relit le dernier.
    catch = sorted({_norm(a["titre"]) for s in _selection(stories) for a in s["articles"]})
    cache = _load_cache()
    fresh = not (cache.get("commune") == lieu["code_insee"] and cache.get("peche") == catch
                 and cache.get("texte") and cache.get("version") == PROMPT_VERSION)
    remark = None
    if fresh:
        news, rubriques, remark = write_news(lieu, stories)
        if remark is None:  # repli sur les titres : on retentera au prochain appel
            _save_cache({"commune": lieu["code_insee"], "peche": catch, "texte": news, "version": PROMPT_VERSION,
                         "rubriques": rubriques, "genere": time.time()})
    else:
        news, rubriques = cache["texte"], cache.get("rubriques") or {}
    if failed and not articles:
        news = dire("flash.presse_locale_injoignable_moment")
    return news, rubriques, stories, fresh, remark


def collect(lieu, complet=False, scan=None, scan_failed=None):
    """1) On rassemble tout, en parallele : presse, et pour le flash complet
    (demande utilisateur 2026-10-03) le bulletin meteo bati sur l'agregat de
    toutes les mesures (environnement compris : rivieres, seismes, air), le
    trafic routier, les trains et les bus. Le flash court garde les resumes."""
    import trafic
    import plantes_flash  # rubrique plantes : meteo par plante, ravageurs, semis (2026-10-07)
    import transports  # bus, tram, metro du reseau de la commune (2026-10-07, remplace la seule STAS)
    jobs = {"presse": (_press, lieu), "trains": (trains_text,), "bus": (transports.flash_text, lieu, complet),
            "plantes": (plantes_flash.text, complet)}
    if complet:
        import meteo_locale
        import trafic_heure
        jobs["meteo"] = (lambda: meteo_locale.bulletin(lieu, scan=scan, scan_failed=scan_failed),)
        jobs["routes"] = (trafic.summary,)
        jobs["radio"] = (trafic_heure.depuis_minuit,)
    else:
        jobs.update(alertes=(alerts_text, lieu), meteo=(weather_text, lieu), station=(nearby_station_text, lieu))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(job[0], *job[1:]) for name, job in jobs.items()}
        found = {}
        for name, future in futures.items():
            try:
                found[name] = future.result()
            except Exception as exc:  # une source en panne n'empeche pas le flash
                print(f"[flash_info] {name} : {exc!r}")
                found[name] = "" if name != "presse" else (dire("flash.presse_locale_injoignable_moment"), {}, [], False, None)
    return found


def build(complet=False, scan=None, scan_failed=None):
    """Rend {"texte": flash complet, "sujets": sujets pour les details,
    "nouveau": presse regeneree ?, "remarque": probleme eventuel}.
    2) Mise en forme, une fois tout rassemble."""
    lieu = reglages.get("lieu")
    found = collect(lieu, complet, scan, scan_failed)
    news, rubriques, stories, fresh, remark = found["presse"]
    if complet:
        transport = [(dire("flash.sur_les_routes") + " " + found["routes"]) if found["routes"] else "",
                     found["trains"], found["bus"]]
        sections = [(son("jingle_meteo") + " " + found["meteo"]) if found["meteo"] else "", found["plantes"],
                    " ".join([son("jingle_transport")] + [t for t in transport if t]) if any(transport) else "",
                    (son("jingle_flashinfo") + " " + found["radio"]) if found.get("radio") else ""]
        intro = son("jingle_flashinfo") + " "
    else:
        sections = [found["alertes"], found["meteo"], found["station"], found["plantes"], found["trains"], found["bus"]]
        intro = ""
        news = sans_sons(news)
    text = intro + (dire("flash.flash_info", commune=lieu['commune'], v1=_spoken_now(), news=news) + " ") + \
        " ".join(t for t in sections if t)
    for story in stories:
        story["rubrique"] = rubriques.get(story["cle"], "Autres infos")
        story.pop("mots", None)
    return {"texte": text.strip(), "sujets": stories[:DETAIL_STORIES], "nouveau": fresh,
            "remarque": remark}
