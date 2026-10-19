"""Systeme d'attention du scanner radio : "plus ca pique, plus on y revient".

Reconstruit le 2026-10-01 (un premier systeme de ce genre -- channel_stats.json,
bonus aviation quand un avion est proche -- avait ete developpe du 1er au 4
septembre sans jamais entrer dans le depot git, et a disparu avec la carte SD).

Chaque cible (fenetre RTLSDR-Airband, bande balayee, ACARS) a un SCORE :
- une vraie capture le fait monter (CAPTURE_POINTS), puis il decroit de moitie
  toutes les HALF_LIFE_SEC ("la piqure s'estompe") ;
- la console le module : parasite / "meritait : non" -> baisse, "oui" -> hausse ;
- le contexte aerien (adsb_watch -> /tmp/contexte_aerien.json) ajoute une
  piqure passagere, ponderee par la RECEVABILITE apprise de la cible (une
  fenetre souvent piquee par les avions mais qui ne capte jamais rien -- tour
  ou sol de Lyon, a 63 km derriere les monts du Lyonnais -- perd son bonus).

Le scanner en tire son programme de cycle (plan_cycle) : temps d'ecoute plus
long et visites supplementaires la ou ca pique, bandes balayees choisies par
anciennete + score, avec un PLANCHER D'EXPLORATION (toute bande balayee revue
au moins toutes les SWEEP_FLOOR_SEC) et un surplus de temps par cycle borne
(MAX_EXTRA_SEC_PER_CYCLE) pour ne pas affamer le reste.
"""
import fcntl
import json
import os
import time

from phrases import Etiquettes, dire

NAS_ROOT = "/media/nas/nd1"
STATE_NAS = f"{NAS_ROOT}/server/radio_log/attention.json"   # survit aux redemarrages
STATE_LOCAL = "/home/vador/autopilot/pi_console/attention.json"
LOCK_PATH = "/tmp/attention.lock"
AIR_CONTEXT_PATH = "/tmp/contexte_aerien.json"
AIR_CONTEXT_MAX_AGE_SEC = 300
METEO_CONTEXT_PATH = "/tmp/contexte_meteo.json"
METEO_CONTEXT_MAX_AGE_SEC = 900

HALF_LIFE_SEC = 1800            # la piqure perd la moitie de sa force en 30 min
CAPTURE_POINTS = 1.0
NOTE_POINTS = {"oui": 0.5, "bof": 0.0, "non": -0.5}
PARASITE_POINTS = -0.5
MAX_SCORE = 5.0

DWELL_GAIN = 0.5                # ecoute = base x (1 + gain x piqure)
MAX_DWELL_SEC = 60
EXTRA_VISIT_SCORE = 1.5         # a partir de la, une deuxieme visite dans le cycle
MAX_EXTRA_SEC_PER_CYCLE = 150   # surplus total par cycle (au-dela, les bonus sont reduits)
SWEEP_FLOOR_SEC = 1800          # toute bande balayee revue au moins toutes les 30 min
SECOND_SWEEP_SCORE = 1.0        # une bande balayee qui pique fort a droit a un 2e balayage
ACARS_BASE_DWELL = 30
# Fenetres de donnees (une ecoute par cycle chacune, meme dongle, en
# sequence) : cible -> duree d'ecoute de base. Voir radio_watch.DATA_SCANNERS.
DATA_WINDOWS = {"acars": ACARS_BASE_DWELL, "vdl2": 25, "aprs": 30, "capteurs": 30,
                "pagers": 30, "adsb": 30, "balises": 55, "sondes": 45, "lora": 30, "dab": 45,
                "dmr": 20}  # veille des frequences DMR captees (radio_watch._dmr_window, 2026-10-07)
DATA_LABELS = Etiquettes("attention.donnees", {"acars": "ACARS", "vdl2": "VDL2", "aprs": "APRS", "capteurs": "capteurs sans fil",
               "pagers": "pagers 466", "adsb": "avions en direct", "balises": "balises de détresse",
               "sondes": "ballons-sondes", "lora": "LoRa et Meshtastic", "dab": "radio numérique DAB+",
               "dmr": "veille DMR"},
                         "nom d'une fenêtre d'écoute de données")
# Fenetres espacees : pas plus d'une visite par intervalle (sauf si ca pique).
DATA_MIN_INTERVAL = {"balises": 900, "adsb": 240, "atis": 1200}  # ATIS change ~toutes les 30 min


# --- etat partage (scanner + console), ecritures sous verrou ---

def _path():
    return STATE_NAS if os.path.ismount(NAS_ROOT) else STATE_LOCAL


def _load():
    try:
        with open(_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _update(fn):
    """Lecture-modification-ecriture atomique, sous verrou (console et scanner
    ecrivent chacun de leur cote)."""
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _load()
        fn(state)
        path = _path()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
        return state


def _decayed(entry, now):
    if not entry:
        return 0.0
    age = max(0.0, now - entry.get("t", now))
    return entry.get("score", 0.0) * 0.5 ** (age / HALF_LIFE_SEC)


def stimulate(target, points, now=None):
    """Piqure (points > 0) ou apaisement (points < 0) d'une cible."""
    if not target or not points:
        return
    now = now or time.time()

    def apply(state):
        entry = state.setdefault(target, {})
        entry["score"] = max(0.0, min(MAX_SCORE, _decayed(entry, now) + points))
        entry["t"] = now
    _update(apply)


def visited(target, hit, air_boost, now=None):
    """Apres une visite : memorise l'heure, et apprend la recevabilite des
    piqures aeriennes (cible piquee par les avions -> a-t-elle capte ?)."""
    now = now or time.time()

    def apply(state):
        entry = state.setdefault(target, {})
        entry["last_visit"] = now
        if air_boost > 0:
            entry["air_visits"] = entry.get("air_visits", 0) + 1
            entry["air_hits"] = entry.get("air_hits", 0) + (1 if hit else 0)
        if hit:
            entry["score"] = max(0.0, min(MAX_SCORE, _decayed(entry, now) + CAPTURE_POINTS))
            entry["t"] = now
            entry["captures"] = entry.get("captures", 0) + 1
    _update(apply)


def _receivability(entry):
    """1 au depart ; tend vers 0,2 si les avions piquent sans jamais rien donner."""
    visits, hits = entry.get("air_visits", 0), entry.get("air_hits", 0)
    return min(1.0, (hits + 1) / (visits * 0.2 + 1))


def air_context(now=None):
    now = now or time.time()
    try:
        with open(AIR_CONTEXT_PATH) as f:
            ctx = json.load(f)
    except (OSError, ValueError):
        return {}
    if now - ctx.get("updated", 0) > AIR_CONTEXT_MAX_AGE_SEC:
        ctx = {}
    # Contexte meteo (lib/meteo_locale.py) : a l'approche d'une perturbation,
    # les fenetres meteo (stations APRS, capteurs) piquent davantage.
    try:
        with open(METEO_CONTEXT_PATH) as f:
            meteo = json.load(f)
    except (OSError, ValueError):
        meteo = None
    if meteo and now - meteo.get("updated", 0) <= METEO_CONTEXT_MAX_AGE_SEC and meteo.get("niveau"):
        targets = dict(ctx.get("cibles") or {})
        for target, boost in (meteo.get("cibles") or {}).items():
            targets[target] = targets.get(target, 0.0) + boost
        ctx = dict(ctx, cibles=targets, meteo=meteo["niveau"])
    return ctx


def pressure(target, state, ctx):
    """(piqure totale, part activite, part avions) d'une cible, ponderees par
    l'interet de l'utilisateur pour sa categorie (lib/interets.py)."""
    entry = state.get(target, {})
    try:
        import interets
        interest = interets.weight(target)
    except Exception:
        interest = 1.0
    activity = _decayed(entry, time.time()) * interest
    air = ctx.get("cibles", {}).get(target, 0.0) * _receivability(entry) * interest
    return activity + air, activity, air


# --- programme d'un cycle ---

def _disabled(target):
    """Categorie mise sur "désactivé" (commande intérêts), ou cible hors du
    mode restreint (lib/restreint.py) : bande entierement sautee, plancher
    d'exploration compris (2026-10-09)."""
    try:
        import interets
        import stations
        # ou fenetre aero desactivee comme station reperee (touche O du mode manuel)
        import restreint
        return interets.disabled(target) or stations.window_disabled(target) or restreint.exclu(target)
    except Exception:
        return False


def plan_cycle(windows, sweeps):
    """Liste d'etapes (genre, objet, cible, duree d'ecoute, piqure avions)."""
    state, ctx, now = _load(), air_context(), time.time()
    windows = [w for w in windows if not _disabled(w["name"])]
    sweeps = [b for b in sweeps if not _disabled(b["name"])]
    steps, extra = [], []
    for w in windows:
        total, _, air = pressure(w["name"], state, ctx)
        base = w.get("dwell_sec", 15)
        dwell = min(MAX_DWELL_SEC, base * (1 + DWELL_GAIN * total))
        steps.append(["airband", w, w["name"], dwell, air])
        extra.append(dwell - base)
    for name, base in DATA_WINDOWS.items():
        if _disabled(name):
            continue
        total, _, air = pressure(name, state, ctx)
        since = now - state.get(name, {}).get("last_visit", 0)
        if since < DATA_MIN_INTERVAL.get(name, 0) and total < EXTRA_VISIT_SCORE:
            continue
        dwell = min(MAX_DWELL_SEC, base * (1 + DWELL_GAIN * total))
        steps.append(["data", name, name, dwell, air])
        extra.append(dwell - base)

    # Surplus borne : si les bonus depassent le budget, on les reduit tous a proportion.
    surplus = sum(extra)
    if surplus > MAX_EXTRA_SEC_PER_CYCLE:
        ratio = MAX_EXTRA_SEC_PER_CYCLE / surplus
        for step, bonus in zip(steps, extra):
            step[3] = step[3] - bonus + bonus * ratio

    # Les plus piquees d'abord (retour rapide apres un evenement), puis
    # une deuxieme visite pour celles qui piquent fort.
    steps.sort(key=lambda s: -pressure(s[2], state, ctx)[0])
    for s in list(steps):
        if s[0] == "airband" and pressure(s[2], state, ctx)[0] >= EXTRA_VISIT_SCORE:
            steps.append(["airband", s[1], s[2], s[1].get("dwell_sec", 15), s[4]])

    # Bandes balayees. 1) Plancher d'exploration STRICT : une bande pas revue
    # depuis SWEEP_FLOOR_SEC passe avant tout (la plus oubliee d'abord). 2) Sinon,
    # anciennete + piqure. 3) En plus, la bande la plus piquee (si elle pique
    # assez) a droit a son propre balayage dans le cycle.
    if sweeps:
        last = lambda b: state.get(b["name"], {}).get("last_visit", 0)
        stale = [b for b in sweeps if now - last(b) >= SWEEP_FLOOR_SEC]
        if stale:
            first = min(stale, key=last)
        else:
            first = max(sweeps, key=lambda b: (now - last(b)) / SWEEP_FLOOR_SEC + pressure(b["name"], state, ctx)[0])
        chosen = [first]
        hottest = max(sweeps, key=lambda b: pressure(b["name"], state, ctx)[0])
        if hottest is not first and pressure(hottest["name"], state, ctx)[0] >= SECOND_SWEEP_SCORE:
            chosen.append(hottest)
        for b in chosen:
            steps.append(["sweep", b, b["name"], None, pressure(b["name"], state, ctx)[2]])
    return [tuple(s) for s in steps]


# --- pour la console ("attention") ---

def describe(labels):
    """Phrase parlee : ou le scanner concentre son ecoute, et pourquoi.
    labels : {cible: nom parle}."""
    state, ctx = _load(), air_context()
    rows = []
    for target, label in labels.items():
        total, activity, air = pressure(target, state, ctx)
        if total >= 0.2:
            why = []
            if activity >= 0.1:
                why.append(dire("attention.activite_recente", activity=format(activity, ".1f")).replace(".", ","))
            if air >= 0.1:
                why.append(f"avions {air:.1f}".replace(".", ","))
            rows.append((total, dire("attention.piqure", label=label, total=format(total, ".1f")).replace(".", ",") + f" ({', '.join(why)})"))
    rows.sort(reverse=True)
    planes = ctx.get("avions")
    head = ((dire("attention.avion_zone", planes=planes, pluriel='s' if planes and planes > 1 else '') + " ") if planes is not None
            else (dire("attention.donnees_avions_recentes") + " "))
    if ctx.get("meteo"):
        head += (dire("attention.vigilance_meteo_renforcee_niveau", meteo=ctx['meteo']) + " ")
    if not rows:
        return head + dire("attention.aucune_piqure_moment_scanner")
    return head + " ; ".join(text for _, text in rows[:6]) + dire("attention.reste_explore_egalite")
