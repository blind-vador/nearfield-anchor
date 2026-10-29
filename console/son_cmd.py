"""Commande "son" / "tuto" (2026-10-30, demande utilisateur) : TUTORIEL des
sons. On parcourt le catalogue (lib/sons.py), groupe par veille, et on ECOUTE
chaque son pour les apprendre -- chacun annonce par un nom court et une breve
description (phrases "sons.evenement_...").

Soeur de la commande "sons" : celle-ci APPREND (on ecoute), l'autre REGLE
(son et phrase, son seul, ou rien). On reutilise ses aides (liste, lecture).

  fleches haut / bas   son precedent / suivant
  Tab / Maj+Tab        groupe suivant / precedent
  Entree               ecouter le son (une touche l'arrete)
  Echap ferme. F1 : les touches.
"""
import conseils  # noqa: E402
import keys  # noqa: E402
import sons_cmd  # noqa: E402  -- _items, _ecouter, _arreter_ecoute
import voix  # noqa: E402
from phrases import dire  # noqa: E402

UP, DOWN = "\x1b[A", "\x1b[B"
TAB, SHIFT_TAB = "\t", "\x1b[Z"
ENTREE = ("\r", "\n")
ESC = "\x1b"


def _texte(item, avec_groupe=False):
    """Nom court + breve description du son ; precede du groupe si on en change."""
    evenement, groupe = item
    nom = dire(f"sons.evenement_{evenement}")
    if avec_groupe:
        return f"{dire(f'sons.groupe_{groupe}').capitalize()}, {nom}"
    return nom


def run(line, key_source, say, out):
    items = sons_cmd._items()
    i = 0
    consigne = dire("son.consigne")
    intro = conseils.intro("son", dire("son.titre"), consigne)
    out(intro + "\n")
    say(f"{intro} {_texte(items[0], True)}")
    for key in key_source:
        if key == keys.WAKE:
            continue
        voix.stop()
        sons_cmd._arreter_ecoute()
        text = None
        if conseils.is_help(key):
            text = consigne
        elif key == ESC:
            break
        elif key in (UP, DOWN):
            j = i + (-1 if key == UP else 1)
            if 0 <= j < len(items):
                nouveau_groupe = items[j][1] != items[i][1]
                i = j
                text = _texte(items[i], nouveau_groupe)
            else:
                bord = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
                text = f"{bord} {_texte(items[i])}"
        elif key in (TAB, SHIFT_TAB):
            pas = 1 if key == TAB else -1
            groupes = list(dict.fromkeys(g for _, g in items))
            k = (groupes.index(items[i][1]) + pas) % len(groupes)
            i = next(n for n, (_, g) in enumerate(items) if g == groupes[k])
            text = _texte(items[i], True)
        elif key in ENTREE:
            text = sons_cmd._ecouter(items[i][0])  # joue le son ; None si tout va bien
        if text:
            out(text + "\n")
            say(text)
    sons_cmd._arreter_ecoute()
    return dire("son.ferme")
