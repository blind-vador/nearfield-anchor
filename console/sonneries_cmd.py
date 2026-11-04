"""Commande sonneries.

  haut / bas           l'événement
  contrôle + flèches   le groupe suivant : une bande, DMR, TETRA, les messages
  Tab                  la veille suivante
  gauche / droite      les sons du dépôt, puis les sonneries par défaut
  Entrée sur le dépôt  déplace le son dans les sonneries actives,
                       sous le nom de l'événement
  Entrée sur un défaut copie le son, sous ce même nom
  Échap ferme. F1 : les touches.
"""
import os
import subprocess

import conseils
import keys
import sonneries
from phrases import dire

UP, DOWN, LEFT, RIGHT = "\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C"
TAB, SHIFT_TAB = '\t', '\x1b[Z'
ENTREE = ("\r", "\n")
ESC = "\x1b"
_ecoute = [None]


def _nom(chemin):
    return os.path.splitext(os.path.basename(chemin or ""))[0].replace("_", " ")


_CATALOGUE = {
    "tetra_appel_prive": "sonneries.catalogue_tetra_prive",
    "tetra_ouverture": "sonneries.catalogue_tetra_public",
    "tetra_message": "sonneries.catalogue_tetra_message",
    "tetra_nouvelle_porteuse": "sonneries.catalogue_tetra_porteuse",
    "dmr_vocal": "sonneries.catalogue_dmr_voix",
    "dmr_nouveau": "sonneries.catalogue_dmr_groupe",
}


def _capital(texte):
    texte = (texte or "").strip().rstrip(".")
    if not texte:
        return texte
    return texte[0].upper() + texte[1:]


def _place(slot):
    """Phrase complète : la bande et l'événement, ou l'appel dans son contexte."""
    if slot.get("catalogue"):
        cle = _CATALOGUE.get(slot["evenement"])
        if cle:
            return dire(cle)
        return dire(f"sons.evenement_{slot['evenement']}")
    import ceremonie_radio as ceremonie
    bande = ceremonie.bande_dite(slot.get("parle"), "bande")
    if slot.get("appel"):
        return dire("sonneries.place_appel",
                    appel=dire(f"sonneries.appel_{slot['appel']}"),
                    bande=bande,
                    detail=dire(f"sonneries.detail_{slot['evenement']}"))
    return dire("sonneries.place",
                evenement=dire(f"sonneries.evenement_{slot['evenement']}"),
                bande=bande)


def _texte(slot, precedent):
    morceau = _capital(_place(slot))
    if precedent is None or precedent.get("veille") != slot.get("veille"):
        morceau = f"{_capital(dire('sons.groupe_' + slot['veille']))}. {morceau}"
    elif slot.get("groupe") == "messages" and precedent.get("groupe") != "messages":
        morceau = f"{dire('sonneries.groupe_messages')}. {morceau}"
    actuel = sonneries.active(slot)
    if slot.get("catalogue"):
        return morceau + "."
    if actuel:
        return f"{morceau}. {dire('sonneries.actuelle', nom=_nom(actuel))}"
    if slot.get("appel"):
        return f"{morceau}. {dire('sonneries.repli_appel')}"
    return f"{morceau}. {dire('sonneries.generale')}"


def _arreter():
    proc, _ecoute[0] = _ecoute[0], None
    if proc is not None and proc.poll() is None:
        proc.terminate()


def _jouer(chemin):
    _arreter()
    if not chemin:
        return
    _ecoute[0] = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", chemin, "-f", "alsa", "default"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(line, key_source, say, out):
    sonneries.assurer()
    items = sonneries.slots()
    if not items:
        return dire("sonneries.vide")
    i, j = 0, -1
    candidats = sonneries.candidats()
    consigne = dire("sonneries.consigne")
    intro = conseils.intro("sonneries", dire("sonneries.titre"), consigne)
    out(intro + "\n")
    say(f"{intro} {_texte(items[0], None)}")
    for key in key_source:
        if key == keys.WAKE:
            continue
        import voix
        voix.stop()
        _arreter()
        text = None
        if conseils.is_help(key):
            text = consigne
        elif key == ESC:
            break
        elif key in (TAB, SHIFT_TAB):
            j = -1
            pas = 1 if key == TAB else -1
            precedent, i = items[i], sonneries.saut(items, i, pas, "veille")
            text = _texte(items[i], precedent)
        elif key in (UP, DOWN):
            j = -1
            pas = -1 if key == UP else 1
            if keys.modifier_held("ctrl"):
                n = sonneries.saut(items, i, pas, "groupe")
                if n is None:
                    text = dire("commun.debut_liste") if pas < 0 else dire("commun.fin_liste")
                else:
                    precedent, i = items[i], n
                    text = _texte(items[i], precedent)
            else:
                n = i + pas
                if 0 <= n < len(items):
                    precedent, i = items[i], n
                    text = _texte(items[i], precedent)
                else:
                    bord = dire("commun.debut_liste") if n < 0 else dire("commun.fin_liste")
                    text = f"{bord} {_texte(items[i], None)}"
        elif key in (LEFT, RIGHT):
            if not candidats:
                text = dire("sonneries.vide")
            else:
                pas = 1 if key == RIGHT else -1
                n = j + pas
                if j < 0 and key == LEFT:
                    n = len(candidats) - 1
                if 0 <= n < len(candidats):
                    j = n
                    origine, chemin = candidats[j]
                    cle = "sonneries.origine_depot" if origine == "depot" else "sonneries.origine_defaut"
                    text = dire(cle, nom=_nom(chemin))
                    _jouer(chemin)
                else:
                    text = dire("commun.debut_liste") if n < 0 else dire("commun.fin_liste")
        elif key in ENTREE:
            if j < 0 or not candidats:
                text = dire("sonneries.choisir")
            else:
                origine, chemin = candidats[j]
                pose = sonneries.poser(items[i], origine, chemin)
                if not pose:
                    text = dire("sonneries.echec")
                else:
                    cle = "sonneries.posee_deplacee" if origine == "depot" else "sonneries.posee_copiee"
                    text = dire(cle, nom=_nom(chemin), place=_place(items[i]))
                    candidats = sonneries.candidats()
                    j = -1
        if text:
            out(text + "\n")
            say(text)
    _arreter()
    return dire("sonneries.ferme")
