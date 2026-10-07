"""Rubrique "plantes" du flash info (2026-10-07, etape 4).

1. Meteo par plante, sur 48 h (previsions de plantes_alertes) : gel, nuits
   ou il faut rentrer, nuits fraiches qui annoncent la rentree, vent,
   chaleur, soif (bilan d'eau), et l'evenement de vie en cours
   (plantes_calendrier).
2. Ravageurs et maladies du mois a Saint-Etienne pour CES plantes, avec un
   traitement ecoresponsable : une fiche Gemini par mois (et quand la
   liste des plantes change), gardee sur la carte SD. Chaque ravageur a la
   meteo qui le favorise ; le flash court ne cite que ceux que la meteo du
   moment favorise, le flash complet les donne tous.
3. Quoi semer ou planter ce mois-ci (pot, balcon, jardin) : dans le flash
   court la premiere semaine du mois, dans le flash complet toujours.
Rien a dire : le flash court se tait, le flash complet le dit en une phrase.
"""
import datetime
import json
import os
import time

import gemini
import plantes
import plantes_alertes as pa
import plantes_calendrier as pc
import reglages
from phrases import dire, etiquette

MONTH_PATH = os.path.join(reglages.CACHE_DIR, "plantes_mois.json")
RETRY_SEC = 3600              # Gemini en panne : nouvel essai au plus une fois par heure
SOON_SHARE = 0.7              # soif "bientot" a 70 % de la reserve
FRESH_NIGHT_C = 10            # nuit fraiche : penser a rentrer les plantes d'hiver
SHORT_PESTS = 2
SOWING_DAYS = 7               # flash court : semis la premiere semaine du mois
WEATHERS = ("pluie", "chaud_sec", "chaud_humide", "froid_humide", "toujours")

MONTH_PROMPT = """Jardinier conseil a {commune} (France, environ {altitude} m d'altitude, climat semi-continental), mois de {mois}.
Plantes de la personne (nom exact, nom latin, emplacement) :
{plantes}

Reponds en JSON strict :
{{
 "ravageurs": [ {{"plante": "nom EXACT de la liste", "nom": "ravageur ou maladie", "signes": "comment le reperer au toucher ou a l'odeur si possible, sinon a l'oeil, 15 mots max",
                 "traitement": "geste ou produit ecoresponsable, 25 mots max", "favorise_par": "pluie" | "chaud_sec" | "chaud_humide" | "froid_humide" | "toujours"}} ],
 "semis": {{"pot": ["plante : ce qu'on fait ce mois-ci, 8 mots max"], "balcon": [...], "jardin": [...]}}
}}
Ravageurs : seulement ceux reellement actifs en {mois} dans cette region, 2 au plus par plante, aucun si rien n'est a craindre.
Traitements : uniquement biologiques ou naturels (purin d'ortie ou de consoude, decoction de prele, savon noir, nematodes,
auxiliaires comme les coccinelles, Bacillus thuringiensis, pieges, paillage, gestes) ; jamais de produit de synthese.
Semis : 4 idees au plus par emplacement, adaptees a {mois} et a cette altitude ; liste vide si la saison ne s'y prete pas."""


def _load():
    try:
        with open(MONTH_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data):
    os.makedirs(os.path.dirname(MONTH_PATH), exist_ok=True)
    tmp = MONTH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, MONTH_PATH)


def _month_key(plants, today):
    return f"{today:%Y-%m}|" + "|".join(sorted(p["nom"] for p in plants))


def monthly(plants, today=None):
    """Fiche du mois (ravageurs, semis), refaite au changement de mois ou de plantes."""
    today = today or datetime.date.today()
    data = _load()
    key = _month_key(plants, today)
    if data.get("cle") == key and data.get("fiche"):
        return data["fiche"]
    if data.get("cle") == key and time.time() - data.get("essai", 0) < RETRY_SEC:
        return None
    lieu = reglages.get("lieu")
    listing = "\n".join(f"- {p['nom']} ({p.get('latin') or '?'}) : {plantes.place_text(p)}" for p in plants) or "- aucune"
    answer, reason = gemini.ask(MONTH_PROMPT.format(commune=lieu.get("commune", "Saint-Étienne"),
                                                    altitude=lieu.get("altitude", 520),
                                                    mois=etiquette(f"mois.{today.month}"), plantes=listing),
                                temperature=0.3, json_mode=True, timeout=60)
    fiche = None
    try:
        fiche = json.loads(answer) if answer else None
    except ValueError:
        pass
    if not isinstance(fiche, dict):
        print(f"[plantes_flash] fiche du mois : {reason or 'reponse illisible'}")
        _save({"cle": key, "essai": time.time(), "fiche": None})
        return None
    names = {p["nom"] for p in plants}
    fiche["ravageurs"] = [r for r in fiche.get("ravageurs") or [] if isinstance(r, dict) and r.get("plante") in names
                          and r.get("nom") and r.get("favorise_par") in WEATHERS]
    _save({"cle": key, "essai": time.time(), "fiche": fiche})
    return fiche


def weather_now(fc, now):
    """Meteos du moment qui favorisent les ravageurs : ensemble de WEATHERS."""
    days = pa._days(fc)
    today = now.date()
    rain = {d: r for d, (_, r) in days.items()}
    last3 = [rain.get(today - datetime.timedelta(days=k), 0) for k in (1, 2, 3)]
    last5 = [rain.get(today - datetime.timedelta(days=k), 0) for k in range(1, 6)]
    temps = [v for _, v in pa._hours(fc, "temperature_2m", now - datetime.timedelta(hours=48), now + datetime.timedelta(hours=24))]
    hum = [v for _, v in pa._hours(fc, "relative_humidity_2m", now - datetime.timedelta(hours=48), now)]
    tmax = max(temps) if temps else None
    out = {"toujours"}
    if max(last3[:2] + [rain.get(today, 0)]) >= 5:
        out.add("pluie")
    if tmax is not None and tmax >= 20 and max(last5) < 1:
        out.add("chaud_sec")
    if tmax is not None and tmax >= 18 and ((hum and sum(hum) / len(hum) >= 80) or sum(r >= 1 for r in last3) >= 2):
        out.add("chaud_humide")
    if tmax is not None and tmax < 12 and sum(r >= 1 for r in last3) >= 2:
        out.add("froid_humide")
    return out


def _plant_weather(p, fc, now, mean_temp):
    """Phrases meteo pour une plante sur 48 h."""
    fiche = p.get("fiche") or {}
    out = []
    end = now + datetime.timedelta(hours=48)
    if not p.get("dedans") and fiche:
        temps = pa._hours(fc, "temperature_2m", now, end)
        if temps:
            low_t, low = min(temps, key=lambda x: x[1])
            try:
                frost = float(fiche["temp_gel_c"]) + (pa.POT_FROST_MARGIN_C if p.get("contenant") == "pot" else 0)
            except (KeyError, TypeError, ValueError):
                frost = None
            if frost is not None and low <= frost:
                out.append(dire("plantes.flash_gel", nom=p["nom"], temp=pa._num(low), quand=pa._when(low_t, now)))
            elif fiche.get("rentrer_l_hiver") and low <= pa.RENTRER_C:
                out.append(dire("plantes.flash_rentrer", nom=p["nom"], temp=pa._num(low), quand=pa._when(low_t, now)))
            elif fiche.get("rentrer_l_hiver") and low <= FRESH_NIGHT_C:
                out.append(dire("plantes.flash_nuits_fraiches", nom=p["nom"], temp=pa._num(low)))
            high_t, high = max(temps, key=lambda x: x[1])
            try:
                if high >= float(fiche["temp_max_c"]):
                    out.append(dire("plantes.flash_chaleur", nom=p["nom"], temp=pa._num(high), quand=pa._when(high_t, now)))
            except (KeyError, TypeError, ValueError):
                pass
        limit = pa.GUSTS_KMH.get(fiche.get("vent"))
        gusts = pa._hours(fc, "wind_gusts_10m", now, now + datetime.timedelta(hours=36))
        if limit and gusts:
            top_t, top = max(gusts, key=lambda x: x[1])
            if top >= limit:
                out.append(dire("plantes.flash_vent", nom=p["nom"], vitesse=pa._num(top), quand=pa._when(top_t, now)))
    if fiche:
        balance = pa.water_deficit(p, pa._days(fc), now.date())
        reserve = max(1.0, float(fiche.get("jours_sans_eau_pot_ete") or 2)) * pa.MM_PER_HOT_DAY
        if p.get("contenant") == "pleine_terre":
            reserve *= pa.GROUND_FACTOR
        if balance and balance[1] >= reserve:
            out.append(dire("plantes.flash_soif", nom=p["nom"], jours=balance[0]))
        elif balance and balance[1] >= SOON_SHARE * reserve:
            out.append(dire("plantes.flash_soif_bientot", nom=p["nom"]))
    ev = pc.next_event(p, mean_temp)
    if ev and ev.get("evenement") and ev["debut"] <= now.date() <= ev["fin"]:
        out.append(dire("plantes.flash_evenement", nom=p["nom"], evenement=pc._event_name(ev["evenement"]),
                        fin=pc.spoken_date(ev["fin"])))
    return out


def _sowing(fiche):
    parts = []
    for place, key in (("pot", "plantes.semis_pot"), ("balcon", "plantes.semis_balcon"), ("jardin", "plantes.semis_jardin")):
        items = [str(x) for x in (fiche.get("semis") or {}).get(place) or [] if x][:4]
        if items:
            parts.append(dire(key, liste=" ; ".join(items)))
    return " ".join(parts)


def text(complet=False, now=None):
    """Texte de la rubrique, "" si rien a dire (flash court)."""
    now = now or datetime.datetime.now()
    plants = plantes.load()
    parts = []
    fc = None
    try:
        fc = pa.forecast()
    except Exception as exc:  # pas de previsions : le reste de la rubrique peut encore servir
        print(f"[plantes_flash] previsions : {exc!r}")
    if fc and plants:
        mean_temp = pc.outdoor_mean_temp() if any(not p.get("dedans") for p in plants) else None
        for p in plants:
            parts += _plant_weather(p, fc, now, mean_temp)
        if complet and not parts:
            parts.append(dire("plantes.flash_rien"))
    fiche = None
    try:
        fiche = monthly(plants, now.date())
    except Exception as exc:
        print(f"[plantes_flash] fiche du mois : {exc!r}")
    if fiche:
        pests = fiche.get("ravageurs") or []
        if not complet and fc:
            weather = weather_now(fc, now) - {"toujours"}
            pests = [r for r in pests if r["favorise_par"] in weather][:SHORT_PESTS]
        for r in pests:
            parts.append(dire("plantes.flash_ravageur", ravageur=r["nom"], plante=r["plante"],
                              signes=str(r.get("signes") or "").rstrip(". "),
                              traitement=str(r.get("traitement") or "").rstrip(". ")))
        if complet or now.day <= SOWING_DAYS:
            sowing = _sowing(fiche)
            if sowing:
                parts.append(dire("plantes.flash_semis", mois=etiquette(f"mois.{now.month}")) + " " + sowing)
    if not parts:
        return ""
    return dire("plantes.flash_titre") + " " + " ".join(parts)
