"""Commande "cherche des sons" : telecharger des sons pour en faire des sonneries.

  haut / bas        le fournisseur
  Tab               le mot de recherche, Entree cherche
  haut / bas        les sons trouves : le nom, puis le son
  Espace            coche ou decoche le son ; les coches restent d'une recherche a l'autre
  I                 auteur et licence
  Tab               un autre mot ; Maj+Tab : un autre fournisseur
  Entree            telecharge les sons coches dans le depot des sonneries
  Echap ferme. F1 : les touches.

Le clavier n'attend jamais le son : phrase et ecoute partent dans un fil, et une
autre fleche les annule (comme le journal).
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import conseils
import keys
import sons_en_ligne as sl
import voix
from journal import Player
from phrases import dire, nombre

UP, DOWN = "\x1b[A", "\x1b[B"
TAB, SHIFT_TAB = "\t", "\x1b[Z"
ENTREE = ("\r", "\n")
ESC, ESPACE = "\x1b", " "


def _duree(son):
    secondes = son.get("duree")
    return f", {nombre(max(1, round(secondes)), 'seconde')}" if secondes else ""


def _phrase(son, coche):
    return dire("sons_ligne.coche_devant", titre=son["titre"]) + _duree(son) if coche else son["titre"] + _duree(son)


def run(line, key_source, say, out, read_line):
    fournisseurs = sl.disponibles()
    i_f = 0
    sons, i = [], -1
    coches = {}
    consigne = dire("sons_ligne.consigne")
    intro = conseils.intro("sons_ligne", dire("sons_ligne.titre"), consigne)
    player = Player()
    apercus = sl.Apercus()
    avance = ThreadPoolExecutor(max_workers=2)
    verrou = threading.Lock()
    tour = [0]
    fil = [None]

    def couper():
        with verrou:
            tour[0] += 1
            player.stop()
        voix.stop()

    def ecouter(mien, phrase, son, suivant):
        def encore():
            return tour[0] == mien
        try:
            pret = avance.submit(apercus.fichier, son)
            if suivant:                     # le suivant se prepare : la fleche sera immediate
                avance.submit(apercus.fichier, suivant)
            voix.say_blocking(phrase, encore=encore)
            chemin = pret.result()
            if not encore():
                return
            if not chemin:
                voix.say_blocking(dire("sons_ligne.ecoute_impossible"), encore=encore)
                return
            with verrou:
                if encore():
                    player.play(chemin)
        except Exception as exc:
            print(f"[sons_en_ligne] ecoute : {exc!r}")

    def jouer(avant=""):
        couper()
        son = sons[i]
        phrase = _phrase(son, son["url"] in coches)
        out(phrase + "\n")
        suivant = sons[i + 1] if i + 1 < len(sons) else None
        fil[0] = threading.Thread(target=ecouter, args=(tour[0], f"{avant} {phrase}".strip(), son, suivant), daemon=True)
        fil[0].start()

    def parler(texte):
        out(texte + "\n")
        say(texte)

    def demander_mot():
        """Lit le mot et cherche. Vrai si des sons sont en liste ; mot vide : retour au fournisseur."""
        nonlocal sons, i
        while True:
            couper()
            parler(dire("sons_ligne.mot"))
            mot = " ".join(read_line(key_source).split())
            if not mot:
                sons, i = [], -1
                parler(dire("sons_ligne.fournisseur", nom=sl.FOURNISSEURS[fournisseurs[i_f]]))
                return False
            voix.say(dire("sons_ligne.recherche", mot=mot))
            try:
                trouves = sl.chercher(fournisseurs[i_f], mot)
            except Exception as exc:
                print(f"[sons_en_ligne] recherche : {type(exc).__name__}")
                voix.say_blocking(dire("sons_ligne.recherche_impossible"))
                continue
            if not trouves:
                voix.say_blocking(dire("sons_ligne.aucun", mot=mot))
                continue
            sons, i = trouves, 0
            jouer(nombre(len(sons), "son") + ".")
            return True

    def telecharger():
        couper()
        choisis = list(coches.values())
        voix.say(dire("sons_ligne.telechargement", sons=nombre(len(choisis), "son")))
        poses = sum(1 for son in choisis if sl.deposer(son, apercus))
        if poses == len(choisis):
            return dire("sons_ligne.fini", sons=nombre(poses, "son"))
        return dire("sons_ligne.fini_partiel", sons=nombre(poses, "son"),
                    echecs=nombre(len(choisis) - poses, "son"))

    try:
        out(intro + "\n")
        say(f"{intro} {dire('sons_ligne.fournisseur', nom=sl.FOURNISSEURS[fournisseurs[0]])}")
        for key in key_source:
            if key == keys.WAKE:
                continue
            if key == ESC:
                break
            if conseils.is_help(key):
                couper()
                parler(consigne)
            elif not sons:                                   # le fournisseur
                if key in (UP, DOWN):
                    i_f = (i_f + (1 if key == DOWN else -1)) % len(fournisseurs)
                    parler(dire("sons_ligne.fournisseur", nom=sl.FOURNISSEURS[fournisseurs[i_f]]))
                elif key == TAB or key in ENTREE:
                    demander_mot()
                else:
                    parler(dire("sons_ligne.aide_fournisseur"))
            elif key in (UP, DOWN):                          # la liste
                n = i + (1 if key == DOWN else -1)
                if 0 <= n < len(sons):
                    i = n
                    jouer()
                else:
                    couper()
                    parler(dire("commun.debut_liste") if n < 0 else dire("commun.fin_liste"))
            elif key == ESPACE:                              # l'ecoute continue
                son = sons[i]
                if son["url"] in coches:
                    del coches[son["url"]]
                    parler(dire("sons_ligne.decoche", total=len(coches)))
                else:
                    coches[son["url"]] = son
                    parler(dire("sons_ligne.coche", total=len(coches)))
            elif key in ("i", "I"):
                son = sons[i]
                parler(dire("sons_ligne.infos", auteur=son["auteur"] or dire("sons_ligne.auteur_inconnu"),
                            licence=son["licence"] or dire("sons_ligne.licence_inconnue")))
            elif key == TAB:
                demander_mot()
            elif key == SHIFT_TAB:
                couper()
                sons, i = [], -1
                parler(dire("sons_ligne.fournisseur", nom=sl.FOURNISSEURS[fournisseurs[i_f]]))
            elif key in ENTREE:
                if not coches:
                    parler(dire("sons_ligne.rien_coche"))
                else:
                    return telecharger()
            else:
                parler(dire("sons_ligne.aide_liste"))
        return dire("sons_ligne.ferme")
    finally:
        couper()
        player.close()
        avance.shutdown(wait=False, cancel_futures=True)
        apercus.fermer()
