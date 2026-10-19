"""Explorateur des fichiers pieces telecharges (preparations STL/G-code).

  fleches haut / bas     fichier precedent / suivant
                         (annonce : titre + description Gemini)
  fleches gauche / droite  action rapide precedente / suivante
  Entree                 execute l'action (defaut : ouvrir la fiche)
  Echap                  ferme. F1 : aide.

Actions : ouvrir, imprimer, supprimer.
"""
import os
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import pieces  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
ESC, ENTER = "\x1b", ("\r", "\n")

ACTIONS = ("ouvrir", "imprimer", "supprimer")


def _action_label(action):
    return dire(f"pieces.pret_action_{action}")


def _announce_item(items, index, action):
    """Fichier courant + action selectionnee."""
    item = items[index]
    ligne = pieces.ligne_pret(item, index=index, total=len(items))
    return ligne + " " + dire("pieces.pret_action_courante", action=_action_label(action))


def _parcourir_fiche(fiche, key_source, say, out):
    """Lecture seule de la fiche memorisee (sections), Esc revient."""
    sections = pieces.sections_fiche(fiche, include_fine=True)
    flat = pieces.flatten_sections(sections)
    if not flat:
        return dire("pieces.fiche_vide")

    def first_of(sid):
        for i, (si, _, _) in enumerate(flat):
            if sections[si]["id"] == sid:
                return i
        return 0

    # Preferer la fiche fine Gemini si presente
    pos = first_of("fine") if any(s["id"] == "fine" for s in sections) else 0

    def speak_at(i, announce_section=False):
        si, ii, text = flat[i]
        if announce_section or ii == 0:
            return sections[si]["titre"] + " " + text
        return text

    intro = dire("pieces.pret_ouvert", titre=fiche.get("title") or "?")
    first = speak_at(pos, announce_section=True)
    opening = f"{intro} {first}".strip()
    out(opening + "\n")
    say(opening)

    for key in key_source:
        voix.stop()
        if key == ESC:
            return dire("pieces.pret_retour_liste")
        if conseils.is_help(key):
            text = dire("pieces.pret_consigne_fiche")
        elif key == UP:
            if pos <= 0:
                text = dire("commun.debut_liste") + " " + speak_at(pos)
            else:
                pos -= 1
                crossed = flat[pos][0] != flat[pos + 1][0]
                text = speak_at(pos, announce_section=crossed)
        elif key == DOWN:
            if pos >= len(flat) - 1:
                text = dire("commun.fin_liste") + " " + speak_at(pos)
            else:
                pos += 1
                crossed = flat[pos][0] != flat[pos - 1][0]
                text = speak_at(pos, announce_section=crossed)
        else:
            text = dire("pieces.pret_consigne_fiche")
        out(f"[{text}]\n")
        say(text)
    return dire("pieces.pret_retour_liste")


def _imprimer(item, say, out):
    """Envoie un G-code vers OctoPrint et propose de lancer (sans forcer si offline)."""
    gcodes = pieces.gcodes_pret(item)
    if not gcodes:
        return dire("pieces.pret_imprimer_besoin_gcode")
    try:
        import octoprint as op
    except ImportError:
        return dire("pieces.pret_imprimer_echec")
    try:
        e = op.etat()
    except op.Erreur as exc:
        return dire("pieces.pret_imprimer_echec")
    if not e.get("connecte"):
        # Televerse quand meme si OctoPrint repond, sinon message
        pass
    f = gcodes[0]
    try:
        info = op.televerser(f["chemin"], dossier=f"pieces/{item['source']}_{item['id']}", selectionner=True)
    except Exception:
        return dire("pieces.pret_imprimer_echec")
    # Ne lance pas automatiquement : l'utilisateur a souvent l'imprimante ouverte/fermee
    try:
        e = op.etat()
        if e.get("connecte") and not (e.get("flags") or {}).get("printing"):
            # select only already done ; ask via phrase that it's ready
            return dire("pieces.pret_imprimer_pret", nom=info.get("nom") or f["nom"])
        if not e.get("connecte"):
            return dire("pieces.pret_imprimer_stocke", nom=info.get("nom") or f["nom"])
    except Exception:
        return dire("pieces.pret_imprimer_stocke", nom=info.get("nom") or f["nom"])
    return dire("pieces.pret_imprimer_pret", nom=info.get("nom") or f["nom"])


def _supprimer(item):
    if pieces.delete_pret(item):
        return dire("pieces.pret_supprime", titre=item.get("title") or item.get("id"))
    return dire("pieces.pret_supprime_echec")


def run(key_source, say, out, read_line=None):
    items = pieces.list_prets()
    if not items:
        text = dire("pieces.prets_vide")
        out(text + "\n")
        say(text)
        return text

    index = 0
    action_i = 0  # ouvrir
    intro = conseils.intro(
        "pieces_prets",
        dire("pieces.prets_titre", n=nombre(len(items), "fichier préparé", "fichiers préparés")),
        dire("pieces.prets_consigne"),
    )
    line = intro + " " + _announce_item(items, index, ACTIONS[action_i])
    out(line + "\n")
    say(line)

    for key in key_source:
        voix.stop()
        if key == ESC:
            text = dire("pieces.prets_ferme")
            out(text + "\n")
            say(text)
            return text
        if conseils.is_help(key):
            text = dire("pieces.prets_consigne")
        elif key == UP:
            if index <= 0:
                text = dire("commun.debut_liste") + " " + pieces.ligne_pret(items[index], index, len(items))
            else:
                index -= 1
                action_i = 0
                text = pieces.ligne_pret(items[index], index, len(items))
        elif key == DOWN:
            if index >= len(items) - 1:
                text = dire("commun.fin_liste") + " " + pieces.ligne_pret(items[index], index, len(items))
            else:
                index += 1
                action_i = 0
                text = pieces.ligne_pret(items[index], index, len(items))
        elif key == LEFT:
            action_i = (action_i - 1) % len(ACTIONS)
            text = dire("pieces.pret_action_courante", action=_action_label(ACTIONS[action_i]))
        elif key == RIGHT:
            action_i = (action_i + 1) % len(ACTIONS)
            text = dire("pieces.pret_action_courante", action=_action_label(ACTIONS[action_i]))
        elif key in ENTER:
            action = ACTIONS[action_i]
            item = items[index]
            if action == "ouvrir":
                say(dire("pieces.pret_ouverture"))
                out(dire("pieces.pret_ouverture") + "\n")
                ret = _parcourir_fiche(item["fiche"], key_source, say, out)
                items = pieces.list_prets()
                if not items:
                    text = dire("pieces.prets_vide")
                    out(text + "\n")
                    say(text)
                    return text
                index = min(index, len(items) - 1)
                action_i = 0
                text = ret + " " + _announce_item(items, index, ACTIONS[action_i])
            elif action == "imprimer":
                if read_line is None:
                    text = dire("pieces.pret_imprimer_echec")
                else:
                    import imprimante_print
                    text = imprimante_print.wizard(item, key_source, say, out, read_line)
            elif action == "supprimer":
                text = _supprimer(item)
                items = pieces.list_prets()
                if not items:
                    out(text + "\n")
                    say(text)
                    empty = dire("pieces.prets_vide")
                    out(empty + "\n")
                    say(empty)
                    return text
                index = min(index, len(items) - 1)
                action_i = 0
                text = text + " " + _announce_item(items, index, ACTIONS[action_i])
            else:
                text = dire("pieces.prets_consigne")
        else:
            text = dire("pieces.prets_consigne")
        out(f"[{text}]\n")
        say(text)
    return dire("pieces.prets_ferme")
