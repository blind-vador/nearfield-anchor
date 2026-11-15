#!/usr/bin/env python3
"""Phrases (2026-11-14, signale par l'utilisateur) :
- la copie deployee suit le depot : "antenne tiret bas direct point fin" quand un
  git pull a la main ne la recopiait pas ;
- la personnalite ne remplace plus un mot technique ("Nom de serre propose",
  "Changement de tuteur", "frequence arrosee") ;
- les fiches et resultats (revue, examen, station, frequences_ia, mesures) ne sont
  jamais reecrits. Lancer : python3 tests/test_phrases_protection.py
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "config")]
import phrases  # noqa: E402

DEPOT = os.path.join(RACINE, "phrases")


class Personnalite(unittest.TestCase):
    def setUp(self):
        self.neutre = phrases._read(os.path.join(DEPOT, "fr.ini"))

    def test_mots_techniques(self):
        e = self.neutre["revue.fiche_nom_simple"]
        self.assertIn("station", phrases.valid(e, "Nom de serre proposé : {nom}, pour {mhz} mégahertz, {bande}. {explication}"))
        e = self.neutre["revue.fiche_renommage"]
        self.assertTrue(phrases.termes_perdus(e["texte"], e["texte"].replace("Changement de nom", "Changement de tuteur")))
        e = self.neutre["revue.frequence_gardee"]
        self.assertTrue(phrases.termes_perdus(e["texte"], "{mhz} mégahertz est arrosée, le scanner continuera d'y fleurir."))
        # colorer autour reste permis, pluriel compris
        e = {"texte": "Liste vide. Tab pour ajouter une station.", "garder": []}
        self.assertIsNone(phrases.valid(e, "Liste vide. Tab pour ajouter une station, comme on sème une graine."))
        self.assertEqual(phrases.termes_perdus("Ajoute une station.", "Ajoute des stations, voyons."), [])
        self.assertEqual(phrases.termes_perdus("Tu regardes {nom}.", "Tu observes {nom}."), [], "regarde n'est pas garde, {nom} n'est pas un mot")

    def test_fiches_jamais_reecrites(self):
        for cle, e in self.neutre.items():
            if cle.split(".")[0] in ("revue", "examen", "station", "frequences_ia", "mesures", "antenne_direct",
                                     "audiographe", "systeme_veille"):
                self.assertFalse(phrases.rewritable(cle, e), cle)


class Copie(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.copie = os.path.join(self.tmp, "fr.ini")
        with open(self.copie, "w", encoding="utf-8") as f:
            f.write("[ancienne.phrase]\ntexte = vieille copie\n")
        # plus vieille que le depot (et non "il y a une heure" : le depot peut dater d'hier)
        vieux = os.path.getmtime(os.path.join(DEPOT, "fr.ini")) - 3600
        os.utime(self.copie, (vieux, vieux))
        self.p = [mock.patch.object(phrases, "BASE_DIR", self.tmp), mock.patch.dict(phrases._copie_vue, clear=True)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copie_suit_le_depot(self):
        self.assertEqual(phrases.dire("antenne_direct.fin"), phrases._read(os.path.join(DEPOT, "fr.ini"))["antenne_direct.fin"]["texte"])
        with open(self.copie, encoding="utf-8") as f:
            self.assertIn("[antenne_direct.fin]", f.read())


if __name__ == "__main__":
    unittest.main()
