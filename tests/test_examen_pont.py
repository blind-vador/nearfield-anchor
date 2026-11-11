"""Examen des prises par le pont (2026-11-11) : la touche E depose un travail,
le Pi range lui-meme le resultat. Plus de fichier partage reecrit en place."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import chemins  # noqa: E402
import phrases  # noqa: E402
phrases.BASE_DIR = os.path.join(ICI, "..", "phrases")
import compagnon  # noqa: E402
import examen  # noqa: E402
import propositions_reglages as pr  # noqa: E402


class Examen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "radio_log")
        os.makedirs(os.path.join(self.log, "rejets"))
        self.travaux = os.path.join(self.tmp, "travaux")
        self.anciens = (examen.RADIO_LOG_DIR, examen.AUDIO_DIR, examen.DEMANDES, examen.LOCK_PATH,
                        examen.ANCIENNE_FILE, pr.STORE_NAS, pr.STORE_LOCAL, pr.LOCK_PATH)
        examen.RADIO_LOG_DIR = self.log
        examen.AUDIO_DIR = os.path.join(self.log, "propositions_audio")
        examen.DEMANDES = os.path.join(self.tmp, "demandes.json")
        examen.LOCK_PATH = os.path.join(self.tmp, "examen.lock")
        examen.ANCIENNE_FILE = os.path.join(self.log, "a_examiner.json")
        pr.STORE_NAS = pr.STORE_LOCAL = os.path.join(self.log, "propositions.json")
        pr.LOCK_PATH = os.path.join(self.tmp, "p.lock")
        self.jobs = []

        def new_job(kind, params):
            jid = f"job{len(self.jobs)}-{kind}"
            dossier = os.path.join(self.travaux, jid)
            os.makedirs(dossier)
            return jid, dossier, {"id": jid, "type": kind, "params": params}

        self.patches = [mock.patch.object(compagnon, "new_job", side_effect=new_job),
                        mock.patch.object(compagnon, "submit", side_effect=self.jobs.append),
                        mock.patch.object(compagnon, "open_jobs", side_effect=lambda: list(self.jobs)),
                        mock.patch.object(compagnon, "companions",
                                          return_value=[{"capacites": ["examen", "examen_lot"]}]),
                        mock.patch("revue_propositions.preparer")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        (examen.RADIO_LOG_DIR, examen.AUDIO_DIR, examen.DEMANDES, examen.LOCK_PATH,
         examen.ANCIENNE_FILE, pr.STORE_NAS, pr.STORE_LOCAL, pr.LOCK_PATH) = self.anciens

    def prise(self, nom, brut=True):
        chemin = os.path.join(self.log, nom)
        open(chemin, "wb").write(b"journal habille")
        if brut:
            open(chemin[:-4] + ".brut.mp3", "wb").write(b"son brut")
        return chemin

    def test_demande_copie_le_brut(self):
        chemin = self.prise("446_civil_mardi.mp3")
        self.assertEqual(examen.demander(chemin), "demande")
        job = self.jobs[0]
        self.assertEqual(job["type"], "examen")
        self.assertEqual(job["params"]["brut"], "446_civil_mardi.brut.mp3")
        copie = os.path.join(self.travaux, job["id"], job["params"]["fichier"])
        self.assertEqual(open(copie, "rb").read(), b"son brut")   # jamais la prise habillee
        self.assertEqual(examen.demander(chemin), "deja")
        self.assertEqual(len(self.jobs), 1)

    def test_sans_brut_et_rejet_par_nom(self):
        self.prise("rejets/canal_x.mp3", brut=False)
        self.assertEqual(examen.demander("canal_x.mp3"), "demande")
        self.assertEqual(self.jobs[0]["params"]["brut"], os.path.join("rejets", "canal_x.mp3"))
        self.assertEqual(examen.demander("introuvable.mp3"), "absent")
        # une prise du journal sans son brut n'est jamais envoyee habillee
        self.assertEqual(examen.demander(self.prise("habillee.mp3", brut=False)), "sans_brut")
        self.assertEqual(len(self.jobs), 1)

    def test_sans_machine(self):
        with mock.patch.object(compagnon, "companions", return_value=[{"capacites": ["whisper"]}]):
            self.assertEqual(examen.demander(self.prise("a.mp3")), "sans_machine")

    def test_ranger_un_examen(self):
        examen.demander(self.prise("446_civil_mardi.mp3"))
        job = self.jobs[0]
        dossier = os.path.join(self.travaux, job["id"])
        os.makedirs(os.path.join(dossier, "propositions_audio"))
        open(os.path.join(dossier, "propositions_audio", "17_léger.wav"), "wb").write(b"RIFF")
        prop = {"id": "17", "le": 1.0, "origine": "demande", "confiance": 0.9, "resume": "Examen demandé — 446 civil.",
                "brut": "446_civil_mardi.mp3", "etat": "attente", "action": {},
                "traitements": [{"niveau": "léger", "audio": "propositions_audio/17_léger.wav", "action": {}}]}
        json.dump([prop], open(os.path.join(dossier, "propositions.json"), "w"))
        dits = []
        class Direct:   # le fil de l'annonce s'execute aussitot
            def __init__(self, target, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        annonces = []
        with mock.patch("veille_donnees.announce",
                        side_effect=lambda t, *a, **k: dits.append(t) or annonces.append(k)), \
                mock.patch("threading.Thread", Direct):
            n = examen.ranger(job, {"etat": "fait"}, dossier)
            examen.ranger(job, {"etat": "fait"}, dossier)   # relu deux fois : une seule proposition
        self.assertEqual(n, 1)
        rangees = json.load(open(pr.STORE_NAS))
        self.assertEqual(len(rangees), 1)
        self.assertEqual(rangees[0]["brut"], "446_civil_mardi.brut.mp3")
        audio = rangees[0]["traitements"][0]["audio"]
        self.assertEqual(audio, os.path.join("propositions_audio", "17_léger.wav"))
        self.assertTrue(os.path.isfile(os.path.join(self.log, audio)))
        self.assertTrue(dits and "446 civil" in dits[0])
        self.assertIs(annonces[0].get("journal"), False)   # dit une fois, pas de prise au journal radio

    def test_nuit_et_ancienne_file(self):
        self.prise("vieux.mp3")
        json.dump([{"fichier": "vieux.mp3", "le": 1}, {"fichier": "disparu.mp3", "le": 2}],
                  open(examen.ANCIENNE_FILE, "w"))
        self.assertEqual(examen.reprendre_ancienne_file(), 1)
        self.assertFalse(os.path.exists(examen.ANCIENNE_FILE))
        self.assertIsNotNone(examen.lancer_nuit())
        self.assertIsNone(examen.lancer_nuit())   # une seule passe en attente
        self.assertEqual([j["type"] for j in self.jobs], ["examen", "examen_lot"])


if __name__ == "__main__":
    unittest.main()
