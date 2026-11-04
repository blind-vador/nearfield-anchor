"""Commande "son" / "tuto" : tutoriel des sonneries.

Même carte que le dialogue sonneries : toutes les veilles sauf les sons SNCF,
les bandes radio, DMR, TETRA, puis les messages. On écoute le son qui sera
vraiment joué, repli compris.

  flèches haut / bas   l'événement
  contrôle + flèches   le groupe suivant : une bande, DMR, TETRA, les messages
  Tab / Maj+Tab        la veille suivante
  Entrée               écouter le son (une touche l'arrête)
  Échap ferme. F1 : les touches.
"""
import conseils  # noqa: E402
import keys  # noqa: E402
import sonneries  # noqa: E402
import sonneries_cmd  # noqa: E402
import voix  # noqa: E402
from phrases import dire  # noqa: E402

UP, DOWN = "\x1b[A", "\x1b[B"
TAB, SHIFT_TAB = "\t", "\x1b[Z"
ENTREE = ("\r", "\n")
ESC = "\x1b"


def run(line, key_source, say, out):
    sonneries.assurer()
    items = sonneries.slots()
    if not items:
        return dire("sonneries.vide")
    i = 0
    consigne = dire("son.consigne")
    intro = conseils.intro("son", dire("son.titre"), consigne)
    out(intro + "\n")
    say(f"{intro} {sonneries_cmd._texte(items[0], None)}")
    for key in key_source:
        if key == keys.WAKE:
            continue
        voix.stop()
        sonneries_cmd._arreter()
        text = None
        if conseils.is_help(key):
            text = consigne
        elif key == ESC:
            break
        elif key in (TAB, SHIFT_TAB):
            pas = 1 if key == TAB else -1
            precedent, i = items[i], sonneries.saut(items, i, pas, "veille")
            text = sonneries_cmd._texte(items[i], precedent)
        elif key in (UP, DOWN):
            pas = -1 if key == UP else 1
            if keys.modifier_held("ctrl"):
                n = sonneries.saut(items, i, pas, "groupe")
                if n is None:
                    text = dire("commun.debut_liste") if pas < 0 else dire("commun.fin_liste")
                else:
                    precedent, i = items[i], n
                    text = sonneries_cmd._texte(items[i], precedent)
            else:
                n = i + pas
                if 0 <= n < len(items):
                    precedent, i = items[i], n
                    text = sonneries_cmd._texte(items[i], precedent)
                else:
                    bord = dire("commun.debut_liste") if n < 0 else dire("commun.fin_liste")
                    text = f"{bord} {sonneries_cmd._texte(items[i], None)}"
        elif key in ENTREE:
            chemin = sonneries.entendre(items[i])
            if chemin:
                sonneries_cmd._jouer(chemin)
            else:
                text = dire("sonneries.vide")
        if text:
            out(text + "\n")
            say(text)
    sonneries_cmd._arreter()
    return dire("son.ferme")
