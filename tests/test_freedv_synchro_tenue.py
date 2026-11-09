"""FreeDV : une synchro attrapee par endroits n'est pas un FreeDV (2026-11-09).

Un faux freedv_rx rejoue le journal -v d'une prise ; seul compte la plus longue
suite de trames "sync: 1", convertie en secondes sur la duree de la prise."""
import os
import stat
import sys
import tempfile
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", "lib")]
import juges_sync  # noqa: E402

FAUX = """#!/bin/sh
cat "$FAUX_JOURNAL" >&2
head -c "$FAUX_SORTIE" /dev/urandom > "$6"
"""


class SynchroTenue(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.bin = os.path.join(self.d, "freedv_rx")
        with open(self.bin, "w") as f:
            f.write(FAUX)
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IEXEC)
        self.pcm = b"\x00\x00" * (8000 * 15)   # 15 s de prise

    def mesure(self, etats, sortie=8000 * 2 * 5):
        j = os.path.join(self.d, "j.txt")
        with open(j, "w") as f:
            f.write("".join(f"frame {i} sync: {e}\n" for i, e in enumerate(etats)))
        os.environ["FAUX_JOURNAL"], os.environ["FAUX_SORTIE"] = j, str(sortie)
        return juges_sync.freedv_mesure(self.bin, "700E", self.pcm)

    def test_synchro_eparpillee_refusee(self):
        # 6 % de trames, par paquets de 7 (0,56 s) : le faux 700E du 185,675 MHz
        etats = ("1" * 7 + "0" * 50) * 3 + "0" * 16
        self.assertIsNone(self.mesure(etats))

    def test_salve_tenue_acceptee(self):
        # 187 trames sur 15 s, 37 d'affilee (3 s) : une salve FreeDV de 2 s
        etats = "0" * 75 + "1" * 37 + "0" * 75
        m = self.mesure(etats)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(m["suite_s"], 3.0, delta=0.1)

    def test_suite_juste_sous_le_seuil(self):
        etats = "0" * 80 + "1" * 17 + "0" * 90     # 1,36 s
        self.assertIsNone(self.mesure(etats))


if __name__ == "__main__":
    unittest.main()
