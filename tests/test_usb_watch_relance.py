"""Port d'un vrai appareil (dongle radio) toujours bloque apres l'echelle de
reparation (2026-11-12) : on previent une fois, puis on refait l'echelle a
intervalles croissants au lieu d'abandonner pour toujours (dongle muet 9 h le
29/10 et 17 h le 20/10, alors qu'un second essai l'avait ramene)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import phrases  # noqa: E402
phrases.BASE_DIR = os.path.join(ICI, "..", "phrases")
import usb_watch  # noqa: E402

PORT = "1-1.1-port1"


class Relance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.t = [1_000_000.0]
        self.gestes, self.annonces = [], []
        self.p = [
            mock.patch.object(usb_watch, "PORTS_IGNORE_FILE", os.path.join(self.tmp, "ign.json")),
            mock.patch.object(usb_watch, "_ports", return_value={PORT: "default"}),
            mock.patch.object(usb_watch, "_port_peer", return_value=None),
            mock.patch.object(usb_watch, "_port_on_pi", return_value=False),
            mock.patch.object(usb_watch, "_jour", return_value=True),
            mock.patch.object(usb_watch, "_helper", side_effect=lambda a, t: self.gestes.append(a) or True),
            mock.patch.object(usb_watch, "_speak", side_effect=self.annonces.append),
            mock.patch.object(usb_watch.time, "time", side_effect=lambda: self.t[0]),
        ]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def passer(self, n, pas=15):
        for _ in range(n):
            self.t[0] += pas
            self.w.check_ports({})

    def test_essais_espaces(self):
        self.w = usb_watch.Watch()
        self.passer(12)
        self.assertEqual(self.gestes, ["port", "port-ancien"])
        self.assertEqual(len(self.annonces), 1)                 # prevenu une fois
        self.passer(4)                                          # 1 min : rien de plus
        self.assertEqual(len(self.gestes), 2)
        self.passer(20)                                         # 5 min passees : l'echelle est refaite
        self.assertEqual(self.gestes, ["port", "port-ancien", "port", "port-ancien"])
        self.assertEqual(len(self.annonces), 1)                 # en silence
        self.passer(20)                                         # le palier suivant est plus long
        self.assertEqual(len(self.gestes), 4)
        self.passer(30)
        self.assertEqual(len(self.gestes), 6)

    def test_reconnu_ensuite(self):
        self.w = usb_watch.Watch()
        self.passer(12)
        with mock.patch.object(usb_watch, "_ports", return_value={PORT: "configured"}):
            self.t[0] += 15
            self.w.check_ports({"1-1.1.1": {"produit": "NESDR SMArt v5"}})
        self.assertNotIn(PORT, self.w.port_given_up)
        self.assertIn("NESDR", self.annonces[-1])


if __name__ == "__main__":
    unittest.main()
