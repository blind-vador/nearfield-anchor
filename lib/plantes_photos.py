"""Photos des plantes (2026-10-07, etape 5).

Travail en cours, sur nd1 (partage "server", dossier server/plantes) :
  <plante>/            un dossier par plante, cree par le Pi ; y deposer les
                       photos (iPhone : Fichiers, ou depuis le Mac)
  <plante>/description_actuelle.txt
                       derniere description, en texte : la reference pour
                       comparer la photo suivante
  a_identifier/        photo d'une plante inconnue : Gemini dit ce que c'est
Archives, sur nd2 (partage "stockage", storage/plantes) :
  <plante>/evolution.txt
                       UNE liste datee des evolutions, une ligne par photo,
                       a laquelle s'ajoute chaque nouvelle observation
  <plante>/photos/     photos gardees a la main (touche G du dialogue
                       plantes) avec leur description : evenements marquants

Agregation (demande utilisateur 2026-10-07) : la description d'une photo
sert de reference jusqu'a la photo suivante ; une fois l'evolution ajoutee
a la liste, l'ancienne description est remplacee. Gemini recoit toujours
la meme quantite : la photo, la description precedente et les EVOLUTION_CONTEXT
dernieres lignes de la liste -- jamais des mois d'historique.
Les photos du dossier de travail sont effacees apres PHOTO_KEEP_DAYS ; une
photo gardee (touche G) part avec la description actuelle.
"""
import datetime
import json
import os
import re
import shutil
import time
import unicodedata

import gemini
import plantes
from phrases import dire

WORK_DIR = "/media/nas/nd1/server/plantes"
ARCHIVE_DIR = "/media/nas/nd2/storage/plantes"
UNKNOWN = "a_identifier"
CURRENT = "description_actuelle.txt"
STATE = ".suivi.json"            # photos deja traitees, dans chaque dossier
EVOLUTION = "evolution.txt"
EVOLUTION_CONTEXT = 5
PHOTO_KEEP_DAYS = 30
MAX_PER_RUN = 6                  # Gemini : au plus 6 photos par passage
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".heic": "image/heic", ".heif": "image/heif"}
README = """Photos des plantes (nearfield anchor, deepspace9)

Un dossier par plante, cree par le Pi. Deposer une photo dans le dossier
de la plante : dans l'heure, le Pi la decrit, la compare a la description
precedente et ajoute l'evolution, datee, a la liste gardee sur le disque de
stockage (storage/plantes/<plante>/evolution.txt). Commande console
"plantes photos" pour ne pas attendre.
Plante inconnue : deposer la photo dans a_identifier.
Les photos d'ici sont effacees apres 30 jours ; pour en garder une, touche
G dans le dialogue plantes (elle part dans storage/plantes/<plante>/photos).
"""

DESCRIBE_PROMPT = """Photo d'une plante en pot ou au jardin, pour une personne aveugle qui la cultive : {nom} ({latin}).
Pour la reconnaitre : {portrait}
Le pot peut contenir PLUSIEURS plantes, ou d'autres pots etre autour : repere {nom} grace a ce portrait et
decris-la, elle d'abord ; cite les voisines en une phrase, sans les decrire en detail. Ne dis qu'elle est
introuvable que si vraiment aucune plante de la photo ne lui ressemble.
Stade note jusqu'ici : {stade}. {lieu}.
Description precedente ({date_precedente}) : {precedente}
Dernieres evolutions notees :
{evolutions}

Reponds en francais correctement accentue, en JSON strict :
{{
 "description": "etat actuel, facon texte alternatif riche, 4 a 6 phrases : ou elle est sur la photo et avec quelles voisines, taille et port, tiges, feuilles (nombre approximatif, forme, aspect, couleur avec un repere tactile ou de comparaison), fleurs ou boutons, fruits, pot et support",
 "evolution": "ce qui a change depuis la description precedente, une phrase ; si pas de description precedente : « Premier état noté. »",
 "stade": "seme" | "germe" | "pousses" | "developpee" | "fleurs" | "fruits" | "repos",
 "problemes": "signes visibles de maladie, ravageur, manque ou exces d'eau, brulure ; chaine vide si rien"
}}
N'invente rien que la photo ne montre pas. Si la photo ne montre pas cette plante, dis-le dans "problemes"."""

IDENTIFY_PROMPT = """Photo d'une plante a identifier, pour une personne aveugle. Reponds en JSON strict :
{{"nom": "nom courant le plus probable", "latin": "nom latin", "certitude": "forte" | "moyenne" | "faible",
 "description": "texte alternatif, 2 phrases : port, feuilles, fleurs ou fruits", "conseil": "une phrase d'entretien"}}"""


def slug(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "plante"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def ensure_folders(plants):
    """Un dossier par plante (et a_identifier), plus le mode d'emploi."""
    os.makedirs(os.path.join(WORK_DIR, UNKNOWN), exist_ok=True)
    for p in plants:
        os.makedirs(os.path.join(WORK_DIR, slug(p["nom"])), exist_ok=True)
    readme = os.path.join(WORK_DIR, "LISEZ-MOI.txt")
    if not os.path.exists(readme):
        _write(readme, README)


def _photos(folder):
    """[(chemin, date de modification)] des photos du dossier, des plus anciennes aux plus recentes."""
    out = []
    for entry in os.scandir(folder):
        if entry.is_file() and not entry.name.startswith(".") and os.path.splitext(entry.name)[1].lower() in MIME:
            out.append((entry.path, entry.stat().st_mtime))
    return sorted(out, key=lambda x: x[1])


def _ask_photo(prompt, path):
    with open(path, "rb") as f:
        data = f.read()
    answer, reason = gemini.ask(prompt, temperature=0.2, jpeg=data, mime=MIME[os.path.splitext(path)[1].lower()],
                                json_mode=True, timeout=90)
    if answer is None:
        return None, reason
    try:
        result = json.loads(answer)
    except ValueError:
        return None, dire("gemini.reponse_gemini_inutilisable")
    return (result, None) if isinstance(result, dict) else (None, dire("gemini.reponse_gemini_inutilisable"))


def evolution_lines(plant, last=None):
    try:
        with open(os.path.join(ARCHIVE_DIR, slug(plant["nom"]), EVOLUTION), encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError:
        return []
    return lines[-last:] if last else lines


def _analyse(plant, path, stamp):
    """Une photo d'une plante connue. Rend [phrases a dire]."""
    folder = os.path.dirname(path)
    current = os.path.join(folder, CURRENT)
    try:
        with open(current, encoding="utf-8") as f:
            previous = f.read().strip()
    except OSError:
        previous = ""
    previous_date, _, previous_text = previous.partition(" : ")
    result, reason = _ask_photo(DESCRIBE_PROMPT.format(
        nom=plant["nom"], latin=plant.get("latin") or "?", stade=plantes.STAGE_NAMES[plant["stade"]],
        portrait=plant.get("description") or "pas de portrait",
        lieu=plantes.place_text(plant).capitalize(), date_precedente=previous_date or "aucune",
        precedente=previous_text or "aucune, c'est la premiere photo",
        evolutions="\n".join(evolution_lines(plant, EVOLUTION_CONTEXT)) or "aucune"), path)
    if result is None:
        print(f"[plantes_photos] {path} : {reason}")
        return None
    day = datetime.date.fromtimestamp(stamp).isoformat()
    description = str(result.get("description") or "").strip()
    evolution = str(result.get("evolution") or "").strip()
    problems = str(result.get("problemes") or "").strip()
    # 1) la liste datee des evolutions, sur le stockage
    archive = os.path.join(ARCHIVE_DIR, slug(plant["nom"]))
    os.makedirs(archive, exist_ok=True)
    line = f"{day} : {evolution}" + (f" Problème : {problems}" if problems else "")
    with open(os.path.join(archive, EVOLUTION), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    # 2) la nouvelle description remplace l'ancienne, desormais agregee
    _write(current, f"{day} : {description}\n")
    if previous_text:  # dit en entier (2026-10-07 : "ne decrit pas l'image")
        said = [dire("plantes.photo_nouvelle", nom=plant["nom"], description=description, evolution=evolution)]
    else:
        said = [dire("plantes.photo_premiere", nom=plant["nom"], description=description)]
    if problems:
        said.append(dire("plantes.photo_probleme", probleme=problems))
    stage = result.get("stade")
    if stage in plantes.STAGES and plantes.STAGES.index(stage) > plantes.STAGES.index(plant["stade"]) \
            and plant["stade"] != "repos" and stage != "repos":
        plant["_stade_photo"] = stage  # la photo montre un stade plus avance (enregistre par run_once)
        said.append(dire("plantes.photo_stade", nom=plant["nom"], stade=plantes.STAGE_NAMES[stage]))
    return said


def _identify(path):
    result, reason = _ask_photo(IDENTIFY_PROMPT, path)
    if result is None:
        print(f"[plantes_photos] {path} : {reason}")
        return None
    _write(path + ".txt", json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    return [dire("plantes.photo_identifiee", nom=result.get("nom") or "?", latin=result.get("latin") or "?",
                 certitude=result.get("certitude") or "?", description=result.get("description") or "",
                 conseil=result.get("conseil") or "")]


def _clean(folder, keep_days=PHOTO_KEEP_DAYS):
    limit = time.time() - keep_days * 86400
    for path, mtime in _photos(folder):
        if mtime < limit:
            for f in (path, path + ".txt"):
                try:
                    os.remove(f)
                except OSError:
                    pass


def run_once():
    """Traite les nouvelles photos. Rend [phrases a dire] ; [] si rien."""
    plants = plantes.load()
    ensure_folders(plants)
    said = []
    stages = {}
    budget = MAX_PER_RUN
    folders = [(p, os.path.join(WORK_DIR, slug(p["nom"]))) for p in plants] + [(None, os.path.join(WORK_DIR, UNKNOWN))]
    for plant, folder in folders:
        state_path = os.path.join(folder, STATE)
        state = _read_json(state_path)
        done = set(state.get("traitees") or [])
        for path, mtime in _photos(folder):
            name = os.path.basename(path)
            if name in done or budget <= 0:
                continue
            if time.time() - mtime < 60:
                continue  # copie peut-etre en cours
            budget -= 1
            result = _identify(path) if plant is None else _analyse(plant, path, mtime)
            if result is None:
                continue  # Gemini indisponible : nouvel essai au prochain passage
            said += result
            done.add(name)
            if plant is not None and plant.get("_stade_photo"):
                stages[plant["id"]] = plant.pop("_stade_photo")
        present = {os.path.basename(p) for p, _ in _photos(folder)}
        state["traitees"] = sorted(done & present) if present else []
        _write(state_path, json.dumps(state, ensure_ascii=False))
        _clean(folder)
    if stages:  # sur la liste relue : une modification faite depuis la console n'est pas ecrasee
        fresh = plantes.load()
        for p in fresh:
            if p["id"] in stages:
                plantes.set_stage(p, stages[p["id"]])
        plantes.save(fresh)
    return said


def keep_latest(plant):
    """Touche G : garde la derniere photo analysee de la plante (et sa
    description) dans storage/plantes/<plante>/photos. Rend la phrase."""
    folder = os.path.join(WORK_DIR, slug(plant["nom"]))
    try:
        done = set(_read_json(os.path.join(folder, STATE)).get("traitees") or [])
        analysed = [(p, m) for p, m in _photos(folder) if os.path.basename(p) in done]
    except OSError:
        return dire("plantes.nas_inaccessible")
    if not analysed:
        return dire("plantes.photo_aucune", nom=plant["nom"])
    path, mtime = analysed[-1]  # la plus recente analysee : sa description est la description actuelle
    dest_dir = os.path.join(ARCHIVE_DIR, slug(plant["nom"]), "photos")
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.datetime.fromtimestamp(mtime)
    dest = os.path.join(dest_dir, f"{stamp:%Y-%m-%d_%Hh%M}{os.path.splitext(path)[1].lower()}")
    if os.path.exists(dest):
        return dire("plantes.photo_deja_gardee")
    shutil.copy2(path, dest)
    try:
        shutil.copy2(os.path.join(folder, CURRENT), dest + ".txt")
    except OSError:
        pass
    return dire("plantes.photo_gardee", nom=plant["nom"])
