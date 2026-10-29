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


def _traitements(p):
    return p.get("traitements") or []


def _line(items, i):
    p = items[i]
    n = len(_traitements(p))
    base = dire("proposition.ligne", index=i + 1, total=len(items), resume=p.get("resume", ""))
    if n:
        base += " " + dire("proposition.niveaux", nombre=nombre(n, "niveau", "niveaux"))
    return base


def run(line, key_source, say, out):
    items = propositions.entries()
    i = 0
    lvl, refused = 0, set()   # niveau courant + niveaux refuses de la proposition i
    player = Player()
    title = dire("proposition.titre", nombre=nombre(len(items), "proposition", "propositions"))
    intro = conseils.intro("proposition", title, dire("proposition.consigne"))
    out(intro + "\n")

    def play_brut():
        """Joue l'extrait BRUT de la proposition courante tout de suite (comme
        le journal : la fleche joue, pas d'annonce verbeuse). i donne le detail."""
        clip = _clip(items[i].get("brut")) if items else None
        if clip:
            player.play(clip)
        elif items:
            say(dire("proposition.pas_d_extrait"))

    def goto(new_i):
        nonlocal i, lvl, refused
        i, lvl, refused = new_i, 0, set()
        play_brut()

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

    try:
        if not items:
            say(f"{intro} {dire('proposition.vide')}")
            for key in key_source:
                if not conseils.is_help(key):
                    break
                say(dire("proposition.consigne"))
            return dire("proposition.fermee")
        say(intro)
        play_brut()  # le 1er brut joue d'emblee, sans attendre Entree

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
                    goto(j)  # navigue ET joue le brut aussitot, aucune annonce
                else:
                    text = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
            elif key in ("\r", "\n", "b", "B"):
                play_brut()  # rejoue le brut
            elif key in ("t", "T"):
                text = advance()
            elif key in ("i", "I"):
                text = _line(items, i)  # details a la demande (bande, proba, niveaux)
            elif key in ("c", "C"):
                traits = _traitements(items[i])
                action = traits[lvl]["action"] if (traits and lvl < len(traits)) else items[i].get("action")
                ok, detail = propositions.apply(action)
                propositions.set_etat(items[i]["id"], "confirme")
                items.pop(i)
                say(dire("proposition.confirmee", detail=detail) if ok
                    else dire("proposition.confirmee_manuelle", detail=detail))
                if items:
                    goto(min(i, len(items) - 1))
                else:
                    text = dire("proposition.plus_rien")
            elif key in ("r", "R"):
                traits = _traitements(items[i])
                refused.add(lvl)
                if not traits or len(refused) >= len(traits):   # tous les niveaux refuses
                    propositions.set_etat(items[i]["id"], "refuse")
                    items.pop(i)
                    say(dire("proposition.refusee"))
                    if items:
                        goto(min(i, len(items) - 1))
                    else:
                        text = dire("proposition.plus_rien")
                else:
                    text = dire("proposition.niveau_refuse") + " " + advance()
            else:
                text = dire("proposition.touches")
            if text:
                out(f"[{text}]\n")
                say(text)
        return dire("proposition.fermee")
    finally:
        player.close()
