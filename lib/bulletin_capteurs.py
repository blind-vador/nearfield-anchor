"""Bulletin horaire des capteurs meteo (demande utilisateur 2026-10-19) : les
releves des thermometres et stations sans fil du voisinage ne sont plus dits
un par un (des centaines d'entrees dans le journal capteurs) ; ils sont
gardes (noter(), appele par capteurs_watch) et, une fois par heure, un
petit bulletin clair dit ce qu'on en comprend : temperature dehors et son
evolution, humidite, vent, pluie, ecarts entre capteurs. Gemini l'ecrit a
partir des chiffres (sans rien inventer), sinon une phrase toute faite.

Rien n'est dit pendant une alerte meteo (meteo_locale.published_level() >= 2) :
les alertes parlent d'elles-memes ; le bulletin est quand meme archive.
Chaque bulletin est une prise vocale du journal "meteo" (fichier
meteo_capteurs_..., voir lib/journaux.py), avec sa fiche (les chiffres).
Thread de meteo_watch (watch()).
"""
import json
import os
import statistics
import time
from datetime import datetime

import veille_donnees as vd
from phrases import dire, nombre
from sound_paths import METEO_SCANER_DIR

RELEVES_PATH = "/home/vador/autopilot/capteurs_heure.json"
import sons
SOUND = sons.chemin("jingle_meteo", os.path.join(METEO_SCANER_DIR, "simple_message.mp3"))  # son propre (2026-10-23, sons.py)
GARDE_SEC = 3 * 3600
ALERTE_NIVEAU = 2
CHAMPS = {"temperature_C": "temperature", "humidity": "humidite", "wind_avg_km_h": "vent_kmh",
          "rain_mm": "pluie_mm", "pressure_hPa": "pression_hpa"}

PROMPT = """Tu es la voix d'un poste de veille domestique, à {commune}. Il est {heure}.
Voici les relevés de la dernière heure des capteurs météo sans fil du voisinage
(thermomètres et stations de jardin captés par radio), avec leur évolution sur l'heure :
{donnees}

Écris un mini bulletin à lire à voix haute, en français, 2 à 4 phrases courtes, clair et
concret : la température dehors maintenant et si elle monte, baisse ou reste stable ;
l'humidité ; le vent ou la pluie s'il y en a ; et ce qu'on peut en comprendre simplement
(gel possible, brouillard probable, air sec, nuit fraîche...). N'invente aucun chiffre,
arrondis au degré ou au demi-degré, ne cite pas les noms techniques des capteurs, pas de
markdown, pas de liste, pas de salutation. Si les capteurs ne sont pas d'accord entre eux,
dis-le en une phrase."""


def noter(key, nom, event, interieur=False):
    """Un releve meteo capte (capteurs_watch) : garde pour le bulletin."""
    valeurs = {CHAMPS[k]: event[k] for k in CHAMPS if event.get(k) is not None}
    if not valeurs:
        return
    data = _lire()
    now = time.time()
    data.setdefault("releves", []).append({"cle": key, "nom": nom, "t": now, "interieur": bool(interieur), **valeurs})
    data["releves"] = [r for r in data["releves"] if now - r["t"] < GARDE_SEC]
    _ecrire(data)


def _lire():
    try:
        with open(RELEVES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _ecrire(data):
    tmp = RELEVES_PATH + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, RELEVES_PATH)


def resume(now=None):
    """Chiffres de la derniere heure : par capteur dehors (dernier, mini, maxi,
    evolution sur l'heure), mediane dehors, interieurs a part. None sans releve."""
    now = now or time.time()
    releves = _lire().get("releves", [])
    heure = [r for r in releves if now - r["t"] <= 3600]
    if not heure:
        return None
    capteurs = {}
    for r in sorted(releves, key=lambda r: r["t"]):
        capteurs.setdefault(r["cle"], []).append(r)
    dehors, dedans = [], []
    for cle, rows in capteurs.items():
        recents = [r for r in rows if now - r["t"] <= 3600]
        if not recents:
            continue
        avant = [r for r in rows if 3000 <= now - r["t"] <= 7200]  # il y a environ une heure
        fiche = {"capteur": rows[-1]["nom"], "releves": len(recents)}
        for champ in CHAMPS.values():
            vals = [r[champ] for r in recents if champ in r]
            if not vals:
                continue
            fiche[champ] = {"maintenant": vals[-1], "mini": min(vals), "maxi": max(vals)}
            ref = [r[champ] for r in avant if champ in r]
            if ref:
                fiche[champ]["evolution_1h"] = round(vals[-1] - ref[-1], 1)
        (dedans if rows[-1].get("interieur") else dehors).append(fiche)
    temps = [c["temperature"]["maintenant"] for c in dehors if "temperature" in c]
    hums = [c["humidite"]["maintenant"] for c in dehors if "humidite" in c]
    evol = [c["temperature"]["evolution_1h"] for c in dehors if "evolution_1h" in c.get("temperature", {})]
    return {"heure": datetime.fromtimestamp(now).strftime("%H:%M"), "capteurs_dehors": dehors,
            "capteurs_dedans": dedans,
            "dehors": {"temperature_mediane": round(statistics.median(temps), 1) if temps else None,
                       "ecart_entre_capteurs": round(max(temps) - min(temps), 1) if len(temps) > 1 else None,
                       "humidite_mediane": round(statistics.median(hums)) if hums else None,
                       "evolution_1h": round(statistics.median(evol), 1) if evol else None}}


def _texte_simple(r):
    d = r["dehors"]
    if d["temperature_mediane"] is None:
        return dire("bulletin_capteurs.dedans_seulement", n=nombre(len(r["capteurs_dedans"]), "capteur"))
    h, m = (int(x) for x in r["heure"].split(":"))
    text = dire("bulletin_capteurs.base", heure=f"{h} heures" + (f" {m}" if m else ""),
                n=nombre(len(r["capteurs_dehors"]), "capteur"),
                temperature=vd.fr_number(d["temperature_mediane"], 1))
    if d["evolution_1h"] is not None:
        e = d["evolution_1h"]
        key = "bulletin_capteurs.stable" if abs(e) < 0.3 else ("bulletin_capteurs.hausse" if e > 0 else "bulletin_capteurs.baisse")
        text += " " + dire(key, ecart=vd.fr_number(abs(e), 1))
    if d["humidite_mediane"] is not None:
        text += " " + dire("bulletin_capteurs.humidite", humidite=d["humidite_mediane"])
    return text


def texte(r):
    """Le bulletin a dire : Gemini, sinon la phrase toute faite."""
    try:
        import gemini
        import reglages
        lieu = reglages.get("lieu")
        text, problem = gemini.ask(PROMPT.format(commune=lieu.get("commune", ""), heure=r["heure"],
                                                 donnees=json.dumps(r, ensure_ascii=False)), temperature=0.4, timeout=40)
        if text and text.strip():
            return " ".join(text.replace("*", "").split()), "gemini"
        print(f"[bulletin_capteurs] Gemini : {problem}")
    except Exception as exc:
        print(f"[bulletin_capteurs] Gemini : {exc!r}")
    return _texte_simple(r), "simple"


def faire(dire_le=True):
    """Le bulletin de l'heure : archive, et dit s'il n'y a pas d'alerte en cours."""
    r = resume()
    if r is None:
        print("[bulletin_capteurs] aucun releve sur l'heure : rien a dire")
        return None
    text, auteur = texte(r)
    try:
        import meteo_locale
        alerte = meteo_locale.published_level() >= ALERTE_NIVEAU
    except Exception:
        alerte = False
    vd.archive("meteo_capteurs", dict(r, bulletin=text, auteur=auteur, dit=dire_le and not alerte))
    if dire_le and not alerte:
        vd.announce(text, SOUND, "meteo_capteurs", "meteo_capteurs", fiche={"source": "capteurs meteo", **r})
    else:
        print(f"[bulletin_capteurs] alerte en cours, bulletin archive sans etre dit : {text}")
    return text


def watch():
    """Thread de meteo_watch : un bulletin au debut de chaque heure."""
    while True:
        now = time.time()
        time.sleep(3600 - now % 3600 + 30)  # hh:00:30, les releves de l'heure sont la
        try:
            faire()
        except Exception as exc:
            print(f"[bulletin_capteurs] {exc!r}")
