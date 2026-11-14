#!/usr/bin/env python3
"""Dongle radio fige (2026-11-14) : reconnu mais muet ("Async read stalled") ->
reinitialisation USB, au plus une fois par REINIT_ECART_SEC ; un simple indice
(passe rtl_power vide) n'y suffit pas seul. Lancer : python3 tests/test_dongle_fige.py
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "config")]
import dongle_fige  # noqa: E402


class Fige(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        sysd = os.path.join(self.tmp, "sys")
        for nom, vid, pid in (("1-1.1", "05e3", "0608"), ("1-1.1.1", "0bda", "2838"), ("1-1.1.1:1.0", "", "")):
            os.makedirs(os.path.join(sysd, nom))
            if vid:
                open(os.path.join(sysd, nom, "idVendor"), "w").write(vid + "\n")
                open(os.path.join(sysd, nom, "idProduct"), "w").write(pid + "\n")
        self.appels = []
        self.horloge = [1000.0]
        self.p = [mock.patch.object(dongle_fige, "ETAT", os.path.join(self.tmp, "etat.json")),
                  mock.patch.object(dongle_fige, "SYS", sysd),
                  mock.patch.object(dongle_fige, "APRES_REINIT_SEC", 0),
                  mock.patch.dict(sys.modules, {"requests": mock.Mock(get=lambda *a, **k: mock.Mock(
                      json=lambda: {"current": {"temperature_2m": 1.5, "relative_humidity_2m": 92}}))}),
                  mock.patch.object(dongle_fige.time, "time", lambda: self.horloge[0]),
                  mock.patch.object(dongle_fige.subprocess, "run",
                                    lambda cmd, **k: (self.appels.append(cmd) if cmd[0] == "sudo" else None)
                                    or mock.Mock(returncode=0, stdout="throttled=0x0", stderr=""))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preuve_nette(self):
        self.assertEqual(dongle_fige.trouver(dongle_fige.SYS), "1-1.1.1")
        self.assertTrue(dongle_fige.signaler("capteurs", "Async read stalled, exiting!"))
        self.assertEqual(self.appels, [dongle_fige.HELPER + ["1-1.1.1"]])
        self.horloge[0] += 60
        self.assertFalse(dongle_fige.signaler("capteurs", "Async read stalled"), "pas deux fois en 5 minutes")
        self.horloge[0] += 400
        self.assertTrue(dongle_fige.signaler("capteurs", "Async read stalled"))
        premiere, seconde = dongle_fige.reinits_depuis(0)
        self.assertIn("bride", premiere["contexte"])          # contexte garde pour comprendre les gels
        self.assertEqual((premiere["depuis_reinit_s"], seconde["depuis_reinit_s"]), (None, 460))

    def test_indice_seul_ne_suffit_pas(self):
        self.assertFalse(dongle_fige.signaler("rtl_power", "passe vide", sur=False))
        dongle_fige.remis("rtl_power")
        self.horloge[0] += 30
        self.assertFalse(dongle_fige.signaler("rtl_power", "passe vide", sur=False), "remis entre-temps")
        self.horloge[0] += 30
        self.assertTrue(dongle_fige.signaler("rtl_power", "passe vide", sur=False), "deux passes vides de suite")
        self.assertEqual(len(self.appels), 1)


if __name__ == "__main__":
    unittest.main()
