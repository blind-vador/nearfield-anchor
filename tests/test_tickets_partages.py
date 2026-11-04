"""Tickets partages : prises, contre-epreuves, conclusion (sans decodeur reel)."""
import os
import sys
import tempfile
import unittest
import wave

RACINE_DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE_DEPOT, "bridge"))
import pont_protocole as proto  # noqa: E402
import tickets_partages as tp  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

OUTILS = {"dsd": "dsd-fme", "multimon": "multimon-ng"}
EPS = ["dsd-fa", "dsd-fs", "multimon"]


class Tickets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.racine = self.tmp.name
        self.pi, self.mac = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        self.cles = {"pi": proto.public_b64(self.pi), "mac": proto.public_b64(self.mac)}
        son = os.path.join(self.racine, "b.wav")
        with wave.open(son, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b"\0\0" * 4800)
        self.tid = tp.ouvrir(self.racine, self.pi, "pi", {"son.wav": son}, {"origine": "brut"}, EPS)
        tp.PRISE_ATTENTE_SEC = 0

    def tearDown(self):
        self.tmp.cleanup()

    def lire(self):
        return tp.lire(self.racine, self.tid, self.cles)

    def test_personne_ne_refait_ce_qui_est_pris(self):
        self.assertEqual(tp.prendre(self.racine, self.tid, "mac", "Mac", self.mac, self.cles, EPS), EPS)
        t, a = self.lire()
        self.assertEqual(tp.a_prendre(t, a, "pi", 9, OUTILS), [])

    def test_negatif_jamais_repasse_positif_repasse(self):
        tp.rendre(self.racine, self.tid, "mac", "Mac", self.mac, {
            "dsd-fa": ({"propose": False, "propres": 0}, None),
            "dsd-fs": ({"propose": True, "propres": 20, "protocole": "DMR"}, None)})
        t, a = self.lire()
        self.assertEqual(tp.a_prendre(t, a, "pi", 9, OUTILS), ["dsd-fs", "multimon"])
        self.assertEqual(tp.a_prendre(t, a, "mac", 9, OUTILS), ["multimon"])

    def test_accord_etablit_desaccord_garde_le_doute(self):
        pos = {"propose": True, "propres": 20, "protocole": "DMR"}
        tp.rendre(self.racine, self.tid, "mac", "Mac", self.mac, {
            "dsd-fa": ({"propose": True, "propres": 9, "protocole": "P25"}, None), "dsd-fs": (pos, None),
            "multimon": ({"propose": False, "messages": []}, None)})
        t, a = self.lire()
        self.assertFalse(tp.conclure(t, a, lambda e, m: True)["fini"])
        tp.rendre(self.racine, self.tid, "pi", "pi", self.pi, {
            "dsd-fa": ({"propose": True, "propres": 9, "protocole": "NXDN"}, None), "dsd-fs": (pos, None)})
        t, a = self.lire()
        c = tp.conclure(t, a, lambda e, m: True)
        self.assertTrue(c["fini"])
        self.assertEqual(c["etablies"], ["dsd-fs"])
        self.assertEqual(list(c["desaccords"]), ["dsd-fa"])
        self.assertTrue(c["verdicts"]["dsd-fa"]["propose"])
        self.assertTrue(c["verdicts"]["dsd-fa"]["doute"])
        self.assertIn("dsd-fa", c["seules"])

    def test_seul_si_personne_ne_peut_repasser(self):
        tp.rendre(self.racine, self.tid, "pi", "pi", self.pi, {
            "dsd-fa": ({"propose": False}, None), "dsd-fs": ({"propose": False}, None),
            "multimon": ({"propose": True, "messages": ["POCSAG: x"]}, None)})
        t, a = self.lire()
        c = tp.conclure(t, a, lambda e, m: False)
        self.assertEqual(c["seules"], ["multimon"])
        self.assertTrue(c["fini"])

    def test_faux_retour_ignore(self):
        faux = Ed25519PrivateKey.generate()
        tp.rendre(self.racine, self.tid, "mac", "Mac", faux, {"dsd-fa": ({"propose": True}, None)})
        _, a = self.lire()
        self.assertEqual(a, {})

    def test_ne_prend_pas_ce_quon_ne_sait_pas(self):
        t, a = self.lire()
        self.assertEqual(tp.a_prendre(t, a, "mac", 9, {"dsd": "x"}), ["dsd-fa", "dsd-fs"])


if __name__ == "__main__":
    unittest.main()
