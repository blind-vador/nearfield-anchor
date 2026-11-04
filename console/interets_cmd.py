"""Commande "interets" : centres d'interet de l'utilisateur, qui ponderent les
piqures du scanner (lib/interets.py).

  fleches haut / bas        parcourir les categories
  fleches gauche / droite   niveau : aucun, faible, normal, fort, passion
  Entree                    enregistre      Echap : annule
  F1                        redit les touches

Forme directe : "interet avions fort", "interets fm faible". Le scanner
relit les reglages de lui-meme (30 s au plus), rien a relancer.
"""
import sys


import conseils  # noqa: E402
import interets  # noqa: E402
import matcher  # noqa: E402
from phrases import dire  # noqa: E402

UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
# mots dits -> categorie (forme directe)
WORDS = {"talkie": "talkies", "talkies": "talkies", "446": "talkies", "pmr": "talkies", "lpd": "talkies",
         "avion": "aviation", "avions": "aviation", "aviation": "aviation", "acars": "aviation",
         "radioamateur": "radioamateurs", "radioamateurs": "radioamateurs", "amateur": "radioamateurs",
         "relais": "radioamateurs", "aprs": "radioamateurs", "lora": "radioamateurs",
         "cb": "cb", "cibi": "cb", "meteo": "meteo", "capteurs": "meteo", "sondes": "meteo",
         "secours": "secours", "pagers": "secours", "balises": "secours", "pompiers": "secours",
         "marine": "marine", "bateaux": "marine", "pro": "professionnels", "professionnels": "professionnels",
         "fm": "fm", "commerciale": "fm", "commerciales": "fm", "dab": "dab", "numerique": "dab"}


def _label(cats, values, i):
    cat = cats[i]
    return dire("interets.ligne", categorie=interets.CATEGORY_NAMES[cat], niveau=interets.LEVEL_NAMES[values[cat]])


def quick(line):
    """ "interet avions fort" -> enregistre et rend la phrase ; None si incomplet."""
    words = matcher.normalize(line).split()
    cat = next((WORDS[w] for w in words if w in WORDS), None)
    level = next((w for w in words if w in interets.WEIGHTS), None)
    if not cat or not level:
        return None
    values = dict(interets.levels(fresh=True))
    values[cat] = level
    if not interets.save(values):
        return dire("interets.nas_inaccessible")
    return dire("interets.enregistre_un", categorie=interets.CATEGORY_NAMES[cat], niveau=interets.LEVEL_NAMES[level])


def summary():
    values = interets.levels(fresh=True)
    return dire("interets.resume", liste=" ; ".join(_label(list(interets.CATEGORIES), values, i)
                                                    for i in range(len(interets.CATEGORIES))))


def run(line, key_source, say, out):
    direct = quick(line)
    if direct:
        return direct
    cats = list(interets.CATEGORIES)
    values = dict(interets.levels(fresh=True))
    i = 0
    intro = conseils.intro("interets", dire("interets.titre"), dire("interets.consigne"))
    import voix
    textes = [intro, dire("interets.consigne"), dire("interets.annule"),
              dire("interets.enregistres"), dire("interets.deja_min"), dire("interets.deja_max"),
              dire("commun.debut_liste"), dire("commun.fin_liste")]
    textes += [_label(cats, values, n) for n in range(len(cats))]
    textes += [interets.LEVEL_NAMES[k] for k in interets.LEVELS]
    voix.prechauffer(textes)
    out(intro + "\n")
    say(f"{intro} {_label(cats, values, i)}")
    for key in key_source:
        if key == "\x1b":
            return dire("interets.annule")
        if key in ("\r", "\n"):
            if not interets.save(values):
                return dire("interets.nas_inaccessible")
            return dire("interets.enregistres")
        if key in (UP, DOWN):
            j = i + (-1 if key == UP else 1)
            if 0 <= j < len(cats):
                i = j
                text = _label(cats, values, i)
            else:
                edge = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
                text = edge + " " + _label(cats, values, i)
        elif key in (LEFT, RIGHT):
            levels = interets.LEVELS
            k = levels.index(values[cats[i]]) + (1 if key == RIGHT else -1)
            if 0 <= k < len(levels):
                values[cats[i]] = levels[k]
                text = interets.LEVEL_NAMES[levels[k]]
            else:
                text = dire("interets.deja_max") if k >= len(levels) else dire("interets.deja_min")
        elif conseils.is_help(key):
            text = dire("interets.consigne")
        else:
            text = dire("interets.touches")
        out(f"[{text}]\n")
        say(text)
    return dire("interets.fermes")
