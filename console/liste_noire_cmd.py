"""Commande "liste noire" : stations DAB+ interdites (lib/dab_liste.py).
Deux modes, Tab passe de l'un a l'autre :

Liste ("liste noire", "liste noire supprimer")
  fleches haut / bas        parcourir les entrees
  une lettre                aller a la prochaine entree qui commence par elle
  Retour arriere            efface l'entree entiere
  Ctrl + A                  selectionne tout ; Retour arriere efface alors tout
  Ctrl + Z                  annule le dernier effacement
Edition ("liste noire editer", "liste noire ajouter")
  taper le nom de la station (echo vocal), Entree l'ajoute ; fleche haut
  relit la ligne ; Retour arriere efface le dernier caractere
Echap ferme (en edition, efface d'abord la ligne en cours). F1 : les touches.

Chaque changement est enregistre tout de suite sur le NAS ; le scanner DAB+
relit la liste a chaque station, rien a relancer.
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import dab_liste  # noqa: E402
import keys  # noqa: E402
import matcher  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN = "\x1b[A", "\x1b[B"
BACKSPACE = ("\x7f", "\x08")
CTRL_A, CTRL_Z, TAB, ESC = "\x01", "\x1a", "\t", "\x1b"
EDIT_WORDS = {"editer", "edition", "ajouter", "ajout", "modifier", "ajoute"}


def _line(entry):
    name, origin = entry
    return dire("liste_noire.ligne_ajoutee" if origin == "ajoutee" else "liste_noire.ligne_base", nom=name)


def _seen(name):
    """Ensemble ou cette station a deja ete captee, ou None."""
    p = dab_liste.plain(name)
    return next((channel for station, channel in dab_liste.seen_stations().items()
                 if dab_liste.plain(station) == p), None)


def _add(name):
    status, entry = dab_liste.add(name)
    if status == "deja":
        return dire("liste_noire.deja", nom=name, entree=entry)
    if status == "nas":
        return dire("liste_noire.nas_inaccessible")
    channel = _seen(name)
    if channel:
        return dire("liste_noire.ajoutee_connue", nom=name, canal=channel)
    return dire("liste_noire.ajoutee_inconnue", nom=name)


def run(line, key_source, say, out):
    editing = bool(set(matcher.normalize(line).split()) & EDIT_WORDS)
    items = dab_liste.entries()
    i = 0
    selected = False
    undo = []          # derniers effacements, pour Ctrl + Z
    typed = ""
    title = dire("liste_noire.titre", nombre=nombre(len(items), "entrée", "entrées"))
    intro = conseils.intro("liste_noire", title, dire("liste_noire.consigne"))
    out(intro + "\n")
    if editing:
        say(f"{intro} {dire('liste_noire.mode_edition')}")
    else:
        say(f"{intro} {_line(items[0]) if items else dire('liste_noire.vide')}")

    for key in key_source:
        voix.stop()
        text = None
        if conseils.is_help(key):
            text = dire("liste_noire.consigne")
        elif key == TAB:
            editing = not editing
            selected = False
            typed = ""
            items = dab_liste.entries()
            i = min(i, max(len(items) - 1, 0))
            if editing:
                text = dire("liste_noire.mode_edition")
            else:
                text = dire("liste_noire.mode_liste") + " " + (_line(items[i]) if items else dire("liste_noire.vide"))
        elif editing:
            if key in ("\r", "\n"):
                if typed.strip():
                    text = _add(typed)
                    typed = ""
                    out("\n")
                else:
                    text = dire("liste_noire.rien_a_ajouter")
            elif key in BACKSPACE:
                if typed:
                    removed, typed = typed[-1], typed[:-1]
                    out("\b \b")
                    text = dire("liste_noire.efface_caractere", caractere=keys.char_name(removed))
                else:
                    text = dire("liste_noire.ligne_vide")
            elif key == ESC:
                if not typed:
                    return dire("liste_noire.fermee")
                typed = ""
                out("\n")
                text = dire("liste_noire.ligne_effacee")
            elif key == UP:
                text = typed if typed.strip() else dire("liste_noire.ligne_vide")
            elif len(key) == 1 and key.isprintable():
                typed += key
                out(key)
                say(keys.char_name(key))
            else:
                text = keys.sequence_name(key)
        else:
            if key == ESC:
                return dire("liste_noire.fermee")
            if key == CTRL_A:
                selected = bool(items)
                text = dire("liste_noire.tout_selectionne", nombre=nombre(len(items), "entrée", "entrées")) \
                    if items else dire("liste_noire.vide")
            elif key in BACKSPACE:
                if not items:
                    text = dire("liste_noire.vide")
                else:
                    gone = list(items) if selected else [items[i]]
                    if not dab_liste.remove(gone):
                        text = dire("liste_noire.nas_inaccessible")
                    else:
                        undo.append(gone)
                        items = dab_liste.entries()
                        i = min(i, max(len(items) - 1, 0)) if not selected else 0
                        if selected:
                            text = dire("liste_noire.tout_efface", nombre=nombre(len(gone), "entrée", "entrées"))
                        else:
                            text = dire("liste_noire.efface", nom=gone[0][0])
                        text += " " + (_line(items[i]) if items else dire("liste_noire.vide"))
                    selected = False
            elif key == CTRL_Z:
                if not undo:
                    text = dire("liste_noire.rien_a_annuler")
                elif not dab_liste.restore(undo[-1]):
                    text = dire("liste_noire.nas_inaccessible")
                else:
                    back = undo.pop()
                    items = dab_liste.entries()
                    i = next((k for k, e in enumerate(items) if e[0] == back[0][0]), 0)
                    text = dire("liste_noire.restaure", nombre=nombre(len(back), "entrée", "entrées")) + " " + _line(items[i])
            else:
                if selected:
                    selected = False
                    say(dire("liste_noire.selection_annulee"))
                if key in (UP, DOWN):
                    if not items:
                        text = dire("liste_noire.vide")
                    else:
                        j = i + (-1 if key == UP else 1)
                        if 0 <= j < len(items):
                            i = j
                            text = _line(items[i])
                        else:
                            edge = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
                            text = edge + " " + _line(items[i])
                elif len(key) == 1 and key.isalnum():
                    letter = dab_liste.plain(key)
                    order = items[i + 1:] + items[:i + 1]
                    hit = next((e for e in order if dab_liste.plain(e[0]).startswith(letter)), None)
                    if hit:
                        i = items.index(hit)
                        text = _line(hit)
                    else:
                        text = dire("liste_noire.aucune_lettre", lettre=keys.char_name(key))
                else:
                    text = dire("liste_noire.touches")
        if text:
            out(f"[{text}]\n")
            say(text)
    return dire("liste_noire.fermee")
