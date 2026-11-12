"""Veille terrain (demande utilisateur 2026-10-17) : sources ouvertes non
etatiques, lues a la voix au fil de l'eau, pour savoir ou ca se passe et
prevenir des amis dehors.

  flux        sites militants a publication ouverte (Le Numero Zero pour
              Saint-Etienne, Rebellyon, Paris-luttes...) : un site "local"
              est lu en entier, les autres seulement s'ils parlent du coin ;
  mastodon    messages publics des mots-diese choisis (API publique, sans
              compte) : lus s'ils contiennent un mot d'urgence, sinon
              seulement gardes au journal ;
  telegram    canaux PUBLICS choisis par l'utilisateur (page web t.me/s/,
              sans compte) : tout est lu.
Un message qui contient un mot d'urgence (blesse, gaz, nasse, evitez...)
est annonce avec le son d'alerte. Tout va au journal du jour sur le NAS
(terrain_<jour>.jsonl) ; commande console "terrain" : les derniers.

Ville : celle des reglages du Pi (commande "ville", reglages "lieu"), relue
a chaque tour. Gemini prepare une fois par semaine son profil (quartiers,
communes voisines, habitants, mots-diese, sites locaux a publication
ouverte, verifies avant usage) ; et toutes les 12 h (MOBILISATION_EVERY_SEC) il lit
ce qui a ete capte pour ajuster les mots-diese et mots des mobilisations du
moment (chaque mot expire apres MOBILISATION_TTL_SEC sans etre repropose) :
rien de fige sur une date.

Reglages : SOURCES_PATH (cree avec les valeurs par defaut, a modifier a la
main) ; profil et mots du moment : PROFIL_PATH. Rien n'est publie ni
envoye : lecture seule.
"""
import chemins
import html
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import veille_donnees as vd  # noqa: E402
from phrases import dire  # noqa: E402
from sound_paths import RADIO_SCANER_DIR  # noqa: E402

SOURCES_PATH = chemins.base("terrain_sources.json")
SEEN_PATH = chemins.base("terrain_vus.json")
POLL_SEC = 120
PANNE_MAX_SEC = 3600
_pannes = {}   # source -> {n: echecs de suite, prochain: heure du prochain essai}
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux armv7l) veille-terrain"}
FIRST_RUN_MAX_AGE_SEC = 3 * 3600   # premier lancement : on lit ce qui a moins de 3 h
FIRST_RUN_MAX_ITEMS = 5
MAX_SPOKEN_CHARS = 400
# Jamais lu a voix haute au-dela (un mot-diese ajoute remonte des messages
# de plusieurs mois) : garde au journal seulement.
MAX_ANNOUNCE_AGE_SEC = 3 * 3600
# Au plus tant d'annonces par tour (un canal tout juste abonne en a vingt
# d'un coup) : les autres vont au journal, une phrase dit combien.
MAX_SAY_PER_ROUND = 6
import sons
SOUND = sons.chemin("terrain_alerte", f"{RADIO_SCANER_DIR}/incoming.mp3")  # sons propres (2026-10-23, sons.py)
SOUND_URGENT = sons.chemin("terrain_urgence", f"{RADIO_SCANER_DIR}/scaner_error.mp3")

PROFIL_PATH = chemins.base("terrain_profil.json")
PROFIL_TTL_SEC = 7 * 86400
MOBILISATION_EVERY_SEC = 12 * 3600  # demande utilisateur 2026-10-18 : un mouvement social s'organise sur des jours
MOBILISATION_TTL_SEC = 48 * 3600
MAX_TAGS = 10

DEFAULT_SOURCES = {
    # Sites regionaux et nationaux : lus seulement s'ils parlent de la ville.
    # Les sites locaux viennent du profil de la ville (Gemini, verifies).
    "flux": [
        {"nom": "Rebellyon", "url": "https://rebellyon.info/spip.php?page=backend", "local": False},
        {"nom": "Paris-luttes", "url": "https://paris-luttes.info/spip.php?page=backend", "local": False},
    ],
    "mastodon_instance": "mastodon.social",
    "mots_diese_en_plus": [],
    "mots_diese_exclus": ["asse", "ol", "om", "psg", "ligue1", "foot", "football"],
    "telegram": [],
    "mots_lieu_en_plus": [],
    "mots_urgence": ["blessé", "blessée", "blessés", "blessure", "arraché", "grenade", "gaz", "lacrymo", "lbd",
                     "nasse", "nassé", "charge", "chargent", "interpell", "garde à vue", "évitez", "eviter",
                     "bloqué", "barrage", "kettling", "medic", "secours", "danger", "explosion",
                     "canon à eau", "bac", "brav", "urgence"],
}
# Sites locaux connus, en secours si Gemini ne repond pas (departement -> sites).
KNOWN_LOCAL_SITES = {
    "42": [{"nom": "Le Numéro Zéro", "url": "https://lenumerozero.info/spip.php?page=backend"}],
    "69": [{"nom": "Rebellyon", "url": "https://rebellyon.info/spip.php?page=backend"}],
    "75": [{"nom": "Paris-luttes", "url": "https://paris-luttes.info/spip.php?page=backend"}],
}

PROFIL_PROMPT = """Ville : {commune} ({departement_nom}, {departement}), France.
Rends un JSON strict, sans commentaire :
{{"quartiers": [au plus 25 noms de quartiers, places et grandes rues ou se tiennent souvent manifestations et rassemblements],
 "communes_proches": [au plus 12 communes voisines],
 "habitants": [gentile(s), ex. "stephanois"],
 "mots_diese": [au plus 5 mots-diese de la ville elle-meme et de sa vie locale (pas de club de sport ni de
                equipe), en minuscules, sans # ni accent ni espace],
 "sites": [au plus 4 sites d'information militants ou a publication ouverte de cette ville ou de sa region
           (ex. reseau Mutu), avec l'adresse de leur flux RSS : {{"nom": "...", "rss": "https://..."}}]}}
N'invente rien : liste vide si tu ne sais pas."""

MOBILISATION_PROMPT = """Nous sommes le {date}. Veille citoyenne des mobilisations sociales a {commune} ({departement_nom}).
Voici des messages recents captes sur des sites militants et Mastodon :
{messages}

Rends un JSON strict : {{"mots_diese": [au plus 8 mots-diese EN COURS des mobilisations, greves, manifestations,
blocages qui concernent la ville ou le pays maintenant, en minuscules, sans # ni accent], "mots_cles": [au plus 12 mots
ou courtes expressions qui reviennent dans ces mobilisations (lieux de rendez-vous, nom du mouvement, actions)]}}.
Pas de mots generiques ("france", "politique") ni de noms de villes seuls. Liste vide si rien ne se passe."""


def load_sources():
    try:
        with open(SOURCES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "mots_lieu_en_plus" in data:
            return {**DEFAULT_SOURCES, **data}
    except (OSError, ValueError):
        pass
    try:  # absent, ou format d'avant le lien avec la ville : on repart des valeurs par defaut
        with open(SOURCES_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SOURCES, f, ensure_ascii=False, indent=1)
    except OSError as exc:
        print(f"[terrain] reglages non ecrits : {exc}")
    return dict(DEFAULT_SOURCES)


# --- ville et mots du moment -------------------------------------------------------------

def _plain(text):
    import unicodedata
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def _list(value):
    """Gemini rend parfois une chaine au lieu d'une liste."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(v).strip() for v in value or [] if str(v).strip()]


def lieu():
    try:
        import reglages
        return reglages.get("lieu") or {}
    except Exception as exc:
        print(f"[terrain] ville illisible : {exc!r}")
        return {}


def _profil_store():
    try:
        with open(PROFIL_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _profil_save(store):
    tmp = PROFIL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PROFIL_PATH)


def _ask_json(prompt):
    import gemini
    text, problem = gemini.ask(prompt, temperature=0.2, json_mode=True, timeout=60)
    if not text:
        print(f"[terrain] Gemini : {problem}")
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _valid_feed(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        return r.ok and ("<rss" in r.text[:2000] or "<feed" in r.text[:2000])
    except requests.RequestException:
        return False


def profil(place):
    """Profil de la ville (Gemini, une fois par semaine), sites verifies."""
    code = place.get("code_insee") or place.get("commune") or "?"
    store = _profil_store()
    entry = store.get("villes", {}).get(code)
    if entry and time.time() - entry.get("t", 0) < PROFIL_TTL_SEC:
        return entry["data"]
    data = _ask_json(PROFIL_PROMPT.format(**{k: place.get(k, "") for k in ("commune", "departement_nom", "departement")}))
    if data is None:
        return entry["data"] if entry else {}
    sites = []
    for site in (data.get("sites") or [])[:4]:
        url = (site or {}).get("rss") or ""
        if url.startswith("https://") and _valid_feed(url):
            sites.append({"nom": site.get("nom") or url, "url": url})
        else:
            print(f"[terrain] site propose ecarte (flux introuvable) : {site}")
    data["sites"] = sites
    store.setdefault("villes", {})[code] = {"t": time.time(), "data": data}
    _profil_save(store)
    print(f"[terrain] profil de {place.get('commune')} : {len(data.get('quartiers') or [])} lieux, "
          f"{len(sites)} site(s) local(aux), mots-diese {data.get('mots_diese')}")
    return data


def mobilisations():
    """{"mots_diese": {mot: t}, "mots_cles": {mot: t}} encore valables."""
    now = time.time()
    found = _profil_store().get("mobilisations", {})
    return {k: {w: t for w, t in (found.get(k) or {}).items() if now - t < MOBILISATION_TTL_SEC}
            for k in ("mots_diese", "mots_cles")}


def update_mobilisations(place, items):
    """Gemini relit les messages des dernieres 48 h et ajuste les mots du moment."""
    store = _profil_store()
    if time.time() - store.get("mobilisations_t", 0) < MOBILISATION_EVERY_SEC:
        return
    store["mobilisations_t"] = time.time()
    _profil_save(store)
    recent = [i for i in items if time.time() - i["t"] < MOBILISATION_TTL_SEC][-80:]
    if not recent:
        return
    lines = "\n".join(f"- {(i['titre'] + ' : ' if i['titre'] else '')}{i['texte'][:200]}" for i in recent)
    data = _ask_json(MOBILISATION_PROMPT.format(date=time.strftime("%d/%m/%Y %H h"), messages=lines,
                                                **{k: place.get(k, "") for k in ("commune", "departement_nom")}))
    if data is None:
        return
    store = _profil_store()
    current = mobilisations()
    now = time.time()
    for key, limit in (("mots_diese", 8), ("mots_cles", 12)):
        for word in (data.get(key) or [])[:limit]:
            word = str(word).strip().lstrip("#").lower()
            if key == "mots_diese":
                word = re.sub(r"[^a-z0-9_]", "", _plain(word))
            if len(word) >= 3:
                current[key][word] = now
    store["mobilisations"] = current
    _profil_save(store)
    print(f"[terrain] mots du moment : {sorted(current['mots_diese'])} / {sorted(current['mots_cles'])}")


def context(conf):
    """Mots de lieu, mots-diese (ville et mobilisations) et mots du moment."""
    place = lieu()
    prof = profil(place) if place else {}
    commune = place.get("commune") or ""
    variants = {commune, commune.replace("-", " "), _plain(commune), _plain(commune).replace("-", " ")}
    if _plain(commune).startswith("saint-"):
        variants |= {"st-" + commune[6:], "st " + commune[6:], "st-" + _plain(commune)[6:]}
    words = {w for w in variants if w}
    for key in ("quartiers", "communes_proches", "habitants"):
        words |= set(_list(prof.get(key)))
    words |= set(_list(conf.get("mots_lieu_en_plus")))
    words = {w for w in words if len(w) >= 3}
    if place.get("departement_nom"):
        words.add(place["departement_nom"])
    slug = re.sub(r"[^a-z0-9]", "", _plain(commune))
    excluded = set(_list(conf.get("mots_diese_exclus")))
    city_tags = [t for t in [slug] + _list(prof.get("mots_diese")) + _list(conf.get("mots_diese_en_plus"))
                 if len(t) >= 3 and t not in excluded]
    if place.get("departement"):
        city_tags.append(f"manif{place['departement']}")
    moment = mobilisations()
    city_tags = list(dict.fromkeys(city_tags))[:MAX_TAGS]
    movement_tags = [t for t in moment["mots_diese"] if t not in city_tags and t not in excluded][:max(0, MAX_TAGS - len(city_tags))]
    # Sites connus du departement : lus en entier ; ceux proposes par Gemini
    # (souvent regionaux) : seulement quand ils parlent du coin.
    sites = [dict(x, local=True) for x in KNOWN_LOCAL_SITES.get(place.get("departement", ""), [])]
    sites += [dict(x, local=False) for x in prof.get("sites") or [] if x["url"] not in {y["url"] for y in sites}]
    # Un nom de ville n'est pas un mot de mobilisation (sinon tout message qui le cite serait lu).
    places = {_plain(w) for w in words} | {"lyon", "paris", "france", _plain(commune)}
    movement = sorted(w for w in moment["mots_cles"] if _plain(w) not in places)
    return {"lieu": sorted(words), "tags_ville": city_tags, "tags_moment": movement_tags,
            "mouvement": movement, "sites": sites}


def _clean(text):
    text = re.sub(r"<br\s*/?>|</p>", " ", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"https?://\S+", "", text)
    return " ".join(text.split())


def _has(text, words):
    """Mot (ou debut de mot) present : "bac" ne prend pas "baccalaureat"
    pour une brigade, mais "interpell" prend "interpellations"."""
    low = (text or "").lower()
    for w in words:
        w = w.lower()
        pattern = rf"(?<!\w){re.escape(w)}" + (r"(?!\w)" if len(w) <= 4 else "")
        if re.search(pattern, low):
            return True
    return False


def _ts(value):
    """Date ISO (2026-10-16T22:30:00Z ou +00:00) -> secondes, 0 si illisible."""
    try:
        import datetime
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


# --- lecteurs : rendent [{id, source, titre, texte, t, lien}] -------------------------

def read_feed(src):
    page = requests.get(src["url"], headers=HEADERS, timeout=TIMEOUT).text
    items = []
    for block in re.findall(r"<item[^>]*>(.*?)</item>", page, re.S):
        def field(name):
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
            return m.group(1) if m else ""
        link = _clean(field("link"))
        desc = field("description")
        desc = re.sub(r"^<!\[CDATA\[|\]\]>$", "", desc.strip())
        items.append({"id": "flux:" + (link or _clean(field("title"))), "source": src["nom"],
                      "titre": _clean(field("title")), "texte": _clean(desc)[:600],
                      "t": _ts(_clean(field("dc:date"))), "lien": link, "local": src.get("local", False)})
    return items


def read_mastodon(instance, tag, city):
    """Messages publics d'un mot-diese. city : mot-diese de la ville (sinon
    d'une mobilisation : il faut alors que le message parle du coin)."""
    items = []
    url = f"https://{instance}/api/v1/timelines/tag/{tag}?limit=20"
    for p in requests.get(url, headers=HEADERS, timeout=TIMEOUT).json():
        if p.get("reblog"):
            continue
        items.append({"id": "masto:" + p["uri"], "source": "Mastodon, " + (p["account"].get("display_name")
                                                                            or p["account"]["acct"]),
                      "titre": "", "texte": _clean(p.get("content"))[:600], "t": _ts(p.get("created_at")),
                      "lien": p.get("url"), "mastodon": True, "tag_ville": city, "tag": tag})
    return items


def read_telegram(channel):
    """channel : {"nom", "titre", "mode"} (ou ancien format : le nom seul)."""
    import telegram_public
    sub = channel if isinstance(channel, dict) else {"nom": channel}
    info = telegram_public.fetch(sub["nom"])
    if info is None:
        raise ValueError("canal sans apercu public")
    title = sub.get("titre") or info["titre"]
    return [{"id": "tg:" + m["id"], "source": f"Telegram, {title}", "titre": "", "texte": m["texte"], "t": m["t"],
             "lien": m["lien"], "telegram": True, "mode": sub.get("mode", "tout")} for m in info["messages"]]


# --- abonnements Telegram (dialogue console "telegram") -----------------------------------

TELEGRAM_MODES = ["tout", "coin", "urgent"]   # tout lire / s'il parle de la ville / seulement l'urgent
SUGGEST_TTL_SEC = 12 * 3600
SUGGEST_MAX_SILENCE_SEC = 30 * 86400  # canal abandonne ou copie d'un vrai : ecarte
SUGGEST_PROMPT = """Ville : {commune} ({departement_nom}), France. Mobilisations du moment : {moment}.
Propose des noms de canaux Telegram PUBLICS (diffusion ouverte) utiles pour suivre en temps reel, depuis
des sources non etatiques, les mobilisations sociales, rassemblements, conseils de securite et entraide
(street medics, legal team, antirepression, collectifs, medias independants, syndicats), a {commune},
dans sa region, ou en France. Rends un JSON strict : {{"canaux": [{{"nom": "nom_du_canal_sans_arobase",
"pourquoi": "quelques mots"}}]}}, au plus 20. Seulement des noms dont tu es raisonnablement sur."""


def subscriptions():
    subs = []
    for c in load_sources().get("telegram", []):
        subs.append(c if isinstance(c, dict) else {"nom": c, "titre": c, "mode": "tout"})
    return subs


def _save_subscriptions(subs):
    conf = load_sources()
    conf["telegram"] = subs
    with open(SOURCES_PATH, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=1)


def subscribe(info, mode="tout"):
    subs = [s for s in subscriptions() if s["nom"].lower() != info["nom"].lower()]
    subs.append({"nom": info["nom"], "titre": info.get("titre") or info["nom"], "mode": mode})
    _save_subscriptions(subs)


def unsubscribe(name):
    _save_subscriptions([s for s in subscriptions() if s["nom"].lower() != name.lower()])


def next_mode(name):
    subs = subscriptions()
    for s in subs:
        if s["nom"].lower() == name.lower():
            s["mode"] = TELEGRAM_MODES[(TELEGRAM_MODES.index(s.get("mode", "tout")) + 1) % len(TELEGRAM_MODES)]
            _save_subscriptions(subs)
            return s["mode"]
    return None


def suggestions(fresh=False):
    """Canaux proposes par Gemini pour la ville, VERIFIES un par un (apercu
    public existant) ; gardes SUGGEST_TTL_SEC. Rend [{nom, titre, description,
    abonnes, pourquoi, dernier}]."""
    import telegram_public
    store = _profil_store()
    cached = store.get("telegram_suggestions") or {}
    place = lieu()
    if not fresh and cached.get("ville") == place.get("code_insee") and time.time() - cached.get("t", 0) < SUGGEST_TTL_SEC:
        return cached["canaux"]
    moment = ", ".join(sorted(mobilisations()["mots_diese"])) or "-"
    data = _ask_json(SUGGEST_PROMPT.format(moment=moment, **{k: place.get(k, "") for k in ("commune", "departement_nom")}))
    found = []
    for c in (data or {}).get("canaux", [])[:20]:
        name = telegram_public.clean_name((c or {}).get("nom"))
        if not name or any(f["nom"].lower() == name.lower() for f in found):
            continue
        try:
            info = telegram_public.fetch(name)
        except Exception:
            info = None
        if info is None or not info["messages"]:
            print(f"[terrain] canal propose ecarte (pas d'apercu public) : {name}")
            continue
        if time.time() - max(m["t"] for m in info["messages"]) > SUGGEST_MAX_SILENCE_SEC:
            print(f"[terrain] canal propose ecarte (muet depuis plus de 30 jours) : {name}")
            continue
        found.append({"nom": info["nom"], "titre": info["titre"], "description": info["description"][:300],
                      "abonnes": info["abonnes"], "pourquoi": c.get("pourquoi", ""),
                      "dernier": max(m["t"] for m in info["messages"])})
    store = _profil_store()
    store["telegram_suggestions"] = {"t": time.time(), "ville": place.get("code_insee"), "canaux": found}
    _profil_save(store)
    return found


# --- decision ---------------------------------------------------------------------------

def worth_saying(item, conf, ctx):
    """(dire ?, urgent ?)"""
    whole = f"{item['titre']} {item['texte']}"
    urgent = _has(whole, conf["mots_urgence"])
    local = _has(whole, ctx["lieu"])
    movement = _has(whole, ctx["mouvement"])
    if item.get("telegram"):
        mode = item.get("mode", "tout")
        return (mode == "tout" or (mode == "coin" and local) or urgent), urgent
    if item.get("mastodon"):
        if item.get("tag_ville"):
            return urgent or movement, urgent
        return local and (urgent or movement), urgent and local
    if item.get("local"):
        return True, urgent
    return local, urgent and local


def spoken(item, urgent):
    body = item["titre"] or item["texte"]
    if item["titre"] and item["texte"] and urgent:
        body = f"{item['titre']}. {item['texte']}"
    if len(body) > MAX_SPOKEN_CHARS:
        body = body[:MAX_SPOKEN_CHARS].rsplit(" ", 1)[0] + "..."
    key = "terrain.urgent" if urgent else "terrain.message"
    return dire(key, source=item["source"], texte=body)


def collect(conf, ctx):
    items = []
    instance = conf.get("mastodon_instance") or "mastodon.social"
    feeds = {f["url"]: f for f in list(conf.get("flux", [])) + ctx["sites"]}  # un site local l'emporte
    readers = ([(read_feed, (f,)) for f in feeds.values()]
               + [(read_mastodon, (instance, t, True)) for t in ctx["tags_ville"]]
               + [(read_mastodon, (instance, t, False)) for t in ctx["tags_moment"]]
               + [(read_telegram, (c,)) for c in conf.get("telegram", [])])
    for reader, args in readers:
        cle = repr((reader.__name__, args))
        panne = _pannes.get(cle)
        if panne and time.time() < panne["prochain"]:
            continue
        try:
            items += reader(*args)
        except Exception as exc:
            # Source en panne (2026-11-12 : trois sites du meme hebergeur injoignables
            # des jours durant, 1 300 lignes de journal par jour) : on la dit une fois,
            # puis on la reessaie de plus en plus rarement, jusqu'a une heure.
            n = panne["n"] + 1 if panne else 1
            _pannes[cle] = {"n": n, "prochain": time.time() + min(POLL_SEC * 2 ** (n - 1), PANNE_MAX_SEC)}
            if n == 1:
                print(f"[terrain] source illisible ({reader.__name__} "
                      f"{args[-1] if reader is read_mastodon else args[0]}) : {exc!r}")
            continue
        if panne:
            print(f"[terrain] source revenue apres {panne['n']} echec(s) : {reader.__name__} {args[-1]}")
            _pannes.pop(cle, None)
    unique = {}
    for i in items:
        unique.setdefault(i["id"], i)
    return list(unique.values())


def main():
    print("[terrain] demarrage de la veille terrain")
    mem = vd.Memory(SEEN_PATH)
    first_run = "vus" not in mem.data
    seen = set(mem.data.get("vus", []))
    while True:
        conf = load_sources()
        ctx = context(conf)
        items = sorted(collect(conf, ctx), key=lambda i: i["t"])
        fresh = [i for i in items if i["id"] not in seen]
        if first_run:
            fresh = [i for i in fresh if time.time() - i["t"] < FIRST_RUN_MAX_AGE_SEC][-FIRST_RUN_MAX_ITEMS:]
            seen |= {i["id"] for i in items}
            first_run = False
        said = skipped = 0
        for item in fresh:
            seen.add(item["id"])
            say, urgent = worth_saying(item, conf, ctx)
            say = say and time.time() - item["t"] < MAX_ANNOUNCE_AGE_SEC
            if say and said >= MAX_SAY_PER_ROUND and not urgent:
                say, skipped = False, skipped + 1
            vd.archive("terrain", dict(item, annonce=say, urgent=urgent))
            if say:
                said += 1
                text = spoken(item, urgent)
                print(f"[terrain] {text}")
                vd.announce(text, SOUND_URGENT if urgent else SOUND, "terrain", "veille_terrain")
        if skipped:
            vd.announce(dire("terrain.autres", nombre=skipped), SOUND, "terrain", "veille_terrain")
        mem.data["vus"] = list(seen)[-3000:]
        mem.save()
        try:
            update_mobilisations(lieu(), items)
        except Exception as exc:
            print(f"[terrain] mots du moment : {exc!r}")
        time.sleep(POLL_SEC)


def latest(n=5):
    """Commande console "terrain" : les n derniers messages du journal du jour."""
    import datetime
    path = os.path.join(vd.ARCHIVE_DIR, f"terrain_{datetime.datetime.now():%Y-%m-%d}.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        rows = []
    rows = [r for r in rows if r.get("annonce")][-n:]
    if not rows:
        return dire("terrain.rien")
    parts = [spoken(r, r.get("urgent")) for r in reversed(rows)]
    return dire("terrain.derniers", nombre=len(parts)) + " " + " ".join(parts)


def words_text():
    """Commande "terrain mots" : ce que la veille surveille en ce moment."""
    ctx = context(load_sources())
    return dire("terrain.mots", ville=lieu().get("commune", "?"),
                tags=", ".join(ctx["tags_ville"] + ctx["tags_moment"]) or "-",
                mouvement=", ".join(ctx["mouvement"]) or dire("terrain.aucun"),
                sites=", ".join(s["nom"] for s in ctx["sites"]) or dire("terrain.aucun"))


if __name__ == "__main__":
    main()
