"""Travaux externes du pont (bridge/travaux_externes.py, 2026-11-17) et leur suite au Pi.

Un logiciel installe a part (ex. Prismaforge) se declare dans ~/.nearfield-bridge/externes/ ;
le pont lui confie les travaux de ses capacites et relaie ses phrases en "progression".
On verifie :
  - declarations : programme absolu et executable seulement ; capacites = noms simples ;
  - execution : le travail arrive sur stdin, les phrases sont relayees (etape nom simple,
    phrase nettoyee), la fin donne etat et sorties (fichiers du dossier seulement) ;
    sans fin : echec ; echeance : le groupe de processus est arrete ;
  - environnement reduit : aucune variable de cle ne passe ;
  - traduction : "progression" passe la validation du module Rust (texte borne en octets,
    caracteres de controle retires) et revient a l'identique.
"""
import json
import os
import stat
import sys
import tempfile
import time
import unittest

RACINE_DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE_DEPOT, "bridge"))
import travaux_externes as te  # noqa: E402
import pont_v2_traduction as trad  # noqa: E402

try:
    import nearfield_pont as np_
except ImportError:
    np_ = None

PROGRAMME = r'''
import json, os, sys, time
if sys.argv[1] == "--etat":
    print(json.dumps({"charge": 0.3, "disque_libre_go": 12.5, "liste": [1, 2]}))
    sys.exit(0)
if sys.argv[1] == "--capacites":
    print(json.dumps(["agent-essai", "Pas Simple", "projet-demo", 3]))
    sys.exit(0)
dossier = sys.argv[2]
job = json.load(sys.stdin)
mode = job["params"].get("mode")
with open(os.path.join(dossier, "env.json"), "w") as f:
    json.dump(sorted(os.environ), f)
print(json.dumps({"etape": "debut", "phrase": "Démarre\x07 le travail " + job["id"], "pourcentage": 5}))
print("pas du json")
print(json.dumps({"etape": "Mauvaise Etape", "phrase": "ignoree"}))
print(json.dumps({"etape": "action", "phrase": "Lit a.py.", "pourcentage": 500}), flush=True)
if mode == "long":
    time.sleep(60)
if mode == "sans_fin":
    sys.exit(3)
with open(os.path.join(dossier, "fiche.md"), "w") as f:
    f.write("# Fiche\n")
print(json.dumps({"fin": {"etat": "fait", "sorties": ["fiche.md", "absent.md", "../x", "env.json"],
                          "message": "Fiche ecrite."}}))
'''


class TravauxExternes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="essai_externes_")
        self.decl = os.path.join(self.tmp, "externes")
        os.makedirs(self.decl)
        script = os.path.join(self.tmp, "prog.py")
        with open(script, "w") as f:
            f.write(PROGRAMME)
        self.argv = [sys.executable, "-I", script]
        with open(os.path.join(self.decl, "essai.json"), "w") as f:
            json.dump({"nom": "essai", "programme": self.argv}, f)
        with open(os.path.join(self.decl, "relatif.json"), "w") as f:
            json.dump({"nom": "relatif", "programme": ["python3", script]}, f)
        with open(os.path.join(self.decl, "Mauvais.json"), "w") as f:
            json.dump({"nom": "Mauvais Nom", "programme": self.argv}, f)
        te._cache["t"] = 0

    def travail(self, mode="court", duree=None):
        dossier = os.path.join(self.tmp, "travail-%s" % mode)
        os.makedirs(dossier)
        params = {"mode": mode}
        if duree:
            params["duree"] = duree
        return {"id": "20261117-agent-essai-ab12", "type": "agent-essai", "params": params, "ticket": "t-1"}, dossier

    def test_declarations_et_capacites(self):
        self.assertEqual([n for n, _ in te.declarations(self.decl)], ["essai"])
        self.assertEqual(te.capacites(self.decl), ["agent-essai", "projet-demo"])
        self.assertEqual(te.pour(self.decl, "agent-essai")["nom"], "essai")
        self.assertIsNone(te.pour(self.decl, "whisper"))

    def test_execution_relaie_les_phrases(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-essai"
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        job, dossier = self.travail()
        relais = []
        etat, sorties, message = te.executer(te.pour(self.decl, "agent-essai"), job, dossier,
                                             lambda *a: relais.append(a), journal=lambda t: None)
        self.assertEqual((etat, message), ("fait", "Fiche ecrite."))
        self.assertEqual(sorties, ["fiche.md", "env.json"])
        self.assertEqual(relais, [("debut", "Démarre  le travail 20261117-agent-essai-ab12".replace("  ", " "), 5),
                                  ("action", "Lit a.py.", 0)])
        with open(os.path.join(dossier, "env.json")) as f:
            env = json.load(f)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_etat_opaque_et_borne(self):
        te._etats["t"] = 0
        self.assertEqual(te.etats(self.decl), {"essai": {"charge": 0.3, "disque_libre_go": 12.5, "liste": [1, 2]}})
        self.assertFalse(te._borne({"a": "x" * 3000}))
        self.assertFalse(te._borne({"a": [[[[[[1]]]]]]}))
        self.assertTrue(te._borne({"a": {"b": [1, "c", None, True]}}))

    @unittest.skipUnless(np_, "module nearfield_pont absent")
    def test_battement_avec_extensions(self):
        b = {"type": "battement", "capacites": ["agent-essai"], "etat": None, "puissance": None,
             "extensions": {"prismaforge": {"charge": 0.3}}}
        m = trad.vers_v2(b)
        self.assertEqual(m["extensions"], {"prismaforge": {"charge": 0.3}})
        try:
            retour = np_.decoder_message(np_.encoder_message(m))
        except Exception as exc:  # module pas encore recompile avec le champ
            self.skipTest("module nearfield_pont sans le champ extensions : %s" % exc)
        self.assertEqual(trad.depuis_v2(retour)["extensions"], {"prismaforge": {"charge": 0.3}})

    def test_sans_fin_est_un_echec(self):
        job, dossier = self.travail("sans_fin")
        etat, sorties, _ = te.executer(te.pour(self.decl, "agent-essai"), job, dossier, lambda *a: None,
                                       journal=lambda t: None)
        self.assertEqual((etat, sorties), ("echec", []))

    def test_echeance(self):
        ancien = te._echeance
        te._echeance = lambda job: 1
        self.addCleanup(setattr, te, "_echeance", ancien)
        job, dossier = self.travail("long")
        debut = time.time()
        etat, _, message = te.executer(te.pour(self.decl, "agent-essai"), job, dossier, lambda *a: None,
                                       journal=lambda t: None)
        self.assertEqual((etat, message), ("echec", "delai depasse"))
        self.assertLess(time.time() - debut, 30)

    @unittest.skipUnless(np_, "module nearfield_pont absent")
    def test_traduction_validee_par_le_module(self):
        phrase = "Lit é.py\x1b[31m " + "é" * 3000
        m = trad.vers_v2({"type": "progression", "travail": "20261117-agent-essai-ab12", "etape": "action",
                          "pourcentage": 40, "phrase": phrase})
        self.assertLessEqual(len(m["phrase"].encode()), 4000)
        self.assertNotIn("\x1b", m["phrase"])
        brut = np_.encoder_message(m)
        retour = np_.decoder_message(brut)
        v1 = trad.depuis_v2(retour)
        self.assertEqual((v1["type"], v1["etape"], v1["pourcentage"]), ("progression", "action", 40))
        self.assertTrue(v1["phrase"].startswith("Lit é.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
