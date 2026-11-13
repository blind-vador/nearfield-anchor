"""Effets sur la voix des annonces (2026-11-13) : chaque effet se pose sur un vrai
fichier, "aleatoire" tire un effet a chaque annonce, la console reste seche, et
l'etape de la commande voix enregistre le choix."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import phrases  # noqa: E402
phrases.BASE_DIR = os.path.join(ICI, "..", "phrases")
import effets_voix  # noqa: E402
import voix_sources  # noqa: E402

DOWN, UP = "\x1b[B", "\x1b[A"


def duree(chemin):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", chemin],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


class Effets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.voix = os.path.join(self.tmp, "voix.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=2", "-ar", "22050", "-ac", "1",
                        self.voix], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chaque_effet_se_pose(self):
        for nom in effets_voix.EFFETS:
            for ext in (".wav", ".mp3"):
                f = os.path.join(self.tmp, nom + ext)
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", self.voix, f], check=True)
                avant = open(f, "rb").read()
                self.assertTrue(effets_voix.cuire(f, nom), (nom, ext))
                self.assertNotEqual(open(f, "rb").read(), avant, nom)
                self.assertGreater(duree(f), 1.5, nom)
        self.assertFalse(effets_voix.cuire(self.voix, "inconnu"))

    def test_lecture_avec_effet(self):
        import sound_player
        lances = []
        vrai = subprocess.Popen

        def espion(args, **k):
            lances.append(args)
            if args[0] == "aplay":
                args = ["cat"]
                k["stdout"] = subprocess.DEVNULL
            return vrai(args, **k)
        with mock.patch.object(sound_player.subprocess, "Popen", espion), \
                mock.patch.object(sound_player.audio_lock, "dropped", lambda: False):
            sound_player.play_sound_effect(self.voix, label="t", effet="echo")
        self.assertIn("-af", lances[0])
        self.assertIn(effets_voix.filtre("echo"), lances[0])

    def test_tirage(self):
        with mock.patch.object(voix_sources, "reglage", lambda s: {"effet": "aleatoire"}):
            tires = {voix_sources.effet("trains") for _ in range(200)}
            self.assertGreater(len(tires), 5)
            self.assertTrue(tires <= set(effets_voix.EFFETS))
            self.assertIsNone(voix_sources.effet("console"))
            self.assertIsNone(voix_sources.effet("touches"))
        with mock.patch.object(voix_sources, "reglage", lambda s: {"effet": "aucun"}):
            self.assertIsNone(voix_sources.effet("trains"))
        with mock.patch.object(voix_sources, "reglage", lambda s: {"effet": "robot"}):
            self.assertEqual(voix_sources.effet("avions"), "robot")


class Dialogue(unittest.TestCase):
    def test_etape_effet(self):
        import voix
        import voix_cmd
        import piper_out
        ecoutes, dits = [], []

        def faux_fichier(texte, chemin, **k):
            shutil.copyfile(os.devnull, chemin)
            return True
        with mock.patch.object(voix, "say_blocking", lambda t, encore=None: dits.append(t)), \
                mock.patch.object(voix, "stop", lambda: None), \
                mock.patch.object(voix, "prechauffer", lambda t: None), \
                mock.patch.object(voix_cmd.conseils, "intro", lambda f, t, c: t), \
                mock.patch.object(piper_out, "to_file", faux_fichier), \
                mock.patch.object(effets_voix, "ecouter", lambda c, e: ecoutes.append(e)):
            r = voix_cmd._choisir_effet("piper:fr_FR-siwis-medium", "+0%", "aucun",
                                        iter([DOWN, DOWN, DOWN, "\r"]), lambda t: None)
            import time
            time.sleep(0.5)
        self.assertEqual(r, effets_voix.CHOIX[3])
        self.assertTrue(dits and dits[0].startswith(phrases.dire("voix.etape_effet")))


if __name__ == "__main__":
    unittest.main()
