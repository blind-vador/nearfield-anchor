"""Audit du 2026-11-11 : la note credite le bon juge (E1, E2), les donnees ont leur
juge (E3), un relais DMR vu ou un relais annonce D-Star oriente les juges (E4, E5)."""
import ast
import json
import math
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.join(ICI, "..")
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import frequences_actives as fa  # noqa: E402
import noise_signatures as ns  # noqa: E402
import notation  # noqa: E402
import propositions  # noqa: E402
import relais  # noqa: E402


def degres(**contenu):
    d = notation.empty_degrees()
    lis = contenu.pop("lisibilite", 0)
    d["contenu"].update(contenu)
    d["lisibilite"] = lis
    return {"degres": d}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.anciens = (propositions.SCORES, relais.STORE_PATH, relais.LOCAL_PATH, relais.LOINTAINS_PATH,
                        fa.PATH, fa.LOCK)
        propositions.SCORES = os.path.join(self.tmp, "juges_freq.json")
        relais.STORE_PATH = os.path.join(self.tmp, "absent", "relais.json")
        relais.LOCAL_PATH = os.path.join(self.tmp, "relais.json")
        relais.LOINTAINS_PATH = os.path.join(self.tmp, "loin.json")
        fa.PATH, fa.LOCK = os.path.join(self.tmp, "fa.json"), os.path.join(self.tmp, "fa.lock")

    def tearDown(self):
        (propositions.SCORES, relais.STORE_PATH, relais.LOCAL_PATH, relais.LOINTAINS_PATH,
         fa.PATH, fa.LOCK) = self.anciens


class AvisDeLaNote(Base):
    def test_avis(self):
        self.assertEqual(notation.avis_juge(degres(voix=100, lisibilite=5)), "oui")
        self.assertEqual(notation.avis_juge(degres(voix=100)), "oui")             # lisibilite non notee
        self.assertIsNone(notation.avis_juge(degres(voix=100, lisibilite=2)))
        self.assertEqual(notation.avis_juge(degres(voix=30, rien=70, lisibilite=3)), "non")
        self.assertEqual(notation.avis_juge(degres(voix=100, lisibilite=1)), "non")
        self.assertIsNone(notation.avis_juge(degres(tonalites=100, lisibilite=4)))   # E1 : la FM ne monte plus
        self.assertEqual(notation.avis_juge({}, rapide=True), "oui")

    def test_prise_numerique_sans_juge(self):
        """E2 : plus de repli sur "nfm" pour une prise numerique."""
        cas = [({"numerique": True, "protocole": "DMR"}, "-fs"),
               ({"numerique": True, "mode_numerique": "NXDN48"}, "-fi"),
               ({"numerique": True}, None),
               ({"juge": "-fy", "numerique": True}, "-fy"),
               ({}, "nfm")]
        for metrics, attendu in cas:
            with mock.patch.object(ns, "capture_info",
                                   return_value={"freq_hz": 145_287_500, "mode": "nfm", "metrics": metrics}):
                self.assertEqual(propositions._juge_courant("/x/prise.mp3")[0], attendu, metrics)


class APriori(Base):
    def test_relais_dstar_aprs(self):
        """E5 : le juge D-Star passe devant sur un relais annonce D-Star, et l'indice s'efface."""
        self.assertEqual(propositions.rang(438_200_000, "-fd"), 0.5)
        relais.learn("F1ZCK-B", "70cm Voice (D-Star) 438.20000MHz -7.6000MHz", "r", 5.2)
        r = propositions.rang(438_200_000, "-fd")
        self.assertGreaterEqual(r, propositions.SEUIL_RETENU)
        import juges_watch
        ordre = juges_watch.ordre({"bandes": {}}, "relais", list(juges_watch.JUGES), 438_200_000)
        self.assertEqual(ordre[0], "-fd")
        self.assertIsNone(propositions.suffisant(438_200_000))   # un indice n'est pas un avis
        data = json.load(open(propositions.SCORES))
        for st in data["frequences"].values():
            for j in st.values():
                for ap in (j.get("a_priori") or {}).values():
                    ap["t"] = time.time() - 30 * 86400
        json.dump(data, open(propositions.SCORES, "w"))
        self.assertLess(propositions.rang(438_200_000, "-fd"), 0.55)   # oublie sans renouvellement

    def test_aprs_pas_un_relais(self):
        self.assertFalse(relais.detecte(144_800_000, ["indicatif F5ZHR"]))
        relais.depuis_texte("relais sortie 144.800 MHz", None)
        self.assertIsNone(relais.relais_proche(144_800_000)[0])

    def test_synchro_relais_dmr(self):
        """E4 : _dmr_learn pose le juge DMR et la memoire de relais."""
        src = open(os.path.join(RACINE, "watchers", "radio_watch.py"), encoding="utf-8").read()
        fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_dmr_learn")
        notes = []
        ns_ = {"_noter_active": lambda f, nature, **k: notes.append((f, nature, k)),
               "dire": lambda *a, **k: "", "DMR_NOUVEAU_SOUND": None}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "radio_watch", "exec"), ns_)
        with mock.patch("dmr_registre.record", return_value={"nouvelle_frequence": False, "nouveaux_groupes": []}):
            ns_["_dmr_learn"]({"freq_hz": 438_525_000, "protocol": "DMR", "dmr": {"relais": True}})
        self.assertGreater(propositions.rang(438_525_000, "-fs"), 0.7)
        self.assertEqual(notes[0][2].get("relais_dmr"), True)


class Donnees(Base):
    def test_note_tonalites(self):
        """E3 : tonalites/donnees -> le juge de donnees monte ici, pas la FM."""
        prise = os.path.join(self.tmp, "p.mp3")
        open(prise, "wb").write(b"x")
        with mock.patch.object(propositions, "renvoyer_flux", return_value="ok"), \
                mock.patch("frequences_actives.noter"):
            note = degres(tonalites=100, lisibilite=4)
            note.update(freq_hz=72_796_875, label="4 m")
            notation._examen_contenu(prise, note)
        self.assertGreaterEqual(propositions.rang(72_796_875, "donnees"), propositions.SEUIL_RETENU)
        self.assertEqual(propositions.rang(72_796_875, "nfm"), 0.5)

    def test_ordre_rang_avant_vu(self):
        src = open(os.path.join(RACINE, "watchers", "radio_watch.py"), encoding="utf-8").read()
        self.assertIn("-propositions.rang(freq, m), -propositions.vu(freq, m)", src)
        self.assertTrue(math.isclose(propositions.rang(1e6, "x"), 0.5))


if __name__ == "__main__":
    unittest.main()
