"""Plan phase 6 (2026-11-11) : a priori des frequences voisines, visites par bandit,
debit symbole estime a l'aveugle pour ordonner les juges, fausses alarmes et
prises manquees mesurees."""
import os
import random
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import phrases  # noqa: E402
phrases.BASE_DIR = os.path.join(ICI, "..", "phrases")
import debit_symbole  # noqa: E402
import frequences_actives as fa  # noqa: E402
import mesures_scanner  # noqa: E402
import propositions  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        j = lambda n: os.path.join(self.tmp, n)  # noqa: E731
        self.anciens = (propositions.SCORES, fa.PATH, fa.LOCK, mesures_scanner.PATH, mesures_scanner.LOCK)
        propositions.SCORES, fa.PATH, fa.LOCK = j("jf.json"), j("fa.json"), j("fa.lock")
        mesures_scanner.PATH, mesures_scanner.LOCK = j("mesures.json"), j("m.lock")

    def tearDown(self):
        (propositions.SCORES, fa.PATH, fa.LOCK, mesures_scanner.PATH, mesures_scanner.LOCK) = self.anciens
        shutil.rmtree(self.tmp, ignore_errors=True)


class Voisines(Base):
    def test_a_priori_du_segment(self):
        for _ in range(3):
            propositions._noter(438_525_000, "-fs", True)
        une = propositions.rang(438_600_000, "-fs")
        self.assertGreater(une, 0.55)                                          # voisine sans souvenir
        for _ in range(3):
            propositions._noter(438_400_000, "-fs", True)
        self.assertGreater(propositions.rang(438_600_000, "-fs"), une)         # deux voisines : plus net
        self.assertLess(propositions.rang(438_600_000, "-fs"), 0.7)            # mais reste un indice
        self.assertEqual(propositions.rang(145_500_000, "-fs"), 0.5)           # autre bande
        self.assertEqual(propositions.rang(438_600_000, "-fd"), 0.5)           # autre juge
        for _ in range(4):
            propositions._noter(438_600_000, "-fs", False)
        self.assertLess(propositions.rang(438_600_000, "-fs"), 0.4)           # ses propres avis l'emportent
        self.assertIsNone(propositions.en_tete(438_600_000))                   # l'a priori n'impose rien


class Bandit(Base):
    def test_revient_plus_sur_ce_qui_rend(self):
        t0 = time.time()
        for f in (160_000_000, 161_000_000):
            fa.noter(f, "voix")
            fa.noter(f, "voix")
        data = fa.load()
        for f, utile in ((160_000_000, True), (161_000_000, False)):
            e = data[fa._cle(f)]
            e.pop("utile_le", None)                # les deux notes de depart sont anterieures
            e["heures"] = [0] * 24
            for k in range(6):   # six visites passees
                e["visite"] = t0 - 3600 * (k + 1)
                if utile:
                    e["utile_le"] = e["visite"] + 60
                fa._regler_visite(e, t0 - 3600 * k)
        rng = random.Random(4)
        bon = np.mean([fa.tirage(data[fa._cle(160_000_000)], t0, rng) for _ in range(300)])
        vide = np.mean([fa.tirage(data[fa._cle(161_000_000)], t0, rng) for _ in range(300)])
        self.assertGreater(bon, 0.7)
        self.assertLess(vide, 0.35)

    def test_reglement_d_une_visite(self):
        fa.noter(150_000_000, "donnees")
        fa.noter(150_000_000, "donnees")
        with mock.patch("ignores.frequence_ignoree", return_value=False), \
                mock.patch("interets.disabled", return_value=False), \
                mock.patch.object(fa, "_deja_suivie", return_value=False):
            self.assertEqual(fa.bands()[0]["channels"], [150_000_000])
            e = fa.load()[fa._cle(150_000_000)]
            fa._regler_visite(e, e["visite"] + 1)                 # trop tot : pas encore regle
            self.assertNotIn("bandit", e)
            fa._regler_visite(e, e["visite"] + fa.REGLEMENT_SEC)
            self.assertEqual(e["bandit"]["b"], 1.0)                # rien d'utile apres la visite


class DebitSymbole(unittest.TestCase):
    def signal(self, debit, fs=48000, bruit=3000):
        rng = np.random.default_rng(debit)
        sps = fs // debit
        x = np.repeat(rng.choice([-3, -1, 1, 3], size=fs * 4 // sps), sps).astype(float)
        x = np.convolve(x, np.ones(max(sps // 2, 1)) / max(sps // 2, 1), "same")
        return (x * 3000 + rng.normal(0, bruit, len(x))).astype(np.float32)

    def test_debits(self):
        for debit in (1200, 2400, 4800, 9600):
            self.assertEqual(debit_symbole.estimer(self.signal(debit), 48000)[0], debit)
        rng = np.random.default_rng(0)
        self.assertIsNone(debit_symbole.estimer(rng.normal(0, 3000, 48000 * 4).astype(np.float32), 48000)[0])
        t = np.arange(48000 * 4) / 48000
        voix = (np.sin(2 * np.pi * 300 * t) * np.sin(2 * np.pi * 3 * t) * 8000).astype(np.float32)
        self.assertIsNone(debit_symbole.estimer(voix, 48000)[0])

    def test_ordre_des_juges(self):
        import juges_watch
        with mock.patch.object(propositions, "rang", return_value=0.5):
            ordre = juges_watch.ordre({"bandes": {}}, "x", list(juges_watch.JUGES), 446_000_000, debit=2400)
        self.assertEqual(set(ordre[:2]), {"-fi", "-fm"})       # NXDN48, dPMR devant
        self.assertEqual(len(ordre), len(juges_watch.JUGES))   # aucun retire


class Mesures(Base):
    def test_precision_et_rappel(self):
        self.assertEqual(mesures_scanner.phrase(), "")
        for _ in range(20):
            mesures_scanner.noter("prise", "bande aviation")
        for _ in range(5):
            mesures_scanner.noter("fausse_alarme", "bande aviation")
        mesures_scanner.noter("manquee", "bande aviation")
        for _ in range(30):
            mesures_scanner.noter("rejet", "bande aviation")
        c = mesures_scanner.totaux()["bande aviation"]
        precision, rappel, ecartees = mesures_scanner.taux(c)
        self.assertAlmostEqual(precision, 0.75)
        self.assertAlmostEqual(rappel, 15 / 16)
        self.assertAlmostEqual(ecartees, 0.6)
        p = mesures_scanner.phrase()
        self.assertIn("75 % de bonnes", p)
        self.assertIn("bande aviation", p)


if __name__ == "__main__":
    unittest.main()
