"""Commande "juge(s)" (2026-10-25) : trier les decodeurs a la volee, comme
"zapper" ou "ignorer", sur ce que l'on entend en ce moment.

  "juge" / "juge suivant" : ce decodage n'est pas le bon -> on passe au decodeur
                            suivant sur le meme signal (sa confiance baisse).
  "juge ok"               : c'est le bon decodeur -> sa confiance monte.
Si la communication se termine avant, son flux brut est garde pour que les
autres juges l'examinent apres."""
import re
import sys


import juge_live  # noqa: E402
from phrases import dire  # noqa: E402


def _mhz(etat):
    return str(etat.get("freq_hz", 0) / 1e6).rstrip("0").rstrip(".").replace(".", ",")


def run(line, key_source=None, say=None, out=None):
    low = " " + re.sub(r"[^\w ]", " ", line.lower()) + " "
    etat = juge_live.courant()
    if etat is None:
        return dire("juges.rien_en_cours")
    ok = any(mot in low for mot in (" ok ", " oui ", " bon ", " garde ", " juste "))
    if not ok and etat.get("suivants") == 0:
        return dire("juges.dernier_rendu")
    juge_live.demander("ok" if ok else "suivant")
    proto = etat.get("protocole") or dire("juges.ce_decodage")
    if ok:
        return dire("juges.bon", decodeur=proto, mhz=_mhz(etat))
    return dire("juges.suivant", decodeur=proto, mhz=_mhz(etat))
