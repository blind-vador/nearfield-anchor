"""Parole demandee par un logiciel installe : catalogue, voix imposee, controles."""
import os
import sys
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
for d in ("lib", "watchers", "tools", "config", "bridge"):
    sys.path.insert(0, os.path.join(ICI, "..", d))

import voix_sources  # noqa: E402

MS = ["fr-FR-DeniseNeural", "fr-FR-HenriNeural"]
LOCALES = ["piper:fr_FR-siwis-medium", "piper:mbrola#fr1"]
DISTANTES = ["piper:mac#Thomas", "piper:google#fr"]


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(voix_sources, "_microsoft_voix", return_value=MS),
                  mock.patch("piper_out.voix_locales", return_value=LOCALES),
                  mock.patch("piper_out.voix_distantes", return_value=DISTANTES),
                  mock.patch.object(voix_sources, "preferences",
                                    return_value={"piper": "desactive", "mbrola": "desactive"}),
                  mock.patch.object(voix_sources, "niveaux_mac", side_effect=lambda n: {x: "normale" for x in n}),
                  mock.patch.object(voix_sources, "_tout", return_value={})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_seulement_les_voix_permises_et_presentes(self):
        with mock.patch.object(voix_sources, "microsoft_ok", return_value=True):
            ids = [v["id"] for v in voix_sources.catalogue()]
        self.assertEqual(ids, MS + DISTANTES + ["espeak"])
        with mock.patch.object(voix_sources, "microsoft_ok", return_value=False):
            ids = [v["id"] for v in voix_sources.catalogue()]
        self.assertEqual(ids, DISTANTES + ["espeak"])

    def test_voix_imposee(self):
        with mock.patch.object(voix_sources, "microsoft_ok", return_value=True):
            c = voix_sources.choix_impose("fr-FR-HenriNeural", "logiciels", "+20%", "-5Hz")
            self.assertEqual((c["moteur"], c["voix"], c["debit"], c["hauteur"], c["aleatoire"]),
                             ("edge", "fr-FR-HenriNeural", "+20%", "-5Hz", False))
            c = voix_sources.choix_impose("piper:mac#Thomas", "logiciels")
            self.assertEqual((c["voix"], c["aleatoire"]), ("piper:mac#Thomas", False))
            with mock.patch.object(voix_sources, "choisir", return_value={"moteur": "edge", "voix": "x"}) as ch:
                c = voix_sources.choix_impose("piper:fr_FR-siwis-medium", "logiciels")  # desactivee
                ch.assert_called_once_with("logiciels")


class Requete(unittest.TestCase):
    def test_controles(self):
        import extensions
        cx = mock.Mock(id="x")
        with mock.patch("speak.parler", return_value="fr-FR-HenriNeural") as p:
            r = extensions.parler(cx, {"texte": "Bonjour\n  toi", "voix": "fr-FR-HenriNeural", "debit": "+25%"})
            self.assertEqual(r, {"dit": True, "voix": "fr-FR-HenriNeural"})
            p.assert_called_once_with("Bonjour toi", voix="fr-FR-HenriNeural", debit="+25%", hauteur=None)
            for mauvais in ({"texte": ""}, {"texte": "a" * 3001}, {"texte": "a", "debit": "vite"},
                            {"texte": "a", "debit": "+300%"}, {"texte": "a", "hauteur": "+90Hz"},
                            {"texte": "a", "voix": 3}):
                with self.assertRaises(extensions.Refus):
                    extensions.parler(cx, mauvais)
        with mock.patch("speak.parler", return_value=None):
            self.assertEqual(extensions.parler(cx, {"texte": "a"})["dit"], False)
        self.assertIn("parler", extensions.LONGUES)


if __name__ == "__main__":
    unittest.main()
