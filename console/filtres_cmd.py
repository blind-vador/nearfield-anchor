"""Commande "filtres" : les parasites appris (commande "parasite") regroupes
par canal, qu'on peut ACTIVER / DESACTIVER sans les supprimer -- pour decider
au cas par cas sans se verrouiller (2026-10-09, demande utilisateur, apres
qu'une accumulation de ~282 empreintes sur le PMR446 a fini par bloquer toute
la bande).

  fleches haut / bas   parcourir les filtres (par frequence)
  Espace               active / desactive le filtre courant (reversible)
  Retour arriere       supprime le filtre courant (Ctrl+A puis Retour : tous)
  Ctrl + A             selectionne tout
  Ctrl + Z             annule la derniere suppression
  Echap ferme. F1 : les touches.

Un filtre desactive est garde mais n'ecarte plus rien (lib/noise_signatures.py,
matches_known_noise ignore actif=False). Le scanner relit le fichier a chaque
pre-ecoute, rien a relancer.
"""
import sys


import conseils  # noqa: E402
import noise_signatures as ns  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN = "\x1b[A", "\x1b[B"
BACKSPACE = ("\x7f", "\x08")
CTRL_A, CTRL_Z, ESC, SPACE = "\x01", "\x1a", "\x1b", " "


def _freq(hz):
    return f"{hz / 1e6:.4f}".rstrip("0").rstrip(".").replace(".", ",")


def _line(g):
    etat = dire("filtres.actif") if g["actif"] else dire("filtres.inactif")
    return dire("filtres.ligne", frequence=_freq(g["freq_hz"]), bande=g["label"],
                nombre=nombre(g["nb"], "empreinte", "empreintes"), etat=etat)


def run(line, key_source, say, out):
    items = ns.filtres()
    i = 0
    selected = False
    undo = []  # suppressions, pour Ctrl + Z : liste d'entrees retirees
    title = dire("filtres.titre", nombre=nombre(len(items), "filtre", "filtres"))
    intro = conseils.intro("filtres", title, dire("filtres.consigne"))
    out(intro + "\n")
    say(f"{intro} {_line(items[0]) if items else dire('filtres.vide')}")

    for key in key_source:
        voix.stop()
        text = None
        if conseils.is_help(key):
            text = dire("filtres.consigne")
        elif key == ESC:
            return dire("filtres.fermee")
        elif key == SPACE:
            if not items:
                text = dire("filtres.vide")
            else:
                g = items[i]
                nouvel_etat = not g["actif"]
                if ns.set_group_active(g["cle"], nouvel_etat):
                    items = ns.filtres()
                    i = min(i, max(len(items) - 1, 0))
                    text = (dire("filtres.active") if nouvel_etat else dire("filtres.desactive")) \
                        + " " + (_line(items[i]) if items else dire("filtres.vide"))
                else:
                    text = _line(g)
        elif key == CTRL_A:
            selected = bool(items)
            text = dire("filtres.tout_selectionne", nombre=nombre(len(items), "filtre", "filtres")) \
                if items else dire("filtres.vide")
        elif key in BACKSPACE:
            if not items:
                text = dire("filtres.vide")
            else:
                cibles = list(items) if selected else [items[i]]
                removed = []
                for g in cibles:
                    removed += ns.delete_group(g["cle"])
                undo.append(removed)
                items = ns.filtres()
                i = 0 if selected else min(i, max(len(items) - 1, 0))
                if selected:
                    text = dire("filtres.tout_supprime", nombre=nombre(len(cibles), "filtre", "filtres"))
                else:
                    text = dire("filtres.supprime", frequence=_freq(cibles[0]["freq_hz"]))
                text += " " + (_line(items[i]) if items else dire("filtres.vide"))
                selected = False
        elif key == CTRL_Z:
            if not undo:
                text = dire("filtres.rien_a_annuler")
            else:
                ns.restore_entries(undo.pop())
                items = ns.filtres()
                i = min(i, max(len(items) - 1, 0))
                text = dire("filtres.restaure") + " " + (_line(items[i]) if items else dire("filtres.vide"))
        else:
            if selected:
                selected = False
                say(dire("filtres.selection_annulee"))
            if key in (UP, DOWN):
                if not items:
                    text = dire("filtres.vide")
                else:
                    j = i + (-1 if key == UP else 1)
                    if 0 <= j < len(items):
                        i = j
                        text = _line(items[i])
                    else:
                        edge = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
                        text = edge + " " + _line(items[i])
            else:
                text = dire("filtres.touches")
        if text:
            out(f"[{text}]\n")
            say(text)
    return dire("filtres.fermee")
