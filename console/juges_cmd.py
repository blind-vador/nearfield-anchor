"""Commande "juges" (2026-10-25) : tes retours sur les decodages differes.

Les juges rejouent les sons bruts devant les decodeurs ; parfois ils prennent
un son hache pour de la voix (du dPMR pris pour du DMR). Tu ecoutes l'entree
"decodage differe" au journal radio, puis tu notes :
  "juges" / "juges derniers" : les derniers decodages differes, avec leur numero
  "juges faux [n]" : le n-ieme (ou le dernier) etait faux -> ce juge ne sera plus
                     cru en dessous de ce niveau, sur cette bande
  "juges bon [n]"  : c'etait bon -> ce niveau compte comme vraie voix
Tes notes fixent les seuils ; les juges ne les deduisent plus seuls."""
import re
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import juges_watch  # noqa: E402


def _num(line):
    m = re.search(r"\d+", line)
    return int(m.group()) if m else None


def run(line, key_source=None, say=None, out=None):
    low = " " + re.sub(r"[^\w ]", " ", line.lower()) + " "
    if any(mot in low for mot in (" faux", " mauvais", " rate", " tort")):
        return juges_watch.retour_utilisateur(_num(line), bon=False)
    if any(mot in low for mot in (" bon", " juste", " vrai", " bien")):
        return juges_watch.retour_utilisateur(_num(line), bon=True)
    return juges_watch.derniers_retours_texte()
