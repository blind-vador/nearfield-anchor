"""Essai de connexion de bout en bout : passage de la v1 a la v2 (pilote).

On part d'un parc v1 deja appairé (cles Ed25519, fiche de confiance cote Pi,
fiche du pont cote Mac) et on fait tourner le VRAI code : le serveur du Pi
(lib/compagnon_serveur.py + lib/pont_v2.py) et la liaison du Mac
(bridge/nearfield_bridge.py, Link). Rien n'est simule a part les mesures de
la machine (capacites, voix, puissance) et les sons.

On verifie :
  - la v1 marche comme avant (bonjour, bienvenue, battements) ;
  - la migration se fait toute seule sur le canal v1, dans les deux sens ;
  - le Mac ouvre une session Noise v2 et ses battements y arrivent ;
  - une reconnexion refait la migration sans rien casser (meme identite) ;
  - un Mac dont la cle v2 a ete remplacee est refuse (une cle v1 ne migre qu'une fois) ;
  - un Pi dont la cle v2 change est refuse par le Mac (cle epinglee) ;
  - sans le module nearfield_pont, rien de tout ca ne se declenche.

Tout l'etat est dans un dossier temporaire (NFA_HOME, HOME du pont) : aucun
fichier du Pi en service n'est touche. Saute si nearfield_pont ou
cryptography manquent.
"""
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest

TMP = tempfile.mkdtemp(prefix="essai_pont_v2_")
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


def attendre(condition, delai=10.0):
    fin = time.time() + delai
    while time.time() < fin:
        if condition():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(DISPONIBLE, "module nearfield_pont ou cryptography absent")
class PassageV1V2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import compagnon
        import compagnon_serveur
        import nearfield_bridge as nb
        import pont_protocole as proto
        import pont_v2
        cls.compagnon, cls.serveur, cls.nb, cls.proto, cls.pont_v2 = compagnon, compagnon_serveur, nb, proto, pont_v2

        # --- Pi : etat local dans le dossier temporaire
        pi_dir = os.path.join(TMP, "pi")
        compagnon.LOCAL_DIR = pi_dir
        compagnon.KEY_PATH = os.path.join(pi_dir, "cle.pem")
        compagnon.TRUST_DIR = os.path.join(pi_dir, "confiance")
        compagnon.PRESENCE = os.path.join(TMP, "presence.json")
        compagnon.BRIDGE_DIR = os.path.join(TMP, "nas")
        pont_v2.V2_DIR = os.path.join(pi_dir, "v2")
        pont_v2.PRESENCE_V2 = os.path.join(TMP, "presence_v2.json")
        compagnon_serveur._son = lambda *a, **k: None

        # --- Mac : HOME du pont dans le dossier temporaire, mesures fixes
        mac_dir = os.path.join(TMP, "mac")
        nb.HOME = mac_dir
        nb.CONFIG = os.path.join(mac_dir, "config.json")
        nb.KEY_PATH = os.path.join(mac_dir, "cle.pem")
        nb.BRIDGES = os.path.join(mac_dir, "ponts")
        nb.STATS = os.path.join(mac_dir, "stats")
        nb.V2_DIR = os.path.join(mac_dir, "v2")
        nb.BEAT_SEC = 0.2
        nb.capabilities = lambda: ["whisper", "decrire_images"]
        nb.mac_voices = lambda: []
        nb.power = lambda etat=None: {"puissance": 100.0, "disponible": 80.0}
        nb.session_state = lambda: {"verrouille": False}
        nb.network_identity = lambda: ("00:11:22:33:44:55", ["127.0.0.1"])
        nb._wake_workers = lambda pi: None

        # --- Parc v1 deja appaire : les deux fiches, comme apres un appairage
        cls.pi_id = compagnon.pi_id()
        cls.mac_id = "macessai0001"
        nb._write(nb.CONFIG, {"id": cls.mac_id, "nom": "Mac mini"})
        cls.cle_pi_v1 = proto.public_b64(compagnon.key())
        cls.cle_mac_v1 = proto.public_b64(nb.key())
        compagnon._write(os.path.join(compagnon.TRUST_DIR, cls.mac_id + ".json"),
                         {"nom": "Mac mini", "cle": cls.cle_mac_v1, "capacites": ["whisper"], "stats": {}})

        # --- Serveurs du Pi : v1 (le vrai _Handler) et v2, sur des ports libres
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        cls.v1 = socketserver.ThreadingTCPServer(("127.0.0.1", 0), compagnon_serveur._Handler)
        cls.v1.daemon_threads = True
        threading.Thread(target=cls.v1.serve_forever, daemon=True).start()
        pont_v2.PORT = pont_v2.demarrer("127.0.0.1", 0)
        nb._write(os.path.join(nb.BRIDGES, cls.pi_id + ".json"),
                  {"cle": cls.cle_pi_v1, "hote": "127.0.0.1", "port": cls.v1.server_address[1]})

    def lancer_mac(self):
        link = self.nb.Link(self.pi_id)
        link.start()
        self.addCleanup(link.shutdown)
        return link

    def presence_v2(self):
        return self.pont_v2.presence().get(self.mac_id) or {}

    def fiche(self):
        return self.compagnon.trusted()[self.mac_id]

    def test_1_passage_complet(self):
        link = self.lancer_mac()
        # v1 : le Pi reconnait la machine comme avant
        self.assertTrue(attendre(lambda: self.nb.stats(self.pi_id).get("connecte")), "liaison v1")
        # migration : la fiche du Pi recoit l'identite v2, le Mac epingle celle du Pi
        self.assertTrue(attendre(lambda: "v2" in self.fiche()), "migration du Mac")
        epingle = self.nb._read(os.path.join(self.nb.V2_DIR, "ponts", self.pi_id + ".json"))
        self.assertEqual(epingle["parc"], self.pont_v2.parc())
        self.assertEqual(epingle["ordonnateur"], self.pont_v2.coffre().cle_publique())
        mac_v2 = nearfield_pont.Coffre.ouvrir(os.path.join(self.nb.V2_DIR, "identite.pem"))
        self.assertEqual(self.fiche()["v2"]["id"], mac_v2.id)
        # session v2 : les battements arrivent par Noise
        self.assertTrue(attendre(lambda: self.presence_v2().get("battements", 0) >= 3), "battements v2")
        self.assertTrue(self.nb.stats(self.pi_id).get("v2"))
        # la v1 continue a vivre a cote
        self.assertTrue(self.nb.stats(self.pi_id).get("connecte"))
        with open(os.path.join(self.pont_v2.V2_DIR, "migrations.json")) as f:
            migrations = json.load(f)
        self.assertEqual(migrations["faites"], {self.cle_mac_v1: mac_v2.id})
        link.shutdown()
        self.assertTrue(attendre(lambda: not self.presence_v2()), "fin de session v2")

    def test_2_reconnexion_refait_la_migration_sans_rien_casser(self):
        avant = self.fiche()["v2"]["id"]
        self.lancer_mac()
        self.assertTrue(attendre(lambda: self.presence_v2().get("battements", 0) >= 2), "v2 apres reconnexion")
        self.assertEqual(self.fiche()["v2"]["id"], avant)

    def test_3_cle_v2_du_mac_remplacee_refusee(self):
        # Quelqu'un remplace la cle v2 du Mac (ou un voleur de cle.pem essaie une
        # autre identite) : la cle v1 a deja migre, le Pi refuse.
        ident = os.path.join(self.nb.V2_DIR, "identite.pem")
        os.replace(ident, ident + ".vraie")
        self.addCleanup(os.replace, ident + ".vraie", ident)
        avant = self.fiche()["v2"]["id"]
        self.lancer_mac()
        self.assertTrue(attendre(lambda: self.nb.stats(self.pi_id).get("connecte")))
        time.sleep(1.5)
        self.assertFalse(self.presence_v2(), "aucune session v2 ne doit s'ouvrir")
        self.assertEqual(self.fiche()["v2"]["id"], avant, "l'identite v2 enregistree ne change pas")

    def test_4_cle_v2_du_pi_changee_refusee_par_le_mac(self):
        ident = os.path.join(self.pont_v2.V2_DIR, "identite.pem")
        os.replace(ident, ident + ".vraie")
        self.addCleanup(os.replace, ident + ".vraie", ident)
        self.pont_v2._moi.clear()
        self.addCleanup(self.pont_v2._moi.clear)
        self.lancer_mac()
        self.assertTrue(attendre(lambda: "erreur_v2" in self.nb.stats(self.pi_id)), "refus cote Mac")
        self.assertIn("a change", self.nb.stats(self.pi_id)["erreur_v2"])
        self.assertFalse(self.presence_v2())

    def test_5_sans_module_v2_rien_ne_change(self):
        # Le Pi sans le module (cas des services apres fusion tant qu'il n'est pas
        # installe) : pas d'annonce de migration, la v1 seule.
        self.serveur.pont_v2.np_, garde = None, self.serveur.pont_v2.np_
        self.addCleanup(setattr, self.serveur.pont_v2, "np_", garde)
        self.nb.update_stats(self.pi_id, v2=None, erreur_v2=None)
        self.lancer_mac()
        self.assertTrue(attendre(lambda: self.nb.stats(self.pi_id).get("connecte")))
        time.sleep(1.0)
        self.assertFalse(self.presence_v2())
        self.assertFalse(self.nb.stats(self.pi_id).get("v2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
