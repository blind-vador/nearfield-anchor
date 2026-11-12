"""Commande "journal proposition" (2026-10-09) : revue de ce que les machines
proposent ; l'utilisateur tranche a l'oreille.

2026-11-11 : trois sortes d'entrees, chacune acceptee ou refusee pour elle-meme
(lib/revue_propositions) : nettoyage audio, nom de station, frequence trouvee par
l'IA. Chaque entree a sa fiche vocale (voix de la veille radio) : ce qui est
propose et pourquoi. Elle est lue quand on arrive sur l'entree, et retiree quand
l'entree est acceptee ou refusee.

  fleches haut / bas   entree precedente / suivante : sa fiche est lue
  Entree, b            ecoute le son brut (nettoyage, nom de station)
  t                    niveau de nettoyage suivant (le joue)
  e                    ce que l'IA a compris (nom de station)
  i                    relit la fiche
  c                    accepte (le niveau en cours pour un nettoyage)
  r                    refuse (pour un nettoyage : le niveau en cours ; tous
                       refuses, la proposition est ecartee)
  Retour arriere       (2026-11-12) refuse d'un coup tous les niveaux de
                       nettoyage de la prise ; ailleurs, comme r
  Echap ferme. F1 : les touches.
"""
import os
import threading

import conseils
import revue_propositions as revue
import voix
from journal import Player
from phrases import dire, nombre
from sound_paths import RADIO_LOG_DIR

UP, DOWN, ESC = "\x1b[A", "\x1b[B", "\x1b"
BACKSPACE = ("\x7f", "\x08")


def _clip(path):
    if not path:
        return None
    if os.path.isfile(path):
        return path
    candidate = os.path.join(RADIO_LOG_DIR, path)
    return candidate if os.path.isfile(candidate) else None


def _traitements(e):
    return (e["item"].get("traitements") or []) if e["genre"] == "nettoyage" else []


def _brut(e):
    """Le son a ecouter avec Entree : l'extrait avant nettoyage, ou la prise qui a
    donne le nom (son brut s'il est garde)."""
    p = e["item"]
    if e["genre"] == "nettoyage":
        return _clip(p.get("brut"))
    if e["genre"] == "nommage" and p.get("fichier"):
        prise = _clip(p["fichier"])
        if prise and prise.endswith(".mp3"):
            return _clip(prise[:-4] + ".brut.mp3") or prise
        return prise
    return None


def run(line, key_source, say, out):
    items = revue.entrees()
    revue.menage(items)
    i = 0
    lvl, refused = 0, set()   # niveau courant + niveaux refuses (nettoyage)
    player = Player()
    tour = [0]                # change a chaque deplacement : une fiche en retard ne joue pas
    title = dire("proposition.titre", nombre=nombre(len(items), "proposition", "propositions"))
    intro = conseils.intro("proposition", title, dire("proposition.consigne"))
    out(intro + "\n")

    def lire_fiche(avant=None):
        """Joue la fiche vocale de l'entree courante (fabriquee si besoin, dans un fil).
        avant : verdict de l'entree precedente, dit en entier avant la fiche."""
        tour[0] += 1
        mon_tour, e = tour[0], items[i]

        def travail():
            if avant:
                voix.say_blocking(avant, encore=lambda: tour[0] == mon_tour)
            path = revue.fiche(e)
            if tour[0] != mon_tour:
                return
            if path:
                player.play(path)
            else:
                say(dire("revue.sans_fiche", texte=revue.texte(e)))
            revue.marquer_entendue(e)
        threading.Thread(target=travail, daemon=True).start()

    def goto(new_i, avant=None):
        nonlocal i, lvl, refused
        i, lvl, refused = new_i, 0, set()
        lire_fiche(avant)

    def advance():
        """Niveau de traitement suivant non refuse (cycle) ; joue et annonce."""
        nonlocal lvl
        traits = _traitements(items[i])
        libres = [k for k in range(len(traits)) if k not in refused]
        if not libres:
            return dire("proposition.pas_de_traitement")
        lvl = libres[(libres.index(lvl) + 1) % len(libres)] if lvl in libres else libres[0]
        clip = _clip(traits[lvl].get("audio"))
        if clip:
            player.play(clip)
        return dire("proposition.niveau_courant", index=lvl + 1, total=len(traits), nom=traits[lvl].get("niveau", ""))

    def retirer(verdict):
        """L'entree courante est tranchee : le verdict, puis la fiche suivante."""
        items.pop(i)
        if items:
            out(f"[{verdict}]\n")
            goto(min(i, len(items) - 1), verdict)
            return None
        return verdict + " " + dire("proposition.plus_rien")

    try:
        if not items:
            say(f"{intro} {dire('proposition.vide')}")
            for key in key_source:
                if not conseils.is_help(key):
                    break
                say(dire("proposition.consigne"))
            return dire("proposition.fermee")
        say(intro)
        lire_fiche()  # la 1re fiche d'emblee

        for key in key_source:
            voix.stop()
            if not items:            # tout a ete tranche : la touche suivante ferme
                return dire("proposition.fermee")
            text = None
            e = items[i]
            if conseils.is_help(key):
                text = dire("proposition.consigne")
            elif key == ESC:
                return dire("proposition.fermee")
            elif key in (UP, DOWN):
                j = i + (-1 if key == UP else 1)
                if 0 <= j < len(items):
                    goto(j)
                else:
                    text = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
            elif key in ("\r", "\n", "b", "B"):
                tour[0] += 1
                clip = _brut(e)
                if clip:
                    player.play(clip)
                else:
                    text = dire("proposition.pas_d_extrait")
            elif key in ("t", "T"):
                tour[0] += 1
                text = advance()
            elif key in ("i", "I"):
                lire_fiche()
            elif key in ("e", "E"):
                p = e["item"]
                text = (" ".join(x for x in (p.get("mise_a_jour"), p.get("explication")) if x)
                        or dire("nommage.sans_explication"))
            elif key in ("c", "C"):
                player.stop()
                text = retirer(revue.accepter(e, lvl if _traitements(e) else None))
            elif key in BACKSPACE:     # tout rejeter d'emblee, sans r r r r
                player.stop()
                verdict = revue.refuser(e)
                if _traitements(e):
                    verdict = dire("proposition.nettoyages_refuses", nombre=nombre(len(_traitements(e)), "niveau", "niveaux"))
                text = retirer(verdict)
            elif key in ("r", "R"):
                player.stop()
                traits = _traitements(e)
                refused.add(lvl)
                if not traits or len(refused) >= len(traits):   # tout est refuse
                    text = retirer(revue.refuser(e))
                else:
                    text = dire("proposition.niveau_refuse") + " " + advance()
            else:
                text = dire("proposition.touches")
            if not items:            # derniere entree tranchee : le verdict ferme la revue
                out(f"[{text}]\n")
                return text or dire("proposition.fermee")
            if text:
                out(f"[{text}]\n")
                say(text)
        return dire("proposition.fermee")
    finally:
        player.close()
