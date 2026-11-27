"""Arbitre du son (lib/audio_lock.py), 2026-11-27 : une phrase de la console prise sans attendre porte sa fin
au plus tard ; passee cette fin, son entree est purgee meme si personne ne l'a rendue. Les sons des logiciels
installes (compagnon_watch) ont la source "logiciels", plus "console"."""
import os
import sys
import tempfile
import time
import unittest

ICI = os.path.dirname(os.path.abspath(__file__))
for d in ("config", "lib"):
    sys.path.insert(0, os.path.join(ICI, "..", d))

import audio_lock  # noqa: E402


class ArbitreFin(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.mkdtemp()
        self.anciens = (audio_lock.REGISTRY, audio_lock.REGISTRY_LOCK)
        audio_lock.REGISTRY = os.path.join(self.dossier, "arbitre.json")
        audio_lock.REGISTRY_LOCK = os.path.join(self.dossier, "arbitre.lock")

    def tearDown(self):
        audio_lock.REGISTRY, audio_lock.REGISTRY_LOCK = self.anciens

    def test_entree_purgee_apres_sa_fin(self):
        audio_lock.prendre_sans_attendre("console", duree_max=0.2)
        gardee = audio_lock.prendre_sans_attendre("console")
        self.assertEqual(len(audio_lock._read()), 2)
        time.sleep(0.3)
        with audio_lock._Registry() as reg:
            ids = [e["id"] for e in reg.entries]
        self.assertEqual(ids, [gardee.id], "sans duree_max, rien ne change")

    def test_logiciels_ne_attendent_plus_une_phrase_perdue(self):
        audio_lock.prendre_sans_attendre("console", duree_max=0.2)
        time.sleep(0.3)
        debut = time.time()
        ticket = audio_lock.acquire("logiciels", timeout=5)
        self.assertLess(time.time() - debut, 1)
        audio_lock.release(ticket)

    def test_source_de_compagnon_watch(self):
        self.assertEqual(audio_lock.SCRIPT_SOURCES["compagnon_watch"], "logiciels")


if __name__ == "__main__":
    unittest.main(verbosity=2)
