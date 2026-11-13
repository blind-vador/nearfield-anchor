"""Decors de la gare (2026-11-13, demande utilisateur) : des evenements que
l'utilisateur compose lui-meme pour la veille SNCF, avec la commande "decors".

Un decor = des conditions, toutes vraies en meme temps, et une action.

  ponctuel  vrai : il tombe de temps en temps, au hasard, comme un train qui
            passe (environ PONCTUEL_PAR_HEURE fois par heure ou ses conditions
            tiennent, jamais deux fois dans la meme heure) ;
            faux : il tombe une fois, des que ses conditions deviennent vraies
            (une fois par minute choisie, par heure choisie, sinon par jour)
  jour      0 = tous les jours, sinon 1 a 31
  mois      0 = tous les mois, sinon 1 a 12
  annee     0 = toutes les annees, sinon l'annee
  heure     "ouverture" (gare ouverte), "fermeture" (gare fermee),
            "toujours", ou une heure 0 a 23 (0 = minuit, dit "24 heures")
  minute    None = n'importe quand, sinon 0 a 59
  meteo     None = indifferent, ou pluie, orage, neige, vent (meteo reelle,
            lib/gare_ambiance.active_weather), ou "sec" (aucun des quatre)
  action    "annonce" : texte dit par la gare apres le jingle "moderne" ou
            "retro" ; "son" : un son joue dans le hall (son = chemin relatif
            a train_scaner : decors/depot/... ou un son de la gare)

Regles dans nearfield-anchor-settings/decors.json (reglages "decors"). Les sons
telecharges pour les decors vont dans train_scaner/decors/depot (meme dialogue
que les sonneries). La veille gare (watchers/sncf_watch.py) relit les decors a
chaque tour (VERIFIER_SEC)."""
import calendar
import json
import os
import random
import time
import uuid

import chemins
import reglages
from sound_paths import TRAIN_SCANER_DIR

DEPOT = os.path.join(TRAIN_SCANER_DIR, "decors", "depot")
EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus")
# Sons deja en place dans la gare, proposes apres le depot (comme les sonneries par defaut).
DOSSIERS_GARE = ("ter_passby", "tgv_passby", "ter_arriving", "tgv_arriving", "ter_leaving",
                 "tgv_leaving", "ter_sag", "tgv_sag")
SONS_GARE = ("horn1.mp3",)
JINGLES = {"moderne": "sncf_moderne.wav", "retro": "sncf_retro.mp3"}
HEURES = ["ouverture", "fermeture", "toujours"] + list(range(1, 24)) + [0]
METEOS = [None, "pluie", "orage", "neige", "vent", "sec"]
PONCTUEL_PAR_HEURE = 0.3          # comme le decor libre de la gare (sncf_watch)
VERIFIER_SEC = 30
FAITS = chemins.base("decors_joues.json")


def nouveau():
    return {"id": "", "ponctuel": False, "jour": 0, "mois": 0, "annee": 0, "heure": "ouverture",
            "minute": None, "meteo": None, "action": "annonce", "texte": "", "jingle": "moderne", "son": ""}


def lire():
    liste = reglages.get("decors").get("decors") or []
    return [dict(nouveau(), **d) for d in liste if isinstance(d, dict)]


def ecrire(liste):
    return reglages.save("decors", {"decors": liste})


def enregistrer(decor):
    """Ajoute le decor, ou remplace celui qui a le meme id. Rend False si le NAS refuse."""
    decor = dict(decor)
    if not decor.get("id"):
        decor["id"] = uuid.uuid4().hex[:10]
        decor["cree"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    liste = lire()
    for n, d in enumerate(liste):
        if d["id"] == decor["id"]:
            liste[n] = decor
            break
    else:
        liste.append(decor)
    return ecrire(liste)


def supprimer(decor_id):
    return ecrire([d for d in lire() if d["id"] != decor_id])


def erreur(decor, maintenant=None):
    """Cle de phrase si les conditions ne peuvent jamais etre vraies, sinon None."""
    t = maintenant or time.localtime()
    jour, mois, annee = decor.get("jour") or 0, decor.get("mois") or 0, decor.get("annee") or 0
    if annee and annee < t.tm_year:
        return "decors.erreur_annee_passee"
    if jour and mois:
        fin = calendar.monthrange(annee or 2028, mois)[1]           # 2028 bissextile : le 29 fevrier existe
        if jour > fin:
            return "decors.erreur_jour_mois"
    if annee and (jour or mois):
        dates = [(m, j) for m in ([mois] if mois else range(1, 13))
                 for j in ([jour] if jour else range(1, calendar.monthrange(annee, m)[1] + 1))
                 if j <= calendar.monthrange(annee, m)[1]]
        if not any((annee, m, j) >= (t.tm_year, t.tm_mon, t.tm_mday) for m, j in dates):
            return "decors.erreur_date_passee"
    if decor.get("action") == "annonce" and not (decor.get("texte") or "").strip():
        return "decors.erreur_texte"
    if decor.get("action") == "son" and not chemin_son(decor.get("son")):
        return "decors.erreur_son"
    return None


# --- sons ---------------------------------------------------------------

def _audios(dossier):
    try:
        noms = os.listdir(dossier)
    except OSError:
        return []
    return sorted((os.path.join(dossier, n) for n in noms
                   if os.path.splitext(n)[1].lower() in EXTENSIONS and os.path.isfile(os.path.join(dossier, n))),
                  key=lambda p: os.path.basename(p).lower())


def candidats():
    """D'abord le depot des decors, puis les sons de la gare. [(origine, chemin relatif)]."""
    os.makedirs(DEPOT, exist_ok=True)
    out = [("depot", os.path.relpath(p, TRAIN_SCANER_DIR)) for p in _audios(DEPOT)]
    for dossier in DOSSIERS_GARE:
        out += [("gare", os.path.relpath(p, TRAIN_SCANER_DIR)) for p in _audios(os.path.join(TRAIN_SCANER_DIR, dossier))]
    out += [("gare", nom) for nom in SONS_GARE if os.path.isfile(os.path.join(TRAIN_SCANER_DIR, nom))]
    return out


def chemin_son(relatif):
    """Chemin absolu d'un son de decor, s'il existe et reste dans train_scaner."""
    if not relatif:
        return None
    chemin = os.path.realpath(os.path.join(TRAIN_SCANER_DIR, relatif))
    racine = os.path.realpath(TRAIN_SCANER_DIR)
    if os.path.commonpath([chemin, racine]) != racine or not os.path.isfile(chemin):
        return None
    return chemin


def chemin_jingle(nom):
    return os.path.join(TRAIN_SCANER_DIR, JINGLES.get(nom) or JINGLES["moderne"])


# --- quand un decor tombe -----------------------------------------------

def conditions(decor, t, meteo=None, ouverte=None):
    """Vrai si toutes les conditions tiennent a l'instant t (struct_time).
    meteo : ensemble des temps reels ; ouverte : gare ouverte (bool)."""
    if decor.get("jour") and decor["jour"] != t.tm_mday:
        return False
    if decor.get("mois") and decor["mois"] != t.tm_mon:
        return False
    if decor.get("annee") and decor["annee"] != t.tm_year:
        return False
    heure = decor.get("heure", "toujours")
    if heure == "ouverture" and not ouverte:
        return False
    if heure == "fermeture" and ouverte:
        return False
    if isinstance(heure, int) and heure != t.tm_hour:
        return False
    if decor.get("minute") is not None and decor["minute"] != t.tm_min:
        return False
    voulu = decor.get("meteo")
    if voulu == "sec" and meteo:
        return False
    if voulu and voulu != "sec" and voulu not in (meteo or ()):
        return False
    return True


def occasion(decor, t):
    """La fenetre ou le decor ne tombe qu'une fois : la minute, l'heure, ou le jour."""
    if decor.get("minute") is not None:
        return time.strftime("%Y%m%d%H%M", t)
    if decor.get("ponctuel") or isinstance(decor.get("heure"), int):
        return time.strftime("%Y%m%d%H", t)
    return time.strftime("%Y%m%d", t)


def a_jouer(decors, t, meteo, ouverte, faits, pas_sec=VERIFIER_SEC, hasard=random.random):
    """Decors qui tombent maintenant ; faits {id: occasion} est mis a jour."""
    out = []
    for d in decors:
        if not d.get("id") or not conditions(d, t, meteo, ouverte):
            continue
        cle = occasion(d, t)
        if faits.get(d["id"]) == cle:
            continue
        if d.get("ponctuel"):
            fenetre = 60.0 if d.get("minute") is not None else 3600.0
            if hasard() >= 1 - (1 - PONCTUEL_PAR_HEURE) ** (pas_sec / fenetre):
                continue
        faits[d["id"]] = cle
        out.append(d)
    return out


def lire_faits():
    try:
        with open(FAITS, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def ecrire_faits(faits, decors):
    ids = {d["id"] for d in decors}
    try:
        os.makedirs(os.path.dirname(FAITS), exist_ok=True)
        tmp = FAITS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in faits.items() if k in ids}, f)
        os.replace(tmp, FAITS)
    except OSError as exc:
        print(f"[decors] {exc}", flush=True)


def besoin_meteo(decors):
    return any(d.get("meteo") for d in decors)
