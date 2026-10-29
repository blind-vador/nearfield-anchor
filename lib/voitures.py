"""Voitures de la rue, par leurs capteurs de pression des pneus (TPMS, 433
MHz, recus par la veille capteurs) : apprentissage et statistiques
automatiques, tout reste sur le Pi et le NAS.

- Passage : trames de pneus recues a moins de PASSAGE_GAP_SEC les unes des
  autres. Les identifiants vus ensemble sont rattaches au meme vehicule
  (ses 4 pneus), appris au fil des passages.
- Vehicule habituel : vu au moins HABITUAL_DAYS jours differents ; appris
  automatiquement, annonce une fois. Nommable depuis la console.
- Statistiques ramenees au temps d'ecoute reel en 433 MHz (la cle n'y est
  qu'une partie du temps) : passages par heure d'ecoute, heures chargees.
- Annonces : vehicule habituel appris ; vehicule inconnu la nuit ;
  vehicule nomme qui passe ; pneu sous-gonfle d'un vehicule nomme ; voiture
  garee tout pres (une fois). Toujours en langage humain : marque probable
  (d'apres le capteur), nombre de pneus, pression en bars.
- Voiture garee (2026-10-02 : 35 "passages" en une nuit etaient UNE Toyota
  garee pres de chez l'utilisateur) : trames qui se suivent a moins de
  PARKED_GAP_SEC forment une serie ; une serie de plus de PARKED_MIN_SEC
  est une voiture garee. Une serie ne compte que pour un passage et n'est
  annoncee qu'au debut, puis une fois "garee".
- Deux marques differentes ne sont jamais le meme vehicule (un pneu Renault
  avait ete rattache a une Toyota passee a la meme seconde).
- Etat sur la carte SD du Pi (statistiques locales, demande utilisateur
  2026-10-02) ; l'ancien fichier du NAS est repris s'il est seul.
Les capteurs de pneus emettent surtout en roulant : une voiture garee est
en general invisible.
"""
import chemins
import fcntl
import json
import os
import time
from datetime import datetime
from phrases import Etiquettes, dire, nombre

NAS_PATH = "/media/nas/nd1/server/radio_log/voitures.json"
LOCAL_PATH = chemins.base("voitures.json")
LOCK_PATH = "/tmp/voitures.lock"
PASSAGE_GAP_SEC = 10
HABITUAL_DAYS = 3
KEEP_DAYS = 30
NIGHT_HOURS = {23, 0, 1, 2, 3, 4, 5}
LOW_PRESSURE_KPA = 160
PARKED_GAP_SEC = 90 * 60   # une voiture garee emet parfois a une heure d'intervalle
MAX_TYRES = 5              # 4 roues + la roue de secours : au-dela, c'est une autre voiture
PARKED_MIN_SEC = 3600
# Modeles rtl_433 qui donnent la marque de la voiture ; les autres sont des
# fabricants de capteurs montes sur beaucoup de marques, ou des capteurs
# ajoutes apres coup.
CAR_BRANDS = Etiquettes("voiture.marque", {"toyota": "Toyota ou Lexus", "lexus": "Lexus", "ford": "Ford", "renault": "Renault ou Dacia",
              "citroen": "Citroën ou Peugeot", "peugeot": "Peugeot", "hyundai": "Hyundai", "elantra": "Hyundai",
              "kia": "Kia", "abarth": "Fiat ou Abarth", "fiat": "Fiat", "porsche": "Porsche", "bmw": "BMW",
              "nissan": "Nissan", "mazda": "Mazda", "honda": "Honda", "volkswagen": "Volkswagen", "audi": "Audi",
              "mercedes": "Mercedes", "pmv": "Toyota ou Lexus"}, "marque probable d'une voiture")
SENSOR_MAKERS = {"schrader": "Schrader", "jansite": "Jansite", "steelmate": "Steelmate", "eeztire": "EezTire",
                 "tyreguard": "TyreGuard", "ave": "AVE", "truck": "poids lourd", "trw": "TRW"}


def _hour(h):
    return dire("capteurs.minuit") if h == 0 else dire("capteurs.midi") if h == 12 \
        else dire("capteurs.heures", heures=nombre(h, "heure"))


def _path():
    return LOCAL_PATH


def _empty():
    return {"ids": {}, "vehicules": {}, "passages": [], "ecoute": {}, "suivant": 1}


def load():
    for path in (LOCAL_PATH, NAS_PATH):  # le NAS seulement pour reprendre l'ancien etat
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return {**_empty(), **data}
        except (OSError, ValueError):
            continue
    return _empty()


def brand_text(model):
    """Modele rtl_433 du capteur -> marque en clair, prudente."""
    low = str(model).lower()
    for key, name in CAR_BRANDS.items():
        if key in low:
            return dire("voiture.probablement", name=name)
    for key, name in SENSOR_MAKERS.items():
        if key in low:
            if key == "truck":
                return dire("voiture.poids_lourd")
            return dire("voiture.capteur_monte_beaucoup_marques", name=name)
    return f"capteur {model}"


def bars(kpa):
    return dire("voiture.bars", pression=f"{kpa / 100:.1f}".replace(".", ","))


def describe(model, n_tyres, pressures):
    """'probablement une Toyota ou Lexus, 4 pneus, environ 2,2 bars'."""
    parts = [brand_text(model)]
    if n_tyres:
        parts.append(nombre(n_tyres, "pneu entendu", "pneus entendus"))
    if pressures:
        lo, hi = min(pressures), max(pressures)
        parts.append(dire("voiture.environ", pression=bars(lo)) if hi - lo < 15
                     else dire("voiture.de_a", de=bars(lo), a=bars(hi)))
    return ", ".join(parts)


def _save(data):
    path = _path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[voitures] etat non ecrit : {exc}")


class _Locked:
    def __enter__(self):
        self.f = open(LOCK_PATH, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.data = load()
        return self.data

    def __exit__(self, *exc):
        if exc[0] is None:
            _save(self.data)
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _prune(data, now):
    cutoff = now - KEEP_DAYS * 86400
    data["passages"] = [p for p in data["passages"] if p[0] >= cutoff]
    oldest = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d")
    data["ecoute"] = {d: h for d, h in data["ecoute"].items() if d >= oldest}


def record_listening(seconds, when=None):
    """Temps d'ecoute en 433 MHz (normalisation des statistiques)."""
    when = datetime.fromtimestamp(when or time.time())
    with _Locked() as data:
        day = data["ecoute"].setdefault(when.strftime("%Y-%m-%d"), {})
        day[str(when.hour)] = day.get(str(when.hour), 0) + seconds


def label(data, vid):
    v = data["vehicules"].get(vid, {})
    if v.get("nom"):
        return v["nom"]
    return dire("voiture.vehicule_numero", vid=vid) + (f", {brand_text(v['marque'])}" if v.get("marque") else "")


def _assign(data, clusters):
    """Rattache les trames de chaque groupe a des vehicules : [(trames, vehicule)].
    Deux vehicules deja connus ne sont jamais fusionnes (deux voitures garees
    cote a cote emettent en meme temps) ; un pneu inconnu rejoint le vehicule
    du groupe s'il a encore de la place (MAX_TYRES), sinon un nouveau."""
    out = []
    for cluster in clusters:
        model = cluster[0][1].split("|")[0]
        groups, unknown = {}, []
        for ev in cluster:
            vid = data["ids"].get(ev[1])
            if vid and data["vehicules"].get(vid, {}).get("marque", model) == model:
                groups.setdefault(vid, []).append(ev)
            else:
                unknown.append(ev)
        if unknown:
            new_ids = {ev[1] for ev in unknown}
            target = max(groups, key=lambda g: len(groups[g])) if groups else None
            if target is not None:
                tyres = set(data["vehicules"].get(target, {}).get("pneus", [])) | {ev[1] for ev in groups[target]}
                if len(tyres | new_ids) > MAX_TYRES:
                    target = None
            if target is None:
                target = str(data["suivant"])
                data["suivant"] += 1
            groups.setdefault(target, []).extend(unknown)
        out += [(sorted(evs), vid) for vid, evs in groups.items()]
    return sorted(out, key=lambda pair: pair[0][0][0])


def learn(events):
    """events : trames TPMS [(instant, identifiant, pression kPa ou None)].
    Rend la liste des phrases a annoncer."""
    if not events:
        return []
    events = sorted(events)
    clusters = []
    for model in {ev[1].split("|")[0] for ev in events}:  # une marque = un vehicule a part
        same = [ev for ev in events if ev[1].split("|")[0] == model]
        current = [same[0]]
        for ev in same[1:]:
            if ev[0] - current[-1][0] <= PASSAGE_GAP_SEC:
                current.append(ev)
            else:
                clusters.append(current)
                current = [ev]
        clusters.append(current)
    clusters.sort(key=lambda c: c[0][0])
    sentences = []
    with _Locked() as data:
        for cluster, vid in _assign(data, clusters):
            t = cluster[0][0]
            ids = {ev[1] for ev in cluster}
            model = cluster[0][1].split("|")[0]
            for i in ids:
                data["ids"][i] = vid
            v = data["vehicules"].setdefault(vid, {"premier": t, "passages": 0, "jours": [], "heures": {},
                                                   "nom": None, "habituel": False})
            v["marque"] = model
            v["pneus"] = sorted(set(v.get("pneus", [])) | ids)[-6:]
            pressures = [ev[2] for ev in cluster if ev[2]]
            desc = describe(model, len(v["pneus"]), pressures)
            day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
            hour = datetime.fromtimestamp(t).hour
            continuing = t - v.get("dernier", 0) <= PARKED_GAP_SEC
            v["dernier"] = cluster[-1][0]
            if day not in v["jours"]:
                v["jours"] = (v["jours"] + [day])[-KEEP_DAYS:]
            if continuing:
                # Meme serie : la voiture n'est pas repartie (garee tout pres).
                if (not v.get("garee_dite") and not v.get("habituel")
                        and v["dernier"] - v.get("serie_debut", t) >= PARKED_MIN_SEC):
                    v["garee_dite"] = True
                    since = datetime.fromtimestamp(v["serie_debut"])
                    sentences.append(dire("voiture.voiture_semble_garee_tout", desc=desc, v1=_hour(since.hour), minute=format(since.minute, "02d")))
                if v.get("nom") and pressures and min(pressures) < LOW_PRESSURE_KPA and not v.get("pression_dite"):
                    v["pression_dite"] = True
                    sentences.append(dire("voiture.pneu_semble_sous_gonfle", nom=v['nom'], bars=bars(min(pressures))))
                continue
            v["serie_debut"], v["garee_dite"], v["pression_dite"] = t, False, False
            v["passages"] += 1
            v["heures"][str(hour)] = v["heures"].get(str(hour), 0) + 1
            data["passages"].append([t, vid])
            if not v["habituel"] and len(v["jours"]) >= HABITUAL_DAYS:
                v["habituel"] = True
                usual = max(v["heures"], key=v["heures"].get)
                sentences.append(dire("voiture.vehicule_habituel_appris_vu", label=label(data, vid), desc=desc, len=len(v['jours']), v1=_hour(int(usual))))
            elif v.get("nom"):
                sentences.append(dire("voiture.passe", capitalize=v['nom'].capitalize()))
            elif not v["habituel"] and hour in NIGHT_HOURS:
                minute = datetime.fromtimestamp(t).minute
                sentences.append(dire("voiture.passage_voiture_inconnue", v1=_hour(hour)) + (f" {minute:02d}" if minute else "")
                                 + f" : {desc}.")
            if v.get("nom") and pressures and min(pressures) < LOW_PRESSURE_KPA:
                v["pression_dite"] = True
                sentences.append(dire("voiture.pneu_semble_sous_gonfle_2", nom=v['nom'], bars=bars(min(pressures))))
        _prune(data, time.time())
    return sentences


def rename(vid, name):
    with _Locked() as data:
        if vid not in data["vehicules"]:
            return False
        data["vehicules"][vid]["nom"] = name
        return True


def summary():
    """Phrase de statistiques pour la console."""
    data = load()
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    listen_today = sum(data["ecoute"].get(today, {}).values())
    today_passages = [p for p in data["passages"] if datetime.fromtimestamp(p[0]).strftime("%Y-%m-%d") == today]
    parts = []
    if listen_today >= 60:
        rate = len(today_passages) / (listen_today / 3600)
        parts.append(dire("voiture.aujourd_hui_passages_minutes", len=len(today_passages), round=round(listen_today / 60), round2=round(rate)))
    else:
        parts.append(dire("voiture.encore_assez_ecoute_aujourd"))
    listen_by_hour, passages_by_hour = {}, {}
    for hours in data["ecoute"].values():
        for h, sec in hours.items():
            listen_by_hour[int(h)] = listen_by_hour.get(int(h), 0) + sec
    for t, _ in data["passages"]:
        h = datetime.fromtimestamp(t).hour
        passages_by_hour[h] = passages_by_hour.get(h, 0) + 1
    rates = {h: passages_by_hour.get(h, 0) / (sec / 3600) for h, sec in listen_by_hour.items() if sec >= 600}
    if len(rates) >= 3:
        busy = sorted(rates, key=rates.get, reverse=True)[:3]
        parts.append((dire("voiture.heures_plus_chargees") + " ") + ", ".join(_hour(h) for h in busy) + ".")
    habitual = [(vid, v) for vid, v in data["vehicules"].items() if v.get("habituel")]
    if habitual:
        items = []
        for vid, v in sorted(habitual, key=lambda p: -p[1].get("dernier", 0))[:8]:
            ago = int((now - v.get("dernier", now)) // 3600)
            when = dire("voiture.heure") if ago < 1 else dire("voiture.heure_2", ago=ago, pluriel='s' if ago > 1 else '')
            items.append(f"{label(data, vid)}, vu {len(v['jours'])} jours, dernier passage {when}")
        parts.append((dire("voiture.vehicule_habituel", len=len(habitual), pluriel='s' if len(habitual) > 1 else '', pluriel2='s' if len(habitual) > 1 else '') + " ") + " ; ".join(items) + ".")
    else:
        parts.append(dire("voiture.aucun_vehicule_habituel_appris"))
    parts.append(dire("voiture.vehicules_differents_reperes_tout", len=len(data['vehicules'])))
    return " ".join(parts)
