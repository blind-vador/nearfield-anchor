"""Travaux signes v2 (spec §6) : du depot par le Pi au resultat relu par le Pi.

Le VRAI code des deux cotes (lib/compagnon.py + lib/pont_v2.py ; bridge/nearfield_bridge.py),
etat dans un dossier temporaire, parc deja migre (cle v2 du Pi epinglee par le Mac,
cle v2 du Mac dans sa fiche cote Pi). Seul le calcul lui-meme est simule.

On verifie :
  - un travail signe est pris, execute tel que SIGNE (pas tel que travail.json le dit),
    et son resultat v2 est accepte par le Pi, sous-dossiers compris ;
  - le Mac refuse : un travail sans objet signe, signe par une autre cle, dont un
    fichier d'entree a change, ou deja rendu (rejeu) ;
  - le Pi refuse : un resultat v1 seul d'une machine migree, un fichier rendu modifie,
    un resultat signe par une autre cle, ou lie a un autre travail ;
  - un Pi non migre (pas de cle epinglee) : la v1 marche comme avant.
Saute si nearfield_pont ou cryptography manquent.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp(prefix="essai_travaux_v2_")
os.environ["NFA_HOME"] = os.path.join(TMP, "autopilot")
os.environ["NFA_USER_HOME"] = TMP

RACINE_DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("bridge", "lib", "watchers", "tools", "config"):
    sys.path.insert(0, os.path.join(RACINE_DEPOT, d))

try:
    import nearfield_pont  # noqa: F401
    import cryptography  # noqa: F401
    DISPONIBLE = True
except ImportError:
    DISPONIBLE = False


@unittest.skipUnless(DISPONIBLE, "module nearfield_pont ou cryptography absent")
class TravauxV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import compagnon
        import nearfield_bridge as nb
        import pont_protocole as proto
        import pont_v2
        cls.c, cls.nb, cls.proto, cls.v2 = compagnon, nb, proto, pont_v2
        pi_dir = os.path.join(TMP, "pi")
        compagnon.LOCAL_DIR = pi_dir
        compagnon.KEY_PATH = os.path.join(pi_dir, "cle.pem")
        compagnon.TRUST_DIR = os.path.join(pi_dir, "confiance")
        compagnon.BRIDGE_DIR = os.path.join(TMP, "nas")
        pont_v2.V2_DIR = os.path.join(pi_dir, "v2")

        mac_dir = os.path.join(TMP, "mac")
        nb.HOME = mac_dir
        nb.CONFIG = os.path.join(mac_dir, "config.json")
        nb.KEY_PATH = os.path.join(mac_dir, "cle.pem")
        nb.BRIDGES = os.path.join(mac_dir, "ponts")
        nb.STATS = os.path.join(mac_dir, "stats")
        nb.V2_DIR = os.path.join(mac_dir, "v2")
        nb.TERMINES = os.path.join(nb.V2_DIR, "termines.json")
        nb.capabilities = lambda: ["whisper", "examen"]
        nb.find_share = lambda card=None: compagnon.BRIDGE_DIR
        cls.pi_id = compagnon.pi_id()
        cls.mac_id = "macessai0002"
        nb._write(nb.CONFIG, {"id": cls.mac_id, "nom": "Mac mini"})
        nb._write(os.path.join(nb.BRIDGES, cls.pi_id + ".json"), {"cle": proto.public_b64(compagnon.key())})

        # Parc migre : chacun connait la cle v2 de l'autre.
        os.makedirs(nb.V2_DIR, exist_ok=True)
        cls.mac_v2 = nearfield_pont.Coffre.charger_ou_creer(os.path.join(nb.V2_DIR, "identite.pem"), "p256")
        cls.epingle = {"ordonnateur": pont_v2.coffre().cle_publique(), "parc": pont_v2.parc()}
        cls.carte = {"nom": "Mac mini", "cle": proto.public_b64(nb.key()), "capacites": ["whisper"], "stats": {},
                     "v2": {"id": cls.mac_v2.id, "identite": cls.mac_v2.cle_publique(), "niveau": "logiciel"}}

    def setUp(self):
        shutil.rmtree(os.path.join(self.c.BRIDGE_DIR, "travaux"), ignore_errors=True)
        for f in (self.nb.TERMINES,):
            if os.path.exists(f):
                os.remove(f)
        self.c._write(os.path.join(self.c.TRUST_DIR, self.mac_id + ".json"), self.carte)
        self.nb._write(os.path.join(self.nb.V2_DIR, "ponts", self.pi_id + ".json"), self.epingle)
        self.nb._refus_travail.clear()
        self.c._refus_resultat.clear()
        self.vus = []

        def calcul(job, folder):
            self.vus.append(job)
            with open(os.path.join(folder, "texte.json"), "w") as f:
                json.dump({"texte": "bonjour"}, f)
            os.makedirs(os.path.join(folder, "audio"), exist_ok=True)
            with open(os.path.join(folder, "audio", "a.wav"), "wb") as f:
                f.write(b"RIFF")
            return "fait", ["texte.json", "audio/a.wav"], "transcrit"
        self.nb._process = calcul

    def deposer(self, params=None):
        job_id, folder, job = self.c.new_job("whisper", params or {"langue": "fr", "seuil": 0.5})
        with open(os.path.join(folder, "son.wav"), "wb") as f:
            f.write(b"\x00" * 1000)
        self.c.submit(job)
        return job_id, folder

    def travailler(self):
        self.nb._work(self.pi_id, False)

    def finis(self):
        return {j["id"]: r for j, r, _ in self.c.finished_jobs()}

    # --- le chemin normal
    def test_aller_retour_signe(self):
        job_id, folder = self.deposer()
        self.assertTrue(os.path.exists(os.path.join(folder, "travail.v2.json")))
        self.travailler()
        self.assertEqual(len(self.vus), 1)
        self.assertTrue(os.path.exists(os.path.join(folder, "resultat.v2.json")))
        r = self.finis()[job_id]
        self.assertEqual(r["etat"], "fait")
        self.assertEqual(set(r["sorties"]), {"texte.json", "audio/a.wav"})

    def test_le_mac_execute_le_travail_signe_pas_le_fichier_en_clair(self):
        job_id, folder = self.deposer({"langue": "fr"})
        clair = os.path.join(folder, "travail.json")
        job = self.c._read(clair)
        self.c._write(clair, dict(job, params={"langue": "xx", "commande": "rm -rf /"}))
        self.travailler()
        self.assertEqual(self.vus[0]["params"], {"langue": "fr"})

    # --- refus cote Mac
    def test_travail_sans_objet_signe_refuse(self):
        job_id, folder = self.deposer()
        os.remove(os.path.join(folder, "travail.v2.json"))
        self.travailler()
        self.assertEqual(self.vus, [])
        self.assertTrue(os.path.exists(os.path.join(folder, "travail.json")))  # pas pris

    def test_travail_signe_par_une_autre_cle_refuse(self):
        job_id, folder = self.deposer()
        intrus = nearfield_pont.Coffre.generer("p256")
        objet = self.c._read(os.path.join(folder, "travail.v2.json"))
        self.c._write(os.path.join(folder, "travail.v2.json"), intrus.signer_objet(objet["charge"]))
        self.travailler()
        self.assertEqual(self.vus, [])

    def test_fichier_d_entree_modifie_refuse(self):
        job_id, folder = self.deposer()
        with open(os.path.join(folder, "son.wav"), "ab") as f:
            f.write(b"x")
        self.travailler()
        self.assertEqual(self.vus, [])

    def test_objet_signe_d_un_autre_travail_refuse(self):
        a, dossier_a = self.deposer()
        b, dossier_b = self.deposer()
        shutil.copy(os.path.join(dossier_a, "travail.v2.json"), os.path.join(dossier_b, "travail.v2.json"))
        os.remove(os.path.join(dossier_a, "travail.json"))  # seul b reste a prendre
        self.travailler()
        self.assertEqual(self.vus, [])

    def test_travail_rejoue_pas_refait(self):
        job_id, folder = self.deposer()
        self.travailler()
        for n in ("resultat.json", "resultat.v2.json"):
            os.remove(os.path.join(folder, n))
        os.rename(os.path.join(folder, "travail.pris.json"), os.path.join(folder, "travail.json"))
        self.travailler()
        self.assertEqual(len(self.vus), 1)

    # --- refus cote Pi
    def test_resultat_v1_seul_refuse_pour_une_machine_migree(self):
        job_id, folder = self.deposer()
        self.travailler()
        os.remove(os.path.join(folder, "resultat.v2.json"))
        self.assertNotIn(job_id, self.finis())

    def test_fichier_rendu_modifie_refuse(self):
        job_id, folder = self.deposer()
        self.travailler()
        with open(os.path.join(folder, "audio", "a.wav"), "ab") as f:
            f.write(b"x")
        self.assertNotIn(job_id, self.finis())

    def test_resultat_signe_par_une_autre_cle_refuse(self):
        job_id, folder = self.deposer()
        self.travailler()
        intrus = nearfield_pont.Coffre.generer("p256")
        objet = self.c._read(os.path.join(folder, "resultat.v2.json"))
        self.c._write(os.path.join(folder, "resultat.v2.json"), intrus.signer_objet(objet["charge"]))
        self.assertNotIn(job_id, self.finis())

    def test_resultat_d_un_autre_travail_refuse(self):
        a, dossier_a = self.deposer()
        self.travailler()
        b, dossier_b = self.deposer()
        self.travailler()
        # Le resultat (valide) de a recopie dans b : lie a la charge de a, refuse pour b.
        shutil.copy(os.path.join(dossier_a, "resultat.v2.json"), os.path.join(dossier_b, "resultat.v2.json"))
        finis = self.finis()
        self.assertIn(a, finis)
        self.assertNotIn(b, finis)

    # --- parc non migre
    def test_v1_inchangee_sans_epingle(self):
        os.remove(os.path.join(self.nb.V2_DIR, "ponts", self.pi_id + ".json"))
        carte = dict(self.carte)
        carte.pop("v2")
        self.c._write(os.path.join(self.c.TRUST_DIR, self.mac_id + ".json"), carte)
        job_id, folder = self.deposer()
        self.travailler()
        self.assertEqual(len(self.vus), 1)
        self.assertFalse(os.path.exists(os.path.join(folder, "resultat.v2.json")))
        self.assertEqual(self.finis()[job_id]["etat"], "fait")


if __name__ == "__main__":
    unittest.main()
