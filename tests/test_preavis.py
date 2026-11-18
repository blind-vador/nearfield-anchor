"""Preavis d'arret et d'orage (lib/preavis.py, 2026-11-18).

nearfield est un point de passage : il previent les logiciels installes et les machines,
attend leurs reponses dans une borne, puis n'agit que sur le Pi. On verifie :
  - orage : niveaux selon les sources (observation de l'utilisateur, capteur de foudre a soi
    ou voisin), sources d'avant le demarrage ignorees, fin apres une demi-heure ;
  - diffusion : on attend les "pret" des extensions et des machines, ou la borne ;
  - arret : la demande est prise par la veille, sinon l'appelant arrete le Pi lui-meme ;
  - protection declaree : le Pi ne s'eteint pas sur foudre proche ;
  - programme installe a part : --preavis sur stdin, reponse bornee ; un programme qui ne
    connait pas --preavis est pret ; traduction v2 du message preavis_recu.
"""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("lib", "config", "bridge"):
    sys.path.insert(0, os.path.join(RACINE, d))
import preavis as pv  # noqa: E402
import travaux_externes as te  # noqa: E402
import pont_v2_traduction as trad  # noqa: E402

try:
    import nearfield_pont as np_
except ImportError:
    np_ = None


class Bac(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.anciens = {k: getattr(pv, k) for k in ("DEMANDE", "PROTECTION", "ETAT_ORAGE", "OBSERVATION", "FOUDRE",
                                                    "DELAI_PRISE_S", "_systemctl", "_annoncer", "_machines_v2")}
        pv.DEMANDE = os.path.join(self.d, "demande.json")
        pv.PROTECTION = os.path.join(self.d, "protection.json")
        pv.ETAT_ORAGE = os.path.join(self.d, "orage.json")
        pv.OBSERVATION = os.path.join(self.d, "obs.json")
        pv.FOUDRE = os.path.join(self.d, "foudre.json")
        pv._annoncer = lambda texte: self.annonces.append(texte)
        pv._machines_v2 = lambda: []
        self.annonces, self.arrets = [], []
        pv._systemctl = lambda action: self.arrets.append(action) or True

    def tearDown(self):
        for k, v in self.anciens.items():
            setattr(pv, k, v)


class Orage(Bac):
    def obs(self, orage, t):
        pv._ecrire(pv.OBSERVATION, {"t": t, "reponses": {"orage": orage}})

    def foudre(self, km, mien, t):
        pv._ecrire(pv.FOUDRE, {"storm_dist_km": km, "mien": mien, "t": t, "source": "x"})

    def niveaux(self, maintenant, demarrage=0):
        return sorted((n, s) for n, s, _k, _t in pv.sources_orage(maintenant, demarrage))

    def test_sources(self):
        t = 1_000_000.0
        self.obs("tout_pres", t)
        self.assertEqual(self.niveaux(t + 60), [("proche", "utilisateur")])
        self.obs("loin", t)
        self.assertEqual(self.niveaux(t + 60), [("approche", "utilisateur")])
        self.obs("aucun", t)
        self.assertEqual(self.niveaux(t + 60), [])
        self.obs("proche", t)
        self.assertEqual(self.niveaux(t + 31 * 60), [], "une observation vaut une demi-heure")
        self.assertEqual(self.niveaux(t + 60, demarrage=t + 10), [], "avant le demarrage : ignoree")
        os.remove(pv.OBSERVATION)
        self.foudre(3, True, t)
        self.assertEqual(self.niveaux(t + 60), [("proche", "capteur")])
        self.foudre(3, False, t)
        self.assertEqual(self.niveaux(t + 60), [("approche", "capteur")], "capteur voisin : approche seulement")
        self.foudre(12, True, t)
        self.assertEqual(self.niveaux(t + 60), [("approche", "capteur")])
        self.foudre(30, True, t)
        self.assertEqual(self.niveaux(t + 60), [])

    def test_evaluer(self):
        t = 2_000_000.0
        e, d = pv.evaluer(None, [("approche", "capteur", 12, t)], t)
        self.assertEqual(d["niveau"], "approche")
        e, d = pv.evaluer(e, [("approche", "capteur", 11, t)], t + 60)
        self.assertIsNone(d, "rien de neuf : pas de nouveau preavis")
        e, d = pv.evaluer(e, [("proche", "utilisateur", None, t)], t + 120)
        self.assertEqual((d["niveau"], d["source"]), ("proche", "utilisateur"))
        e, d = pv.evaluer(e, [("approche", "capteur", 9, t)], t + 180)
        self.assertIsNone(d, "on ne redescend pas de proche a approche")
        e, d = pv.evaluer(e, [], t + 180 + 29 * 60)
        self.assertIsNone(d)
        e, d = pv.evaluer(e, [], t + 180 + 30 * 60)
        self.assertEqual(d["niveau"], "fini")
        e, d = pv.evaluer(e, [], t + 180 + 60 * 60)
        self.assertIsNone(d)

    def test_foudre_proche_eteint_le_pi_sauf_protection(self):
        pv.emettre = self._emettre_vide = (lambda *a, **k: {"manquants": []})
        try:
            with unittest.mock.patch.object(pv.time, "sleep", lambda s: None):
                pv._diffuser_orage({"niveau": "proche", "source": "utilisateur", "distance_km": None})
                self.assertEqual(self.arrets, ["extinction"])
                self.assertIn("débranche", " ".join(self.annonces))
                pv._arret_en_cours.clear()
                self.arrets.clear()
                pv.declarer_protection()
                pv._diffuser_orage({"niveau": "proche", "source": "capteur", "distance_km": 2.4})
                self.assertEqual(self.arrets, [], "Pi declare protege : il reste allume")
                self.assertIn("responsabilité", self.annonces[-1])
                self.arrets.clear()
                pv._diffuser_orage({"niveau": "approche", "source": "capteur", "distance_km": 12})
                self.assertEqual(self.arrets, [])
        finally:
            pv.emettre = Diffusion.EMETTRE
            pv._arret_en_cours.clear()


class Diffusion(Bac):
    EMETTRE = pv.emettre

    def test_attend_les_prets(self):
        envoyes = []
        pv._machines_v2 = lambda: ["mac", "pc-absent"]

        def envoyer(cid, msg):
            envoyes.append((cid, msg))
            if cid == "mac":
                threading.Timer(0.3, pv.reponse, args=(msg["id"], "machine:mac", True)).start()
            return cid == "mac"

        def diffuser(pid, genre, donnees):
            threading.Timer(0.2, pv.reponse, args=(pid, "extension:ide", True)).start()
            return ["ide"]

        debut = time.time()
        r = pv.emettre("orage", {"niveau": "approche"}, 10, envoyer, diffuser)
        self.assertLess(time.time() - debut, 3, "tous prets : on n'attend pas la borne")
        self.assertEqual(r["attendus"], ["extension:ide", "machine:mac"])
        self.assertEqual(r["manquants"], [])
        self.assertEqual(envoyes[0][1]["type"], "preavis")
        self.assertFalse(pv.reponse(r["preavis"], "machine:mac", True), "preavis clos")

    def test_borne(self):
        debut = time.time()
        r = pv.emettre("arret", {"action": "redemarrage"}, 1, lambda c, m: True,
                       lambda pid, g, d: (pv.reponse(pid, "extension:inconnue", True), ["ide"])[1])
        self.assertGreaterEqual(time.time() - debut, 1)
        self.assertEqual(r["manquants"], ["extension:ide"])


class Arret(Bac):
    def test_sans_veille_on_arrete_soi_meme(self):
        pv.DELAI_PRISE_S = 1
        self.assertEqual(pv.arreter_pi("redemarrage", "utilisateur"), "direct")
        self.assertEqual(self.arrets, ["redemarrage"])
        self.assertFalse(os.path.exists(pv.DEMANDE))

    def test_veille_prend_la_demande(self):
        pv.DELAI_PRISE_S = 5
        faits = []
        pv.emettre_orig, pv.emettre = pv.emettre, lambda g, d, delai: faits.append((g, d)) or {}
        try:
            fil = threading.Timer(0.5, pv.prendre_demande)
            fil.start()
            self.assertEqual(pv.arreter_pi("extinction", "utilisateur"), "preavis")
            fil.join()
            for _ in range(40):
                if self.arrets:
                    break
                time.sleep(0.05)
            self.assertEqual(faits[0][0], "arret")
            self.assertEqual(faits[0][1]["action"], "extinction")
            self.assertEqual(self.arrets, ["extinction"])
        finally:
            pv.emettre = pv.emettre_orig

    def test_maintenant(self):
        self.assertEqual(pv.arreter_pi("redemarrage", "utilisateur", attendre=False), "direct")
        self.assertFalse(os.path.exists(pv.DEMANDE))

    def test_protection(self):
        self.assertIsNone(pv.protection())
        pv.declarer_protection()
        self.assertEqual(pv.protection()["responsabilite"], "acceptee")
        self.assertTrue(pv.retirer_protection())
        self.assertFalse(pv.retirer_protection())


PROGRAMME = r'''
import json, sys
if sys.argv[1] != "--preavis":
    sys.exit(2)
p = json.load(sys.stdin)
with open(sys.argv[0] + ".recu", "w") as f:
    json.dump(p, f)
print(json.dumps({"pret": p["genre"] == "arret", "attente_s": 40}))
'''


class ProgrammeInstalle(unittest.TestCase):
    def ecrire(self, dossier, nom, source):
        prog = os.path.join(dossier, nom + ".py")
        with open(prog, "w") as f:
            f.write("#!%s\n%s" % (sys.executable, source))
        os.chmod(prog, os.stat(prog).st_mode | stat.S_IXUSR)
        with open(os.path.join(dossier, nom + ".json"), "w") as f:
            json.dump({"nom": nom, "programme": [prog]}, f)
        return prog

    def test_preavis(self):
        d = tempfile.mkdtemp()
        prog = self.ecrire(d, "logiciel", PROGRAMME)
        self.assertEqual(te.preavis(d, {"id": "p1", "genre": "arret", "donnees": {}}, 10), (True, None))
        with open(prog + ".recu") as f:
            self.assertEqual(json.load(f)["genre"], "arret")
        self.assertEqual(te.preavis(d, {"id": "p2", "genre": "orage", "donnees": {}}, 10), (False, 40))
        self.ecrire(d, "ancien", "import sys\nsys.exit(2)\n")
        self.assertEqual(te.preavis(d, {"id": "p3", "genre": "arret", "donnees": {}}, 10), (True, None),
                         "un programme qui ne connait pas --preavis n'a rien a faire")
        self.assertEqual(te.preavis(tempfile.mkdtemp(), {"id": "p4", "genre": "arret"}, 10), (True, None))

    def test_trop_long(self):
        d = tempfile.mkdtemp()
        self.ecrire(d, "lent", "import time\ntime.sleep(5)\n")
        self.assertEqual(te.preavis(d, {"id": "p", "genre": "orage"}, 1), (False, None))

    @unittest.skipIf(np_ is None, "module nearfield_pont absent")
    def test_traduction(self):
        v2 = trad.vers_v2({"type": "preavis_recu", "reponse_a": "pv-1", "pret": False, "attente_s": 40})
        self.assertEqual(trad.depuis_v2(v2), {"type": "preavis_recu", "reponse_a": "pv-1", "pret": False,
                                             "attente_s": 40})
        m = {"type": "preavis", "id": "pv-2", "genre": "orage", "donnees": {"niveau": "proche"}}
        self.assertEqual(trad.depuis_v2(m), m)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
