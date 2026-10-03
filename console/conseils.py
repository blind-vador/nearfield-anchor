"""Niveau d'aide de la console : consignes d'utilisation (quelles touches
font quoi) dites a chaque ouverture d'un dialogue, ou seulement la
premiere fois qu'on utilise chaque fonction.

  console.json, "conseils" : "toujours" (defaut) ou "premiere_fois"
  console.json, "conseils_vus" : fonctions dont la consigne a deja ete dite

Commande "conseils" (activer, desactiver, remettre a zero). Dans tous les
dialogues, F1 redit la consigne, quel que soit le mode. Les rappels sur
une mauvaise touche et les confirmations (redemarrer, eteindre...) sont
toujours dits : ce sont des reponses, pas des consignes.
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")
import reglages  # noqa: E402
from phrases import dire  # noqa: E402

F1 = ("\x1b[[A", "\x1bOP", "\x1b[11~")
ALWAYS = "toujours"
FIRST_TIME = "premiere_fois"


def mode():
    return reglages.get("console").get("conseils", ALWAYS)


def intro(feature, title, consigne):
    """Texte d'ouverture d'un dialogue : titre, plus la consigne si le mode
    le veut (toujours, ou premiere utilisation de cette fonction)."""
    settings = reglages.get("console")
    seen = list(settings.get("conseils_vus") or [])
    if feature and feature not in seen:
        settings["conseils_vus"] = seen + [feature]
        reglages.save("console", settings)
        first = True
    else:
        first = False
    if consigne and (settings.get("conseils", ALWAYS) == ALWAYS or first):
        return f"{title} {consigne}".strip()
    return title


def is_help(key):
    return key in F1


def command(line):
    """Commande "conseils" : rend la phrase a dire."""
    import matcher
    words = set(matcher.normalize(line).split())
    settings = reglages.get("console")
    if words & {"zero", "reinitialise", "reinitialiser", "remise", "oublie"}:
        settings["conseils_vus"] = []
        reglages.save("console", settings)
        return dire("conseils.consignes_remises_zero_chaque")
    if words & {"desactive", "desactives", "desactivee", "desactivees", "desactiver", "sans", "expert",
                 "off", "coupe", "arrete", "moins"}:
        settings["conseils"] = FIRST_TIME
        reglages.save("console", settings)
        return (dire("conseils.mode_sans_conseils_consignes"))
    if words & {"active", "actives", "activee", "activees", "activer", "toujours", "guide", "on",
                 "debutant", "remets"}:
        settings["conseils"] = ALWAYS
        reglages.save("console", settings)
        return dire("conseils.mode_guide_consignes_sont")
    current = dire("conseils.guide_consignes_chaque_fois") if settings.get("conseils", ALWAYS) == ALWAYS else \
        dire("conseils.sans_conseils_consignes_premiere")
    return (dire("conseils.mode_actuel_dis_conseils", current=current))
