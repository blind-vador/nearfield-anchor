"""Phrases gardees et preparees a l'avance pour les logiciels installes (voix Gemini comprises)."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
for d in ("lib", "watchers", "tools", "config", "bridge"):
    sys.path.insert(0, os.path.join(ICI, "..", d))

import speak  # noqa: E402
import voix_sources  # noqa: E402

GEMINI = {"moteur": "gemini", "voix": "gemini:Charon", "debit": "+0%", "hauteur": "+0Hz", "aleatoire": False}


class Verrou:
    dropped = False


class PhrasesGardees(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.syntheses = []

        def fichier(texte, voix, debit=None):
            self.syntheses.append(texte)
            fd, p = tempfile.mkstemp(suffix=".wav", dir=self.d)
            os.write(fd, b"RIFF")
            os.close(fd)
            return p
        self.joues = []
        self.p = [mock.patch.object(speak, "TTS_CACHE_DIR", os.path.join(self.d, "cache")),
                  mock.patch.object(voix_sources, "choix_impose", return_value=dict(GEMINI)),
                  mock.patch("gemini_tts.fichier", side_effect=fichier),
                  mock.patch("audio_lock.acquire", return_value=Verrou()),
                  mock.patch("audio_lock.release"),
                  mock.patch.object(speak, "play_sound_effect", side_effect=lambda p, **k: self.joues.append(p))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.d)

    def test_nom_du_fichier_gemini(self):
        self.assertTrue(voix_sources.fichier_cache("/c", GEMINI).endswith("/gemini-Charon.wav"))

    def test_gardee_puis_rejouee_sans_synthese(self):
        speak.parler("Une validation t'attend.", voix="gemini:Charon", garder=True)
        speak.parler("Une validation t'attend.", voix="gemini:Charon", garder=True)
        self.assertEqual(self.syntheses, ["Une validation t'attend."])
        self.assertEqual(len(self.joues), 2)
        self.assertTrue(all(os.path.exists(p) for p in self.joues))         # l'echantillon garde reste

    def test_sans_garder_rien_ne_reste(self):
        speak.parler("Réponse du chef, unique.", voix="gemini:Charon")
        speak.parler("Réponse du chef, unique.", voix="gemini:Charon")
        self.assertEqual(len(self.syntheses), 2)
        self.assertFalse(any(os.path.exists(p) for p in self.joues))

    def test_preparer_puis_dire(self):
        self.assertTrue(speak.preparer("Bonjour, je suis l'ordonnateur.", voix="gemini:Charon"))
        self.assertEqual(self.joues, [])                                      # preparer ne dit rien
        speak.parler("Bonjour, je suis l'ordonnateur.", voix="gemini:Charon", garder=True)
        self.assertEqual(len(self.syntheses), 1)


class PreparationParLePointDAcces(unittest.TestCase):
    def test_controles_et_file(self):
        import extensions
        faites = []
        with mock.patch.object(speak, "preparer", side_effect=lambda t, **k: faites.append(t) or True), \
                mock.patch.object(extensions.time, "sleep"):
            cx = mock.Mock(id="logiciel")
            for mauvais in ({"textes": []}, {"textes": ["x"] * 201}, {"textes": [""]},
                            {"textes": ["ok"], "voix": 3}, {"textes": ["ok"], "debit": "vite"}):
                with self.assertRaises(extensions.Refus):
                    extensions.voix_preparer(cx, mauvais)
            extensions.voix_preparer(cx, {"textes": ["Un", "Deux  trois"], "voix": "gemini:Charon"})
            extensions._preparation["fil"].join(5)
        self.assertEqual(faites, ["Un", "Deux trois"])

    def test_phrases_deja_pretes_sautees_sans_pause(self):
        import extensions
        pauses, faites = [], []
        with mock.patch.object(speak, "deja_pret", side_effect=lambda t, **k: t.startswith("vieille")),                 mock.patch.object(speak, "preparer", side_effect=lambda t, **k: faites.append(t) or True),                 mock.patch.object(extensions.time, "sleep", side_effect=pauses.append):
            extensions._preparation["file"][:] = [("vieille %d" % i, "gemini:Charon", None) for i in range(50)] +                 [("neuve", "gemini:Charon", None)]
            extensions._preparer_la_file(7, 0.5)
        self.assertEqual((faites, pauses), (["neuve"], [7]))

    def test_trois_echecs_arretent_la_file(self):
        import extensions
        with mock.patch.object(speak, "preparer", return_value=False),                 mock.patch.object(speak, "deja_pret", return_value=False):
            extensions._preparation["file"][:] = [("p%d" % i, None, None) for i in range(10)]
            extensions._preparer_la_file(0, 0)
        self.assertEqual(extensions._preparation["file"], [])


if __name__ == "__main__":
    unittest.main()
