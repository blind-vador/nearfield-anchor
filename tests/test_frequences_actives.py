"""Frequences actives, relais lus dans un texte, rejets gardes et repeches."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import wave

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("lib", "bridge", "config", "watchers"):
    sys.path.insert(0, os.path.join(RACINE, d))
import frequences_actives as fa  # noqa: E402
import nommage  # noqa: E402
import noise_signatures as ns  # noqa: E402
import radio_log  # noqa: E402
import rejets  # noqa: E402
import relais  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fa.PATH, fa.LOCK = os.path.join(self.tmp, "fa.json"), os.path.join(self.tmp, "fa.lock")
        relais.STORE_PATH = os.path.join(self.tmp, "absent", "relais.json")
        relais.LOCAL_PATH = os.path.join(self.tmp, "relais.json")
        self.nommage = nommage.PATH
        nommage.PATH = os.path.join(self.tmp, "nommage.json")
        fa._relais_trouve_vrai = fa._relais_trouve
        self.trouves = []
        fa._relais_trouve = lambda e, r: (self.trouves.append((e["freq_hz"], r)), relais.detecte(e["freq_hz"], r))

    def tearDown(self):
        fa._relais_trouve = fa._relais_trouve_vrai
        nommage.PATH = self.nommage
        shutil.rmtree(self.tmp)


class Actives(Base):
    def test_trafic_non_vocal_revisite_des_la_deuxieme_fois(self):
        fa.noter(446_100_000, "numerique", protocole="DMR")
        self.assertFalse(fa.bands())
        fa.noter(446_100_000, "numerique", protocole="DMR")
        b = fa.bands()
        self.assertEqual(b[0]["channels"], [446_100_000])
        self.assertFalse(fa.bands(), "visite deja faite : on attend")
        self.assertEqual(fa.resume()[2], 1)

    def test_pic_inconnu_doit_revenir(self):
        fa.noter(150_000_000, "signal")
        fa.noter(150_000_000, "signal")
        self.assertFalse(fa.bands())
        fa.noter(150_000_000, "signal")
        self.assertEqual(fa.bands()[0]["channels"], [150_000_000])

    def test_fm_diffusion_exclue(self):
        for _ in range(3):
            fa.noter(95_000_000, "voix", modulation="wfm")
        self.assertFalse(fa.bands())

    def test_tonalite_repetee_fait_un_relais(self):
        fa.noter(145_650_000, "voix", tonalite=123.0)
        self.assertFalse(self.trouves)
        fa.noter(145_650_000, "voix", tonalite=123.0)
        self.assertEqual(self.trouves[0][0], 145_650_000)
        self.assertIn("145650000", relais.load())

    def test_paire_au_decalage(self):
        for _ in range(2):
            fa.noter(431_000_000, "voix")
            fa.noter(438_600_000, "voix")
        self.assertTrue(any("va peut-être" in r for _, rs in self.trouves for r in rs))

    def test_hors_radioamateur_reste_frequence_active(self):
        fa.noter(467_000_000, "numerique", relais_dmr=True)
        self.assertEqual(self.trouves[0][0], 467_000_000)
        self.assertEqual(relais.load(), {})


class Textes(Base):
    def test_frequence_annoncee_est_apprise(self):
        relais.depuis_texte("Relais de Lyon sortie 145,650 T123 -0.600", 446_000_000, "test")
        r = relais.load()["145650000"]
        self.assertEqual((r["tonalite"], r["decalage_hz"]), (123, -600_000))
        self.assertIn(145_050_000, relais.bands()[0]["channels"], "l'entree est ecoutee aussi")

    def test_sans_indice_rien(self):
        self.assertEqual(relais.depuis_texte("rendez vous a 14.30 devant la gare"), [])
        self.assertEqual(relais.load(), {})

    def test_hors_radioamateur_avec_mhz(self):
        self.assertEqual(relais.depuis_texte("passez sur 162.025 MHz"), [162_025_000])
        self.assertTrue(fa.load()["162025000"]["annoncee"])

    def test_indicatif_de_relais_lu_en_morse(self):
        relais.depuis_texte("DE F1ZBV", 145_600_000, "morse")
        self.assertEqual(fa.load()["145600000"]["indicatif_relais"], "F1ZBV")
        self.assertEqual(self.trouves[0][0], 145_600_000)


class Rejets(Base):
    def setUp(self):
        super().setUp()
        self.anciens = radio_log.RADIO_LOG_DIR, rejets.REJETS_DIR, rejets.INDEX_PATH, rejets.LOCK_PATH
        radio_log.RADIO_LOG_DIR = self.tmp
        rejets.REJETS_DIR = os.path.join(self.tmp, "rejets")
        os.makedirs(rejets.REJETS_DIR)
        rejets.INDEX_PATH = os.path.join(rejets.REJETS_DIR, "index.json")
        rejets.LOCK_PATH = os.path.join(self.tmp, "r.lock")

    def tearDown(self):
        radio_log.RADIO_LOG_DIR, rejets.REJETS_DIR, rejets.INDEX_PATH, rejets.LOCK_PATH = self.anciens
        super().tearDown()

    def _wav(self, nom):
        chemin = os.path.join(self.tmp, nom)
        with wave.open(chemin, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x10\x20" * 32000)
        return chemin

    def test_garde_tous_les_rendus(self):
        chemin = rejets.garder(146_000_000, "nfm", "essai", "aucune voix prouvee",
                               [{"juge": "nfm", "wav": self._wav("a.wav")}, {"juge": "usb", "wav": self._wav("b.wav")}],
                               target="essai")
        self.assertTrue(os.path.isfile(chemin))
        e = rejets.entries()[0]
        self.assertEqual((e["juge"], e["rendus"][0]["juge"], e["target"]), ("nfm", "usb", "essai"))
        self.assertTrue(os.path.isfile(rejets.path_of(e["rendus"][0]["fichier"])))

    def test_trop_court_rien(self):
        self.assertIsNone(rejets.garder(1, "nfm", "x", "y", [{"juge": "nfm", "pcm": b"\0" * 100, "taux": 16000}]))


class Empreintes(unittest.TestCase):
    def test_empreinte_de_juge_ne_vaut_que_pour_lui(self):
        tmp = tempfile.mkdtemp()
        ancien = ns.SIGNATURES_NAS, ns.SIGNATURES_LOCAL
        ns.SIGNATURES_NAS = ns.SIGNATURES_LOCAL = os.path.join(tmp, "p.json")
        try:
            import numpy as np
            t = np.arange(16000 * 2) / 16000
            pcm = (8000 * np.sin(2 * np.pi * 800 * t)).astype("<i2").tobytes()
            fp = ns.fingerprint(pcm, 16000, "s16le")
            ns.add_signature({"freq_hz": 100_000_000, "label": "x", "fingerprint": fp, "juge": "am"})
            self.assertTrue(ns.matches_known_noise(100_000_000, pcm, 16000, "s16le", "am")[0])
            self.assertFalse(ns.matches_known_noise(100_000_000, pcm, 16000, "s16le", "nfm")[0])
            self.assertEqual(ns.oublier_ressemblantes(100_000_000, fp, "nfm"), 0)
            self.assertEqual(ns.oublier_ressemblantes(100_000_000, fp, "am"), 1)
        finally:
            ns.SIGNATURES_NAS, ns.SIGNATURES_LOCAL = ancien
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
