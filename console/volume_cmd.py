"""Commande "volume" (2026-10-22, demande utilisateur) : le volume general,
puis un curseur par veille et par sorte de son (lib/volumes.py). Chaque
changement est garde et s'entend tout de suite, meme au milieu d'un son.

  fleches haut / bas         curseur precedent / suivant
  Tab / Maj+Tab              veille suivante / precedente
  fleches droite / gauche    plus fort / moins fort, de 5
  page precedente / suivante plus fort / moins fort, de 20
  debut / fin                silence / 100 pour cent
  Retour arriere             remet a 100 pour cent
  Ctrl + Z                   annule le dernier changement
  Echap ferme. F1 : les touches.

"volume 60" regle directement le volume general.
"""
import re
import sys


import conseils  # noqa: E402
import keys  # noqa: E402
import voix  # noqa: E402
import volumes  # noqa: E402
from phrases import dire  # noqa: E402

UP, DOWN, LEFT, RIGHT = "\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C"
PGUP, PGDN = "\x1b[5~", "\x1b[6~"
HOME, END = ("\x1b[1~", "\x1b[H"), ("\x1b[4~", "\x1b[F")
TAB, SHIFT_TAB = "\t", "\x1b[Z"
BACKSPACE = ("\x7f", "\x08")
CTRL_Z, ESC = "\x1a", "\x1b"
GENERAL = ("general", None)
PAS, GRAND_PAS = 5, 20


def _curseurs():
    """[(source, canal, nom de la veille, nom du curseur)] dans l'ordre parcouru."""
    items = [(None, None, None, dire("volume.general"))]
    for source, nom, canaux in volumes.VEILLES:
        items.append((source, volumes.VEILLE, nom, dire("volume.de_la_veille")))
        if len(canaux) > 1:
            items += [(source, canal, nom, libelle) for canal, libelle in canaux]
    return items


def _valeur(item):
    source, canal = item[0], item[1]
    if source is None:
        return volumes.general()
    return volumes.pct(source, canal)


def _regler(item, valeur):
    source, canal = item[0], item[1]
    if source is None:
        return volumes.regler_general(valeur)
    return volumes.regler(source, canal, valeur)


def _pour_cent(v):
    return dire("volume.inconnu") if v is None else dire("volume.pour_cent", v=v)


def _texte(item, avec_veille=True):
    source, _, veille, libelle = item
    nom = libelle if source is None or not avec_veille else f"{veille.capitalize()}, {libelle}"
    return f"{nom} : {_pour_cent(_valeur(item))}."


def run(line, key_source, say, out):
    m = re.search(r"\b(\d{1,3})\b", line or "")
    if m:  # "volume 60" : le general, sans dialogue
        v = volumes.regler_general(int(m.group(1)))
        return dire("volume.general_regle", v=_pour_cent(v))
    items = _curseurs()
    i = 0
    annuler = []
    consigne = dire("volume.consigne")
    intro = conseils.intro("volume", dire("volume.titre"), consigne)
    out(intro + "\n")
    say(f"{intro} {_texte(items[0])}")
    for key in key_source:
        if key == keys.WAKE:
            continue
        voix.stop()
        item = items[i]
        text = None
        if conseils.is_help(key):
            text = consigne
        elif key == ESC:
            break
        elif key in (UP, DOWN):
            j = i + (-1 if key == UP else 1)
            if 0 <= j < len(items):
                i = j
                text = _texte(items[i])
            else:
                bord = dire("commun.debut_liste") if j < 0 \
                    else dire("commun.fin_liste")
                text = f"{bord} {_texte(item)}"
        elif key in (TAB, SHIFT_TAB):
            pas = 1 if key == TAB else -1
            j = i
            while True:
                j = (j + pas) % len(items)
                if items[j][1] in (volumes.VEILLE, None) or j == i:
                    break
            i = j
            text = _texte(items[i])
        elif key in (LEFT, RIGHT, PGUP, PGDN, CTRL_Z) or key in HOME or key in END or key in BACKSPACE:
            if key == CTRL_Z:
                if not annuler:
                    text = dire("volume.rien_a_annuler")
                else:
                    k, avant = annuler.pop()
                    i = k
                    _regler(items[i], avant)
                    text = dire("volume.annule") + " " + _texte(items[i])
            else:
                avant = _valeur(item)
                base = 100 if avant is None else avant
                if key in HOME:
                    nouveau = 0
                elif key in END or key in BACKSPACE:
                    nouveau = 100
                else:
                    nouveau = base + {RIGHT: PAS, LEFT: -PAS, PGUP: GRAND_PAS, PGDN: -GRAND_PAS}[key]
                garde = _regler(item, nouveau)
                if avant is not None and garde != avant:
                    annuler.append((i, avant))
                if garde == avant and nouveau != avant:
                    text = dire("volume.limite") + " " + _pour_cent(garde)
                else:
                    text = _pour_cent(garde)
        if text:
            out(text + "\n")
            say(text)
    return dire("volume.ferme")
