#!/usr/bin/env python3
"""Carte de l'antenne entretenue en continu, audiographe actualise et antenne en
direct (2026-11-14). Lancer : python3 tests/test_carte_antenne.py
"""
import ast
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(ICI)
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import phrases  # noqa: E402
phrases.BASE_DIR = os.path.join(RACINE, "phrases")
phrases.PERSONA_DIR = tempfile.mkdtemp()
import carte_antenne  # noqa: E402

BANDES = [{"name": "radioamateur_2m", "name_spoken": "radioamateur 2 mètres", "type": "sweep",
           "low_hz": 144_000_000, "high_hz": 146_000_000, "step_hz": 12_500},
          {"name": "pmr", "type": "fm_channels"}]


def passe(f0, f1, pas, bruit=-40.0, pics=()):
    bins = []
    f = f0
    while f <= f1:
        db = bruit + (0.3 if int(f / pas) % 2 else 0.0)
        for pf, pdb in pics:
            if abs(f - pf) < pas:
                db = bruit + pdb
        bins.append((f, db))
        f += pas
    return bins


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.object(carte_antenne, "CARTE", os.path.join(self.tmp, "carte.json")),
                  mock.patch.object(carte_antenne, "BANDES", os.path.join(self.tmp, "bandes.json")),
                  mock.patch.object(carte_antenne, "DIRECT_DEMANDE", os.path.join(self.tmp, "dem.json")),
                  mock.patch.object(carte_antenne, "DIRECT_RESULTAT", os.path.join(self.tmp, "res.json"))]
        for x in self.p:
            x.start()
        carte_antenne._bandes.clear()
        carte_antenne.declarer(BANDES)

    def tearDown(self):
        for x in self.p:
            x.stop()
        carte_antenne._bandes.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)


class Carte(Base):
    def test_entretenue_a_chaque_passe(self):
        for k in range(5):
            carte_antenne.noter(passe(144e6, 146e6, 12_500, bruit=-40 + (6 if k == 4 else 0),
                                      pics=[(145.5e6, 20)]))
        e, = carte_antenne.lire()
        self.assertEqual((e["nom"], e["dit"]), ("radioamateur_2m", "radioamateur 2 mètres"))
        self.assertEqual(e["pics"], 1)
        self.assertAlmostEqual(e["pic_mhz"], 145.5, places=1)
        self.assertEqual(len(e["planchers"]), 5)
        self.assertEqual(carte_antenne.ecart_habituel(e), 6.0)     # parasite proche : le plancher monte

    def test_passe_etroite_et_inconnue(self):
        self.assertIsNone(carte_antenne.noter(passe(145.0e6, 145.2e6, 12_500)))   # une frequence : pas une carte
        e = carte_antenne.noter(passe(160e6, 170e6, 25_000))
        self.assertEqual(e["dit"], "160 à 170 mégahertz")
        self.assertEqual([x["nom"] for x in carte_antenne.lire()], ["160_170"])

    def test_bandes_relues_par_la_console(self):
        carte_antenne._bandes.clear()                       # autre processus : relit le fichier du scanner
        noms = [s["nom"] for s in carte_antenne.segments_direct()]
        self.assertEqual(noms, ["radioamateur_2m", "uhf_400_470"])

    def test_force(self):
        faible = carte_antenne.resumer(passe(400e6, 470e6, 100_000, pics=[(433.9e6, 8)]))
        fort = carte_antenne.resumer(passe(400e6, 470e6, 100_000, pics=[(433.9e6, 25)]))
        self.assertGreater(fort["force"], faible["force"])

    def test_demande_directe(self):
        self.assertIsNone(carte_antenne.direct_demande())
        carte_antenne.demander_direct("uhf_400_470")
        self.assertEqual(carte_antenne.direct_demande(), "uhf_400_470")
        plus_tard = time.time() + 60
        with mock.patch.object(carte_antenne.time, "time", lambda: plus_tard):
            self.assertIsNone(carte_antenne.direct_demande(), "console partie : fin du direct")
        carte_antenne.arreter_direct()
        self.assertIsNone(carte_antenne.direct_demande())


class Scanner(Base):
    def test_boucle_directe(self):
        with open(os.path.join(RACINE, "watchers", "radio_watch.py"), encoding="utf-8") as f:
            source = f.read()
        noeud = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "_antenne_direct")
        tours = []

        def mesure(nom):
            tours.append(nom)
            if len(tours) == 3:
                carte_antenne.arreter_direct()
            return carte_antenne.noter(passe(400e6, 470e6, 100_000), nom=nom, dit="UHF")
        espace = {"carte_antenne": types.SimpleNamespace(
                      direct_demande=carte_antenne.direct_demande, mesure_directe=mesure,
                      publier_direct=lambda r: publies.append(r)),
                  "meteo_locale": types.SimpleNamespace(set_scanner_state=lambda e: None), "time": time}
        publies = []
        exec(compile(ast.get_source_segment(source, noeud), "radio_watch", "exec"), espace)
        carte_antenne.demander_direct("uhf_400_470")
        self.assertTrue(espace["_antenne_direct"]())
        self.assertEqual([r["seq"] for r in publies], [1, 2, 3])
        self.assertIn("carte_antenne.noter(bins)", source)                 # chaque passe du scanner
        self.assertIn("carte_antenne.direct_demande() is not None", source)  # le cycle laisse la main


class Console(Base):
    def setUp(self):
        super().setUp()
        import antenne_cmd
        import audiographe
        self.cmd, self.ag = antenne_cmd, audiographe
        self.joues, self.sons = [], []
        self.q = [mock.patch.object(audiographe, "jouer", lambda c: self.joues.append(c)),
                  mock.patch.object(antenne_cmd, "_trop_chaud", lambda: False),
                  mock.patch.object(antenne_cmd.audio_lock, "acquire", lambda *a, **k: object()),
                  mock.patch.object(antenne_cmd.audio_lock, "release", lambda t: None)]
        for x in self.q:
            x.start()

    def tearDown(self):
        for x in self.q:
            x.stop()
        super().tearDown()

    def test_direct_sans_scanner(self):
        forces = iter([5, 5.5, 9, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4])

        def mesure(nom, segs=None):
            time.sleep(0.05)
            e = carte_antenne.noter(passe(400e6, 470e6, 100_000), nom=nom, dit=nom)
            return dict(e, force=next(forces, 4))

        def wav(c, g, f, mieux=False, moins=False, duree=1.5):
            self.sons.append((f, mieux, moins))
            return "x.wav"

        def touches():
            yield " "
            while len(self.sons) < 4:
                time.sleep(0.02)
            yield "\x1b[C"                    # fin de liste : on reste
            yield "\x1b"
        dit = []
        with mock.patch.object(self.cmd, "_scanner_actif", lambda: False), \
                mock.patch.object(carte_antenne, "mesure_directe", mesure), \
                mock.patch.object(self.ag, "wav_direct", wav), \
                mock.patch.object(self.cmd.conseils if hasattr(self.cmd, "conseils") else __import__("conseils"),
                                  "intro", lambda f, t, c: t):
            fin = self.cmd.direct(touches(), dit.append, lambda s: None)
        self.assertEqual(fin, phrases.dire("antenne_direct.fin"))
        self.assertTrue(dit[0].endswith("UHF 400 à 470 mégahertz."), dit[0])
        self.assertEqual(self.sons[:4], [(5, False, False), (5.5, False, False), (9, True, False), (4, False, True)])
        self.assertTrue(any(d.startswith(phrases.dire("commun.fin_liste")) for d in dit), dit)
        self.assertIsNone(carte_antenne.direct_demande())

    def test_audiographe_actualise_et_touche(self):
        for f0 in (144e6, 400e6, 160e6):
            carte_antenne.noter(passe(f0, f0 + 2e6 if f0 == 144e6 else f0 + 70e6 if f0 == 400e6 else f0 + 10e6, 25_000))
        dit = []
        voix = self.cmd.voix

        def parler(t):
            dit.append(t)
            voix._generation += 1               # comme voix.stop() en tete de say_blocking
        with mock.patch.object(voix, "say_blocking", parler):
            self.assertTrue(self.cmd._audiographe_carte())
        bandes = [d for d in dit if "il y a" in d and d != dit[0]]
        self.assertEqual([b.split(",")[0] for b in bandes], ["radioamateur 2 mètres", "160 à 170 mégahertz", "UHF 400 à 470 mégahertz"])
        self.assertEqual(len(self.joues), 2 + 3)
        # une touche pendant un balayage : on s'arrete a la bande suivante
        self.joues.clear()
        dit.clear()

        def jouer_puis_touche(c):
            self.joues.append(c)
            if len(self.joues) == 3:
                voix._generation += 1
        with mock.patch.object(voix, "say_blocking", parler), mock.patch.object(self.ag, "jouer", jouer_puis_touche):
            self.assertFalse(self.cmd._audiographe_carte())
        self.assertEqual(len(self.joues), 3)


if __name__ == "__main__":
    unittest.main()
