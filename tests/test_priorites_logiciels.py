"""Les logiciels installes (personnages, conversation attendue) passent avant les veilles de fond, meme avec un
reglage des priorites enregistre avant que cette source existe (2026-11-24 : la voix d un logiciel attendait la fin
des analyses silencieuses de la veille radio, puis parlait attenuee)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
for d in ("lib", "config"):
    sys.path.insert(0, os.path.join(ICI, "..", d))

import audio_lock  # noqa: E402


class Priorites(unittest.TestCase):
    def ordre(self, donnees):
        d = tempfile.mkdtemp()
        chemin = os.path.join(d, "priorites.json")
        if donnees is not None:
            with open(chemin, "w") as f:
                json.dump(donnees, f)
        with mock.patch.object(audio_lock, "SETTINGS", chemin),                 mock.patch.object(audio_lock, "SETTINGS_CACHE", os.path.join(d, "absent.json")):
            return audio_lock.settings(fresh=True)["ordre"]

    def test_reglage_ancien_sans_logiciels(self):
        o = self.ordre({"ordre": ["console", "alertes", "disques", "radio", "avions"]})
        self.assertEqual(o[:2], ["console", "logiciels"])
        self.assertLess(o.index("logiciels"), o.index("radio"))
        self.assertEqual(o[-2:], ["imprimante", "helicos"])          # les autres absentes restent en fin

    def test_choix_de_l_utilisateur_respecte(self):
        o = self.ordre({"ordre": ["radio", "console", "logiciels"]})
        self.assertEqual(o[:3], ["radio", "console", "logiciels"])

    def test_par_defaut(self):
        self.assertEqual(self.ordre(None)[:3], ["alertes", "console", "logiciels"])


if __name__ == "__main__":
    unittest.main()
