"""Commande "observation" : dire au Pi le temps qu'il fait (lib/meteo_obs.py).

  "observation"                       dialogue complet
  "il pleut fort", "tonnerre au loin",
  "orage tout pres", "il fait beau"... enregistre directement, sans dialogue

Dialogue :
  fleches haut / bas        champ precedent / suivant
  fleches gauche / droite   valeur precedente / suivante
  Entree sur OK             enregistre         Echap : annule      F1 : touches
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import matcher  # noqa: E402
import meteo_locale  # noqa: E402
import meteo_obs  # noqa: E402

UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
TITLE = "Observation météo : dis au Pi ce qui se passe dehors."
CONSIGNE = ("Flèches haut et bas : champ. Gauche et droite : valeur. Entrée sur OK pour enregistrer. "
            "Échap : annuler. F1 redit ces touches.")
OK = len(meteo_obs.FIELDS)
SAFETY = "Débranche les appareils électriques sensibles, ferme fenêtres et volets, reste à l'intérieur."


def _field_text(i, values):
    if i == OK:
        return "OK, entrée pour enregistrer."
    _, question, options, _ = meteo_obs.FIELDS[i]
    return f"{question} : {options[values[i]][1]}."


def _reply(answers, quick):
    total, level, reasons = meteo_obs.record(answers, quick=quick)
    text = f"Noté : {meteo_obs.spoken(answers)}. {total} observation{'s' if total > 1 else ''} au total."
    if level >= 2:
        text += f" Niveau de vigilance du Pi : {meteo_locale.LEVEL_NAMES[level]}"
        text += (f", {', '.join(reasons)}." if reasons else ".")
        text += " J'écoute davantage les stations météo voisines."
    if answers.get("orage") in ("proche", "tout_pres"):
        text += " " + SAFETY
    if quick:
        text += " Dis observation pour préciser le reste."
    return text


def quick(line):
    """Phrase directe ("il pleut fort"...) -> reponse, ou None si rien de
    reconnu (le dialogue s'ouvre alors)."""
    answers = meteo_obs.parse_quick(matcher.normalize(line))
    return _reply(answers, quick=True) if answers else None


def run(line, key_source, say, out):
    prefill = meteo_obs.parse_quick(matcher.normalize(line))
    values = []
    for code, _, options, default in meteo_obs.FIELDS:
        codes = [c for c, _ in options]
        values.append(codes.index(prefill[code]) if prefill.get(code) in codes else default)
    field = 0
    intro = conseils.intro("observation", TITLE, CONSIGNE)
    out(intro + "\n")
    say(f"{intro} {_field_text(field, values)}")
    for key in key_source:
        if key == "\x1b":
            return "Observation annulée."
        if key in (UP, DOWN):
            field = (field + (1 if key == DOWN else -1)) % (OK + 1)
            text = _field_text(field, values)
        elif key in (LEFT, RIGHT) and field < OK:
            n = len(meteo_obs.FIELDS[field][2])
            values[field] = (values[field] + (1 if key == RIGHT else -1)) % n
            text = meteo_obs.FIELDS[field][2][values[field]][1]
        elif key in ("\r", "\n") and field == OK:
            answers = {code: meteo_obs.FIELDS[i][2][values[i]][0] for i, (code, *_) in enumerate(meteo_obs.FIELDS)}
            return _reply(answers, quick=False)
        elif key in ("\r", "\n"):
            text = "Descends jusqu'à OK pour enregistrer."
        elif conseils.is_help(key):
            text = CONSIGNE
        else:
            text = "Flèches, entrée sur OK, ou échap. F1 pour l'aide."
        out(f"[{text}]\n")
        say(text)
    return "Observation fermée."
