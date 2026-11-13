#!/usr/bin/env python3
"""Plus de presomption de bande (2026-11-13, doctrine de l'utilisateur : on ne
retire un decodeur que s'il est physiquement impossible sur la frequence).

1. Bandes continues (aviation, PMR446, marine) : rtl_airband n'y rend qu'une
   modulation ; une emission sans voix dans cette modulation passe tout de
   suite au banc complet (toutes modulations, tous decodeurs).
2. 85-110 MHz : plus de "FM large, seule hypothese".
3. Mode manuel / exploration DMR : tous les decodeurs (il n'y avait que deux
   ports pour cinq), et l'analogique d'une station numerique n'est plus coupe
   d'office : l'arbitre le coupe sur synchro numerique validee.
Lancer : python3 tests/test_bandes_agnostiques.py
"""
import ast
import os
import sys
import time
import types
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(ICI)
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]


def _fonctions(rel, noms, espace):
    with open(os.path.join(RACINE, rel), encoding="utf-8") as f:
        source = f.read()
    for noeud in ast.parse(source).body:
        if isinstance(noeud, ast.FunctionDef) and noeud.name in noms:
            exec(compile(ast.get_source_segment(source, noeud), rel, "exec"), espace)
    return espace


class FmLarge(unittest.TestCase):
    def ordre(self, prouvees):
        iq = types.SimpleNamespace(prouver=lambda f: (prouvees, "mesure", None))
        prop = types.SimpleNamespace(suffisant=lambda f: None)
        with mock.patch.dict(sys.modules, {"modulation_iq": iq, "propositions": prop}):
            esp = _fonctions("watchers/radio_watch.py", ("_modulations_prouvees",), {"_derniere_preuve": {}})
            return esp["_modulations_prouvees"](86_200_000, "wfm")

    def test_plus_seule_hypothese(self):
        self.assertEqual(self.ordre([]), ["wfm", "nfm", "am", "usb", "lsb"])
        self.assertEqual(self.ordre(["wfm"]), ["wfm", "nfm", "am", "usb", "lsb"])

    def test_etroite_prouvee_passe_devant(self):
        self.assertEqual(self.ordre(["nfm"]), ["nfm", "wfm", "am", "usb", "lsb"])


class FenetresContinues(unittest.TestCase):
    def setUp(self):
        import airband_bridge
        self.ab = airband_bridge
        self.rejets = []
        ns = types.SimpleNamespace
        self.p = [mock.patch.object(airband_bridge, "audio_lock", ns(acquire=lambda: ns(silent=False),
                                                                     release=lambda l: None)),
                  mock.patch.object(airband_bridge, "radio_log", ns(RadioLogSession=lambda *a, **k: ns(discard=lambda: None))),
                  mock.patch.object(airband_bridge, "sound_player", ns(set_recording_session=lambda s: None)),
                  mock.patch.object(airband_bridge, "noise_signatures", ns(capture_ended=lambda: None)),
                  mock.patch.object(airband_bridge, "_au_rejet", lambda *a: self.rejets.append(a))]
        for x in self.p:
            x.start()
        airband_bridge._reprise.clear()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def relayer(self, secondes):
        paquet = b"\0" * int(self.ab.AIRBAND_SAMPLE_RATE_HZ * 4 * secondes)
        sock = mock.Mock()
        sock.recvfrom.side_effect = OSError
        with mock.patch.object(self.ab, "_prelisten", lambda q, f, j: ([paquet], False)):
            return self.ab._relay_channel(sock, 446_006_250, "446 civil", window_name="pmr446")

    def test_emission_sans_voix_au_banc_complet(self):
        """Du DMR sur le 446 : la FM etroite n'y trouve pas de voix -> banc complet."""
        self.assertFalse(self.relayer(3.0))
        self.assertEqual(self.ab.reprendre(), (446_006_250, "nfm", "446 civil", "pmr446"))
        self.assertIsNone(self.ab.reprendre(), "une seule reprise")

    def test_clic_de_silencieux_pas_repris(self):
        self.relayer(0.3)
        self.assertIsNone(self.ab.reprendre())
        self.assertEqual(len(self.rejets), 1)

    def test_boucle_reprend(self):
        with open(os.path.join(RACINE, "watchers", "radio_watch.py"), encoding="utf-8") as f:
            src = f.read()
        bloc = src[src.index("hit = airband_bridge.scan_window(obj, dwell_sec=dwell)"):][:600]
        self.assertIn("airband_bridge.reprendre()", bloc)
        self.assertIn("_handle_candidate(freq_r, mode_r, nom_r, target=fenetre_r)", bloc)


class Manuel(unittest.TestCase):
    def setUp(self):
        import manual_tuner
        self.mt = manual_tuner
        self.lances = []

        class Proc:
            stdout = iter(())

            def __init__(s, args, **k):
                self.lances.append(args)
        sock = mock.MagicMock()
        self.p = [mock.patch.object(manual_tuner.subprocess, "Popen", Proc),
                  mock.patch.object(manual_tuner.socket, "socket", lambda *a: sock),
                  mock.patch.object(manual_tuner.threading, "Thread", lambda **k: mock.Mock())]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def drapeaux(self, numerique):
        t = self.mt.ManualStream()
        t._start_decoders(446_100_000, t._generation, numerique=numerique)
        return [a[1] for a in self.lances if a[0] == "dsd-fme"], t

    def test_station_numerique_tous_les_decodeurs(self):
        flags, _ = self.drapeaux(True)
        self.assertEqual(flags, ["-fs", "-fm", "-fz", "-fi", "-fn"])

    def test_tous_lances(self):
        flags, _ = self.drapeaux(False)
        self.assertEqual(flags, ["-fa", "-fm", "-fz", "-fi", "-fn"])   # avant : seulement les deux premiers

    def test_arbitre(self):
        _, t = self.drapeaux(True)
        stats = {"clean": 0, "total": 0}
        lignes = iter(["sync"] * 12)

        def ligne(st, texte):
            st["clean"] += 1
            st["total"] += 1
        proc = types.SimpleNamespace(stdout=(l.encode() for l in lignes))
        with mock.patch.object(self.mt.decodeurs, "dsd_line", ligne):
            t._watch_dsd(proc, stats, t._generation)
        self.assertGreater(t._synchro_vue, time.time() - 1, "synchros validees : l'analogique se tait")


if __name__ == "__main__":
    unittest.main()
