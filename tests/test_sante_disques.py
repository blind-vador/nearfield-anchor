"""Sante des disques (2026-11-12) : evaluation des mesures SMART et du systeme
de fichiers, bilan lu par la commande "sante", alarmes de la veille systeme
(une par sorte, rien de plus apres "compris", rien pour un disque debranche)."""
import datetime
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
import sante_disques as sd  # noqa: E402


def attr(i, brut, nom="x", echec=""):
    return {"id": i, "name": nom, "when_failed": echec, "raw": {"value": brut, "string": str(brut)}}


def releve(passe=True, **bruts):
    ids = {"realloues": 5, "en_attente": 197, "illisibles": 198, "crc": 199, "cycles": 193}
    table = [attr(9, 12000), attr(194, 30, "Temperature_Celsius")]
    table[1]["raw"]["string"] = "30 (Min/Max 16/41)"
    table += [attr(ids[k], v) for k, v in bruts.items()]
    return {"smart_status": {"passed": passe}, "power_on_time": {"hours": 12000}, "temperature": {"current": 30},
            "ata_smart_attributes": {"table": table},
            "ata_smart_data": {"self_test": {"status": {"value": 0, "passed": True}}},
            "ata_smart_self_test_log": {"standard": {"table": [
                {"type": {"string": "Short offline"}, "status": {"string": "Completed without error", "passed": True},
                 "lifetime_hours": 11990}]}}}


NAS = {"id": "sda", "type": "hdd", "modele": "Hitachi", "serie": "J84", "montages": ["/media/nas/nd1"],
       "parties": ["sda1"]}
SD = {"id": "mmcblk0", "type": "sd", "modele": "MSSD0", "serie": "0x1", "montages": ["/", "/boot/firmware"],
      "parties": ["mmcblk0p1", "mmcblk0p2"]}


class Evaluation(unittest.TestCase):
    def test_sain(self):
        m = sd.mesures_smart(releve(realloues=0, en_attente=0))
        self.assertEqual(m["temperature_max"], 41)
        self.assertTrue(m["dernier_test"]["reussi"])
        niveau, alarmes, constats = sd.evaluer(NAS, m)
        self.assertEqual((niveau, alarmes, constats), ("bon", [], []))

    def test_secteurs_en_attente(self):
        niveau, alarmes, _ = sd.evaluer(NAS, sd.mesures_smart(releve(en_attente=8)))
        self.assertEqual(niveau, "preoccupant")
        self.assertEqual(alarmes, [sd.SECTEURS])

    def test_verdict_et_auto_test(self):
        niveau, alarmes, _ = sd.evaluer(NAS, sd.mesures_smart(releve(passe=False)))
        self.assertEqual((niveau, alarmes), ("critique", [sd.DEFAILLANT]))
        r = releve()
        r["ata_smart_self_test_log"]["standard"]["table"][0]["status"] = {"string": "Completed: read failure",
                                                                          "passed": False}
        self.assertIn(sd.DEFAILLANT, sd.evaluer(NAS, sd.mesures_smart(r))[1])

    def test_hausses_depuis_le_bilan_precedent(self):
        avant = {"mesures": sd.mesures_smart(releve(realloues=2, crc=10))}
        niveau, alarmes, _ = sd.evaluer(NAS, sd.mesures_smart(releve(realloues=2, crc=10)), avant)
        self.assertEqual((niveau, alarmes), ("surveiller", []))            # stable : a surveiller
        niveau, alarmes, _ = sd.evaluer(NAS, sd.mesures_smart(releve(realloues=6, crc=10)), avant)
        self.assertEqual((niveau, alarmes), ("preoccupant", [sd.SECTEURS]))
        niveau, alarmes, c = sd.evaluer(NAS, sd.mesures_smart(releve(realloues=2, crc=14)), avant)
        self.assertEqual((niveau, alarmes), ("surveiller", []))            # liaison : cable ou alimentation
        self.assertIn("câble", " ".join(c))

    def test_carte_sd(self):
        niveau, alarmes, _ = sd.evaluer(SD, {"erreurs_fs": 0, "lecture_seule": [], "erreurs_noyau": 0})
        self.assertEqual(niveau, "bon")
        niveau, alarmes, _ = sd.evaluer(SD, {"erreurs_fs": 0, "lecture_seule": ["/"], "erreurs_noyau": 4})
        self.assertEqual((niveau, alarmes), ("critique", [sd.ERREURS]))

    def test_lignes_du_noyau(self):
        lignes = {"I/O error, dev sda, sector 123 op 0x0:(READ)": "sda",
                  "Buffer I/O error on dev sdb1, logical block 7": "sdb",
                  "EXT4-fs error (device mmcblk0p2): ext4_find_entry:1683": "mmcblk0",
                  "EXT4-fs (sda1): Remounting filesystem read-only": "sda",
                  "critical medium error, dev sdb, sector 9": "sdb",
                  "mmc0: Timeout waiting for hardware interrupt.": "mmcblk0",
                  "usb 1-1.1.1: USB disconnect, device number 9": None,
                  "EXT4-fs (sda1): mounted filesystem with ordered data mode": None}
        for ligne, dev in lignes.items():
            self.assertEqual(sd.disque_de_ligne(ligne), dev, ligne)

    def test_tests_du_dimanche(self):
        self.assertIsNone(sd.test_du_jour(datetime.date(2026, 11, 12)))   # jeudi
        self.assertEqual(sd.test_du_jour(datetime.date(2026, 11, 1)), "long")
        self.assertEqual(sd.test_du_jour(datetime.date(2026, 11, 15)), "court")


class Bilan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.object(sd, "RAPPORT", os.path.join(self.tmp, "r.json")),
                  mock.patch.object(sd, "HISTORIQUE", os.path.join(self.tmp, "h.json")),
                  mock.patch.object(sd, "peripheriques", return_value=[SD, NAS]),
                  mock.patch.object(sd, "erreurs_noyau", return_value={}),
                  mock.patch.object(sd, "mesures_fs", return_value={"erreurs_fs": 0, "ecrit_go": 537.4,
                                                                    "lecture_seule": []})]
        for x in self.p:
            x.start()
        self.lances = []

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sortie(self, args, timeout=60):
        self.lances.append(args)
        return ""

    def test_bilan_et_commande_sante(self):
        self.assertIn("demain", sd.pour_sante()[1][0])
        with mock.patch.object(sd, "smart", return_value=releve()), mock.patch.object(sd, "_sortie", self._sortie):
            r = sd.bilan(lancer_test="court")
        self.assertEqual([d["niveau"] for d in r["disques"]], ["bon", "bon"])
        self.assertEqual(self.lances, [["sudo", "-n", sd.AIDE, "test-court", "/dev/sda"]])   # pas sur la carte SD
        problemes, details = sd.pour_sante()
        self.assertEqual(problemes, [])
        self.assertIn("la carte SD du Pi : en bon état", details[0])
        self.assertIn("537 gigaoctets", details[0])
        self.assertIn("le disque nd1 du NAS : en bon état, 30 degrés", details[0])
        with mock.patch.object(sd, "smart", return_value=releve(en_attente=3)):
            sd.bilan()
        problemes, _ = sd.pour_sante()
        self.assertEqual(len(problemes), 1)
        self.assertIn("3 secteurs illisibles", problemes[0])


class Alarmes(unittest.TestCase):
    def setUp(self):
        import veille_systeme as vs
        self.vs = vs
        self.tmp = tempfile.mkdtemp()
        self.declenchees = []
        self.p = [mock.patch.object(sd, "RAPPORT", os.path.join(self.tmp, "r.json")),
                  mock.patch.object(sd, "HISTORIQUE", os.path.join(self.tmp, "h.json")),
                  mock.patch.object(vs, "DISQUE_ACQUIT", os.path.join(self.tmp, "a.json")),
                  mock.patch.object(sd, "peripheriques", return_value=[NAS]),
                  mock.patch.object(sd, "erreurs_noyau", return_value={}),
                  mock.patch.object(sd, "mesures_fs", return_value={"erreurs_fs": 0, "lecture_seule": []}),
                  mock.patch.object(sd, "smart", return_value=releve(en_attente=3)),
                  mock.patch.object(vs, "_plus_tard", lambda f, *a: None),
                  mock.patch.object(vs.alarmes, "declencher", side_effect=self._declencher)]
        for x in self.p:
            x.start()
        vs._derniers.clear()

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _declencher(self, evenement, texte, **reglages):
        self.declenchees.append((evenement, texte, reglages))

    def test_bilan_preoccupant(self):
        w = self.vs._SanteDisques()
        sd.bilan()
        w._rapport()
        self.assertEqual(len(self.declenchees), 1)
        evenement, texte, reglages = self.declenchees[0]
        self.assertEqual(evenement, sd.SECTEURS)
        self.assertIn("3 secteurs illisibles", texte)
        self.assertEqual(reglages["rappel_sec"], 2 * 3600)
        self.assertTrue(reglages["encore"]())                      # le bilan le dit toujours
        reglages["quand_fini"]("compris")
        os.utime(sd.RAPPORT, (1, 1))                               # bilan suivant, identique
        w._rapport()
        self.assertEqual(len(self.declenchees), 1)                 # compris : pas redit
        with mock.patch.object(sd, "smart", return_value=releve(en_attente=9)):
            sd.bilan()
        w._rapport()
        self.assertEqual(len(self.declenchees), 2)                 # pire : redit

    def test_erreurs_en_direct(self):
        w = self.vs._SanteDisques()
        with mock.patch.object(self.vs.os.path, "exists", return_value=True):
            for _ in range(3):
                w.ligne_noyau("I/O error, dev sda, sector 1 op 0x0:(READ)")
        w._erreurs["sda"] = [t - 20 for t in w._erreurs["sda"]]   # la rafale est finie
        w._noyau()
        self.assertEqual([e for e, _, _ in self.declenchees], [sd.ERREURS])
        self.assertEqual(self.declenchees[0][2]["rappel_sec"], 3600)

    def test_disque_debranche_pas_d_alarme(self):
        w = self.vs._SanteDisques()
        with mock.patch.object(self.vs.os.path, "exists", return_value=True):
            for _ in range(5):
                w.ligne_noyau("I/O error, dev sda, sector 1 op 0x0:(READ)")
        w._erreurs["sda"] = [t - 20 for t in w._erreurs["sda"]]
        with mock.patch.object(sd, "peripheriques", return_value=[]):
            w._noyau()
        self.assertEqual(self.declenchees, [])

    def test_chaleur(self):
        w = self.vs._SanteDisques()
        with mock.patch.object(sd, "temperature", return_value=57):
            w._chaleur()
        self.assertEqual([e for e, _, _ in self.declenchees], [sd.CHAUD])
        self.assertIn("57 degrés", self.declenchees[0][1])


if __name__ == "__main__":
    unittest.main()
