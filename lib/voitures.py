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
  vehicule nomme qui passe ; pneu sous-gonfle d'un vehicule nomme.
Les capteurs de pneus emettent surtout en roulant : une voiture garee est
en general invisible.
"""
import fcntl
import json
import os
import time
from datetime import datetime

NAS_PATH = "/media/nas/nd1/server/radio_log/voitures.json"
LOCAL_PATH = "/home/vador/autopilot/voitures.json"
LOCK_PATH = "/tmp/voitures.lock"
PASSAGE_GAP_SEC = 10
HABITUAL_DAYS = 3
KEEP_DAYS = 30
NIGHT_HOURS = {23, 0, 1, 2, 3, 4, 5}
LOW_PRESSURE_KPA = 160


def _hour(h):
    return "minuit" if h == 0 else "midi" if h == 12 else f"{h} heures"


def _path():
    return NAS_PATH if os.path.isdir(os.path.dirname(NAS_PATH)) else LOCAL_PATH


def _empty():
    return {"ids": {}, "vehicules": {}, "passages": [], "ecoute": {}, "suivant": 1}


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return {**_empty(), **data}
    except (OSError, ValueError):
        return _empty()


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
    return v.get("nom") or f"véhicule numéro {vid}"


def learn(events):
    """events : trames TPMS [(instant, identifiant, pression kPa ou None)].
    Rend la liste des phrases a annoncer."""
    if not events:
        return []
    events = sorted(events)
    clusters, current = [], [events[0]]
    for ev in events[1:]:
        if ev[0] - current[-1][0] <= PASSAGE_GAP_SEC:
            current.append(ev)
        else:
            clusters.append(current)
            current = [ev]
    clusters.append(current)
    sentences = []
    with _Locked() as data:
        for cluster in clusters:
            t = cluster[0][0]
            ids = {ev[1] for ev in cluster}
            known = [data["ids"][i] for i in ids if i in data["ids"]]
            if known:
                vid = max(set(known), key=known.count)
            else:
                vid = str(data["suivant"])
                data["suivant"] += 1
            for i in ids:
                data["ids"][i] = vid
            v = data["vehicules"].setdefault(vid, {"premier": t, "passages": 0, "jours": [], "heures": {},
                                                   "nom": None, "habituel": False})
            day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
            hour = datetime.fromtimestamp(t).hour
            v["passages"] += 1
            v["dernier"] = t
            if day not in v["jours"]:
                v["jours"] = (v["jours"] + [day])[-KEEP_DAYS:]
            v["heures"][str(hour)] = v["heures"].get(str(hour), 0) + 1
            data["passages"].append([t, vid])
            if not v["habituel"] and len(v["jours"]) >= HABITUAL_DAYS:
                v["habituel"] = True
                usual = max(v["heures"], key=v["heures"].get)
                sentences.append(f"Véhicule habituel appris : {label(data, vid)}, vu {len(v['jours'])} jours "
                                 f"différents, surtout vers {_hour(int(usual))}.")
            elif v.get("nom"):
                sentences.append(f"{v['nom'].capitalize()} passe.")
            elif not v["habituel"] and hour in NIGHT_HOURS:
                minute = datetime.fromtimestamp(t).minute
                sentences.append(f"Passage d'un véhicule inconnu, à {_hour(hour)}" + (f" {minute}." if minute else "."))
            pressures = [ev[2] for ev in cluster if ev[2]]
            if v.get("nom") and pressures and min(pressures) < LOW_PRESSURE_KPA:
                sentences.append(f"Un pneu de {v['nom']} semble sous-gonflé : {round(min(pressures))} kilopascals.")
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
        parts.append(f"Aujourd'hui : {len(today_passages)} passages pour {round(listen_today / 60)} minutes "
                     f"d'écoute, soit environ {round(rate)} par heure.")
    else:
        parts.append("Pas encore assez d'écoute aujourd'hui pour une statistique.")
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
        parts.append("Heures les plus chargées : " + ", ".join(_hour(h) for h in busy) + ".")
    habitual = [(vid, v) for vid, v in data["vehicules"].items() if v.get("habituel")]
    if habitual:
        items = []
        for vid, v in sorted(habitual, key=lambda p: -p[1].get("dernier", 0))[:8]:
            ago = int((now - v.get("dernier", now)) // 3600)
            when = "dans l'heure" if ago < 1 else f"il y a {ago} heure{'s' if ago > 1 else ''}"
            items.append(f"{label(data, vid)}, vu {len(v['jours'])} jours, dernier passage {when}")
        parts.append(f"{len(habitual)} véhicule{'s' if len(habitual) > 1 else ''} "
                     f"habituel{'s' if len(habitual) > 1 else ''} : " + " ; ".join(items) + ".")
    else:
        parts.append("Aucun véhicule habituel appris pour l'instant, il faut quelques jours.")
    parts.append(f"{len(data['vehicules'])} véhicules différents repérés en tout.")
    return " ".join(parts)
