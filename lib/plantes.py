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
                                None tant qu'elle n'a pas pu etre faite ; "maturite" et
                                "jours_maturite" ajoutes quand la plante porte des fruits
  a_confirmer                   observations d'une photo (fruits, fleurs, probleme...),
                                avec l'endroit ou les chercher : l'utilisateur les
                                confirme au toucher (C) ou les signale fausses (H) ;
                                rien ne change de stade sans sa confirmation

Gemini n'est appele qu'a l'ajout d'une plante (recherche, puis fiche) :
tous les conseils sont ensuite calcules sur le Pi a partir de la fiche.
"""
import datetime
import json
import uuid

import gemini
import reglages
from phrases import Etiquettes, dire, nombre

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
 "conseils": "2 phrases au plus : ce qui compte le plus pour la reussir",
 "jours_maturite": [min, max],             // des premiers petits fruits a la maturite ; null sinon
 "maturite": "comment une personne aveugle reconnait un fruit mur : au toucher (fermete, souplesse), a l'odeur, au poids, a la facon dont il se detache ; 2 phrases ; chaine vide si pas de fruit"
}}"""

MATURITY_PROMPT = """Plante : {nom} ({latin}), cultivee a {commune}. Ses fruits sont en train de se former.
Pour une personne aveugle, en JSON strict :
{{"jours_maturite": [min, max],   // des premiers petits fruits a la maturite
 "maturite": "comment reconnaitre un fruit mur au toucher (fermete, souplesse, peau), a l'odeur, au poids, a la facon dont il se detache de la plante ; ce qu'il ne faut pas confondre ; 2 a 3 phrases"}}"""


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
    if isinstance(fiche, dict) and fiche.get("maturite"):
        tip = str(fiche["maturite"]).strip()
        fiche["maturite"] = tip[:1].lower() + tip[1:]
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


# --- suggestions (2026-10-07) : plantes reperees par Gemini sur les photos ---
# (voisines d'une plante photographiee, ou photo du dossier a_identifier),
# proposees a l'ajout avec leurs informations pre-remplies.

def _key(name, latin):
    import unicodedata
    text = (latin or name or "").split("(")[0]
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in text).split()[:2])  # genre + espece


def suggestions():
    return list(reglages.get("plantes_suggestions").get("suggestions") or [])


def _save_suggestions(items, refused=None):
    data = reglages.get("plantes_suggestions")
    data["suggestions"] = items
    if refused is not None:
        data["refusees"] = refused
    return reglages.save("plantes_suggestions", data)


def usual_place(plants):
    """Emplacement le plus courant des plantes : (dedans, emplacement, contenant, abri)."""
    import collections
    places = collections.Counter((bool(p.get("dedans")), p.get("emplacement"), p.get("contenant") or "pot",
                                  bool(p.get("abri_pluie"))) for p in plants)
    return places.most_common(1)[0][0] if places else (True, None, "pot", True)


def add_suggestions(found, base=None, source=""):
    """Enregistre les plantes reperees qui ne sont ni dans la liste, ni deja
    proposees, ni refusees. found : [{"nom", "latin", "description",
    "stade", "certitude", "ou"}] ; base : plante photographiee (son
    emplacement sert de pre-remplissage). Rend les nouvelles suggestions."""
    plants = load()
    data = reglages.get("plantes_suggestions")
    known = {_key(p["nom"], p.get("latin")) for p in plants}
    known |= {_key(s["nom"], s.get("latin")) for s in data.get("suggestions") or []}
    known |= set(data.get("refusees") or [])
    place = ((bool(base.get("dedans")), base.get("emplacement"), base.get("contenant") or "pot", bool(base.get("abri_pluie")))
             if base else usual_place(plants))
    new = []
    for f in found or []:
        if not isinstance(f, dict) or not f.get("nom"):
            continue
        key = _key(f["nom"], f.get("latin"))
        if not key or key in known or find(f["nom"], plants):
            continue
        known.add(key)
        new.append({"nom": str(f["nom"]).strip(), "latin": str(f.get("latin") or "").strip(),
                    "description": str(f.get("description") or "").strip(),
                    "stade": f.get("stade") if f.get("stade") in STAGES else "developpee",
                    "certitude": str(f.get("certitude") or ""), "ou": str(f.get("ou") or "").strip(),
                    "dedans": place[0], "emplacement": place[1], "contenant": place[2], "abri_pluie": place[3],
                    "source": source, "le": _today()})
    if new:
        _save_suggestions((data.get("suggestions") or []) + new)
    return new


def suggestion_text(s):
    """Lecture d'une suggestion : nom, certitude, description, ou, ce qui sera pre-rempli."""
    plant_like = {"dedans": s["dedans"], "emplacement": s["emplacement"], "contenant": s["contenant"],
                  "abri_pluie": s["abri_pluie"]}
    parts = [dire("plantes.suggestion_ligne", nom=s["nom"], latin=s.get("latin") or "?",
                  certitude=s.get("certitude") or "?")]
    if s.get("source"):
        parts.append(dire("plantes.suggestion_source", source=s["source"]))
    if s.get("description"):
        parts.append(s["description"])
    if s.get("ou"):
        parts.append(dire("plantes.a_verifier_ou", ou=s["ou"].rstrip(".")))
    parts.append(dire("plantes.suggestion_prerempli", stade=STAGE_NAMES[s["stade"]], lieu=place_text(plant_like)))
    return " ".join(parts)


def accept_suggestion(index):
    """Ajoute la suggestion a la liste (fiche Gemini comprise). Rend (plante, phrase)."""
    items = suggestions()
    s = items.pop(index)
    plants = load()
    plant = new_plant(s, s["stade"], 0, s["dedans"], s["emplacement"], s["contenant"], s["abri_pluie"], plants)
    plant["fiche"] = make_fiche(plant)
    plants.append(plant)
    if not save(plants):
        return None, dire("plantes.nas_inaccessible")
    _save_suggestions(items)
    return plant, dire("plantes.suggestion_ajoutee", ligne=line(plant))


def refuse_suggestion(index):
    """Ne plus jamais proposer cette plante. Rend la phrase."""
    items = suggestions()
    s = items.pop(index)
    refused = list(reglages.get("plantes_suggestions").get("refusees") or [])
    refused.append(_key(s["nom"], s.get("latin")))
    if not _save_suggestions(items, refused):
        return dire("plantes.nas_inaccessible")
    return dire("plantes.suggestion_refusee", nom=s["nom"])


def ensure_maturity(plant):
    """Conseils de maturite des fruits dans la fiche (Gemini, une fois). Rend
    True si la fiche a change."""
    fiche = plant.get("fiche")
    if not fiche or fiche.get("maturite"):
        return False
    lieu = reglages.get("lieu")
    answer, _ = gemini.ask(MATURITY_PROMPT.format(nom=plant["nom"], latin=plant.get("latin") or "?",
                                                  commune=lieu.get("commune", "Saint-Étienne")),
                           temperature=0.2, json_mode=True)
    try:
        data = json.loads(answer) if answer else None
    except ValueError:
        return False
    if not isinstance(data, dict) or not data.get("maturite"):
        return False
    tip = str(data["maturite"]).strip()
    fiche["maturite"] = tip[:1].lower() + tip[1:]  # dit apres "Pour reconnaitre un fruit mur :"
    if isinstance(data.get("jours_maturite"), list):
        fiche["jours_maturite"] = data["jours_maturite"]
    return True


def _verdict(plant, confirmed):
    """C ou H sur la premiere observation en attente. Rend la phrase (rien
    n'est enregistre ici : save() par l'appelant)."""
    pending = plant.get("a_confirmer") or []
    if not pending:
        return dire("plantes.rien_a_confirmer", nom=plant["nom"])
    item = pending.pop(0)
    plant["a_confirmer"] = pending
    import plantes_photos
    today = _today()
    if confirmed:
        line = f"{today} : " + dire("plantes.evolution_verifie", detail=item["detail"])
        text = dire("plantes.confirme", detail=item["detail"])
        stage = item.get("stade")
        if stage in STAGES and stage != plant["stade"] and STAGES.index(stage) > STAGES.index(plant["stade"]):
            set_stage(plant, stage)
            text += " " + dire("plantes.note_stade", nom=plant["nom"], stade=STAGE_NAMES[stage])
        if plant["stade"] == "fruits":
            ensure_maturity(plant)
            if (plant.get("fiche") or {}).get("maturite"):
                text += " " + dire("plantes.maturite_conseil", conseil=plant["fiche"]["maturite"])
    else:
        line = f"{today} : " + dire("plantes.evolution_faux", detail=item["detail"])
        text = dire("plantes.infirme", detail=item["detail"])
    try:
        plantes_photos.append_evolution(plant, line)
    except OSError:
        pass  # le stockage decroche : la decision est quand meme gardee dans le registre
    if pending:
        text += " " + pending_text(plant)
    return text


def confirm(plant):
    return _verdict(plant, True)


def refute(plant):
    return _verdict(plant, False)


def pending_text(plant):
    """La prochaine observation a verifier, avec l'endroit ou chercher."""
    pending = plant.get("a_confirmer") or []
    if not pending:
        return ""
    o = pending[0]
    where = dire("plantes.a_verifier_ou", ou=o["ou"]) if o.get("ou") else ""
    return dire("plantes.a_verifier", detail=o["detail"], ou=where).strip()


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
    text = dire("plantes.ligne", nom=plant["nom"], stade=STAGE_NAMES[plant["stade"]], lieu=place_text(plant))
    if plant.get("a_confirmer"):
        text += ", " + dire("plantes.observations_en_attente", nombre=nombre(len(plant["a_confirmer"]), "observation"))
    return text


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
    if plant["stade"] == "fruits" and fiche and fiche.get("maturite"):
        parts.append(dire("plantes.maturite_conseil", conseil=fiche["maturite"]))
    if plant.get("a_confirmer"):
        parts.append(pending_text(plant))
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
        elif plant.get("stade") == "fruits":
            done += ensure_maturity(plant)
    if done:
        save(plants)
    return done
