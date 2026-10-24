"""Commande "juge(s)" (2026-10-25) : trier les decodeurs a la volee, comme
"zapper" ou "ignorer", sur ce que l'on entend en ce moment.

  "juge" / "juge suivant" : ce decodage n'est pas le bon -> on passe au decodeur
                            suivant sur le meme signal (sa confiance baisse).
  "juge ok"               : c'est le bon decodeur -> sa confiance monte.
Si la communication se termine avant, son flux brut est garde pour que les
autres juges l'examinent apres."""
import re
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import juge_live  # noqa: E402


def _mhz(etat):
    return str(etat.get("freq_hz", 0) / 1e6).rstrip("0").rstrip(".").replace(".", ",")


def run(line, key_source=None, say=None, out=None):
    low = " " + re.sub(r"[^\w ]", " ", line.lower()) + " "
    etat = juge_live.courant()
    if etat is None:
        return "Rien n'est en cours de décodage en ce moment."
    ok = any(mot in low for mot in (" ok ", " oui ", " bon ", " garde ", " juste "))
    juge_live.demander("ok" if ok else "suivant")
    proto = etat.get("protocole") or "ce décodage"
    if ok:
        return f"Noté : {proto} sur {_mhz(etat)} mégahertz est le bon décodeur."
    return f"Je passe au décodeur suivant sur {_mhz(etat)} mégahertz."
