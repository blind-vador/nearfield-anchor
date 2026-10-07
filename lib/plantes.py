"""Plantes de l'utilisateur (2026-10-06) : registre, recherche et fiche par
Gemini. Reglage "plantes" sur le NAS (reglages.py), une entree par plante :

  id, nom, latin, description   description = texte alternatif, pour la
                                reconnaitre sans la voir
  stade, stade_depuis           STAGES ; date AAAA-MM-JJ du debut du stade
  historique                    [{"stade", "date"}] : stades constates
  dedans                        True / False
  emplacement                   "balcon" | "jardin" (dehors seulement)
  contenant                     "pot" | "pleine_terre" (pleine terre : jardin)
  abri_pluie                    True : la pluie ne l'arrose pas (balcon du
                                dessus, auvent...) ; toujours vrai dedans
  arrosee_le                    AAAA-MM-JJ du dernier arrosage dit, ou None
  fiche                         donnees moyennes rendues par Gemini (FICHE_PROMPT),
                                None tant qu'elle n'a pas pu etre faite

Gemini n'est appele qu'a l'ajout d'une plante (recherche, puis fiche) :
tous les conseils sont ensuite calcules sur le Pi a partir de la fiche.
"""
import datetime
import json
import uuid

import gemini
import reglages
from phrases import Etiquettes, dire

STAGES = ["seme", "germe", "pousses", "developpee", "fleurs", "fruits", "repos"]
STAGE_NAMES = Etiquettes("plantes.stade", {
    "seme": "graines semées", "germe": "en germination", "pousses": "jeunes pousses", "developpee": "plante développée",
    "fleurs": "en fleurs", "fruits": "en fruits", "repos": "au repos"}, "stade d'une plante")
# "depuis quand" a l'ajout : (libelle, jours)
SINCE = [("aujourd_hui", 0), ("quelques_jours", 3), ("une_semaine", 7), ("deux_semaines", 14),
         ("un_mois", 30), ("deux_mois", 60), ("plus_trois_mois", 100)]
SINCE_NAMES = Etiquettes("plantes.depuis", {
    "aujourd_hui": "aujourd'hui", "quelques_jours": "il y a quelques jours", "une_semaine": "il y a une semaine",
    "deux_semaines": "il y a deux semaines", "un_mois": "il y a un mois", "deux_mois": "il y a deux mois",
    "plus_trois_mois": "il y a plus de trois mois"}, "depuis quand une plante est a ce stade")

SEARCH_PROMPT = """Tu aides une personne aveugle a enregistrer ses plantes. Elle a tape : "{texte}".
Propose de 1 a 5 plantes qui correspondent le mieux (la plus probable d'abord ; variantes courantes si le nom est vague).
Pour chacune, une description en francais facon texte alternatif, 2 phrases au plus, pour la reconnaitre SANS LA VOIR :
port et taille adulte, forme et toucher des feuilles, odeur, fleurs ou fruits. Pas de couleurs seules sans autre indice.
JSON strict : {{"candidats": [{{"nom": "nom courant", "latin": "nom latin", "description": "..."}}]}}"""

FICHE_PROMPT = """Plante : {nom} ({latin}), cultivee a {commune} (France, environ {altitude} m d'altitude).
Donne des donnees MOYENNES de culture, prudentes (minimums realistes), en JSON strict avec exactement ces cles :
{{
 "jours_germination": [min, max],          // du semis a la levee ; null si on ne la seme pas
 "jours_premieres_feuilles": [min, max],   // de la levee aux premieres vraies feuilles
 "jours_floraison": [min, max],            // de la levee aux premieres fleurs ; null si elle ne fleurit pas en culture
 "jours_fruits": [min, max],               // des fleurs aux premiers fruits ou a la recolte ; null sinon
 "temp_gel_c": nombre,                     // temperature sous laquelle elle souffre ou gele
 "temp_min_croissance_c": nombre,
 "temp_max_c": nombre,                     // au dela, stress de chaleur
 "vent": "fragile" | "moyen" | "robuste",
 "eau": "faible" | "moyen" | "fort",
 "jours_sans_eau_pot_ete": nombre,         // en pot dehors, par temps chaud et sec
 "vivace": true | false,
 "rentrer_l_hiver": true | false,
 "conseils": "2 phrases au plus : ce qui compte le plus pour la reussir"
}}"""


def join_names(names):
    """ "A, B et C" """
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " " + dire("plantes.et") + " " + names[-1]


def find(text, plants):
    """Plante de la liste designee par un texte libre (nom suivi du latin,
    guillemets en moins...), ou None. Le nom le plus long gagne."""
    import unicodedata

    def norm(s):
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
        return " ".join("".join(c if c.isalnum() else " " for c in s).split())
    t = norm(text)
    hits = [p for p in plants if norm(p["nom"]) and (norm(p["nom"]) in t or t in norm(p["nom"]))]
    return max(hits, key=lambda p: len(p["nom"])) if hits else None


def _today():
    return datetime.date.today().isoformat()


def load():
    return list(reglages.get("plantes").get("plantes") or [])


def save(plants):
    """False si le NAS ne repond pas (rien n'est alors enregistre)."""
    return reglages.save("plantes", {"plantes": plants})


def search(text):
    """Plantes candidates pour un nom tape : ([{"nom", "latin", "description"}], raison)."""
    answer, reason = gemini.ask(SEARCH_PROMPT.format(texte=text), temperature=0.3, json_mode=True)
    if answer is None:
        return [], reason
    try:
        found = json.loads(answer).get("candidats") or []
    except (ValueError, AttributeError):
        return [], dire("gemini.reponse_gemini_inutilisable")
    found = [c for c in found if isinstance(c, dict) and c.get("nom")][:5]
    return found, (None if found else dire("plantes.rien_trouve", texte=text))


def make_fiche(plant):
    """Fiche moyenne par Gemini (dict), ou None."""
    lieu = reglages.get("lieu")
    prompt = FICHE_PROMPT.format(nom=plant["nom"], latin=plant.get("latin") or "?",
                                 commune=lieu.get("commune", "Saint-Étienne"), altitude=lieu.get("altitude", 520))
    answer, _ = gemini.ask(prompt, temperature=0.2, json_mode=True)
    try:
        fiche = json.loads(answer) if answer else None
    except ValueError:
        return None
    return fiche if isinstance(fiche, dict) else None


def new_plant(candidate, stage, days_ago, dedans, emplacement, contenant, abri_pluie, existing=()):
    since = (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()
    name = candidate["nom"].strip()
    taken = {p["nom"].casefold() for p in existing}
    if name.casefold() in taken:  # deux basilics : "basilic 2"
        n = 2
        while f"{name} {n}".casefold() in taken:
            n += 1
        name = f"{name} {n}"
    return {"id": uuid.uuid4().hex[:8], "nom": name, "latin": candidate.get("latin") or "",
            "description": candidate.get("description") or "", "stade": stage, "stade_depuis": since,
            "historique": [{"stade": stage, "date": since}], "dedans": dedans,
            "emplacement": None if dedans else emplacement, "contenant": contenant,
            "abri_pluie": True if dedans else abri_pluie, "arrosee_le": None,
            "ajoutee_le": _today(), "fiche": None}


def set_stage(plant, stage):
    plant["stade"], plant["stade_depuis"] = stage, _today()
    plant.setdefault("historique", []).append({"stade": stage, "date": _today()})


def place_text(plant):
    """ "dehors, sur le balcon, en pot, à l'abri de la pluie" """
    if plant.get("dedans"):
        parts = [dire("plantes.lieu_dedans")]
    else:
        parts = [dire("plantes.lieu_dehors")]
        if plant.get("emplacement"):
            parts.append(dire("plantes.lieu_balcon" if plant["emplacement"] == "balcon" else "plantes.lieu_jardin"))
    parts.append(dire("plantes.en_pot" if plant.get("contenant") == "pot" else "plantes.pleine_terre"))
    if not plant.get("dedans"):
        parts.append(dire("plantes.abritee" if plant.get("abri_pluie") else "plantes.sous_la_pluie"))
    return ", ".join(parts)


def days_since(iso):
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(iso)).days
    except (TypeError, ValueError):
        return None


def line(plant):
    return dire("plantes.ligne", nom=plant["nom"], stade=STAGE_NAMES[plant["stade"]], lieu=place_text(plant))


def card(plant):
    """Touche I : tout ce que le Pi sait de la plante."""
    parts = [plant["nom"] + (f", {plant['latin']}" if plant.get("latin") else "") + "."]
    if plant.get("description"):
        parts.append(plant["description"])
    days = days_since(plant.get("stade_depuis"))
    parts.append(dire("plantes.fiche_stade", stade=STAGE_NAMES[plant["stade"]],
                      depuis=dire("plantes.depuis_aujourdhui") if not days else dire("plantes.depuis_jours", jours=days)))
    parts.append(place_text(plant).capitalize() + ".")
    watered = days_since(plant.get("arrosee_le"))
    if watered is not None:
        parts.append(dire("plantes.arrosee_aujourdhui") if watered == 0 else dire("plantes.arrosee_il_y_a", jours=watered))
    fiche = plant.get("fiche")
    if fiche and fiche.get("conseils"):
        parts.append(str(fiche["conseils"]))
    elif not fiche:
        parts.append(dire("plantes.fiche_manquante"))
    return " ".join(parts)


def complete_missing():
    """Refait les fiches qui manquent (Gemini indisponible a l'ajout).
    Rend le nombre de fiches faites."""
    plants = load()
    done = 0
    for plant in plants:
        if not plant.get("fiche"):
            plant["fiche"] = make_fiche(plant)
            done += bool(plant["fiche"])
    if done:
        save(plants)
    return done
