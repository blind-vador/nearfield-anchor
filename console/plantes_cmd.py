"""Commande "plantes" : les plantes de l'utilisateur (lib/plantes.py).
Deux modes, Tab passe de l'un a l'autre :

Liste
  fleches haut / bas        parcourir les plantes
  une lettre                aller a la prochaine plante qui commence par elle
  fleches gauche / droite   stade precedent / suivant (constate aujourd'hui)
  I                         fiche : description, stade depuis quand, lieu, conseil
  A                         arrosee aujourd'hui
  R                         rentree / sortie
  E                         changer l'emplacement (memes questions qu'a l'ajout)
  Retour arriere            retire la plante ; Ctrl + Z annule
Ajout ("plantes ajouter")
  taper un nom, meme approximatif, Entree : Gemini propose jusqu'a 5
  plantes, chacune decrite pour la reconnaitre ; haut / bas, Entree choisit.
  Puis : stade, depuis quand, dedans ou dehors, balcon ou jardin, pot ou
  pleine terre, a l'abri de la pluie ou non.
Echap ferme (en ajout, efface d'abord la ligne). F1 : les touches.

Formes directes : "plante basilic arrosée", "plante menthe rentrée",
"plante tomate fleurie".
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import keys  # noqa: E402
import matcher  # noqa: E402
import plantes  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
BACKSPACE = ("\x7f", "\x08")
CTRL_Z, TAB, ESC, ENTER = "\x1a", "\t", "\x1b", ("\r", "\n")
ADD_WORDS = {"ajouter", "ajout", "ajoute", "nouvelle", "nouveau"}
# formes directes : mot dit -> action
QUICK = {"arrosee": "arrosee", "arrose": "arrosee", "arroser": "arrosee", "arrosees": "arrosee",
         "rentree": "dedans", "rentre": "dedans", "rentrees": "dedans", "dedans": "dedans", "interieur": "dedans",
         "sortie": "dehors", "sorti": "dehors", "sorties": "dehors", "dehors": "dehors", "exterieur": "dehors",
         "semee": "seme", "semees": "seme", "germee": "germe", "levee": "germe", "pousses": "pousses",
         "developpee": "developpee", "fleurie": "fleurs", "fleurs": "fleurs", "fleur": "fleurs",
         "fruits": "fruits", "fruit": "fruits", "repos": "repos"}


class Closed(Exception):
    """Echap pendant une question : on abandonne ce qui etait en cours."""


def _ask(key_source, say, out, question, options, start=0):
    """Choix aux fleches haut / bas, Entree valide. Rend l'indice ; Echap -> Closed."""
    i = start
    out(question + "\n")
    say(f"{question} {options[i]}")
    for key in key_source:
        voix.stop()
        if key == ESC:
            raise Closed
        if key in ENTER:
            return i
        if key in (UP, DOWN):
            j = i + (-1 if key == UP else 1)
            if 0 <= j < len(options):
                i = j
                text = options[i]
            else:
                text = (dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")) + " " + options[i]
        elif conseils.is_help(key):
            text = dire("plantes.choix_consigne") + " " + question
        else:
            text = dire("plantes.choix_consigne")
        out(f"[{text}]\n")
        say(text)
    raise Closed


def _yes_no(key_source, say, out, question, default):
    return _ask(key_source, say, out, question, [dire("plantes.oui"), dire("plantes.non")], 0 if default else 1) == 0


def _place(key_source, say, out, plant=None):
    """Questions d'emplacement : (dedans, emplacement, contenant, abri_pluie)."""
    plant = plant or {}
    dedans = _ask(key_source, say, out, dire("plantes.question_dedans"),
                  [dire("plantes.lieu_dedans"), dire("plantes.lieu_dehors")], 0 if plant.get("dedans", False) else 1) == 0
    emplacement = None
    if not dedans:
        emplacement = ("balcon", "jardin")[_ask(key_source, say, out, dire("plantes.question_emplacement"),
                                                [dire("plantes.choix_balcon"), dire("plantes.choix_jardin")],
                                                1 if plant.get("emplacement") == "jardin" else 0)]
    contenant = "pot"
    if emplacement == "jardin":
        contenant = ("pot", "pleine_terre")[_ask(key_source, say, out, dire("plantes.question_contenant"),
                                                 [dire("plantes.en_pot"), dire("plantes.pleine_terre")],
                                                 1 if plant.get("contenant") == "pleine_terre" else 0)]
    abri = True
    if not dedans:
        abri = _yes_no(key_source, say, out, dire("plantes.question_abri"),
                       plant.get("abri_pluie", emplacement == "balcon"))
    return dedans, emplacement, contenant, abri


def _type(key_source, say, out):
    """Saisie avec echo vocal. Rend le texte a l'appui sur Entree, None sur
    Echap avec une ligne vide, TAB sur Tab."""
    typed = ""
    for key in key_source:
        voix.stop()
        if key in ENTER:
            if typed.strip():
                out("\n")
                return typed.strip()
            say(dire("plantes.rien_tape"))
        elif key == TAB:
            return TAB
        elif key in BACKSPACE:
            if typed:
                removed, typed = typed[-1], typed[:-1]
                out("\b \b")
                say(dire("plantes.efface_caractere", caractere=keys.char_name(removed)))
            else:
                say(dire("plantes.ligne_vide"))
        elif key == ESC:
            if not typed:
                return None
            typed = ""
            out("\n")
            say(dire("plantes.ligne_effacee"))
        elif key == UP:
            say(typed if typed.strip() else dire("plantes.ligne_vide"))
        elif conseils.is_help(key):
            say(dire("plantes.consigne"))
        elif len(key) == 1 and key.isprintable():
            typed += key
            out(key)
            say(keys.char_name(key))
        else:
            say(keys.sequence_name(key))
    return None


def _add(key_source, say, out, plants):
    """Mode ajout. Rend la phrase de sortie, ou TAB pour revenir a la liste."""
    say(dire("plantes.mode_ajout"))
    while True:
        text = _type(key_source, say, out)
        if text is None or text == TAB:
            return text
        say(dire("plantes.recherche", texte=text))
        found, reason = plantes.search(text)
        if not found:
            say(reason + " " + dire("plantes.retape"))
            continue
        try:
            options = [dire("plantes.candidat", nom=c["nom"], description=c.get("description") or "") for c in found]
            pick = found[_ask(key_source, say, out, dire("plantes.question_candidat", nombre=nombre(len(found), "plante")), options)]
            stage = plantes.STAGES[_ask(key_source, say, out, dire("plantes.question_stade"),
                                        [plantes.STAGE_NAMES[s] for s in plantes.STAGES], 3)]
            days = plantes.SINCE[_ask(key_source, say, out, dire("plantes.question_depuis"),
                                      [plantes.SINCE_NAMES[k] for k, _ in plantes.SINCE])][1]
            dedans, emplacement, contenant, abri = _place(key_source, say, out)
        except Closed:
            say(dire("plantes.ajout_annule"))
            continue
        plant = plantes.new_plant(pick, stage, days, dedans, emplacement, contenant, abri, plants)
        say(dire("plantes.prepare_fiche", nom=plant["nom"]))
        plant["fiche"] = plantes.make_fiche(plant)
        plants.append(plant)
        if not plantes.save(plants):
            plants.pop()
            say(dire("plantes.nas_inaccessible"))
            continue
        done = dire("plantes.ajoutee", ligne=plantes.line(plant))
        if not plant["fiche"]:
            done += " " + dire("plantes.fiche_plus_tard")
        say(done + " " + dire("plantes.autre_plante"))


def quick(line):
    """ "plante basilic arrosée" -> fait et rend la phrase ; None sinon."""
    words = matcher.normalize(line).split()
    action = next((QUICK[w] for w in words if w in QUICK), None)
    if not action:
        return None
    plants = plantes.load()
    hits = [p for p in plants if matcher.normalize(p["nom"]) in matcher.normalize(line)]
    if not hits:
        return dire("plantes.inconnue_directe") if plants else dire("plantes.aucune")
    plant = max(hits, key=lambda p: len(p["nom"]))
    text = _apply(plant, action)
    if not plantes.save(plants):
        return dire("plantes.nas_inaccessible")
    return text


def _apply(plant, action):
    if action == "arrosee":
        plant["arrosee_le"] = plantes._today()
        return dire("plantes.note_arrosee", nom=plant["nom"])
    if action in ("dedans", "dehors"):
        plant["dedans"] = action == "dedans"
        if plant["dedans"]:
            plant["abri_pluie"] = True
        elif not plant.get("emplacement"):
            plant["emplacement"] = "balcon"
        return dire("plantes.note_lieu", nom=plant["nom"], lieu=plantes.place_text(plant))
    plantes.set_stage(plant, action)
    return dire("plantes.note_stade", nom=plant["nom"], stade=plantes.STAGE_NAMES[action])


def run(line, key_source, say, out):
    direct = quick(line)
    if direct:
        return direct
    plants = plantes.load()
    adding = bool(set(matcher.normalize(line).split()) & ADD_WORDS) or not plants
    i = 0
    undo = []
    title = dire("plantes.titre", nombre=nombre(len(plants), "plante")) if plants else dire("plantes.titre_vide")
    intro = conseils.intro("plantes", title, dire("plantes.consigne"))
    out(intro + "\n")
    say(intro)
    if not adding and plants:
        say(plantes.line(plants[0]))

    keys_iter = iter(key_source)
    while True:
        if adding:
            result = _add(keys_iter, say, out, plants)
            if result is None:
                return dire("plantes.fermee")
            adding = False
            i = min(i, max(len(plants) - 1, 0))
            say(dire("plantes.mode_liste") + " " + (plantes.line(plants[i]) if plants else dire("plantes.aucune")))
        key = next(keys_iter, None)
        if key is None:
            return dire("plantes.fermee")
        voix.stop()
        text = None
        if key == ESC:
            return dire("plantes.fermee")
        if key == TAB:
            adding = True
            continue
        if conseils.is_help(key):
            text = dire("plantes.consigne")
        elif key == CTRL_Z:
            if not undo:
                text = dire("plantes.rien_a_annuler")
            else:
                at, back = undo.pop()
                plants.insert(at, back)
                if not plantes.save(plants):
                    plants.pop(at)
                    undo.append((at, back))
                    text = dire("plantes.nas_inaccessible")
                else:
                    i = at
                    text = dire("plantes.remise", nom=back["nom"])
        elif not plants:
            text = dire("plantes.aucune")
        elif key in (UP, DOWN):
            j = i + (-1 if key == UP else 1)
            if 0 <= j < len(plants):
                i = j
                text = plantes.line(plants[i])
            else:
                text = (dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")) + " " + plantes.line(plants[i])
        elif key in (LEFT, RIGHT):
            k = plantes.STAGES.index(plants[i]["stade"]) + (1 if key == RIGHT else -1)
            if 0 <= k < len(plantes.STAGES):
                text = _apply(plants[i], plantes.STAGES[k])
                if not plantes.save(plants):
                    text = dire("plantes.nas_inaccessible")
            else:
                text = dire("plantes.stade_bout")
        elif key in ("i", "I"):
            import plantes_calendrier
            text = plantes.card(plants[i])
            try:
                temp = None if plants[i].get("dedans") else plantes_calendrier.outdoor_mean_temp()
                text = (text + " " + plantes_calendrier.describe(plants[i], temp)).strip()
            except Exception as exc:  # pas de reseau : la fiche suffit
                print(f"[plantes] calendrier : {exc!r}")
        elif key in ("a", "A"):
            text = _apply(plants[i], "arrosee")
            if not plantes.save(plants):
                text = dire("plantes.nas_inaccessible")
        elif key in ("r", "R"):
            text = _apply(plants[i], "dehors" if plants[i].get("dedans") else "dedans")
            if not plantes.save(plants):
                text = dire("plantes.nas_inaccessible")
        elif key in ("e", "E"):
            try:
                plants[i]["dedans"], plants[i]["emplacement"], plants[i]["contenant"], plants[i]["abri_pluie"] = \
                    _place(keys_iter, say, out, plants[i])
                text = dire("plantes.note_lieu", nom=plants[i]["nom"], lieu=plantes.place_text(plants[i]))
                if not plantes.save(plants):
                    text = dire("plantes.nas_inaccessible")
            except Closed:
                text = dire("plantes.inchange") + " " + plantes.line(plants[i])
        elif key in BACKSPACE:
            gone = plants.pop(i)
            if not plantes.save(plants):
                plants.insert(i, gone)
                text = dire("plantes.nas_inaccessible")
            else:
                undo.append((i, gone))
                i = min(i, max(len(plants) - 1, 0))
                text = dire("plantes.retiree", nom=gone["nom"]) + " " + (plantes.line(plants[i]) if plants else dire("plantes.aucune"))
        elif len(key) == 1 and key.isalnum():
            letter = matcher.normalize(key)
            order = plants[i + 1:] + plants[:i + 1]
            hit = next((p for p in order if matcher.normalize(p["nom"]).startswith(letter)), None)
            if hit:
                i = plants.index(hit)
                text = plantes.line(hit)
            else:
                text = dire("plantes.aucune_lettre", lettre=keys.char_name(key))
        else:
            text = dire("plantes.touches")
        if text:
            out(f"[{text}]\n")
            say(text)
