"""Point d'acces local des extensions (lib/extensions.py, Prismaforge lot 2 §5).

Le VRAI serveur (prise Unix dans un dossier temporaire) et le client de reference ;
la validation humaine est simulee par une cle humaine logicielle (l'appareil a
biometrie est essaye dans test_pont_v2_migration, test_13).

On verifie :
  - prise 0600 dans un dossier 0700 ;
  - enrolement : seulement apres validation humaine ; refuse par l'humain = rien ;
    deux extensions du meme nom refusees ;
  - ouverture : cle inconnue refusee, defi d'une autre connexion refuse ;
  - ticket : brouillon controle (zones, capacites inconnues, champ en trop), validation
    humaine, ticket signe par l'ordonnateur et verifiable ; refus de l'humain = pas de ticket ;
  - journal : entrees signees par l'extension, chainees ; fourche, entree d'une autre
    cle et ecriture apres cloture refusees ; lecture ; evenement aux abonnes ;
  - travail : seulement une capacite du ticket et du parc, parametres bornes ;
  - liste fermee : un type inconnu (« executer ») est refuse.
Saute si nearfield_pont manque.
"""
import os
import sys
import tempfile
import threading
import time
import unittest

TMP = tempfile.mkdtemp(prefix="essai_ext_")
os.environ["NFA_HOME"] = os.path.join(TMP, "autopilot")
os.environ["NFA_USER_HOME"] = TMP

RACINE_DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("bridge", "lib", "watchers", "tools", "config"):
    sys.path.insert(0, os.path.join(RACINE_DEPOT, d))

try:
    import nearfield_pont as np_
    DISPONIBLE = True
except ImportError:
    DISPONIBLE = False


@unittest.skipUnless(DISPONIBLE, "module nearfield_pont absent")
class PointExtensions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import compagnon
        import pont_v2
        import extensions
        cls.c, cls.v2, cls.ext = compagnon, pont_v2, extensions
        d = os.path.join(TMP, "pi")
        compagnon.LOCAL_DIR = d
        compagnon.KEY_PATH = os.path.join(d, "cle.pem")
        compagnon.TRUST_DIR = os.path.join(d, "confiance")
        compagnon.BRIDGE_DIR = os.path.join(TMP, "nas")
        pont_v2.V2_DIR = os.path.join(d, "v2")
        pont_v2._coffre[0] = None
        extensions.SURVEILLANCE_SEC = 0.2
        cls.dits = []
        extensions.ANNONCES = True
        extensions._annoncer = lambda texte, son="journal_entree": texte and cls.dits.append(texte)
        # Deux machines du parc qui savent faire "whisper" ; seul le Mac est present au depart.
        compagnon.PRESENCE = os.path.join(TMP, "presence.json")
        cls.mac, cls.pc = np_.Coffre.generer(), np_.Coffre.generer()
        for cid, nom, c in (("macessai", "Mac mini", cls.mac), ("pcessai", "PC essai", cls.pc)):
            compagnon._write(os.path.join(compagnon.TRUST_DIR, cid + ".json"),
                             {"nom": nom, "capacites": ["whisper"], "stats": {},
                              "v2": {"id": c.id, "identite": c.cle_publique(), "niveau": "materiel"}})
        cls.presents(["macessai"])
        # Cle humaine (simulee) et validation sans appareil.
        cls.humain = np_.Coffre.generer()
        hid = cls.humain.id
        os.makedirs(pont_v2.V2_DIR, exist_ok=True)
        compagnon._write(os.path.join(pont_v2.V2_DIR, "humains.json"),
                         {hid: {"identite": cls.humain.cle_publique(), "machine": "macessai", "nom": "Mac"}})
        cls.decision = ["accepte"]
        cls.demandes = []

        def demander(sujet, charge, delai=None):
            cls.demandes.append((sujet, charge))
            if cls.decision[0] is None:
                raise np_.ErreurPont("refuse par l'humain")
            return np_.preparer_validation(cls.humain, pont_v2.parc(), charge, cls.decision[0])
        pont_v2.demander_validation = demander
        cls.choix = ["redacteur"]
        cls.arbitrage_libre = threading.Event()
        cls.arbitrage_libre.set()

        def arbitrer(charge, options, delai=None):
            cls.arbitrage_libre.wait(10)
            c = dict(charge, options=list(options), choix=cls.choix[0])
            return np_.preparer_validation(cls.humain, pont_v2.parc(), c, "accepte"), cls.choix[0]
        pont_v2.demander_arbitrage = arbitrer
        cls.serveur = extensions.demarrer()
        cls.cle = np_.Coffre.generer()
        r = extensions.Client(cls.cle).enroler("prismalab-ia", ["agent-code", "whisper"])
        assert r["type"] == "enrole", r

    @classmethod
    def presents(cls, ids):
        cls.c._write(cls.c.PRESENCE, {i: {"vu": time.time(), "depuis": time.time()} for i in ids})

    @classmethod
    def tearDownClass(cls):
        cls.serveur.close()

    def setUp(self):
        self.decision[0] = "accepte"

    def client(self, cle=None):
        cl = self.ext.Client(cle or self.cle)
        r = cl.ouvrir()
        self.addCleanup(cl.fermer)
        return cl, r

    def brouillon(self, tid, **autres):
        b = {"type": "ticket", "parc": self.v2.parc(), "id": tid, "titre": "Corriger le guide",
             "plan": "Relire docs/guide.md et corriger les fautes.", "mode": "lax",
             "budget": {"secondes": 600, "cout_centimes": 50},
             "capacites": ["whisper"], "zones": ["docs/guide.md"]}
        b.update(autres)
        return b

    def ticket_emis(self, cl, tid):
        self.assertEqual(cl.requete("ticket_proposer", brouillon=self.brouillon(tid))["type"], "reponse")
        r = cl.requete("ticket_valider", ticket=tid)
        self.assertEqual(r["type"], "reponse", r)
        return r["ticket"]

    # --------------------------------------------------------------------------------

    def test_prise_privee(self):
        prise = self.ext.chemin_prise()
        self.assertEqual(os.stat(prise).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(prise)).st_mode & 0o777, 0o700)

    def test_enrolement_valide_par_l_humain(self):
        sujet, charge = [d for d in self.demandes if d[0] == "extension"][0]
        self.assertEqual(charge["nom"], "prismalab-ia")
        self.assertEqual(charge["empreinte"], self.cle.id)
        self.assertIn(self.cle.id, self.ext.fiches())
        # Meme nom : refuse ; refuse par l'humain : rien d'enregistre.
        r = self.ext.Client(np_.Coffre.generer()).enroler("prismalab-ia", [])
        self.assertEqual(r["type"], "erreur")
        self.decision[0] = None
        autre = np_.Coffre.generer()
        r = self.ext.Client(autre).enroler("autre-outil", [])
        self.assertEqual(r["type"], "erreur")
        self.assertNotIn(autre.id, self.ext.fiches())
        self.decision[0] = "refuse"
        r = self.ext.Client(autre).enroler("autre-outil", [])
        self.assertEqual(r["type"], "erreur")
        self.assertNotIn(autre.id, self.ext.fiches())

    def test_ouverture(self):
        cl, r = self.client()
        self.assertEqual(r["type"], "accueil")
        self.assertEqual(r["nom"], "prismalab-ia")
        intrus = self.ext.Client(np_.Coffre.generer())
        self.assertEqual(intrus.ouvrir()["type"], "erreur")
        intrus.fermer()
        # Defi d'une autre connexion : la presentation ne vaut que pour la sienne.
        a = self.ext.Client(self.cle)
        a._connecter()
        b = self.ext.Client(self.cle)
        b._connecter()
        objet = self.cle.signer_objet({"type": "extension_ouverture", "parc": a.bonjour["parc"],
                                       "id": "ouverture-essai-defi", "defi": a.bonjour["defi"]})
        b._envoyer({"type": "presentation", "objet": objet})
        self.assertEqual(self.ext._recevoir(b.conn)["type"], "erreur")
        a.fermer()
        b.fermer()

    def test_liste_fermee(self):
        cl, _ = self.client()
        r = cl.requete("executer", commande="rm -rf /")
        self.assertEqual(r["type"], "erreur")
        self.assertIn("inconnu", r["raison"])

    def test_brouillons_refuses(self):
        cl, _ = self.client()
        for b in (self.brouillon("t-zone", zones=["../etc/passwd"]),
                  self.brouillon("t-cap", capacites=["inconnue"]),
                  self.brouillon("t-champ", commande="ls"),
                  self.brouillon("T majuscule"),
                  self.brouillon("t-parc", parc="autre")):
            self.assertEqual(cl.requete("ticket_proposer", brouillon=b)["type"], "erreur", b)
        # Valider sans brouillon : refuse.
        self.assertEqual(cl.requete("ticket_valider", ticket="jamais-propose")["type"], "erreur")

    def test_ticket_journal_travail(self):
        cl, _ = self.client()
        ab, _ = self.client()
        self.assertEqual(ab.requete("evenements")["type"], "reponse")
        ticket = self.ticket_emis(cl, "guide-1")
        humaines = [self.humain.cle_publique()]
        b = np_.verifier_ticket(ticket, self.v2.coffre().cle_publique(), self.v2.parc(), humaines)
        self.assertEqual(b["titre"], "Corriger le guide")
        self.assertEqual(ab.recevoir()["genre"], "ticket_emis")
        # Deja emis : on ne le repropose pas.
        self.assertEqual(cl.requete("ticket_proposer", brouillon=self.brouillon("guide-1"))["type"], "erreur")
        # Journal chaine, signe par l'extension.
        p = self.v2.parc()
        e0 = np_.ecrire_journal(self.cle, p, ticket, None, "prise", "Je prends le ticket.")
        self.assertEqual(cl.requete("journal_ecrire", entree=e0)["n"], 0)
        evt = ab.recevoir()
        self.assertEqual((evt["genre"], evt["journal_genre"], evt["texte"]), ("journal", "prise", "Je prends le ticket."))
        e1 = np_.ecrire_journal(self.cle, p, ticket, e0, "progression", "La moitie est faite.")
        self.assertEqual(cl.requete("journal_ecrire", entree=e1)["n"], 1)
        fourche = np_.ecrire_journal(self.cle, p, ticket, e0, "note", "Autre suite.")
        self.assertEqual(cl.requete("journal_ecrire", entree=fourche)["type"], "erreur")
        autre = np_.ecrire_journal(np_.Coffre.generer(), p, ticket, e1, "note", "Intrus.")
        self.assertEqual(cl.requete("journal_ecrire", entree=autre)["type"], "erreur")
        self.assertIn("Je prends le ticket.", self.dits, "le Pi dit le texte du journal")
        # Validation demandee et ticket lance annonces aussi (texte selon les phrases installees).
        self.assertGreaterEqual(len(self.dits), 3)
        lu = cl.requete("journal_lire", ticket="guide-1")
        self.assertEqual([e["charge"]["n"] for e in lu["entrees"]], [0, 1])
        etat = cl.requete("ticket_lire", ticket="guide-1")
        self.assertEqual((etat["entrees"], etat["clos"]), (2, False))
        # Travail : capacite du ticket et du parc seulement.
        r = cl.requete("travail_deposer", ticket="guide-1", capacite="whisper", params={"langue": "fr"})
        self.assertEqual(r["type"], "reponse", r)
        job = self.c._read(os.path.join(self.c.BRIDGE_DIR, "travaux", r["travail"], "travail.json"))
        self.assertEqual((job["type"], job["ticket"], job["params"]), ("whisper", "guide-1", {"langue": "fr"}))
        # Les bornes du ticket voyagent dans le travail signe.
        self.assertEqual((job["bornes"]["plan"], job["bornes"]["zones"], job["bornes"]["capacites"]),
                         ("Relire docs/guide.md et corriger les fautes.", ["docs/guide.md"], ["whisper"]))
        self.assertEqual(job["bornes"]["empreinte"], self.v2.empreinte_charge(ticket["charge"]))
        self.assertEqual(cl.requete("travail_deposer", ticket="guide-1", capacite="agent-code")["type"], "erreur")
        self.assertEqual(cl.requete("travail_deposer", ticket="guide-1", capacite="whisper",
                                    params={"x": "a" * 20000})["type"], "erreur")
        # Cloture : plus rien apres.
        e2 = np_.ecrire_journal(self.cle, p, ticket, e1, "cloture", "Termine.")
        self.assertEqual(cl.requete("journal_ecrire", entree=e2)["n"], 2)
        self.assertTrue(cl.requete("ticket_lire", ticket="guide-1")["clos"])
        e3 = np_.ecrire_journal(self.cle, p, ticket, e2, "note", "Apres coup.")
        self.assertEqual(cl.requete("journal_ecrire", entree=e3)["type"], "erreur")
        self.assertEqual(cl.requete("travail_deposer", ticket="guide-1", capacite="whisper")["type"], "erreur")

    def test_ticket_refuse_par_l_humain(self):
        cl, _ = self.client()
        self.assertEqual(cl.requete("ticket_proposer", brouillon=self.brouillon("refuse-1"))["type"], "reponse")
        self.decision[0] = "refuse"
        self.assertEqual(cl.requete("ticket_valider", ticket="refuse-1")["type"], "erreur")
        self.assertFalse(os.path.exists(os.path.join(self.v2.V2_DIR, "tickets", "refuse-1", "ticket.json")))

    def test_validation_longue_ne_bloque_pas_la_connexion(self):
        cl, _ = self.client()
        cl.requete("ticket_proposer", brouillon=self.brouillon("lent-1"))
        lache = threading.Event()
        vrai = self.v2.demander_validation

        def lente(sujet, charge, delai=None):
            lache.wait(5)
            return vrai(sujet, charge)
        self.v2.demander_validation = lente
        self.addCleanup(setattr, self.v2, "demander_validation", vrai)
        rid = "lent"
        cl._envoyer({"type": "ticket_valider", "id": rid, "ticket": "lent-1"})
        debut = time.time()
        r = cl.requete("ticket_lire", ticket="inexistant")  # repond pendant que l'humain lit
        self.assertEqual(r["type"], "erreur")
        self.assertLess(time.time() - debut, 2)
        lache.set()
        r = cl.recevoir()
        self.assertEqual((r["type"], r["reponse_a"]), ("reponse", rid))


    # --- modes, controle mecanique, desaccords, contre-epreuve ----------------------------

    def bloc(self, parent, tid, zones, secondes=200, mode="lax"):
        return self.brouillon(tid, titre="Bloc " + tid, zones=zones, mode=mode,
                              budget={"secondes": secondes, "cout_centimes": 10},
                              parent={"ticket": parent["charge"]["id"], "empreinte": np_.empreinte_objet(parent)})

    def emettre(self, cl, b):
        self.assertEqual(cl.requete("ticket_proposer", brouillon=b)["type"], "reponse")
        return cl.requete("ticket_valider", ticket=b["id"])

    def test_blocs_en_mode_lax(self):
        cl, _ = self.client()
        parent = self.emettre(cl, self.brouillon("projet-lax", zones=["docs/"]))["ticket"]
        avant = len(self.demandes)
        r = self.emettre(cl, self.bloc(parent, "projet-lax-a", ["docs/guide.md"]))
        self.assertEqual(r["type"], "reponse", r)
        self.assertIn("couvert_par", r["ticket"]["charge"])
        self.assertEqual(len(self.demandes), avant, "lax : pas de nouvelle validation")
        # Budget cumule au-dela du parent, zone hors du parent : refuses.
        b = self.bloc(parent, "projet-lax-b", ["docs/x.md"], secondes=500)
        self.assertEqual(cl.requete("ticket_proposer", brouillon=b)["type"], "erreur")
        b = self.bloc(parent, "projet-lax-c", ["src/"])
        self.assertEqual(cl.requete("ticket_proposer", brouillon=b)["type"], "erreur")

    def test_blocs_en_mode_strict(self):
        cl, _ = self.client()
        parent = self.emettre(cl, self.brouillon("projet-strict", zones=["src/"], mode="strict"))["ticket"]
        avant = len(self.demandes)
        r = self.emettre(cl, self.bloc(parent, "projet-strict-a", ["src/a.py"], mode="strict"))
        self.assertEqual(r["type"], "reponse", r)
        self.assertIn("validation", r["ticket"]["charge"])
        self.assertEqual(len(self.demandes), avant + 1, "strict : chaque bloc est valide")
        # Un bloc lax sous un parent strict : refuse.
        b = self.bloc(parent, "projet-strict-b", ["src/b.py"], mode="lax")
        self.assertEqual(cl.requete("ticket_proposer", brouillon=b)["type"], "erreur")
        self.decision[0] = "refuse"
        r = self.emettre(cl, self.bloc(parent, "projet-strict-c", ["src/c.py"], mode="strict"))
        self.assertEqual(r["type"], "erreur")

    def test_controle_mecanique_arrete_le_ticket(self):
        cl, _ = self.client()
        t = self.ticket_emis(cl, "meca-1")
        p = self.v2.parc()
        e0 = np_.ecrire_journal(self.cle, p, t, None, "prise", "Je commence.")
        cl.requete("journal_ecrire", entree=e0)
        e1 = np_.ecrire_journal(self.cle, p, t, e0, "progression", "Fait.",
                                {"fichiers": ["docs/guide.md"], "consomme": {"secondes": 100, "cout_centimes": 5}})
        self.assertNotIn("arrete", cl.requete("journal_ecrire", entree=e1))
        e2 = np_.ecrire_journal(self.cle, p, t, e1, "resultat", "Fini.", {"fichiers": ["src/secret.py"]})
        r = cl.requete("journal_ecrire", entree=e2)
        self.assertIn("hors des zones", r["arrete"])
        etat = cl.requete("ticket_lire", ticket="meca-1")
        self.assertTrue(etat["clos"])
        self.assertTrue(any("arrêté" in d for d in self.dits))

    def test_budget_depasse_arrete_le_ticket(self):
        cl, _ = self.client()
        t = self.ticket_emis(cl, "meca-2")
        e0 = np_.ecrire_journal(self.cle, self.v2.parc(), t, None, "progression", "Long.",
                                {"consomme": {"secondes": 601}})
        self.assertIn("budget", cl.requete("journal_ecrire", entree=e0)["arrete"])

    def dernier(self, cl, tid):
        return cl.requete("journal_lire", ticket=tid)["entrees"][-1]

    def test_desaccord_pause_arbitrage_puis_arret(self):
        cl, _ = self.client()
        t = self.ticket_emis(cl, "debat-1")
        p = self.v2.parc()
        self.arbitrage_libre.clear()
        self.addCleanup(self.arbitrage_libre.set)
        self.choix[0] = "verificateur"
        e0 = np_.ecrire_journal(self.cle, p, t, None, "desaccord", "Le vérificateur conteste le cache.",
                                {"points": [{"sujet": "cache", "redacteur": "plus rapide", "verificateur": "fuite"}],
                                 "impact": "un fichier"})
        self.assertTrue(cl.requete("journal_ecrire", entree=e0)["pause"])
        self.assertTrue(cl.requete("ticket_lire", ticket="debat-1")["pause"])
        r = cl.requete("travail_deposer", ticket="debat-1", capacite="whisper")
        self.assertIn("pause", r["raison"])
        self.arbitrage_libre.set()
        fin = time.time() + 5
        while cl.requete("ticket_lire", ticket="debat-1")["pause"] and time.time() < fin:
            time.sleep(0.05)
        v = self.dernier(cl, "debat-1")
        self.assertEqual((v["charge"]["genre"], v["charge"]["donnees"]["choix"]), ("validation", "verificateur"))
        self.assertEqual(v["signataire"], self.v2.coffre().id, "arbitrage ecrit par l'ordonnateur")
        # Deuxieme desaccord : le ticket s'arrete.
        e2 = np_.ecrire_journal(self.cle, p, t, v, "desaccord", "Encore un désaccord.")
        self.assertIn("deuxieme", cl.requete("journal_ecrire", entree=e2)["arrete"])
        self.assertTrue(cl.requete("ticket_lire", ticket="debat-1")["clos"])

    def test_arret_valide_par_l_humain(self):
        cl, _ = self.client()
        parent = self.emettre(cl, self.brouillon("arret-1", zones=["docs/"]))["ticket"]
        self.assertEqual(self.emettre(cl, self.bloc(parent, "arret-1-a", ["docs/guide.md"]))["type"], "reponse")
        self.assertEqual(cl.requete("ticket_arreter", ticket="arret-1", raison=" ")["type"], "erreur")
        self.decision[0] = None
        self.assertEqual(cl.requete("ticket_arreter", ticket="arret-1", raison="plus utile")["type"], "erreur")
        self.assertFalse(cl.requete("ticket_lire", ticket="arret-1")["clos"])
        self.decision[0] = "accepte"
        self.assertTrue(cl.requete("ticket_arreter", ticket="arret-1", raison="plus utile")["arrete"])
        self.assertEqual(self.demandes[-1][0], "arret")
        self.assertEqual(self.demandes[-1][1]["raison"], "plus utile")
        v = self.dernier(cl, "arret-1")
        self.assertEqual((v["charge"]["genre"], v["signataire"]), ("cloture", self.v2.coffre().id))
        self.assertEqual(v["charge"]["donnees"]["validation"]["signataire"], self.humain.id)
        self.assertTrue(cl.requete("ticket_lire", ticket="arret-1-a")["clos"], "bloc arrete avec son parent")
        self.assertIn("clos", cl.requete("ticket_arreter", ticket="arret-1", raison="encore")["raison"])

    def test_arbitrage_redemande(self):
        cl, _ = self.client()
        t = self.ticket_emis(cl, "redemande-1")
        self.ticket_emis(cl, "redemande-2")
        self.assertEqual(cl.requete("arbitrage_demander", ticket="redemande-2")["type"], "erreur", "pas en pause")
        arbitrer = self.v2.demander_arbitrage

        def absent(charge, options, delai=None):
            raise np_.ErreurPont("aucun appareil avec une cle humaine n'est connecte")
        self.v2.demander_arbitrage = absent
        try:
            e0 = np_.ecrire_journal(self.cle, self.v2.parc(), t, None, "desaccord", "Avis contraires.")
            self.assertTrue(cl.requete("journal_ecrire", entree=e0)["pause"])
            fin = time.time() + 5
            while "redemande-1" in self.ext._arbitrages and time.time() < fin:
                time.sleep(0.05)
            self.assertTrue(cl.requete("ticket_lire", ticket="redemande-1")["pause"])
            self.assertEqual(cl.requete("arbitrage_demander", ticket="redemande-1")["type"], "erreur")
        finally:
            self.v2.demander_arbitrage = arbitrer
        self.choix[0] = "redacteur"
        self.assertEqual(cl.requete("arbitrage_demander", ticket="redemande-1")["choix"], "redacteur")
        self.assertFalse(cl.requete("ticket_lire", ticket="redemande-1")["pause"])

    def rendu_par(self, coffre, job_id):
        """Resultat v2 de ce travail, signe par cette machine (comme bridge/_rendre_v2)."""
        dossier = self.c._path("travaux", job_id)
        travail = self.c._read(os.path.join(dossier, "travail.v2.json"))
        self.c._write(os.path.join(dossier, "resultat.v2.json"), coffre.signer_objet({
            "type": "resultat", "parc": self.v2.parc(), "id": "resultat-" + job_id, "travail": job_id,
            "objet_travail": self.v2.empreinte_charge(travail["charge"]), "etat": "fait", "message": "",
            "duree": 1, "fichiers": {}}))

    def test_contre_epreuve_choisie_par_l_ordonnateur(self):
        cl, _ = self.client()
        self.ticket_emis(cl, "verif-1")
        job = cl.requete("travail_deposer", ticket="verif-1", capacite="whisper", params={"x": 1})["travail"]
        self.assertEqual(cl.requete("contre_epreuve", ticket="verif-1", travail=job, capacite="whisper")["type"],
                         "erreur", "pas de resultat signe")
        # Resultat signe par une cle inconnue : refuse aussi.
        self.rendu_par(np_.Coffre.generer(), job)
        self.assertEqual(cl.requete("contre_epreuve", ticket="verif-1", travail=job, capacite="whisper")["type"],
                         "erreur")
        self.rendu_par(self.mac, job)
        # Seul le Mac est la : meme machine, contexte neutre.
        r = cl.requete("contre_epreuve", ticket="verif-1", travail=job, capacite="whisper")
        self.assertEqual((r["type"], r["meme_machine"]), ("reponse", True), r)
        verif = self.c._read(self.c._path("travaux", r["travail"], "travail.json"))
        self.assertTrue(verif["params"]["contexte_neutre"])
        self.assertNotIn("exclure", verif)
        e = self.dernier(cl, "verif-1")
        self.assertEqual((e["charge"]["genre"], e["signataire"]), ("contre_epreuve", self.v2.coffre().id))
        # Le PC revient : la contre-epreuve lui est confiee, l'auteur est exclu.
        self.presents(["macessai", "pcessai"])
        self.addCleanup(self.presents, ["macessai"])
        r = cl.requete("contre_epreuve", ticket="verif-1", travail=job, capacite="whisper")
        self.assertEqual((r["meme_machine"], r["verificateur"]), (False, "PC essai"), r)
        signe = self.c.lire_travail(self.c._path("travaux", r["travail"]))
        self.assertEqual(signe["exclure"], ["macessai"])
        # Un travail d'un autre ticket : refuse.
        self.ticket_emis(cl, "verif-2")
        self.assertEqual(cl.requete("contre_epreuve", ticket="verif-2", travail=job, capacite="whisper")["type"],
                         "erreur")


    # --- interface de l'IDE (lot 3) ------------------------------------------------------

    def test_relais_de_l_interface(self):
        import base64

        class FausseSession:
            def __init__(self):
                self.recus = []

            def envoyer(self, msg):
                np_.encoder_message(msg)  # la liste fermee v2 doit l'accepter
                self.recus.append(msg)
        coquille = FausseSession()
        self.v2._sessions["coquille-essai"] = coquille
        self.addCleanup(self.v2._sessions.pop, "coquille-essai", None)
        carte = {"nom": "Coquille", "role": "interface"}
        requete = {"type": "interface_requete", "id": "r1", "methode": "GET", "chemin": "/"}
        # Pas de front : 503.
        self.ext.recevoir_requete_interface("coquille-essai", carte, requete)
        self.assertEqual(coquille.recus[-1]["statut"], 503)
        # Une extension sans la capacite "interface" ne peut pas servir.
        cl, _ = self.client()
        self.assertEqual(cl.requete("interface_servir")["type"], "erreur")
        # Le front (capacite interface) recoit la requete et repond ; gros corps en morceaux.
        cle_front = np_.Coffre.generer()
        self.assertEqual(self.ext.Client(cle_front).enroler("ide-front", ["interface"])["type"], "enrole")
        front, _ = self.client(cle_front)
        self.assertEqual(front.requete("interface_servir")["type"], "reponse")
        self.ext.recevoir_requete_interface("coquille-essai", carte, dict(requete, id="r2", chemin="/api/tickets"))
        evt = front.recevoir()
        self.assertEqual((evt["genre"], evt["chemin"], evt["appareil"]["id"]),
                         ("interface_requete", "/api/tickets", "coquille-essai"))
        corps = ("x" * 80000).encode()
        r = front.requete("interface_reponse", requete=evt["requete"], statut=200, type_contenu="text/plain",
                          corps=base64.b64encode(corps).decode())
        self.assertEqual(r["type"], "reponse", r)
        morceaux = [m for m in coquille.recus if m["reponse_a"] == "r2"]
        self.assertEqual([m["n"] for m in morceaux], [0, 1, 2])
        self.assertEqual([m["fin"] for m in morceaux], [False, False, True])
        self.assertEqual(b"".join(base64.b64decode(m["corps"]) for m in morceaux), corps)
        # Une reponse a une requete inconnue (ou deja servie) : refusee.
        self.assertEqual(front.requete("interface_reponse", requete=evt["requete"], statut=200, corps="")["type"],
                         "erreur")
        # Front parti : 503 de nouveau.
        front.fermer()
        time.sleep(0.2)
        self.ext.recevoir_requete_interface("coquille-essai", carte, dict(requete, id="r3"))
        self.assertEqual(coquille.recus[-1]["statut"], 503)

    def test_liste_des_tickets(self):
        cl, _ = self.client()
        self.ticket_emis(cl, "liste-1")
        r = cl.requete("tickets_lister")
        self.assertIn("liste-1", [t["id"] for t in r["tickets"]])
        t = next(t for t in r["tickets"] if t["id"] == "liste-1")
        self.assertEqual((t["mode"], t["clos"], t["pause"]), ("lax", False, False))

    def test_parc_lire(self):
        cl, _ = self.client()
        r = cl.requete("parc_lire")
        self.assertEqual(r["parc"], self.v2.parc())
        self.assertIn("whisper", r["capacites"])
        # Presence et etat des logiciels externes (2026-11-18) : transportes tels quels.
        import compagnon
        compagnon.note_battement("macessai", ["whisper"], [], None, None,
                                 {"prismaforge": {"charge": 0.4, "disque_libre_go": 120.5}})
        r = cl.requete("parc_lire")
        mac = next(m for m in r["machines"] if m["id"] == "macessai")
        self.assertTrue(mac["en_ligne"])
        self.assertEqual(mac["extensions"], {"prismaforge": {"charge": 0.4, "disque_libre_go": 120.5}})
        pc = next(m for m in r["machines"] if m["id"] == "pcessai")
        self.assertEqual((pc["en_ligne"], pc["extensions"]), (False, {}))
        self.assertNotIn("ordonnateur", r)


    def test_artefacts_references_par_le_journal(self):
        import base64
        import hashlib
        cl, _ = self.client()
        t = self.ticket_emis(cl, "art-1")
        diff = b"--- a/docs/guide.md\n+++ b/docs/guide.md\n@@ -1,2 +1,2 @@\n-Bonjour\n+Bonsoir\n fin\n"
        r = cl.requete("artefact_deposer", ticket="art-1", nom="diff-1.patch",
                       contenu=base64.b64encode(diff).decode())
        self.assertEqual(r["empreinte"], hashlib.sha256(diff).hexdigest())
        # Jamais reecrit ; noms surs seulement ; taille bornee.
        self.assertEqual(cl.requete("artefact_deposer", ticket="art-1", nom="diff-1.patch", contenu="")["type"],
                         "erreur")
        for nom in ("../x", "Diff", ".cache", "a/b"):
            self.assertEqual(cl.requete("artefact_deposer", ticket="art-1", nom=nom, contenu="")["type"], "erreur")
        gros = base64.b64encode(b"x" * (180 * 1024 + 1)).decode()
        self.assertEqual(cl.requete("artefact_deposer", ticket="art-1", nom="gros", contenu=gros)["type"], "erreur")
        lu = cl.requete("artefact_lire", ticket="art-1", nom="diff-1.patch")
        self.assertEqual((base64.b64decode(lu["contenu"]), lu["reference"]), (diff, None))
        # Une entree signee le reference : il fait foi.
        e0 = np_.ecrire_journal(self.cle, self.v2.parc(), t, None, "resultat", "Diff du guide.",
                                {"fichiers": ["docs/guide.md"], "artefacts": {"diff-1.patch": r["empreinte"]}})
        self.assertEqual(cl.requete("journal_ecrire", entree=e0)["n"], 0)
        self.assertEqual(cl.requete("artefact_lire", ticket="art-1", nom="diff-1.patch")["reference"], 0)
        # Joint a un travail : copie dans son dossier, couverte par la signature du travail.
        r = cl.requete("travail_deposer", ticket="art-1", capacite="whisper", params={}, artefacts=["diff-1.patch"])
        self.assertEqual(r["type"], "reponse", r)
        dossier = os.path.join(self.c.BRIDGE_DIR, "travaux", r["travail"])
        with open(os.path.join(dossier, "diff-1.patch"), "rb") as f:
            self.assertEqual(f.read(), diff)
        objet = self.c._read(os.path.join(dossier, "travail.v2.json"))
        np_.verifier_fichiers(objet, dossier)
        self.assertIn("diff-1.patch", str(objet))
        # Un artefact qui ne fait pas foi, ou inconnu : refuse.
        cl.requete("artefact_deposer", ticket="art-1", nom="libre.txt", contenu=base64.b64encode(b"x").decode())
        for noms in (["libre.txt"], ["absent.patch"], ["../x"], ["a"] * 9):
            self.assertEqual(cl.requete("travail_deposer", ticket="art-1", capacite="whisper", params={},
                                        artefacts=noms)["type"], "erreur", noms)

    def test_progression_et_sorties_d_un_travail(self):
        import base64
        import hashlib
        cl, _ = self.client()
        ab, _ = self.client()
        ab.requete("evenements")
        self.ticket_emis(cl, "analyse-1")
        self.assertEqual(ab.recevoir()["genre"], "ticket_emis")
        job_id = cl.requete("travail_deposer", ticket="analyse-1", capacite="whisper", params={})["travail"]
        mac = self.c.trusted()["macessai"]
        # Phrase en direct de la machine qui fait le travail ; une autre machine est ignoree.
        self.ext.progression("macessai", mac, {"type": "progression", "travail": job_id, "etape": "action",
                                               "pourcentage": 10, "phrase": "Lit a.py."})
        self.ext.progression("pcessai", {"nom": "PC"}, {"type": "progression", "travail": job_id,
                                                         "etape": "texte", "phrase": "Intrus."})
        self.ext.progression("macessai", mac, {"type": "progression", "travail": "inconnu", "etape": "texte"})
        evt = ab.recevoir()
        self.assertEqual((evt["genre"], evt["ticket"], evt["etape"], evt["phrase"], evt["machine"]),
                         ("travail_progression", "analyse-1", "action", "Lit a.py.", "Mac mini"))
        # Fin : sorties couvertes par la signature recueillies sur la carte SD, puis annoncees.
        dossier = os.path.join(TMP, "nas-travail-" + job_id)
        os.makedirs(dossier)
        fiche = "# Fiche d'architecture\n".encode()
        for nom, contenu in (("fiche.md", fiche), ("autre.md", b"x"), ("trop.md", b"y")):
            with open(os.path.join(dossier, nom), "wb") as f:
                f.write(contenu)
        resultat = {"etat": "fait", "message": "Fiche ecrite.", "duree": 8,
                    "fichiers": {"fiche.md": hashlib.sha256(fiche).hexdigest(),
                                 "autre.md": "sha256:" + hashlib.sha256(b"x").hexdigest(),
                                 "trop.md": hashlib.sha256(b"autre chose").hexdigest(), "../x": "0"}}
        self.ext.recueillir({"id": job_id, "ticket": "analyse-1", "type": "whisper"}, resultat, dossier)
        evt = ab.recevoir()
        self.assertEqual((evt["genre"], evt["etat"], evt["sorties"]), ("travail_fini", "fait", ["autre.md", "fiche.md"]))
        lu = cl.requete("sortie_lire", ticket="analyse-1", travail=job_id, nom="fiche.md")
        self.assertEqual((base64.b64decode(lu["contenu"]), lu["etat"]), (fiche, "fait"))
        for nom in ("trop.md", "../x", "inconnu.md"):
            self.assertEqual(cl.requete("sortie_lire", ticket="analyse-1", travail=job_id, nom=nom)["type"], "erreur")
        with open(self.ext._dossier("tickets", "analyse-1", "sorties", job_id, "fiche.md"), "ab") as f:
            f.write(b"ajout")
        self.assertEqual(cl.requete("sortie_lire", ticket="analyse-1", travail=job_id, nom="fiche.md")["type"],
                         "erreur")
        self.ext.progression("macessai", mac, {"type": "progression", "travail": job_id, "etape": "texte"})

    def test_travail_autorise_par_l_humain(self):
        import base64
        import hashlib
        cl, _ = self.client()
        t = self.ticket_emis(cl, "appli-1")
        diff = b"--- a/docs/guide.md\n+++ b/docs/guide.md\n@@ -1 +1 @@\n-a\n+b\n"
        r = cl.requete("artefact_deposer", ticket="appli-1", nom="modifs.patch", contenu=base64.b64encode(diff).decode())
        e0 = np_.ecrire_journal(self.cle, self.v2.parc(), t, None, "resultat", "Diff.",
                                {"fichiers": ["docs/guide.md"], "artefacts": {"modifs.patch": r["empreinte"]}})
        cl.requete("journal_ecrire", entree=e0)
        avant = len(self.demandes)
        # Capacite hors du ticket : refuse sans deranger l'humain.
        r = cl.requete("travail_autoriser", ticket="appli-1", capacite="agent-code", params={}, action="Appliquer.")
        self.assertEqual((r["type"], len(self.demandes)), ("erreur", avant))
        # Refuse par l'humain : aucun travail.
        self.decision[0] = "refuse"
        r = cl.requete("travail_autoriser", ticket="appli-1", capacite="whisper", params={"projet": "demo"},
                       action="Appliquer le diff relu.", artefacts=["modifs.patch"])
        self.assertEqual(r["type"], "erreur")
        self.decision[0] = "accepte"
        r = cl.requete("travail_autoriser", ticket="appli-1", capacite="whisper", params={"projet": "demo"},
                       action="Appliquer le diff relu.", artefacts=["modifs.patch"])
        self.assertEqual(r["type"], "reponse", r)
        sujet, charge = self.demandes[-1]
        self.assertEqual((sujet, charge["action"], charge["artefacts"]),
                         ("travail", "Appliquer le diff relu.", {"modifs.patch": hashlib.sha256(diff).hexdigest()}))
        dossier = os.path.join(self.c.BRIDGE_DIR, "travaux", r["travail"])
        job = self.v2.travail_signe(dossier)
        self.assertEqual((job["autorisation"]["n"], job["autorisation"]["empreinte"]),
                         (r["n"], self.v2.empreinte_charge(charge)))
        with open(os.path.join(dossier, "modifs.patch"), "rb") as f:
            self.assertEqual(f.read(), diff)
        entree = cl.requete("journal_lire", ticket="appli-1")["entrees"][r["n"]]["charge"]
        self.assertEqual((entree["genre"], entree["donnees"]["autorisation"]["id"]), ("validation", charge["id"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
