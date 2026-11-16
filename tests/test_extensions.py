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
        extensions._annoncer = lambda texte: texte and cls.dits.append(texte)
        # Une machine du parc qui sait faire "whisper".
        compagnon._write(os.path.join(compagnon.TRUST_DIR, "macessai.json"),
                         {"nom": "Mac mini", "capacites": ["whisper"], "stats": {},
                          "v2": {"id": "x", "identite": np_.Coffre.generer().cle_publique(), "niveau": "materiel"}})
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
        cls.serveur = extensions.demarrer()
        cls.cle = np_.Coffre.generer()
        r = extensions.Client(cls.cle).enroler("prismalab-ia", ["agent-code", "whisper"])
        assert r["type"] == "enrole", r

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
             "plan": "Relire docs/guide.md et corriger les fautes.", "budget": {"secondes": 600, "cout_centimes": 50},
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
