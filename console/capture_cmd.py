"""Commande "capture" (2026-10-19) : enregistre tout ce qui sort du
haut-parleur (lib/capture_son.py).

  capture demarre / commence / debut     lance l'enregistrement
  capture stop / fin / arrete            l'arrete et dit ou trouver le MP3
  capture                                ou on en est

La phrase de confirmation est dite AVANT de lancer l'enregistrement, et
l'arret se fait AVANT de parler : la capture ne garde pas la console qui
parle d'elle-meme.
"""
import os
import sys
import threading
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import capture_son  # noqa: E402
import matcher  # noqa: E402
from phrases import dire  # noqa: E402

DEBUT = {"demarre", "demarrer", "commence", "commencer", "debut", "start", "lance", "lancer", "go", "enregistre"}
FIN = {"stop", "fin", "arrete", "arreter", "stoppe", "termine", "terminer", "finis", "coupe"}


def _duree(sec):
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    if h:
        return dire("capture.duree_h", h=h, m=m)
    if m:
        return dire("capture.duree_m", m=m, s=s)
    return dire("capture.duree_s", s=s)


def _taille(octets):
    mo = octets / 1e6
    return dire("capture.taille", mo=(f"{mo:.1f}".replace(".", ",") if mo < 10 else str(round(mo))))


def run(line, say_and_wait, say):
    mots = set(matcher.normalize(line).split())
    if mots & FIN:
        infos, raison = capture_son.arreter()  # d'abord arreter : la reponse ne doit pas etre enregistree
        if raison:
            return dire(f"capture.{raison}")
        enc = infos["encodeur"]
        if enc is None:
            return dire("capture.disponible_wav", duree=_duree(infos["duree"]), nom=infos["nom"])

        def annoncer():  # le MP3 se fait en fond : on le dit quand il est pret
            enc.wait()
            if enc.returncode == 0 and os.path.exists(infos["mp3"]):
                say(dire("capture.mp3_pret", taille=_taille(os.path.getsize(infos["mp3"])), nom=infos["nom"]))
            else:
                say(dire("capture.mp3_rate"))
        threading.Thread(target=annoncer, daemon=True).start()
        return dire("capture.arretee", duree=_duree(infos["duree"]), attente=_duree(infos["estimation"]))
    if mots & DEBUT:
        if capture_son.en_cours():
            return dire("capture.deja")
        say_and_wait(dire("capture.demarre"))  # dite avant : hors de l'enregistrement
        _, raison = capture_son.demarrer()
        return dire(f"capture.{raison}") if raison else ""
    etat = capture_son.en_cours()
    if etat:
        return dire("capture.en_cours", duree=_duree(time.time() - etat["debut"]))
    return dire("capture.aucune_en_cours")
