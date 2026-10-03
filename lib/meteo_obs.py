"""Observations meteo de l'utilisateur (commande "observation" de la
console, ou phrases directes : "il pleut fort", "tonnerre au loin"...).

Inspire de la meteo participative (mPING de la NOAA : types de
precipitation signales par des citoyens pour verifier radars et modeles ;
CoCoRaHS ; codes "temps present" de l'OMM, qui prevoient le cas "tonnerre
entendu" et distinguent pluie faible, moderee, forte). Tout se percoit sans
la vue : bruit de la pluie, tonnerre, vent sur le visage (echelle de
Beaufort decrite au toucher et a l'oreille), odeur.

Chaque observation garde un instantane de ce que disaient le modele et les
voisins a ce moment-la. Usage :
  1. tout de suite : tonnerre ou forte pluie font monter le niveau de
     menace (meteo_locale.threat) ; on entend en general le tonnerre
     jusqu'a une quinzaine de kilometres ;
  2. verification du modele : probabilite de pluie annoncee vs pluie
     observee, par tranche (diagramme de fiabilite, calcul de type Brier) ;
  3. seuil d'orage local : instabilite (CAPE) quand le tonnerre est entendu ;
  4. fiabilite de chaque type de signe avant-coureur : suivi ou non d'un
     orage ou d'une forte pluie observes dans les 2 heures.
Des comptes simples, explicables -- comme les notes radio.
"""
import json
import os
import time
from datetime import datetime

import meteo_locale
import reglages
from phrases import Champs, Etiquettes, dire

OBS_PATH = "/media/nas/nd1/server/meteo_observations.json"
OBS_LOCAL = "/home/vador/autopilot/meteo_observations.json"
SIGNS_LOG = "/media/nas/nd1/server/meteo_signes.jsonl"
RECENT_PATH = "/tmp/observation_meteo.json"
RECENT_MAX_AGE_SEC = 45 * 60
SIGN_FOLLOW_SEC = 2 * 3600
MIN_SAMPLES = 5

# (code, question, [(code, valeur dite)], defaut)
FIELDS = Champs("observation", [
    ("precip", "Précipitations", [("aucune", "aucune"), ("bruine", "bruine"), ("faible", "pluie faible"),
                                  ("moderee", "pluie modérée"), ("forte", "forte pluie"),
                                  ("violente", "averse violente"), ("grele", "grêle"), ("neige", "neige")], 0),
    ("orage", "Orage", [("aucun", "aucun"), ("loin", "tonnerre au loin, grondement sourd"),
                        ("proche", "tonnerre proche, claquement net"),
                        ("tout_pres", "tonnerre tout près, claquement sec et immédiat")], 0),
    ("vent", "Vent ressenti", [("calme", "calme"), ("leger", "léger, les feuilles bruissent"),
                               ("modere", "modéré, on le sent bien"),
                               ("fort", "fort, il siffle, on a du mal à tenir un parapluie"),
                               ("tres_fort", "très fort, on a du mal à marcher")], 0),
    ("evolution", "Évolution", [("continue", "ça continue"), ("commence", "ça commence"),
                                ("intensifie", "ça s'intensifie"), ("calme", "ça se calme"),
                                ("arret", "ça vient de s'arrêter")], 0),
    ("ressenti", "Ressenti", [("doux", "doux"), ("froid", "froid"), ("frais", "frais"), ("chaud", "chaud"),
                              ("lourd", "lourd et étouffant")], 0),
    ("odeur", "Odeur", [("aucune", "aucune"), ("pluie", "odeur de pluie"), ("ozone", "odeur d'ozone")], 0),
    ("direction", "Direction", [("inconnue", "inconnue")] + [(d, d) for d in meteo_locale.DIRECTIONS], 0),
    ("certitude", "Certitude", [("sur", "sûr"), ("hesitant", "hésitant")], 0),
], "grille d'observation météo")
RAINY = {"bruine", "faible", "moderee", "forte", "violente", "grele", "neige"}
HEAVY = {"forte", "violente", "grele"}
PROB_BUCKETS = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]


# --- phrases directes --------------------------------------------------------

QUICK = [  # (mots normalises, champ, valeur) : le premier qui correspond gagne, par champ
    (("tout pres", "tres pres", "au dessus", "claque"), "orage", "tout_pres"),
    (("tonnerre proche", "orage proche", "gronde fort", "tonnerre fort"), "orage", "proche"),
    (("tonnerre", "gronde", "orage au loin", "orage loin"), "orage", "loin"),
    (("grele",), "precip", "grele"),
    (("neige",), "precip", "neige"),
    (("deluge", "trombes", "averse violente", "pleut tres fort"), "precip", "violente"),
    (("pleut fort", "forte pluie", "pleut beaucoup", "averse"), "precip", "forte"),
    (("bruine", "pleuviote", "crachin"), "precip", "bruine"),
    (("pleut un peu", "pluie faible", "quelques gouttes"), "precip", "faible"),
    (("il pleut", "pluie"), "precip", "moderee"),
    (("il fait beau", "rien du tout", "temps calme", "plus rien"), "precip", "aucune"),
    (("tempete", "vent tres fort"), "vent", "tres_fort"),
    (("vent fort", "souffle fort"), "vent", "fort"),
    (("commence",), "evolution", "commence"),
    (("s intensifie", "de plus en plus"), "evolution", "intensifie"),
    (("se calme", "diminue"), "evolution", "calme"),
    (("s arrete", "vient de s arreter", "arrete de pleuvoir"), "evolution", "arret"),
    (("lourd", "etouffant"), "ressenti", "lourd"),
    (("ozone",), "odeur", "ozone"),
]


def parse_quick(normalized):
    """Phrase normalisee -> reponses partielles ({} si rien de reconnu)."""
    found = {}
    text = f" {normalized} "
    for words, field, value in QUICK:
        if field in found:
            continue
        if any(f" {w} " in text for w in words):
            found[field] = value
    if found.get("evolution") == "arret" and "precip" not in found:
        found["precip"] = "aucune"
    return found


def spoken(answers):
    parts = []
    for code, _, options, _ in FIELDS:
        value = answers.get(code)
        if value is None or value in ("aucune", "aucun", "calme", "continue", "doux", "inconnue", "sur"):
            continue
        parts.append(dict(options).get(value, value))
    return ", ".join(parts) or dire("observation.rien_particulier")


# --- enregistrement ------------------------------------------------------------

def _path():
    return OBS_PATH if os.path.isdir(os.path.dirname(OBS_PATH)) else OBS_LOCAL


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _snapshot(fc, lieu):
    """Ce que disaient le modele et les voisins au moment de l'observation."""
    snap = {}
    if fc:
        hourly = fc.get("hourly") or {}
        i = meteo_locale._hourly_index(fc)
        for field in ("precipitation_probability", "precipitation", "cape", "weather_code", "wind_gusts_10m"):
            values = hourly.get(field) or []
            snap[field] = values[i] if i < len(values) else None
        out = meteo_locale.storm_outlook(fc, hours=3)
        snap["cape_max_3h"] = out["cape"][0]
        _, snap["pression_3h"] = meteo_locale.pressure_trend(fc)
    signs = meteo_locale.precursors(lieu)
    snap["signes"] = [s.get("type") for s in signs]
    return snap


def record(answers, quick=False):
    """Enregistre l'observation et met a jour aussitot le niveau de menace.
    Rend (nombre d'observations, niveau, raisons)."""
    lieu = reglages.get("lieu")
    full = {code: answers.get(code, options[default][0]) for code, _, options, default in FIELDS}
    fc = meteo_locale.forecast(lieu)
    obs = {"t": time.time(), "le": datetime.now().isoformat(timespec="seconds"), "reponses": full,
           "rapide": quick, "modele": _snapshot(fc, lieu)}
    notes = load() + [obs]
    path = _path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[meteo_obs] observation non enregistree : {exc}")
    meteo_locale._write_json(RECENT_PATH, {"t": obs["t"], "reponses": full})
    level, reasons = meteo_locale.threat(lieu, fc)
    meteo_locale.publish_context(level, reasons)
    return len(notes), level, reasons


def recent():
    """Observation de moins de RECENT_MAX_AGE_SEC (pour meteo_locale.threat)."""
    data = meteo_locale._read_json(RECENT_PATH)
    if not data or time.time() - data.get("t", 0) > RECENT_MAX_AGE_SEC:
        return None
    return data["reponses"]


def log_signs(signs):
    """Journal des signes avant-coureurs annonces (meteo_watch)."""
    try:
        with open(SIGNS_LOG, "a", encoding="utf-8") as f:
            for s in signs:
                f.write(json.dumps({"t": s["t"], "type": s.get("type"), "texte": s["texte"]},
                                   ensure_ascii=False) + "\n")
    except OSError:
        pass


def _signs():
    try:
        with open(SIGNS_LOG, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


# --- apprentissage -----------------------------------------------------------------

def _weight(obs):
    return 0.5 if obs["reponses"].get("certitude") == "hesitant" else 1.0


def calibration():
    """{(bas, haut): (pluie observee ponderee, total pondere)} : probabilite
    de pluie annoncee par le modele vs pluie observee."""
    table = {b: [0.0, 0.0] for b in PROB_BUCKETS}
    for obs in load():
        prob = (obs.get("modele") or {}).get("precipitation_probability")
        if prob is None:
            continue
        bucket = next(b for b in PROB_BUCKETS if b[0] <= prob < b[1])
        w = _weight(obs)
        table[bucket][1] += w
        if obs["reponses"].get("precip") in RAINY:
            table[bucket][0] += w
    return table


def calibrated_note(prob):
    """Phrase pour le bulletin si les observations disent autre chose que
    le modele pour cette probabilite (au moins MIN_SAMPLES cas)."""
    if prob is None:
        return ""
    bucket = next(b for b in PROB_BUCKETS if b[0] <= prob < b[1])
    rained, total = calibration()[bucket]
    if total < MIN_SAMPLES:
        return ""
    observed = rained / total
    if abs(observed * 100 - prob) < 20:
        return ""
    return (dire("observation.modele_annonce_cent_risque", round=round(prob), round2=round(observed * 10)))


def thunder_threshold():
    """Instabilite (CAPE) typique quand le tonnerre a ete entendu ici, ou None."""
    values = sorted((obs.get("modele") or {}).get("cape_max_3h") for obs in load()
                    if obs["reponses"].get("orage") not in (None, "aucun")
                    and (obs.get("modele") or {}).get("cape_max_3h") is not None)
    if len(values) < 3:
        return None
    return values[len(values) // 4]  # premier quartile : "deja a partir de"


def sign_reliability():
    """{type de signe: (suivis d'orage ou forte pluie, total)}."""
    observations = load()
    severe = [o["t"] for o in observations
              if o["reponses"].get("orage") not in (None, "aucun") or o["reponses"].get("precip") in HEAVY]
    result = {}
    for sign in _signs():
        if time.time() - sign["t"] < SIGN_FOLLOW_SEC:
            continue  # trop recent pour juger
        hits, total = result.get(sign.get("type"), (0, 0))
        followed = any(0 <= t - sign["t"] <= SIGN_FOLLOW_SEC for t in severe)
        result[sign.get("type")] = (hits + (1 if followed else 0), total + 1)
    return result


SIGN_NAMES = Etiquettes("observation.signe", {"chute_temp": "chutes de température", "pression": "baisses de pression",
                                             "vent": "coups de vent", "pluie": "débuts de pluie"},
                        "type de signe avant-coureur, au pluriel")


def summary():
    """Commande "bilan meteo" : ce que le Pi a appris, et sur combien."""
    observations = load()
    if not observations:
        return (dire("observation.aucune_observation_instant_dis"))
    parts = [dire("observation.observation_enregistree", len=len(observations), pluriel='s' if len(observations) > 1 else '', pluriel2='s' if len(observations) > 1 else '')]
    table = calibration()
    lines = []
    for (low, high), (rained, total) in table.items():
        if total >= MIN_SAMPLES:
            lines.append(dire("observation.quand_modele_annonce_cent", low=low, min=min(high, 100), round=round(rained / total * 10)))
    parts.append((dire("observation.pluie") + " ") + ("; ".join(lines) + "." if lines
                               else dire("observation.encore_assez_observations_faut", MIN_SAMPLES=MIN_SAMPLES)))
    threshold = thunder_threshold()
    parts.append(dire("observation.orages_chez_toi_tonnerre", round=round(threshold)) if threshold is not None
                 else dire("observation.orages_encore_assez_tonnerres"))
    reliability = sign_reliability()
    known = [(t, h, n) for t, (h, n) in reliability.items() if n >= MIN_SAMPLES]
    if known:
        parts.append((dire("observation.signes_avant_coureurs") + " ") + "; ".join(
            dire("observation.suivies_orage_forte_pluie", get=SIGN_NAMES.get(t, t), h=h, n=n) for t, h, n in known) + ".")
    else:
        parts.append(dire("observation.signes_avant_coureurs_encore"))
    quick = sum(1 for o in observations if o.get("rapide"))
    parts.append(dire("observation.dont_phrase_directe_observations", quick=quick))
    return " ".join(parts)
