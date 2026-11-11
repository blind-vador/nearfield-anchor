"""Plan phases 3 et 5 (2026-11-11) : frequences trouvees par l'IA (surveillance 14 jours),
analyse de station apres Whisper (nom, pourquoi, mise a jour), une proposition par frequence."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import phrases  # noqa: E402
phrases.BASE_DIR = os.path.join(ICI, "..", "phrases")
import analyse_station  # noqa: E402
import explication_messages as em  # noqa: E402
import frequences_actives as fa  # noqa: E402
import frequences_ia  # noqa: E402
import gemini  # noqa: E402
import nommage  # noqa: E402
import propositions  # noqa: E402
import relais  # noqa: E402
import stations  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        j = lambda n: os.path.join(self.tmp, n)  # noqa: E731
        self.anciens = (propositions.SCORES, relais.STORE_PATH, relais.LOCAL_PATH, fa.PATH, fa.LOCK,
                        nommage.PATH, analyse_station.FICHES)
        propositions.SCORES, relais.STORE_PATH, relais.LOCAL_PATH = j("jf.json"), j("x/relais.json"), j("relais.json")
        fa.PATH, fa.LOCK, nommage.PATH, analyse_station.FICHES = j("fa.json"), j("fa.lock"), j("nommage.json"), j("canaux.json")
        self.p = [mock.patch.object(stations, "load", return_value={"stations": []}),
                  mock.patch.object(stations, "name_near", return_value=None),
                  mock.patch.object(fa, "_relais_trouve")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        (propositions.SCORES, relais.STORE_PATH, relais.LOCAL_PATH, fa.PATH, fa.LOCK,
         nommage.PATH, analyse_station.FICHES) = self.anciens


class FrequencesIA(Base):
    def test_ranger(self):
        relais._save({"145600000": {"nom": "F1ZXX", "mode": "fm", "vu": time.time()}})
        res = frequences_ia.ranger([
            {"mhz": "145,600", "modulation": "FM", "certitude": 0.9},          # relais connu
            {"mhz": 438.6125, "modulation": "DMR", "certitude": 0.8},          # nouvelle
            {"mhz": 2400, "certitude": 1},                                     # hors dongle
            {"mhz": 162.5, "certitude": 0.2},                                  # trop incertaine
            {"mhz": 446.00625, "certitude": 1}], "test", freq_source=446_006_250)  # la source elle-meme
        self.assertEqual(res["connues"], [145_600_000])
        self.assertEqual(res["surveillees"], [438_612_500])
        self.assertEqual(res["ecartees"], 2)
        e = fa.load()[fa._cle(438_612_500)]
        self.assertAlmostEqual(e["expire"] - time.time(), 14 * 86400, delta=60)
        self.assertNotIn("expire", fa.load()[fa._cle(145_600_000)])
        self.assertGreater(propositions.rang(438_612_500, "-fs"), 0.6)
        self.assertIn("quatorze jours", frequences_ia.phrase(res))

    def test_surveillance_qui_donne_ou_s_efface(self):
        frequences_ia.ranger([{"mhz": 160.0, "certitude": 1}], "test")
        frequences_ia.ranger([{"mhz": 161.0, "certitude": 1}], "test")
        fa.noter(160_000_000, "voix", modulation="nfm")                  # elle a donne : elle reste
        self.assertNotIn("expire", fa.load()[fa._cle(160_000_000)])
        data = fa.load()
        data[fa._cle(161_000_000)]["expire"] = time.time() - 1
        json.dump(data, open(fa.PATH, "w"))
        fa.noter(170_000_000, "signal")                                   # tout noter fait le menage
        self.assertNotIn(fa._cle(161_000_000), fa.load())
        self.assertIn(fa._cle(160_000_000), fa.load())


class AnalyseStation(Base):
    def reponse(self, **k):
        r = {"explication": "Deux equipes de chantier se coordonnent pour une grue.", "nom": "Chantier, levage",
             "pourquoi": "ballet de grue", "mise_a_jour": None, "indicatifs": ["F4ABC"],
             "frequences": [{"mhz": 446.1, "modulation": "FM", "certitude": 0.9}]}
        r.update(k)
        return json.dumps(r), None

    def test_nom_pourquoi_et_mise_a_jour(self):
        demandes = []
        with mock.patch.object(gemini, "ask", side_effect=lambda q, **k: demandes.append(q) or self.reponse()):
            res = analyse_station.analyser(160_650_000, "nfm", "bande VHF", "nfm", "on monte la charge", "a.mp3", "Mac")
        self.assertEqual(res["pourquoi"], "ballet de grue")
        attente = nommage.en_attente()
        self.assertEqual(len(attente), 1)
        self.assertEqual((attente[0]["nom"], attente[0]["pourquoi"]), ("Chantier, levage", "ballet de grue"))
        self.assertIn("bande VHF", demandes[0])
        self.assertIn("Je propose le nom Chantier, levage, ballet de grue", analyse_station.phrase(res))
        # deuxieme analyse : la fiche precedente part avec la question, la proposition est remplacee
        maj = "En sachant qu'on avait noté un chantier, je constate une livraison, donc je propose Chantier, livraisons."
        with mock.patch.object(gemini, "ask", side_effect=lambda q, **k: demandes.append(q) or self.reponse(
                nom="Chantier, livraisons", pourquoi="noria de camions", mise_a_jour=maj)):
            analyse_station.analyser(160_650_000, "nfm", "bande VHF", "nfm", "le camion arrive", "b.mp3", "Mac")
        self.assertIn("ballet de grue", demandes[1])
        attente = nommage.en_attente()
        self.assertEqual([a["nom"] for a in attente], ["Chantier, livraisons"])
        self.assertEqual(len(analyse_station.fiche(160_650_000)["historique"]), 2)

    def test_station_deja_nommee(self):
        with mock.patch.object(stations, "name_near", return_value="Chantier, levage"), \
                mock.patch.object(stations, "rename") as rename, \
                mock.patch.object(gemini, "ask", return_value=self.reponse(nom="Grue de la gare", mise_a_jour="Donc...")):
            data = {analyse_station._cle(160_650_000): {"nom": "Chantier, levage"}}
            json.dump(data, open(analyse_station.FICHES, "w"))
            analyse_station.analyser(160_650_000, "nfm", "VHF", None, "texte", "c.mp3")
            item = nommage.en_attente()[0]
            self.assertEqual(item["ancien_nom"], "Chantier, levage")
            self.assertTrue(nommage.confirmer(item))
            rename.assert_called_once()

    def test_reponse_message_json(self):
        self.assertEqual(em.lire_reponse('{"explication": "Un ACARS.", "frequences": []}'), ("Un ACARS.", []))
        self.assertEqual(em.lire_reponse("texte libre"), ("texte libre", []))
        q = em.question("messages", "/x/message_a.mp3", {"texte": "QSY 145.6375"}) if hasattr(em, "question") else ""
        self.assertIn('"mhz"', q)


if __name__ == "__main__":
    unittest.main()
