"""Capteurs sans fil deja entendus (2026-10-14, demande utilisateur : apres
chaque redemarrage du Pi, tous les capteurs etaient "redecouverts"). Avant,
la memoire des annonces etait dans /tmp, vide a chaque demarrage, et un
capteur redevenait "nouveau" tous les 7 jours. Ici, sur la carte SD, une
entree par capteur (cle "modele|id|canal", comme capteurs_watch._key) :
  nom, type, bande     dernier nom, type (capteurs_watch._kind), 433 ou 868
  premier, dernier     premiere et derniere trame entendues (horodatages)
  n, jours             trames retenues, jours distincts ou il a ete entendu
  mien                 appaire par l'utilisateur (lib/capteurs_miens.py) ; un
                       capteur a soi n'est jamais oublie ni annonce "nouveau"
Les pneus de voitures n'y sont pas (lib/voitures.py a son propre etat).
Ecrit des la premiere trame d'un nouveau capteur, sinon une fois par passe
(capteurs_watch.scan_window -> save). Amorce : seed(), depuis les releves
bruts du NAS et les statistiques locales (lib/stats_locales.py).
"""
import chemins
import datetime
import glob
import json
import os
import time

PATH = chemins.base("capteurs_connus.json")
ARCHIVE_GLOB = "/media/nas/nd1/server/radio_log/donnees/capteurs_2*.jsonl"
_state = {"data": None, "dirty": False}


def load():
    if _state["data"] is None:
        try:
            with open(PATH, encoding="utf-8") as f:
                _state["data"] = json.load(f)
        except (OSError, ValueError):
            _state["data"] = {}
    return _state["data"]


def save():
    if not _state["dirty"]:
        return
    try:
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(load(), f, ensure_ascii=False)
        os.replace(tmp, PATH)
        _state["dirty"] = False
    except OSError as exc:
        print(f"[capteurs_connus] non enregistre : {exc}")


def seen(key, name, kind, band, t=None, mine=False, n=1, write=True):
    """Note une trame ; rend True si ce capteur n'avait JAMAIS ete entendu."""
    t = t or time.time()
    data = load()
    e = data.get(key)
    first = e is None
    if first:
        e = data[key] = {"nom": name, "type": kind, "bande": band, "premier": t, "dernier": t, "n": 0, "jours": 0}
    day = datetime.date.fromtimestamp(t).isoformat()
    if e.get("dernier_jour") != day and day > (e.get("dernier_jour") or ""):
        e["jours"] += 1
        e["dernier_jour"] = day
    e["n"] += n
    e["premier"] = min(e["premier"], t)
    e["dernier"] = max(e["dernier"], t)
    e["nom"] = name or e["nom"]
    e["type"] = kind or e["type"]
    e["bande"] = band or e.get("bande")
    if mine:
        e["mien"] = True
    _state["dirty"] = True
    if first and write:
        save()
    return first


def seed(kind_of, name_of):
    """Remplit le registre avec ce qui a deja ete entendu : statistiques
    locales (jours resumes) puis releves bruts du NAS. kind_of(event) et
    name_of(event) : ceux de capteurs_watch. Rend le nombre de capteurs."""
    try:
        import stats_locales
        for key, s in (stats_locales.load().get("capteurs") or {}).items():
            if s.get("type") == "pneu":
                continue
            for day, d in sorted((s.get("jours") or {}).items()):
                t = time.mktime(time.strptime(day, "%Y-%m-%d")) + 43200
                seen(key, s.get("nom"), s.get("type"), None, t=t, n=d.get("n", 1), write=False)
            if s.get("premier") and key in load():
                load()[key]["premier"] = s["premier"]   # heure exacte, pas le midi du jour resume
    except Exception as exc:
        print(f"[capteurs_connus] statistiques locales : {exc!r}")
    for path in sorted(glob.glob(ARCHIVE_GLOB)):
        try:
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            continue
        for ev in rows:
            if ev.get("doublon") or not ev.get("model"):
                continue
            kind = kind_of(ev)
            if kind == "pneu":
                continue
            key = "|".join(str(ev.get(k, "")) for k in ("model", "id", "channel"))
            seen(key, name_of(ev), kind, str(ev.get("bande", ""))[:3] or None, t=ev.get("t"), write=False)
    save()
    return len(load())


def _date(t):
    d = datetime.date.fromtimestamp(t)
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
            "novembre", "décembre"]
    return f"{d.day} {mois[d.month - 1]}"


def _label(key, e):
    """Nom dit a voix haute : modele et numero (deux thermometres du meme
    modele chez les voisins ne se distinguent que par lui)."""
    parts = key.split("|")
    number = parts[1] if len(parts) > 1 and parts[1] else ""
    return f"{e['nom']} numéro {number}" if number else e["nom"]


def summary(top=5):
    """Ce que le Pi sait des capteurs deja entendus (commande "capteurs connus")."""
    from phrases import dire, dire_ou, nombre
    data = load()
    if not data:
        return dire("capteurs_connus.vide")
    now = time.time()
    today = datetime.date.today().isoformat()
    mine = [e for e in data.values() if e.get("mien")]
    heard_today = sum(1 for e in data.values() if e.get("dernier_jour") == today)
    week_new = [e for e in data.values() if now - e["premier"] < 7 * 86400]
    texts = [dire("capteurs_connus.titre_miens" if mine else "capteurs_connus.titre",
                  capteurs=nombre(len(data), "capteur"), miens=len(mine), aujourdhui=heard_today)]
    regular = sorted(((k, e) for k, e in data.items() if not e.get("mien")), key=lambda kv: -kv[1]["n"])[:top]
    if regular:
        texts.append(dire("capteurs_connus.fideles", liste=" ; ".join(
            dire("capteurs_connus.fidele", nom=_label(k, e), fois=nombre(e["n"], "fois", "fois"),
                 jours=nombre(e["jours"], "jour"), depuis=_date(e["premier"])) for k, e in regular)))
    for e in mine:
        texts.append(dire("capteurs_connus.mien", nom=e["nom"], fois=nombre(e["n"], "fois", "fois"),
                          jour=_date(e["dernier"]), heure=time.strftime("%H h %M", time.localtime(e["dernier"]))))
    if week_new:
        texts.append(dire("capteurs_connus.nouveaux", capteurs=nombre(len(week_new), "capteur")))
    return " ".join(texts)
