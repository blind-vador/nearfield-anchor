"""Console : appairage v2 SANS PAROLE (2026-11-16). Le Pi sonne ; la personne tape sur le
clavier du Pi le code que l'appareil lui dit, puis Entree. On verifie : bon code = appaire
(son de reussite) ; code faux = son d'erreur, on peut recommencer ; trois codes faux = fin
sans rien accepter ; Echap = annule ; rien n'est dit a voix haute (sauf F1, demande)."""
import os
import sys
import tempfile
import time
import unittest

TMP = tempfile.mkdtemp(prefix="essai_appairage_console_")
os.environ["NFA_HOME"] = os.path.join(TMP, "autopilot")
os.environ["NFA_USER_HOME"] = TMP
RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("console", "bridge", "lib", "watchers", "tools", "config"):
    sys.path.insert(0, os.path.join(RACINE, d))

import appairage  # noqa: E402
import sons  # noqa: E402


class FauxApp:
    def __init__(self):
        self.code, self.machine = "482913", {"nom": "Mac (Prismaforge)", "capacites": []}
        self.acceptee = False
        self.fermee = False

    def ouvrir(self):
        pass

    def etat(self):
        return "code"

    def accepter(self):
        self.acceptee = True
        return self.machine["nom"]

    def fermer(self):
        self.fermee = True


class FauxPont:
    APPAIRAGE_SEC = 30

    class np_:
        class ErreurPont(Exception):
            pass

    def __init__(self):
        self.app = FauxApp()

    def AppairageV2(self):
        return self.app


def touches(texte):
    time.sleep(1.5)  # le temps que la surveillance voie le code
    for t in texte:
        yield t


class AppairageSansParole(unittest.TestCase):
    def setUp(self):
        self.sons, self.dits = [], []
        self._vrai = sons.jouer
        sons.jouer = self.sons.append
        self.pont = FauxPont()

    def tearDown(self):
        sons.jouer = self._vrai

    def lancer(self, texte):
        return appairage._run_v2(self.pont, touches(texte), self.dits.append, lambda t: None)

    def test_bon_code(self):
        r = self.lancer("482913\r")
        self.assertIn("appairage_reussi", r)
        self.assertTrue(self.pont.app.acceptee)
        self.assertEqual(self.dits, [], "le Pi ne parle pas")
        self.assertEqual(self.sons[:2], ["appairage_ouvert", "appairage_code"])

    def test_code_faux_puis_juste(self):
        r = self.lancer("111111\r48297\x7f13\r")
        self.assertIn("appairage_erreur", self.sons)
        self.assertIn("appairage_reussi", r, "Retour arriere puis le bon chiffre")
        self.assertTrue(self.pont.app.acceptee)

    def test_trois_codes_faux(self):
        r = self.lancer("1\r2\r3\r482913\r")
        self.assertIn("appairage_erreur", r)
        self.assertFalse(self.pont.app.acceptee)
        self.assertTrue(self.pont.app.fermee)

    def test_echap(self):
        r = self.lancer("48\x1b")
        self.assertIn("appairage_annule", r)
        self.assertFalse(self.pont.app.acceptee)


if __name__ == "__main__":
    unittest.main(verbosity=2)
