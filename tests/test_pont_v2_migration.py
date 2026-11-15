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
  - sans le module nearfield_pont, rien de tout ca ne se declenche ;
  - le trafic reel passe par la v2 (avis de travail, tickets, voix macOS et son
    retour, TETRA, numerique avec banc et rendus), traduit et valide ;
  - une demande TETRA v2 vise l'hote de la session, jamais une adresse recue ;
  - un message que la liste fermee refuse passe par la v1 en secours ;
  - une session v2 impossible (pare-feu ferme) ou tombee est retentee toute seule,
    avec un certificat neuf redemande par la v1.

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
import uuid
from unittest import mock

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
        pont_v2.PORT = pont_v2.demarrer("127.0.0.1", 0, recu=compagnon_serveur._traiter)
        nb._write(os.path.join(nb.BRIDGES, cls.pi_id + ".json"),
                  {"cle": cls.cle_pi_v1, "hote": "127.0.0.1", "port": cls.v1.server_address[1]})

    def lancer_mac(self):
        link = self.nb.Link(self.pi_id)
        link.start()

        def arreter():
            link.shutdown()
            link.join(10)  # sa fin ecrit les stats : qu'elle n'empiete pas sur l'essai suivant
            if link.pilote_v2:
                link.pilote_v2.arreter()
        self.addCleanup(arreter)
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
        self.nb.update_stats(self.pi_id, erreur_v2=None)
        self.lancer_mac()
        self.assertTrue(attendre(lambda: self.nb.stats(self.pi_id).get("erreur_v2")), "refus cote Mac")
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

    # --- trafic reel par la v2 --------------------------------------------------------

    def connecte_en_v2(self):
        link = self.lancer_mac()
        self.assertTrue(attendre(lambda: self.pont_v2.en_v2(self.mac_id)), "session v2 cote Pi")
        self.assertTrue(attendre(lambda: link.pilote_v2 is not None and link.pilote_v2.session is not None))
        self.assertTrue(attendre(lambda: self.mac_id in self.serveur._channels), "liaison v1")
        return link

    def espions(self, link):
        """(envois du Pi, envois du Mac) : [(type v1, passe par la v2 ?)]."""
        pi, mac = [], []
        vrai_pi = self.pont_v2.envoyer

        def pi_envoyer(cid, msg):
            r = vrai_pi(cid, msg)
            pi.append((msg["type"], r))
            return r
        vrai_mac = link.pilote_v2.envoyer

        def mac_envoyer(msg):
            r = vrai_mac(msg)
            mac.append((msg["type"], r))
            return r
        p = mock.patch.object(self.pont_v2, "envoyer", side_effect=pi_envoyer)
        p.start()
        self.addCleanup(p.stop)
        link.pilote_v2.envoyer = mac_envoyer
        return pi, mac

    def test_6_travail_et_tickets_par_la_v2(self):
        link = self.connecte_en_v2()
        pi, _ = self.espions(link)
        reveils = []
        with mock.patch.object(self.nb, "_wake_workers", side_effect=reveils.append):
            self.assertTrue(self.serveur._notify(self.mac_id, "20261115-whisper-essai"))
            self.assertTrue(attendre(lambda: reveils), "avis de travail recu par le Mac")
        # (comme _signal_tickets, sans toucher au /tmp/tickets_signal du service en marche)
        self.serveur._envoyer(self.mac_id, self.serveur._channels[self.mac_id],
                              {"type": "tickets", "ids": ["ticket-essai-1"]})
        self.assertTrue(attendre(lambda: "ticket-essai-1" in self.nb._tickets_attendus), "tickets")
        self.assertIn(("travail", True), pi)
        self.assertIn(("tickets", True), pi)

    def test_7_voix_mac_aller_retour_par_la_v2(self):
        import base64
        import queue
        link = self.connecte_en_v2()
        pi, mac = self.espions(link)
        demandes = []

        def faux_say(lien, msg):
            demandes.append(msg)
            lien.send({"type": "son", "reponse_a": msg["id"], "n": 0, "pcm": base64.b64encode(b"\x01\x00" * 800).decode()})
            lien.send({"type": "son_fin", "reponse_a": msg["id"], "ok": True, "taux": 22050})
        rid = uuid.uuid4().hex
        file = queue.Queue()
        self.serveur._demandes_voix[rid] = file
        self.addCleanup(self.serveur._demandes_voix.pop, rid, None)
        with mock.patch.object(self.nb, "say_to_pi", side_effect=faux_say):
            self.serveur._envoyer(self.mac_id, self.serveur._channels[self.mac_id],
                                  {"type": "dire", "id": rid, "texte": "Essai.", "voix": "Thomas", "debit": "+10%"})
            son = file.get(timeout=10)
            fin = file.get(timeout=10)
        self.assertEqual(demandes[0]["debit"], "+10%")
        self.assertEqual((son["type"], base64.b64decode(son["pcm"])), ("son", b"\x01\x00" * 800))
        self.assertEqual((fin["type"], fin["ok"], fin["taux"]), ("son_fin", True, 22050))
        self.assertIn(("dire", True), pi)
        self.assertEqual([r for _, r in mac if _ in ("son", "son_fin")], [True, True])

    def test_8_tetra_et_numerique_par_la_v2(self):
        import base64
        import queue
        link = self.connecte_en_v2()
        pi, mac = self.espions(link)
        recues = []

        def faux_tetra(lien, msg):
            recues.append(msg)
            lien.send({"type": "tetra_voix", "reponse_a": msg["id"], "n": 0, "taux": 8000,
                       "pcm": base64.b64encode(b"\x00\x01" * 100).decode()})
            lien.send({"type": "tetra_resultat", "reponse_a": msg["id"], "ok": True, "erreur": None,
                       "reseau": {"mcc": 208, "mnc": 1, "nom": "Réseau"}, "bsch": 4, "sysinfo": 2, "enc": [],
                       "clair": True, "mac_resource": 3, "clip": False, "voix": True, "resume": "Réseau vu.",
                       "activite": {"appels": 1}, "duree": 12.5})

        def faux_numerique(lien, msg):
            recues.append(msg)
            for piste in ("numerique_voix", "numerique_banc_dmr", "numerique_rendu_fs"):
                lien.send({"type": piste, "reponse_a": msg["id"], "n": 0, "taux": 8000,
                           "pcm": base64.b64encode(b"\x00\x02" * 50).decode()})
            lien.send({"type": "numerique_resultat", "reponse_a": msg["id"], "ok": True, "erreur": None,
                       "protocole": "DMR", "meta": {"cc": 1, "tg": [91]}, "chiffre": False, "clair": True,
                       "voix": True, "audio": False, "flux": False, "numerique_confirme": True,
                       "digital_actif": True, "sync_hits": 12, "voix_rms": 340.5, "audio_rms": None,
                       "resume": "DMR en clair.", "duree": 8, "banc": ["dmr"], "banc_preuve": {"raison": "x"},
                       "preuve": None, "rendus": [{"mode": "-fs", "protocole": "DMR", "confirme": True, "hits": 4,
                                                   "rms": 12.0, "chiffre": False, "voix": True}]})
        files = {}
        for genre, demandes in (("tetra", self.serveur._demandes_tetra), ("numerique", self.serveur._demandes_numerique)):
            rid = uuid.uuid4().hex
            files[genre] = (rid, queue.Queue())
            demandes[rid] = files[genre][1]
            self.addCleanup(demandes.pop, rid, None)
        canal = self.serveur._channels[self.mac_id]
        with mock.patch.object(self.nb, "tetra_to_pi", side_effect=faux_tetra), \
                mock.patch.object(self.nb, "numerique_to_pi", side_effect=faux_numerique):
            self.serveur._envoyer(self.mac_id, canal, {"type": "tetra", "id": files["tetra"][0], "host": "10.9.9.9",
                                                       "port": 1234, "freq_hz": 392_512_500, "gain": 32,
                                                       "secondes": 12, "max_secondes": 60, "ppm": 0})
            t = [files["tetra"][1].get(timeout=10) for _ in range(2)]
            self.serveur._envoyer(self.mac_id, canal, {"type": "numerique", "id": files["numerique"][0],
                                                       "host": "10.9.9.9", "port": 1234, "freq_hz": 467_238_000,
                                                       "gain": 32, "secondes": 8, "max_secondes": 8, "ppm": 0,
                                                       "mode": "-fa", "banc": True, "modes": ["-fs"]})
            n = [files["numerique"][1].get(timeout=10) for _ in range(4)]
        # la machine vise l'hote de sa session v2, pas l'adresse envoyee
        self.assertEqual([r["host"] for r in recues], ["127.0.0.1", "127.0.0.1"])
        self.assertEqual((recues[0]["gain"], recues[0]["max_secondes"]), (32, 60))
        self.assertEqual((recues[1]["mode"], recues[1]["modes"], recues[1]["banc"]), ("-fa", ["-fs"], True))
        self.assertEqual([m["type"] for m in t], ["tetra_voix", "tetra_resultat"])
        self.assertEqual((t[0]["taux"], t[1]["reseau"]["nom"], t[1]["duree"], t[1]["bsch"]), (8000, "Réseau", 12.5, 4))
        self.assertEqual([m["type"] for m in n],
                         ["numerique_voix", "numerique_banc_dmr", "numerique_rendu_fs", "numerique_resultat"])
        res = n[-1]
        self.assertEqual((res["sync_hits"], res["voix_rms"], res["duree"], res["rendus"][0]["hits"]), (12, 340.5, 8, 4))
        self.assertTrue(all(r for t_, r in pi if t_ in ("tetra", "numerique")))
        self.assertTrue(mac and all(r for _, r in mac if _ != "battement"), mac)

    def test_9_refus_de_la_liste_fermee_passe_par_la_v1(self):
        import queue
        link = self.connecte_en_v2()
        pi, _ = self.espions(link)
        recues = []
        rid = uuid.uuid4().hex
        self.serveur._demandes_numerique[rid] = queue.Queue()
        self.addCleanup(self.serveur._demandes_numerique.pop, rid, None)
        with mock.patch.object(self.nb, "numerique_to_pi", side_effect=lambda lien, msg: recues.append(msg)):
            # un mode qui ressemble a une commande : refuse par nearfield_pont, la v1 l'achemine comme avant
            self.serveur._envoyer(self.mac_id, self.serveur._channels[self.mac_id],
                                  {"type": "numerique", "id": rid, "host": "10.9.9.9", "port": 1234, "freq_hz": 1,
                                   "gain": 32, "secondes": 8, "max_secondes": 8, "ppm": 0, "mode": "-fa; rm -rf /"})
            self.assertTrue(attendre(lambda: recues))
        self.assertEqual(pi, [("numerique", False)])
        self.assertEqual(recues[0]["host"], "10.9.9.9")  # arrive par la v1, inchange
        self.assertTrue(self.pont_v2.en_v2(self.mac_id), "la session v2 reste ouverte")

    def test_10_session_v2_retentee_apres_ouverture_du_port(self):
        # Le Pi annonce un port ferme (pare-feu pas encore ouvert) : la session v2
        # echoue, le trafic reste en v1 ; une fois le port "ouvert", le Mac redemande
        # un certificat par la v1 et la session s'etablit, sans reconnexion v1.
        vrai_port = self.pont_v2.PORT
        ferme = socket.socket()
        ferme.bind(("127.0.0.1", 0))
        self.pont_v2.PORT = ferme.getsockname()[1]   # personne n'ecoute : refus de connexion
        ferme.close()
        self.addCleanup(setattr, self.pont_v2, "PORT", vrai_port)
        echecs = []
        vraie = self.nb.Link.v2_terminee

        def compter(lien, etablie):
            echecs.append(etablie)
            return vraie(lien, etablie)
        with mock.patch.object(self.nb, "RELANCE_V2_SEC", (0.5, 1)), \
                mock.patch.object(self.nb.Link, "v2_terminee", compter):
            self.lancer_mac()
            self.assertTrue(attendre(lambda: len(echecs) >= 2), "tentatives refusees puis relancees")
            self.assertEqual(set(echecs), {False})
            self.assertFalse(self.pont_v2.en_v2(self.mac_id))
            self.pont_v2.PORT = vrai_port                  # le pare-feu s'ouvre
            self.assertTrue(attendre(lambda: self.pont_v2.en_v2(self.mac_id), 15), "session v2 retentee")
            self.assertTrue(attendre(lambda: self.nb.stats(self.pi_id).get("v2")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
