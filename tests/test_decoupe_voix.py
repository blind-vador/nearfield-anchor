"""Decoupe des longs textes pour les voix bornees (lib/decoupe_voix.py, 2026-11-21)."""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import decoupe_voix as d  # noqa: E402
import voix_externe  # noqa: E402

LONGUE = ("Le message indique que cet avion, parti de Lyon ce matin avec un retard important dû à la météo sur "
          "les Alpes, approche maintenant de sa destination et que les passagers devront patienter avant de "
          "débarquer car la porte est occupée.")


class Decoupe(unittest.TestCase):
    def test_phrases_entieres_et_regroupees(self):
        self.assertEqual(d.morceaux("Bonjour. Il fait beau.\nÀ demain", 180), ["Bonjour. Il fait beau. À demain"])
        self.assertEqual(d.morceaux("Un. Deux.", 5), ["Un.", "Deux."])

    def test_jamais_apres_un_petit_mot(self):
        for taille in (40, 60, 90, 120, 180):
            ms = d.morceaux(LONGUE, taille)
            self.assertEqual(" ".join(ms), LONGUE)
            for m in ms[:-1]:
                self.assertLessEqual(len(m), taille)
                self.assertNotIn(m.split()[-1].lower(), d.FAIBLES, (taille, m))

    def test_ponctuation_puis_mot_ouvrant(self):
        ms = d.morceaux(LONGUE, 180)
        self.assertTrue(ms[0].endswith("Alpes,"), ms)
        ms = d.morceaux("Le train de neuf heures arrive bientôt en gare de Lyon et le suivant partira en retard", 60)
        self.assertTrue(ms[1].startswith("et le suivant"), ms)

    def test_octets(self):
        t = "é" * 50 + " " + "à" * 50
        for m in d.morceaux(t, 80, mesure=d.octets):
            self.assertLessEqual(d.octets(m), 80)

    def test_google_utilise_la_decoupe(self):
        self.assertTrue(all(len(m) <= voix_externe.GOOGLE_MAX for m in voix_externe._morceaux(LONGUE * 3)))
        self.assertFalse(any(m.split()[-1].lower() in d.FAIBLES for m in voix_externe._morceaux(LONGUE * 3)[:-1]))


class FauxEdge:
    requetes = []

    class Communicate:
        def __init__(self, texte, voix, rate=None, pitch=None):
            FauxEdge.requetes.append(texte)
            self.texte = texte

        async def save(self, chemin):
            with open(chemin, "wb") as f:
                f.write(b"MP3:" + self.texte.encode())

        async def stream(self):
            yield {"type": "WordBoundary"}
            yield {"type": "audio", "data": b"[" + self.texte[:5].encode() + b"]"}


class Edge(unittest.TestCase):
    def test_court_d_un_bloc_long_en_phrases(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.mp3")
            FauxEdge.requetes = []
            asyncio.run(d.edge_enregistrer(FauxEdge, "Bonjour.", "fr-FR-DeniseNeural", p))
            self.assertEqual(FauxEdge.requetes, ["Bonjour."])
            long_ = " ".join([LONGUE] * 30)
            FauxEdge.requetes = []
            asyncio.run(d.edge_enregistrer(FauxEdge, long_, "fr-FR-DeniseNeural", p))
            self.assertGreater(len(FauxEdge.requetes), 1)
            self.assertTrue(all(r.endswith(".") and d.octets(r) <= d.EDGE_MAX_OCTETS for r in FauxEdge.requetes))
            with open(p, "rb") as f:
                self.assertEqual(f.read().count(b"[Le me"), len(FauxEdge.requetes))


if __name__ == "__main__":
    unittest.main()
