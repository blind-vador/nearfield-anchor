"""Confiance dans la sortie d'un decodeur (2026-11-13) : sur un relais DMR, FreeDV,
BLU ou AM n'ouvrent plus ; une frequence neuve n'est bridee par rien ; un protocole
prouve dans la prise explique les autres sorties ; en direct, un juge qui a perdu la
confiance ecoute sans prendre la parole. Chiffres repris du Pi le 2026-11-13."""
import os
import shutil
import sys
import tempfile
import time
import unittest

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import phrases  # noqa: E402
phrases.BASE_DIR = os.path.join(ICI, "..", "phrases")
import confiance_juges as cj  # noqa: E402
import prise_unique  # noqa: E402
import propositions  # noqa: E402

RELAIS = 467_238_000


def faits(oui, non):
    t = time.time()
    return [{"t": t, "bon": True}] * oui + [{"t": t, "bon": False}] * non


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ancien = propositions.SCORES
        propositions.SCORES = os.path.join(self.tmp, "jf.json")
        cle = propositions._cle(RELAIS)
        propositions._ecrire_scores({"frequences": {cle: {
            "-fs": {"faits": faits(30, 0), "vu": 49, "a_priori": {"synchro relais DMR": {"poids": 2.0, "t": time.time()}}},
            "-fa": {"faits": faits(1, 0), "vu": 30},
            "freedv-700E": {"faits": faits(0, 10), "vu": 6},
            "freedv-1600": {"faits": faits(0, 5), "vu": 2},
            "lsb": {"faits": faits(1, 1)},
            "donnees": {"vu": 9}}}})

    def tearDown(self):
        propositions.SCORES = self.ancien
        shutil.rmtree(self.tmp, ignore_errors=True)


class Confiance(Base):
    def ouvre(self, freq, rendu, prouve=None):
        return cj.confiance(freq, rendu.get("mode") or rendu.get("juge"), rendu, prouve)[0] >= cj.SEUIL_OUVERTURE

    def test_relais_dmr(self):
        self.assertTrue(self.ouvre(RELAIS, {"mode": "-fs", "confirme": True, "protocole": "DMR"}))
        self.assertTrue(self.ouvre(RELAIS, {"mode": "-fa", "confirme": True, "protocole": "DMR"}))
        self.assertFalse(self.ouvre(RELAIS, {"mode": "freedv-700E", "confirme": True}))
        self.assertFalse(self.ouvre(RELAIS, {"juge": "lsb", "detail": {"part": 0.7}}))

    def test_frequence_neuve(self):
        for rendu in ({"mode": "freedv-700E", "confirme": True}, {"juge": "nfm", "detail": {"part": 0.3}},
                      {"mode": "-fi", "confirme": False, "protocole": "NXDN"}):
            self.assertTrue(self.ouvre(145_500_000, rendu), rendu)

    def test_protocole_prouve_explique(self):
        rendu = {"mode": "freedv-700E", "confirme": True}
        self.assertTrue(self.ouvre(145_500_000, rendu))
        self.assertFalse(self.ouvre(145_500_000, rendu, prouve="DMR"))       # le DMR lu en BLU
        self.assertTrue(self.ouvre(145_500_000, {"mode": "-fs", "confirme": True, "protocole": "DMR"}, "DMR"))

    def test_trier(self):
        rendus = [{"mode": "freedv-700E", "confirme": True}, {"mode": "-fs", "confirme": False, "protocole": "DMR"}]
        ouvrables, ecartes = cj.trier(RELAIS, rendus)
        self.assertEqual([r["mode"] for r in ouvrables], ["-fs"])
        self.assertEqual([r["mode"] for r in ecartes], ["freedv-700E"])
        self.assertIn("DMR", cj.raison(ecartes[0]["confiance"]))

    def test_cote_basse_sans_refus_ouvre(self):
        """Doctrine : un NXDN confirme sur un relais DMR connu, jamais refuse, s'ouvre."""
        ouvrables, ecartes = cj.trier(RELAIS, [{"mode": "-fi", "confirme": True, "protocole": "NXDN"}])
        self.assertEqual([r["mode"] for r in ouvrables], ["-fi"])
        # un autre protocole prouve dans la prise l'explique : ecarte
        ouvrables, ecartes = cj.trier(RELAIS, [{"mode": "-fi", "confirme": False, "protocole": "NXDN"}], prouve="DMR")
        self.assertEqual([r["mode"] for r in ecartes], ["-fi"])

    def test_confiance_retrouvee(self):
        """Valide plusieurs fois depuis les rejets, le juge rouvre."""
        for _ in range(12):
            propositions._noter(RELAIS, "freedv-700E", True)
        self.assertTrue(self.ouvre(RELAIS, {"mode": "freedv-700E", "confirme": True}))


class Direct(Base):
    def test_arbitre(self):
        hist = cj.historique(RELAIS)
        # Agnostique (2026-11-13) : NXDN et dPMR ont une cote basse sur ce relais DMR,
        # mais personne ne les a refuses : ils gardent la parole.
        self.assertLess(cj.a_priori(RELAIS, "-fi", hist), cj.SEUIL_OUVERTURE)
        self.assertEqual({m for m in ("-fs", "-fi", "-fm") if cj.bloque(RELAIS, m, hist)}, set())
        for _ in range(4):   # refuse quatre fois ici : il ecoute sans parler
            propositions._noter(RELAIS, "-fi", False)
        hist = cj.historique(RELAIS)
        bloques = {m for m in ("-fs", "-fi", "-fm") if cj.bloque(RELAIS, m, hist)}
        self.assertEqual(bloques, {"-fi"})
        a = prise_unique.Arbitre()
        a.bloques = bloques
        self.assertFalse(a.prendre("-fi", "NXDN"))
        self.assertTrue(a.prendre("-fs", "DMR"))


if __name__ == "__main__":
    unittest.main()
