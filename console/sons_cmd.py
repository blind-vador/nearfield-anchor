"""Commande "sons" (2026-10-23, demande utilisateur) : chaque evenement du
catalogue (lib/sons.py), groupe par veille ; pour chacun, son et phrase, son
seul ou rien. Chaque changement est garde et vaut tout de suite, dans toutes
les veilles.

  fleches haut / bas         evenement precedent / suivant
  Tab / Maj+Tab              groupe suivant / precedent
  fleches droite / gauche    mode suivant / precedent
  Entree                     ecouter le son (une touche l'arrete)
  Echap ferme. F1 : les touches.
"""
import subprocess
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import keys  # noqa: E402
import sons  # noqa: E402
import voix  # noqa: E402
from phrases import dire  # noqa: E402

UP, DOWN, LEFT, RIGHT = "\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C"
TAB, SHIFT_TAB = "\t", "\x1b[Z"
ENTREE = ("\r", "\n")
ESC = "\x1b"


def _items():
    """[(evenement, groupe)] dans l'ordre du catalogue."""
    return [(e, rel.split("/")[0]) for e, rel in sons.SONS.items()]


def _texte(item, avec_groupe=False):
    evenement, groupe = item
    nom = dire(f"sons.evenement_{evenement}")
    if avec_groupe:
        nom = f"{dire(f'sons.groupe_{groupe}').capitalize()}, {nom}"
    return f"{nom} : {dire('sons.mode_' + sons.mode(evenement))}."


_ecoute = [None]


def _arreter_ecoute():
    proc, _ecoute[0] = _ecoute[0], None
    if proc is not None and proc.poll() is None:
        proc.terminate()


def _ecouter(evenement):
    """Joue le son meme regle sur "rien" ; la touche suivante l'arrete."""
    chemin = sons.chemin(evenement)
    if not chemin:
        return dire("sons.introuvable")
    _ecoute[0] = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", chemin,
                                   "-f", "alsa", "default"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return None


def run(line, key_source, say, out):
    items = _items()
    i = 0
    consigne = dire("sons.consigne")
    intro = conseils.intro("sons", dire("sons.titre"), consigne)
    out(intro + "\n")
    say(f"{intro} {_texte(items[0], True)}")
    for key in key_source:
        if key == keys.WAKE:
            continue
        voix.stop()
        _arreter_ecoute()
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
        elif key in (LEFT, RIGHT):
            evenement = items[i][0]
            modes = sons.MODES
            k = (modes.index(sons.mode(evenement)) + (1 if key == RIGHT else -1)) % len(modes)
            sons.regler(evenement, modes[k])
            text = dire("sons.mode_" + modes[k])
        elif key in ENTREE:
            text = _ecouter(items[i][0])
        if text:
            out(text + "\n")
            say(text)
    _arreter_ecoute()
    return dire("sons.ferme")
