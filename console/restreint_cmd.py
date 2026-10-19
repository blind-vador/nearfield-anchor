"""Commande "restreint" (2026-10-17) : cocher les bandes, fenetres et veilles
que le scanner a le droit d'ecouter (lib/restreint.py).

  fleches haut / bas       parcourir
  Espace                   cocher / decocher
  T                        tout decocher
  Entree                   enregistre ; le mode restreint s'allume s'il y a
                           au moins une case cochee, sinon il s'arrete
  Echap                    ferme sans rien changer
"restreint arret" (ou "mode normal") : le scanner reprend tout, les cases
restent gardees pour la prochaine fois.
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import matcher  # noqa: E402
import restreint  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN = "\x1b[A", "\x1b[B"
OFF_WORDS = {"arret", "arrete", "stop", "off", "fin", "normal", "desactive", "desactiver", "enleve"}


def _say_item(item, checked):
    state = dire("restreint.cochee") if item["nom"] in checked else dire("restreint.non_cochee")
    return f"{item['libelle']}, {state}"


def _summary(checked, items):
    names = [i["libelle"] for i in items if i["nom"] in checked]
    return dire("restreint.actif", nombre=nombre(len(names), "cible"), liste=", ".join(names))


def run(line, key_source, say, out):
    words = set(matcher.normalize(line).split())
    conf = restreint.load(fresh=True)
    items = restreint.cibles()
    if words & OFF_WORDS:
        restreint.save(False, conf.get("cibles", []))
        return dire("restreint.arrete")
    if not items:
        return dire("restreint.pas_de_cibles")
    checked = set(conf.get("cibles", []))
    pos = 0
    consigne = dire("restreint.consigne")
    state = _summary(checked, items) if restreint.actif() else dire("restreint.inactif")
    intro = conseils.intro("restreint", dire("restreint.titre"), consigne)
    out(intro + "\n")
    say(f"{intro} {state} {_say_item(items[pos], checked)}")
    for key in key_source:
        if key == "\x1b":
            return dire("restreint.rien_change")
        if key in (UP, DOWN):
            pos = (pos + (-1 if key == UP else 1)) % len(items)
            text = _say_item(items[pos], checked)
        elif key == " ":
            checked ^= {items[pos]["nom"]}
            text = _say_item(items[pos], checked)
        elif key in ("t", "T"):
            checked = set()
            text = dire("restreint.tout_decoche")
        elif key in ("\r", "\n"):
            restreint.save(bool(checked), checked)
            return _summary(checked, items) if checked else dire("restreint.arrete")
        elif conseils.is_help(key):
            text = consigne
        else:
            text = dire("restreint.aide_courte")
        out(f"[{text}]\n")
        say(text)
    return dire("restreint.rien_change")
