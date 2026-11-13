"""Series de la nuit (2026-11-13) : prises a voix claire regroupees par frequence,
transcrites une fois, une seule analyse par frequence sur leurs textes concatenes."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import phrases  # noqa: E402
phrases.BASE_DIR = os.path.join(ICI, "..", "phrases")
import compagnon  # noqa: E402
import examen  # noqa: E402
import series_nuit as sn  # noqa: E402

F1, F2 = 160_650_000, 446_006_250


def note(fichier, freq, le, **k):
    n = {"fichier": fichier, "freq_hz": freq, "capture_le": le, "label": "bande pro", "mode": "nfm",
         "mesures": {"juge": "nfm"}}
    n.update(k)
    return n


class Series(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "radio_log")
        os.makedirs(self.log)
        self.notes = [
            note("a.mp3", F1, "2026-11-10T09:00:00", validation_rapide=True),
            note("b.mp3", F1, "2026-11-12T14:05:00", degres={"contenu": {"voix": 80}, "lisibilite": 4}),
            note("c.mp3", F1, "2026-11-12T15:00:00", degres={"contenu": {"voix": 80}, "lisibilite": 2}),  # pas claire
            note("d.mp3", F2, "2026-11-12T16:00:00", validation_rapide=True, mesures={"chiffre": True}),
            note("e.mp3", F2, "2026-11-12T17:00:00", validation_rapide=True),
            note("f.mp3", F2, "2026-11-12T18:00:00", validation_rapide=True),   # pas de son brut
        ]
        for n in "abcde":
            for ext in (".mp3", ".brut.mp3"):
                open(os.path.join(self.log, n + ext), "wb").write(b"x" * 100)
        open(os.path.join(self.log, "f.mp3"), "wb").write(b"x")
        notes = os.path.join(self.tmp, "notes.json")
        json.dump(self.notes, open(notes, "w"))
        self.jobs = []

        def new_job(kind, params):
            dossier = os.path.join(self.tmp, f"job{len(self.jobs)}")
            os.makedirs(dossier)
            job = {"id": f"job{len(self.jobs)}", "type": kind, "params": params}
            self.jobs.append((job, dossier))
            return job["id"], dossier, job

        self.p = [mock.patch.object(sn, "NOTES", notes), mock.patch.object(sn, "TEXTES", os.path.join(self.tmp, "t.json")),
                  mock.patch.object(examen, "RADIO_LOG_DIR", self.log),
                  mock.patch.object(sn.chemins, "base", lambda *a: os.path.join(self.tmp, *a)),
                  mock.patch.object(compagnon, "rank", return_value=[{"id": "mac"}]),
                  mock.patch.object(compagnon, "new_job", side_effect=new_job),
                  mock.patch.object(compagnon, "submit")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_voix_claire(self):
        self.assertEqual([sn.voix_claire(n) for n in self.notes], [True, True, False, False, True, True])

    def test_une_serie_par_frequence_puis_une_analyse(self):
        self.assertEqual(sn.lancer(), 2)
        series = {j["params"]["freq_hz"]: [f["origine"] for f in j["params"]["fichiers"]] for j, _ in self.jobs}
        self.assertEqual(series, {F1: ["a.mp3", "b.mp3"], F2: ["e.mp3"]})
        job, dossier = self.jobs[0]
        self.assertTrue(os.path.isfile(os.path.join(dossier, "p00.mp3")))
        json.dump([{"origine": "a.mp3", "texte": "Grue deux, tu montes la charge.", "confiance": 0.8},
                   {"origine": "b.mp3", "texte": "Levage termine, on descend.", "confiance": 0.75}],
                  open(os.path.join(dossier, "transcriptions.json"), "w"))
        with mock.patch("analyse_station.analyser", return_value={"nom": "Chantier"}) as analyser:
            examen.ranger(job, {"etat": "fait"}, dossier)
        args, kw = analyser.call_args
        self.assertEqual(args[0], F1)
        self.assertEqual(kw["serie"], 2)
        texte = args[4]
        self.assertLess(texte.index("Grue deux"), texte.index("Levage"))   # du plus ancien au plus recent
        self.assertIn("[10/11 09 h 00]", texte)
        self.jobs.clear()
        self.assertEqual(sn.lancer(), 0)          # deja transcrites ou envoyees : rien ne repart

    def test_soir_garde_le_texte_sans_analyse(self):
        with mock.patch("analyse_station.analyser") as analyser, mock.patch("noms_whisper.marquer_fait"):
            examen._suite({"origine": "e.mp3", "freq_hz": F2, "soir": True},
                          {"texte": "Allo la base", "confiance": 0.9}, None, "mac")
        analyser.assert_not_called()
        self.assertEqual(sn.textes()["e.mp3"]["texte"], "Allo la base")


if __name__ == "__main__":
    unittest.main()
