"""Wi-Fi connecte mais box muette (watchers/veille_systeme.py, _Box, 2026-11-18).

Le 18/11, le Pi est reste 48 min associe au Wi-Fi sans joindre la box, sans erreur de la
puce : rien ne reparait. On verifie : on coupe et rallume la connexion (pilote, puis puce
un essai sur deux), on le dit a chaque fois, on reessaie une minute plus tard (dix minutes
apres dix essais), un redemarrage apres cinq essais au plus une fois par heure, et rien
quand la panne est ailleurs.
"""
import os
import sys
import tempfile
import time
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("watchers", "lib", "config"):
    sys.path.insert(0, os.path.join(RACINE, d))
import veille_systeme as vs  # noqa: E402

BOX = "systeme_veille.reseau_box"


class BoxMuette(unittest.TestCase):
    def setUp(self):
        self.dits, self.gestes, self.redemarre = [], [], []
        self.revient = False
        vs._plus_tard = lambda f, *a: f(*a)          # tout de suite, dans l'essai
        vs.WIFI_REDEMARRAGE = os.path.join(tempfile.mkdtemp(), "wifi_redemarrage")
        self.box = vs._Box(gestes=self.gestes.append, parler=self.dits.append,
                           attendre=lambda s: self.revient, redemarrer=lambda: self.redemarre.append(1))

    def test_essais_annonces_puis_redemarrage(self):
        t = time.time()
        self.assertFalse(self.box.verifier(None, t))
        self.assertFalse(self.box.verifier("systeme_veille.reseau_internet", t), "panne derriere la box")
        self.assertTrue(self.box.verifier(BOX, t))
        self.assertEqual(self.gestes, ["pilote"])
        self.assertIn("J'éteins la connexion réseau du Pi et je la rallume. Essai 1.", self.dits[0])
        self.assertIn("dans une minute", self.dits[1])
        self.assertFalse(self.box.verifier(BOX, t + 30), "on attend la minute")
        for _ in range(4):
            self.assertTrue(self.box.verifier(BOX, self.box.prochain + 1))
        self.assertEqual(self.gestes, ["pilote", "bus", "pilote", "bus", "pilote"])
        self.assertEqual(self.redemarre, [1], "cinq essais : redemarrage")
        self.assertIn("Je redémarre le Pi", self.dits[-1])

    def test_pas_deux_redemarrages_dans_l_heure_puis_dix_minutes(self):
        open(vs.WIFI_REDEMARRAGE, "w").close()
        t = time.time()
        for _ in range(10):
            self.box.verifier(BOX, max(t, self.box.prochain) + 1)
        self.assertEqual(self.redemarre, [])
        self.assertIn("dans dix minutes", self.dits[-1])
        self.assertGreater(self.box.prochain - time.time(), 500)

    def test_retour(self):
        self.revient = True
        self.box.verifier(BOX)
        self.assertIn("répond de nouveau", self.dits[-1])
        self.assertEqual(self.box.essais, 0)
        self.box.verifier(None)
        self.assertEqual(self.box.essais, 0)


if __name__ == "__main__":
    unittest.main()
