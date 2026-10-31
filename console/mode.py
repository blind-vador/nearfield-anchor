"""Commande "mode" (2026-10-31) : point d'entree unique des modes du scanner.
Chaque mode est une ENTREE de ce dialogue (plus de commande textuelle dediee
pour "manuel" ni "restreint"). Aujourd'hui :
  - manuel    : regler soi-meme la plage et la frequence aux fleches ;
  - restreint : cocher les bandes que le scanner a le droit d'ecouter.
Concu pour accueillir les rythmes a venir (lent / dechaine / entre-deux) :
il suffira d'ajouter une entree a ENTREES et une branche a _lancer().

Le scanner reste agnostique : "mode" ne dit PAS quoi viser, il regle COMMENT
on parcourt (et, pour restreint, le perimetre). Les deux axes sont
orthogonaux -- "restreint" borne le perimetre, un futur "dechaine" le rythme.

  fleches haut / bas   parcourir les modes
  Entree               entrer dans le mode choisi
  Echap                sortir sans rien changer
Une phrase qui nomme deja un mode ("passe en manuel", "restreint arret")
saute directement a l'entree concernee.
"""
import conseils
import manuel
import matcher
import restreint_cmd
from phrases import dire

UP, DOWN = "\x1b[A", "\x1b[B"


def _entrees():
    return [
        {"nom": "manuel", "libelle": dire("mode.entree_manuel"), "mots": {"manuel", "manuelle"}},
        {"nom": "restreint", "libelle": dire("mode.entree_restreint"),
         "mots": {"restreint", "restreindre", "restriction"}},
    ]


def _lancer(nom, line, key_source, say, out, read_line):
    if nom == "manuel":
        return manuel.run(key_source, say, out, read_line)
    if nom == "restreint":
        return restreint_cmd.run(line, key_source, say, out)
    return dire("mode.inconnu")


def run(line, key_source, say, out, read_line=None):
    words = set(matcher.normalize(line).split())
    entrees = _entrees()
    # Saut direct si la phrase nomme deja un mode (retro-compat des tournures
    # "passe en manuel", "restreint arret" : le reste de la ligne suit).
    for e in entrees:
        if words & e["mots"]:
            return _lancer(e["nom"], line, key_source, say, out, read_line)

    pos = 0
    intro = conseils.intro("mode", dire("mode.titre"), dire("mode.consigne"))
    out(intro + "\n")
    say(f"{intro} {entrees[pos]['libelle']}")
    for key in key_source:
        if key == "\x1b":
            return dire("mode.rien_change")
        if key in (UP, DOWN):
            pos = (pos + (-1 if key == UP else 1)) % len(entrees)
            text = entrees[pos]["libelle"]
        elif key in ("\r", "\n"):
            return _lancer(entrees[pos]["nom"], line, key_source, say, out, read_line)
        elif conseils.is_help(key):
            text = dire("mode.consigne")
        else:
            text = dire("mode.aide_courte")
        out(f"[{text}]\n")
        say(text)
    return dire("mode.rien_change")
