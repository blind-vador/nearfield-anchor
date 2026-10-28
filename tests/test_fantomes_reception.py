#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test systematique anti-fantomes (2026-10-28).

Un "fantome" = une ouverture de decodeur + enregistrement + annonce de "fin de
reception" declenchee alors que RIEN en clair n'a ete capte (synchro validee
sans voix, relais au repos, signalisation, flux chiffre). Tous les chemins
numeriques du scanner doivent faire transiter leur decision par l'UNIQUE point
central lib radio_watch._reception_a_presenter(), mode-agnostique.

Ne necessite ni dongle ni dependances materielles : on extrait la fonction pure
du source et on la verifie par table de verite, puis on controle que chaque
chemin de ceremonie reference bien le gate. Lancer : python3 tests/test_fantomes_reception.py
"""
import ast
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(RACINE, "watchers", "radio_watch.py")


def _charger_source():
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def _extraire_fonction(source, nom):
    """Rend l'objet fonction `nom` execute en isolation (sans importer le
    module entier, donc sans effet de bord materiel). La fonction visee doit
    etre pure (aucune dependance externe)."""
    arbre = ast.parse(source)
    for noeud in arbre.body:
        if isinstance(noeud, ast.FunctionDef) and noeud.name == nom:
            code = ast.get_source_segment(source, noeud)
            espace = {}
            exec(compile(code, SRC, "exec"), espace)
            return espace[nom]
    raise AssertionError("fonction %s introuvable dans %s" % (nom, SRC))


def test_table_de_verite(presenter):
    # (protocol, muted, had_audio, autre) -> attendu
    cas = [
        # du contenu en clair a ete capte : on presente
        (("DMR", False, True, False), True, "voix DMR en clair"),
        (("NXDN", False, True, False), True, "voix NXDN en clair"),
        (("M17", False, True, False), True, "voix M17 en clair (mode non-DMR aussi)"),
        (("dPMR", False, True, False), True, "voix dPMR en clair"),
        # synchro validee mais aucun clair capte : fantome a eviter
        (("DMR", False, False, False), False, "DMR signalisation/IDLE, pas de voix"),
        (("NXDN", False, False, False), False, "NXDN synchro sans voix"),
        (("P25", False, False, False), False, "P25 synchro sans voix"),
        # chiffre : on note mais on ne presente pas
        (("DMR", True, True, False), False, "DMR chiffre (meme si des trames)"),
        (("TETRA", True, False, False), False, "chiffre sans audio"),
        # rien decode
        ((None, False, False, False), False, "aucun protocole"),
        ((None, True, False, False), False, "aucun protocole, muet"),
        # canal mixte : l'autre cote (FM) en clair suffit a presenter
        ((None, False, False, True), True, "FM seule d'un canal mixte"),
        (("DMR", True, False, True), True, "num chiffre mais FM claire (mixte)"),
        (("DMR", False, True, True), True, "num clair + FM (mixte)"),
    ]
    echecs = []
    for args, attendu, label in cas:
        obtenu = bool(presenter(*args))
        marque = "ok" if obtenu == attendu else "ECHEC"
        if obtenu != attendu:
            echecs.append("  [%s] %s : attendu %s, obtenu %s  args=%r"
                          % (marque, label, attendu, obtenu, args))
        print("  [%s] %s" % (marque, label))
    assert not echecs, "table de verite:\n" + "\n".join(echecs)


def test_structure_gate(source):
    """Tout chemin qui annonce une 'fin de reception' (radio.reception_terminee*)
    doit se trouver dans une fonction qui appelle aussi _reception_a_presenter :
    garde anti-regression contre un nouveau site de ceremonie non protege."""
    arbre = ast.parse(source)
    problemes = []
    for noeud in arbre.body:
        if not isinstance(noeud, ast.FunctionDef):
            continue
        seg = ast.get_source_segment(source, noeud) or ""
        if "radio.reception_terminee" in seg:
            if "_reception_a_presenter" not in seg:
                problemes.append(noeud.name)
            print("  [ok] %s : ceremonie gardee par le point central" % noeud.name)
    assert not problemes, ("ceremonie 'fin de reception' SANS gate central dans : "
                           + ", ".join(problemes))


def main():
    source = _charger_source()
    presenter = _extraire_fonction(source, "_reception_a_presenter")
    print("== table de verite (point central, mode-agnostique) ==")
    test_table_de_verite(presenter)
    print("== structure : chaque ceremonie passe par le gate ==")
    test_structure_gate(source)
    print("\nTOUS LES TESTS PASSENT")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print("\nECHEC :", exc)
        sys.exit(1)
