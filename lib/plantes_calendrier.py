"""Calendrier de vie des plantes (2026-10-06, etape 2) : a partir de la fiche
moyenne (lib/plantes.py) et des stades constates, le Pi calcule la fenetre
du prochain evenement (levee, premieres vraies feuilles, premieres fleurs,
premiers fruits), au plus tot / au plus tard.

- Ancrage sur la date REELLE du stade quand l'utilisateur l'a dit ; sinon
  estimee a partir des autres stades connus (moyenne de la fiche).
- Le froid freine : pour une plante dehors, si la temperature moyenne des
  7 derniers jours (Open-Meteo) est sous temp_min_croissance_c + 3, la
  fenetre s'allonge de moitie ; en dessous du minimum de croissance, on
  ne promet rien (croissance quasi arretee).
- watch() (thread de meteo_watch) : quand une fenetre s'ouvre, une prise
  vocale au journal "plantes", une seule fois ; si elle est depassee de
  LATE_DAYS sans que le stade ait ete note, un rappel unique. Jamais de
  22 h a 7 h (QUIET_HOURS) : ce qui tombe la nuit est dit le matin.
"""
import datetime
import os
import time

import requests

import plantes
import reglages
from phrases import dire, etiquette

STATE_PATH = "/home/vador/autopilot/plantes_calendrier.json"  # carte SD : survit au redemarrage
SOUND = "/media/nas/nd1/server/radio_scaner/incoming.mp3"
import sons
PHOTO_SOUND = sons.chemin("plante_photo", SOUND)  # son propre (2026-10-23, sons.py)
POLL_SEC = 1800
QUIET_HOURS = (22, 7)
LATE_DAYS = 3
TEMP_CACHE_SEC = 3 * 3600
# evenement : (cle de duree dans la fiche, stade qui le constate)
EVENTS = {"levee": ("jours_germination", "germe"), "feuilles": ("jours_premieres_feuilles", "pousses"),
          "fleurs": ("jours_floraison", "fleurs"), "fruits": ("jours_fruits", "fruits"),
          "maturite": ("jours_maturite", None)}  # fruits murs : pas de stade apres, un conseil au toucher
_temp = {"t": 0, "valeur": None}


def _span(fiche, key):
    try:
        lo, hi = fiche[key]
        return (int(lo), int(hi)) if 0 <= lo <= hi else None
    except (KeyError, TypeError, ValueError):
        return None


def _mean(span):
    return (span[0] + span[1]) / 2


def _stage_date(plant, stage):
    for h in reversed(plant.get("historique") or []):
        if h.get("stade") == stage:
            try:
                return datetime.date.fromisoformat(h["date"])
            except (KeyError, ValueError):
                return None
    return None


def _levee(plant, fiche):
    """Date de la levee : dite, ou estimee depuis le semis ou les premieres feuilles."""
    known = _stage_date(plant, "germe")
    if known:
        return known
    sown, germ = _stage_date(plant, "seme"), _span(fiche, "jours_germination")
    if sown and germ:
        return sown + datetime.timedelta(days=_mean(germ))
    leaves, first = _stage_date(plant, "pousses"), _span(fiche, "jours_premieres_feuilles")
    if leaves and first:
        return leaves - datetime.timedelta(days=_mean(first))
    return None


def outdoor_mean_temp():
    """Temperature moyenne des 7 derniers jours au lieu choisi (cache 3 h), ou None."""
    if time.time() - _temp["t"] < TEMP_CACHE_SEC:
        return _temp["valeur"]
    lieu = reglages.get("lieu")
    try:
        resp = requests.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
            "latitude": lieu["lat"], "longitude": lieu["lon"], "daily": "temperature_2m_mean",
            "past_days": 7, "forecast_days": 1, "timezone": "Europe/Paris"})
        values = [v for v in resp.json()["daily"]["temperature_2m_mean"][:7] if v is not None]
        _temp.update(t=time.time(), valeur=sum(values) / len(values) if values else None)
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        print(f"[plantes_calendrier] temperature : {exc!r}")
        _temp.update(t=time.time() - TEMP_CACHE_SEC + 600, valeur=None)  # nouvel essai dans 10 min
    return _temp["valeur"]


def next_event(plant, mean_temp=None, today=None):
    """Prochain evenement attendu : {"evenement", "debut", "fin", "stade",
    "ralenti", "ancre"} ou None (au repos, pas de fiche, rien a prevoir).
    {"evenement": None, "froid": True} : trop froid pour promettre quoi que ce soit."""
    fiche = plant.get("fiche") or {}
    stage = plant.get("stade")
    if not fiche or stage == "repos":
        return None
    if stage == "seme":
        event, anchor = "levee", _stage_date(plant, "seme")
    elif stage == "germe":
        event, anchor = "feuilles", _stage_date(plant, "germe")
    elif stage in ("pousses", "developpee"):
        event, anchor = "fleurs", _levee(plant, fiche)
    elif stage == "fleurs":
        event, anchor = "fruits", _stage_date(plant, "fleurs")
    else:  # fruits : la maturite, comptee depuis les premiers fruits
        event, anchor = "maturite", _stage_date(plant, "fruits")
    span = _span(fiche, EVENTS[event][0])
    if not anchor or not span:
        return None
    slowed = False
    if not plant.get("dedans") and mean_temp is not None:
        try:
            tmin = float(fiche.get("temp_min_croissance_c"))
        except (TypeError, ValueError):
            tmin = None
        if tmin is not None and mean_temp < tmin:
            return {"evenement": None, "froid": True}
        if tmin is not None and mean_temp < tmin + 3:
            span, slowed = (round(span[0] * 1.5), round(span[1] * 1.5)), True
    return {"evenement": event, "debut": anchor + datetime.timedelta(days=span[0]),
            "fin": anchor + datetime.timedelta(days=span[1]), "stade": EVENTS[event][1],
            "ralenti": slowed, "ancre": anchor.isoformat()}


def spoken_date(d):
    return f"{d.day} {etiquette(f'mois.{d.month}')}"


def _event_name(event):
    return dire(f"plantes.evenement_{event}")


def describe(plant, mean_temp=None, today=None):
    """Phrase pour la fiche (touche I) : ce qui est attendu et quand."""
    today = today or datetime.date.today()
    ev = next_event(plant, mean_temp)
    if not ev:
        return ""
    if ev["evenement"] is None:
        return dire("plantes.calendrier_froid")
    what = _event_name(ev["evenement"])
    if today < ev["debut"]:
        text = dire("plantes.calendrier_futur", evenement=what, debut=spoken_date(ev["debut"]), fin=spoken_date(ev["fin"]))
    elif today <= ev["fin"]:
        text = dire("plantes.calendrier_en_cours", evenement=what, fin=spoken_date(ev["fin"]))
    elif ev["stade"] is None:  # maturite
        text = dire("plantes.calendrier_murs", fin=spoken_date(ev["fin"]))
    else:
        text = dire("plantes.calendrier_retard", evenement=what, fin=spoken_date(ev["fin"]))
    if ev["ralenti"]:
        text += " " + dire("plantes.calendrier_ralenti")
    return text


def _quiet(now=None):
    hour = (now or datetime.datetime.now()).hour
    start, end = QUIET_HOURS
    return hour >= start or hour < end


def _load_state():
    import json
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    import json
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def due_messages(plants, state, mean_temp, today=None):
    """[(cle, texte)] des annonces a faire maintenant (sans les marquer)."""
    today = today or datetime.date.today()
    said = state.setdefault("dites", {})
    out = []
    for plant in plants:
        ev = next_event(plant, mean_temp)
        if not ev or ev["evenement"] is None:
            continue
        base = f"{plant['id']}|{ev['evenement']}|{ev['ancre']}"
        what = _event_name(ev["evenement"])
        if ev["stade"] is None:  # maturite : un conseil au toucher, pas de stade a noter, pas de rappel
            tip = (plant.get("fiche") or {}).get("maturite")
            if ev["debut"] <= today <= ev["fin"] and base not in said:
                out.append((base, dire("plantes.annonce_fenetre", nom=plant["nom"], evenement=what,
                                       fin=spoken_date(ev["fin"]))
                            + (" " + dire("plantes.maturite_conseil", conseil=tip) if tip else "")))
            continue
        word = dire(f"plantes.mot_{ev['stade']}")
        if ev["debut"] <= today <= ev["fin"] and base not in said:
            out.append((base, dire("plantes.annonce_fenetre", nom=plant["nom"], evenement=what, fin=spoken_date(ev["fin"]))
                        + " " + dire("plantes.calendrier_dis", nom=plant["nom"].lower(), mot=word)))
        elif today > ev["fin"] + datetime.timedelta(days=LATE_DAYS) and base + "|retard" not in said:
            out.append((base + "|retard", dire("plantes.annonce_retard", nom=plant["nom"], evenement=what,
                                               fin=spoken_date(ev["fin"]))
                        + " " + dire("plantes.calendrier_dis_fait", nom=plant["nom"].lower(), mot=word)))
    return out


def run_once(announce=None):
    """Un passage : fiches manquantes (une fois par jour), puis annonces dues."""
    state = _load_state()
    today = datetime.date.today().isoformat()
    if state.get("fiches_le") != today:
        try:
            plantes.complete_missing()
        except Exception as exc:
            print(f"[plantes_calendrier] fiches : {exc!r}")
        state["fiches_le"] = today
    if _quiet():
        _save_state(state)
        return []
    plants = plantes.load()
    messages = due_messages(plants, state, outdoor_mean_temp() if any(not p.get("dedans") for p in plants) else None)
    if announce is None:
        import veille_donnees as vd
        announce = lambda text: vd.announce(text, SOUND, "plante_calendrier", "plantes")  # noqa: E731
    for key, text in messages:
        announce(text)
        state["dites"][key] = today
    _save_state(state)
    return messages


def _alerts(announce=None):
    """Etape 3 : alertes redites jusqu'au "compris" (lib/plantes_alertes.py).
    Tourne aussi la nuit, en silence, pour suivre les situations."""
    import plantes_alertes
    if announce is None:
        import veille_donnees as vd
        announce = lambda text: vd.announce(text, plantes_alertes.SOUND, "plante_alerte", "plantes")  # noqa: E731
    return plantes_alertes.run_once(plantes.load(), _quiet(), announce)


def _photos(announce=None):
    """Etape 5 : nouvelles photos des plantes (lib/plantes_photos.py), une
    fois par heure ; resultats dits hors des heures de silence."""
    import plantes_photos
    if time.time() - _photos_last[0] < PHOTOS_EVERY_SEC:
        return []
    _photos_last[0] = time.time()
    said = plantes_photos.run_once()
    if said and not _quiet():
        if announce is None:
            import veille_donnees as vd
            announce = lambda text: vd.announce(text, PHOTO_SOUND, "plante_photo", "plantes")  # noqa: E731
        announce(" ".join(said))
    return said


PHOTOS_EVERY_SEC = 3600 - 120
_photos_last = [0.0]


def watch():
    while True:
        for job in (run_once, _alerts, _photos):
            try:
                job()
            except Exception as exc:  # une panne ne doit pas arreter meteo_watch
                print(f"[plantes_calendrier] {job.__name__} : {exc!r}")
        time.sleep(POLL_SEC)
