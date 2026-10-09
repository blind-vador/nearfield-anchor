"""Commande "journal proposition" (2026-10-09) : revue du matin des reglages
proposes par la passe d'analyse du Mac mini (lib/propositions.py). L'IA a le
droit de creuser fort (debruitage agressif) ; l'utilisateur tranche a l'oreille.

  fleches haut / bas   proposition precedente / suivante (dit le resume)
  Entree               ecoute l'extrait AVANT
  p                    ecoute l'extrait APRES (nettoye par l'IA), si present
  c                    confirme : le reglage est applique au Pi
  r                    refuse : la proposition est ecartee (et sert de retour)
  Echap ferme. F1 : les touches.
"""
import os
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import propositions  # noqa: E402
import voix  # noqa: E402
from journal import Player  # noqa: E402
from phrases import dire, nombre  # noqa: E402
from sound_paths import RADIO_LOG_DIR  # noqa: E402

UP, DOWN, ESC = "\x1b[A", "\x1b[B", "\x1b"


def _clip(path):
    if not path:
        return None
    if os.path.isfile(path):
        return path
    candidate = os.path.join(RADIO_LOG_DIR, path)
    return candidate if os.path.isfile(candidate) else None


def _line(items, i):
    p = items[i]
    return dire("proposition.ligne", index=i + 1, total=len(items), resume=p.get("resume", ""))


def run(line, key_source, say, out):
    items = propositions.entries()
    i = 0
    player = Player()
    title = dire("proposition.titre", nombre=nombre(len(items), "proposition", "propositions"))
    intro = conseils.intro("proposition", title, dire("proposition.consigne"))
    out(intro + "\n")
    try:
        if not items:
            say(f"{intro} {dire('proposition.vide')}")
            for key in key_source:
                if not conseils.is_help(key):
                    break
                say(dire("proposition.consigne"))
            return dire("proposition.fermee")
        say(f"{intro} {_line(items, i)}")

        for key in key_source:
            voix.stop()
            text = None
            if conseils.is_help(key):
                text = dire("proposition.consigne")
            elif key == ESC:
                return dire("proposition.fermee")
            elif key in (UP, DOWN):
                j = i + (-1 if key == UP else 1)
                if 0 <= j < len(items):
                    i = j
                    text = _line(items, i)
                else:
                    text = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
            elif key in ("\r", "\n"):
                clip = _clip(items[i].get("avant"))
                if clip:
                    player.play(clip)
                else:
                    text = dire("proposition.pas_d_extrait")
            elif key in ("p", "P"):
                clip = _clip(items[i].get("apres"))
                if clip:
                    player.play(clip)
                else:
                    text = dire("proposition.pas_d_apres")
            elif key in ("c", "C"):
                ok, detail = propositions.apply_action(items[i])
                propositions.set_etat(items[i]["id"], "confirme")
                items.pop(i)  # retiree de la file a traiter
                i = min(i, max(len(items) - 1, 0))
                text = (dire("proposition.confirmee", detail=detail) if ok
                        else dire("proposition.confirmee_manuelle", detail=detail))
                text += " " + (_line(items, i) if items else dire("proposition.plus_rien"))
            elif key in ("r", "R"):
                propositions.set_etat(items[i]["id"], "refuse")
                items.pop(i)
                i = min(i, max(len(items) - 1, 0))
                text = dire("proposition.refusee") + " " + (_line(items, i) if items else dire("proposition.plus_rien"))
            else:
                text = dire("proposition.touches")
            if text:
                out(f"[{text}]\n")
                say(text)
        return dire("proposition.fermee")
    finally:
        player.close()
