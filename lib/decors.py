"""Decors de la gare (2026-11-13, demande utilisateur) : des evenements que
l'utilisateur compose lui-meme pour la veille SNCF, avec la commande "decors".

Un decor = un nom (facultatif ; il nomme aussi le dossier de ses sons), des
conditions, toutes vraies en meme temps, et une action.

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
            "retro" ; "son" : un des sons coches, retire au hasard a chaque
            fois, joue dans le hall (sons = chemins relatifs a train_scaner).
            Jamais les sons de la gare (ter_passby...) : on les confondrait
            avec les vrais trains.

Regles dans nearfield-anchor-settings/decors.json (reglages "decors"). Les sons
telecharges pour les decors vont dans train_scaner/decors/depot (meme dialogue
que les sonneries) ; a l'enregistrement, ceux du depot coches pour un decor
quittent le depot pour train_scaner/<nom du decor> (ranger) ; renomme, le
dossier suit ; decoches, ils retournent au depot. La veille gare (watchers/sncf_watch.py) relit les decors a
chaque tour (VERIFIER_SEC)."""
import calendar
import json
import os
import random
import shutil
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
    return {"id": "", "nom": "", "ponctuel": False, "jour": 0, "mois": 0, "annee": 0, "heure": "ouverture",
            "minute": None, "meteo": None, "action": "annonce", "texte": "", "jingle": "moderne", "sons": []}


def _normaliser(d):
    d = dict(nouveau(), **d)
    ancien = d.pop("son", None)                     # avant 2026-11-13 : un seul son
    if not d.get("sons"):
        d["sons"] = [ancien] if ancien else []
    return d


def lire():
    liste = reglages.get("decors").get("decors") or []
    return [_normaliser(d) for d in liste if isinstance(d, dict)]


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
    if decor.get("action") == "son" and not any(chemin_son(c) and not de_la_gare(c) for c in decor.get("sons") or ()):
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


def de_la_gare(relatif):
    """Son de la veille gare (trains reels) : jamais propose pour un decor."""
    return os.path.dirname(relatif) in set(DOSSIERS_GARE) | {""}


def candidats(deja=()):
    """D'abord le depot des decors, puis les sons deja ranges pour des decors
    (deja : ceux du decor en cours d'abord). Jamais les sons de la gare.
    [(origine, chemin relatif)]."""
    try:
        os.makedirs(DEPOT, exist_ok=True)
    except OSError:
        pass
    out = [("depot", os.path.relpath(p, TRAIN_SCANER_DIR)) for p in _audios(DEPOT)]
    vus = {c for _, c in out}
    depot = os.path.relpath(DEPOT, TRAIN_SCANER_DIR)
    for c in list(deja) + [c for d in lire() for c in d.get("sons") or ()]:
        if c not in vus and not de_la_gare(c) and os.path.dirname(c) != depot and chemin_son(c):
            out.append(("decor", c))
            vus.add(c)
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


def nom_dossier(nom):
    """Nom de dossier lisible tire du nom du decor (sans / ni caracteres genants)."""
    propre = "".join(" " if ch in '/\\:*?"<>|' else ch for ch in nom)
    propre = " ".join(propre.split()).strip(" .")[:90].rstrip(" .,")
    if not propre or propre.lower() in {x.lower() for x in DOSSIERS_GARE + ("ambi", "decors")}:
        propre = f"decor {propre}".strip()
    return propre


def _deplacer(chemin, dossier):
    """Deplace le fichier dans le dossier (sans ecraser) ; rend le nouveau chemin."""
    base, ext = os.path.splitext(os.path.basename(chemin))
    cible, n = os.path.join(dossier, base + ext), 2
    while os.path.exists(cible):
        cible, n = os.path.join(dossier, f"{base} {n}{ext}"), n + 1
    os.makedirs(dossier, exist_ok=True)
    shutil.move(chemin, cible)
    return cible


def ranger(decor, nom, avant=()):
    """Les sons coches du depot, ou du dossier d'un ancien nom du decor, vont
    dans train_scaner/<nom> ; ceux qu'il avait (avant) et qui ne sont plus
    coches retournent au depot. Un son partage avec un autre decor ne bouge
    pas. Met decor["sons"] a jour ; rend le nombre de sons deplaces."""
    dossier = os.path.join(TRAIN_SCANER_DIR, nom_dossier(nom))
    depot = os.path.realpath(DEPOT)
    racine = os.path.realpath(TRAIN_SCANER_DIR)
    autres = {c for d in lire() if d["id"] != decor.get("id") for c in d.get("sons") or ()}
    vides, sons, deplaces = set(), [], 0

    def a_lui(relatif, chemin):
        """Dans le depot, ou dans un dossier de decor que personne d'autre n'utilise."""
        ici = os.path.dirname(chemin)
        if ici == depot:
            return True
        return (relatif in avant and relatif not in autres and not de_la_gare(relatif)
                and os.path.dirname(ici) == racine)

    for relatif in decor.get("sons") or []:
        chemin = chemin_son(relatif)
        if not chemin or de_la_gare(relatif) or os.path.dirname(chemin) == os.path.realpath(dossier) \
                or not a_lui(relatif, chemin):
            sons.append(relatif)
            continue
        try:
            cible = _deplacer(chemin, dossier)
        except OSError as exc:
            print(f"[decors] ranger {relatif} : {exc}", flush=True)
            sons.append(relatif)
            continue
        if os.path.dirname(chemin) != depot:
            vides.add(os.path.dirname(chemin))
        sons.append(os.path.relpath(cible, TRAIN_SCANER_DIR))
        deplaces += 1
    for relatif in avant:
        chemin = chemin_son(relatif)
        if relatif in decor.get("sons", ()) or not chemin or not a_lui(relatif, chemin) \
                or os.path.dirname(chemin) == depot:
            continue
        try:
            _deplacer(chemin, DEPOT)
            vides.add(os.path.dirname(chemin))
            deplaces += 1
        except OSError as exc:
            print(f"[decors] rendre {relatif} au depot : {exc}", flush=True)
    for ancien in vides:
        try:
            os.rmdir(ancien)                    # seulement s'il est vide
        except OSError:
            pass
    decor["sons"] = sons
    return deplaces


def son_au_hasard(decor):
    """Un des sons du decor, retire au hasard a chaque fois (chemin absolu), ou None."""
    presents = [c for c in (chemin_son(r) for r in decor.get("sons") or () if not de_la_gare(r)) if c]
    return random.choice(presents) if presents else None


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
