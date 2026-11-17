"""Essai de connexion de bout en bout : passage de la v1 a la v2, puis v2 seule.

On part d'un parc v1 deja appairé (cles Ed25519, fiche de confiance cote Pi,
fiche du pont cote Mac) et on fait tourner le VRAI code : le serveur du Pi
(lib/compagnon_serveur.py + lib/pont_v2.py) et la liaison du Mac
(bridge/nearfield_bridge.py, Link). Rien n'est simule a part les mesures de
la machine (capacites, voix, puissance) et les sons.

On verifie :
  - la migration se fait toute seule sur le canal v1, puis le Mac passe en v2 SEULE :
    sa liaison v1 se ferme, il garde cle de transport et certificat, et le Pi le tient
    pour "autonome" (son ecoute v1 n'est plus necessaire) ;
  - apres un redemarrage, le Mac se reconnecte en v2 sans la v1 ;
  - le certificat se renouvelle dans la session, a mi-vie, avec une preuve signee par
    la cle d'identite ; un certificat expire (longue veille) se renouvelle aussi ;
  - une demande de certificat sans preuve, ou avec la preuve d'une autre cle, est refusee ;
  - un Pi dont la cle v2 change est refuse par le Mac (cle epinglee) ;
  - le trafic reel passe par la v2 (avis de travail, tickets, voix macOS et son
    retour, TETRA, numerique avec banc et rendus), traduit et valide ;
  - une demande TETRA v2 vise l'hote de la session, jamais une adresse recue ;
  - un message que la liste fermee refuse n'est pas envoye (plus de v1 en secours) ;
  - appairage v2 d'une nouvelle machine par le NAS : meme code des deux cotes, fiche
    v2 seule, session etablie ; un engagement non tenu est refuse ;
  - coffre materiel (puce simulee, meme protocole que pf-enclave) : l'identite logicielle
    du Mac passe dans la puce par une rotation contresignee, sans nouvel appairage, et
    l'ancienne ne vaut plus rien ; la cle humaine est annoncee, et une validation demandee
    par le Pi revient signee par elle (dialogue accepte), ou refusee.

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
        nb._wake_service = lambda: None
        nb.PUCE = os.path.join(mac_dir, "bin", "pf-enclave")  # absent : pas de puce par defaut
        nb.HUMAIN = os.path.join(nb.V2_DIR, "humain.puce")
        nb.ANNONCES_HUMAIN = os.path.join(nb.V2_DIR, "humain-annonces")

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
        pont_v2.PORT = pont_v2.demarrer("127.0.0.1", 0, recu=compagnon_serveur._traiter,
                                         evenement=compagnon_serveur._evenement_v2)
        nb._write(os.path.join(nb.BRIDGES, cls.pi_id + ".json"),
                  {"cle": cls.cle_pi_v1, "hote": "127.0.0.1", "port": cls.v1.server_address[1]})

    def lancer_mac(self):
        """Comme la boucle du service : LiaisonV2 si l'etat v2 est garde, sinon Link (v1)."""
        nb = self.nb
        link = nb.LiaisonV2(self.pi_id) if nb._etat_v2(self.pi_id) else nb.Link(self.pi_id)
        nb._links[self.pi_id] = link
        link.start()

        def arreter():
            for lien in {link, nb._links.get(self.pi_id)}:
                if lien is not None:
                    lien.shutdown()
                    lien.join(10)  # sa fin ecrit les stats : qu'elle n'empiete pas sur l'essai suivant
            nb._links.pop(self.pi_id, None)
            attendre(lambda: not self.presence_v2(), 5)
        self.addCleanup(arreter)
        return link

    def lien(self):
        return self.nb._links.get(self.pi_id)

    def presence_v2(self):
        return self.pont_v2.presence().get(self.mac_id) or {}

    def fiche(self):
        return self.compagnon.trusted()[self.mac_id]

    def certificat_garde(self):
        return self.nb._read(os.path.join(self.nb._dossier_session(self.pi_id), "certificat.json"))["objet"]

    def connecte_en_v2(self):
        self.lancer_mac()
        nb = self.nb
        self.assertTrue(attendre(lambda: isinstance(self.lien(), nb.LiaisonV2) and self.lien().pilote_v2 is not None
                                 and self.lien().pilote_v2.etablie and self.lien().pilote_v2.session is not None),
                        "session v2 cote Mac")
        self.assertTrue(attendre(lambda: isinstance(self.serveur._channels.get(self.mac_id), self.serveur.CanalV2)),
                        "canal v2 cote Pi")
        return self.lien()

    def test_01_migration_puis_v2_seule(self):
        self.lancer_mac()
        self.assertTrue(attendre(lambda: "v2" in self.fiche()), "migration du Mac")
        epingle = self.nb._read(os.path.join(self.nb.V2_DIR, "ponts", self.pi_id + ".json"))
        self.assertEqual(epingle["parc"], self.pont_v2.parc())
        self.assertEqual(epingle["ordonnateur"], self.pont_v2.coffre().cle_publique())
        mac_v2 = nearfield_pont.Coffre.ouvrir(os.path.join(self.nb.V2_DIR, "identite.pem"))
        self.assertEqual(self.fiche()["v2"]["id"], mac_v2.id)
        # Bascule : la liaison v1 s'arrete, LiaisonV2 prend le relais
        self.assertTrue(attendre(lambda: isinstance(self.lien(), self.nb.LiaisonV2)), "bascule en v2 seule")
        self.assertTrue(attendre(lambda: self.presence_v2().get("battements", 0) >= 2), "battements v2")
        self.assertTrue(attendre(lambda: isinstance(self.serveur._channels.get(self.mac_id), self.serveur.CanalV2)))
        self.assertTrue(self.fiche().get("v2_autonome"))
        self.assertFalse(self.serveur.v1_necessaire(True), "plus besoin de l'ecoute v1")
        self.assertTrue(self.nb.stats(self.pi_id).get("connecte"))
        self.assertIn("127.0.0.1", self.fiche()["ips"])
        self.assertEqual(self.fiche().get("adresse_materielle"), "00:11:22:33:44:55")  # par le battement v2
        cle = os.path.join(self.nb._dossier_session(self.pi_id), "transport.cle")
        self.assertEqual(os.stat(cle).st_mode & 0o777, 0o600)
        with open(os.path.join(self.pont_v2.V2_DIR, "migrations.json")) as f:
            self.assertEqual(json.load(f)["faites"], {self.cle_mac_v1: mac_v2.id})

    def test_02_redemarrage_sans_la_v1(self):
        avant = self.fiche()["v2"]["id"]
        with mock.patch.object(self.nb.Link, "_session", side_effect=AssertionError("v1 utilisee")):
            link = self.connecte_en_v2()
        self.assertIsInstance(link, self.nb.LiaisonV2)
        self.assertEqual(self.fiche()["v2"]["id"], avant)
        self.assertTrue(attendre(lambda: self.presence_v2().get("battements", 0) >= 2))

    def test_03_renouvellement_a_mi_vie_dans_la_session(self):
        avant = self.certificat_garde()
        with mock.patch.object(self.nb, "CERT_MI_VIE_SEC", 13 * 3600):  # tout certificat est "a mi-vie"
            self.lancer_mac()
            self.assertTrue(attendre(lambda: self.certificat_garde() != avant, 15), "certificat renouvele")
        neuf = self.certificat_garde()["charge"]
        self.assertNotEqual(neuf["transport"], avant["charge"]["transport"])
        self.assertGreater(neuf["serie"], avant["charge"]["serie"])
        self.assertTrue(attendre(lambda: self.pont_v2.en_v2(self.mac_id)), "reconnecte avec le nouveau")

    def test_04_certificat_expire_renouvele(self):
        avant = self.certificat_garde()
        vrai = self.nb.PiloteV2
        nb = self.nb

        class PiloteDormeur(vrai):
            premier = [True]

            def __init__(self, *a, **k):
                vrai.__init__(self, *a, **k)
                if PiloteDormeur.premier[0]:
                    PiloteDormeur.premier[0] = False
                    self.expire = int(time.time()) - 60  # comme apres une longue veille
        with mock.patch.object(nb, "PiloteV2", PiloteDormeur):
            self.lancer_mac()
            self.assertTrue(attendre(lambda: self.certificat_garde() != avant, 15), "certificat expire renouvele")
            self.assertTrue(attendre(lambda: self.pont_v2.en_v2(self.mac_id)), "puis session normale")

    def test_05_demande_de_certificat_sans_preuve_refusee(self):
        import base64
        card = self.fiche()
        t = base64.b64encode(nearfield_pont.CleTransport().public).decode()
        with self.assertRaises(nearfield_pont.CertificatRefuse):
            self.pont_v2._renouveler(card, {"type": "certificat_demande", "transport": t})
        intrus = nearfield_pont.Coffre.generer("p256")
        preuve = nearfield_pont.preparer_renouvellement(intrus, self.pont_v2.parc(), base64.b64decode(t))
        with self.assertRaises(nearfield_pont.ErreurPont):
            self.pont_v2._renouveler(card, {"type": "certificat_demande", "transport": t, "preuve": preuve})

    def test_06_cle_v2_du_pi_changee_refusee_par_le_mac(self):
        ident = os.path.join(self.pont_v2.V2_DIR, "identite.pem")
        self.addCleanup(self.pont_v2._coffre.__setitem__, 0, None)  # apres la remise en place
        os.replace(ident, ident + ".vraie")
        self.addCleanup(os.replace, ident + ".vraie", ident)
        self.pont_v2._coffre[0] = None
        self.pont_v2._moi.clear()
        self.addCleanup(self.pont_v2._moi.clear)
        self.lancer_mac()
        time.sleep(2)
        self.assertFalse(self.presence_v2(), "aucune session avec un faux Pi")
        self.assertFalse(self.nb.stats(self.pi_id).get("v2"))

    # --- trafic reel par la v2 --------------------------------------------------------

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

    def test_07_travail_et_tickets_par_la_v2(self):
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

    def test_08_voix_mac_aller_retour_par_la_v2(self):
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

    def test_09_tetra_et_numerique_par_la_v2(self):
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

    def test_10_refus_de_la_liste_fermee_n_est_pas_envoye(self):
        link = self.connecte_en_v2()
        pi, _ = self.espions(link)
        recues = []
        with mock.patch.object(self.nb, "numerique_to_pi", side_effect=lambda lien, msg: recues.append(msg)):
            # un mode qui ressemble a une commande : refuse par nearfield_pont ; plus de v1 en secours
            with self.assertRaises(OSError):
                self.serveur._envoyer(self.mac_id, self.serveur._channels[self.mac_id],
                                      {"type": "numerique", "id": uuid.uuid4().hex, "host": "10.9.9.9", "port": 1234,
                                       "freq_hz": 1, "gain": 32, "secondes": 8, "max_secondes": 8, "ppm": 0,
                                       "mode": "-fa; rm -rf /"})
            time.sleep(0.5)
        self.assertEqual(recues, [])
        self.assertTrue(self.pont_v2.en_v2(self.mac_id), "la session v2 reste ouverte")

    # --- appairage v2 d'une nouvelle machine ------------------------------------------

    def machine_neuve(self, nom):
        """Une autre machine (son propre dossier du pont), qui n'a jamais connu la v1."""
        nb = self.nb
        dossier = os.path.join(TMP, nom)
        patches = [mock.patch.object(nb, "HOME", dossier), mock.patch.object(nb, "CONFIG", os.path.join(dossier, "config.json")),
                   mock.patch.object(nb, "KEY_PATH", os.path.join(dossier, "cle.pem")),
                   mock.patch.object(nb, "BRIDGES", os.path.join(dossier, "ponts")),
                   mock.patch.object(nb, "STATS", os.path.join(dossier, "stats")),
                   mock.patch.object(nb, "V2_DIR", os.path.join(dossier, "v2")),
                   mock.patch.object(nb, "PENDING", os.path.join(dossier, "appairage.json")),
                   mock.patch.object(nb, "find_share", lambda card=None: self.compagnon.BRIDGE_DIR),
                   mock.patch.object(nb, "_mount_url", lambda share: None),
                   mock.patch.object(nb, "_ask", lambda question, wait_while: "Oui")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        nb._write(nb.CONFIG, {"id": nom, "nom": "PC " + nom})

    def appairer(self, nom, tricher=None):
        """Lance les deux cotes ; rend (appairage Pi, resultat Mac etape 1)."""
        import threading as th
        p = mock.patch.object(self.compagnon, "_local_ip", lambda: "127.0.0.1")
        p.start()
        self.addCleanup(p.stop)
        app = self.pont_v2.AppairageV2()
        app.ouvrir()
        self.addCleanup(app.fermer)
        self.machine_neuve(nom)
        if tricher:
            tricher()
        etape1 = {}
        fil = th.Thread(target=lambda: etape1.update(r=self.nb.pairing_wait()), daemon=True)
        fil.start()
        self.assertTrue(attendre(lambda: app.etat() in ("code", "erreur", "refus"), 20), "reponse de la machine")
        fil.join(20)
        return app, etape1.get("r")

    def test_11_appairage_v2_d_une_nouvelle_machine(self):
        app, r = self.appairer("pcneuf0001")
        self.assertEqual(app.etat(), "code")
        self.assertEqual(r[0], "OK")
        self.assertEqual(r[2], nearfield_pont.code_parle(app.code), "meme code des deux cotes")
        self.assertEqual(app.machine["compagnon"], "pcneuf0001")
        nom = app.accepter()
        self.assertEqual(nom, "PC pcneuf0001")
        etape2 = self.nb.pairing_decide()
        self.assertEqual(etape2[0], "OK", etape2)
        fiche = self.compagnon.trusted()["pcneuf0001"]
        self.assertNotIn("cle", fiche)  # aucune cle v1 : machine appairee en v2
        parc = self.pont_v2.parc()
        pont = self.nb.bridges()[parc]
        self.assertTrue(pont["v2"])
        self.assertTrue(self.nb._etat_v2(parc))
        lien = self.nb.LiaisonV2(parc)
        lien.start()
        self.addCleanup(lambda: (lien.shutdown(), lien.join(10)))
        with mock.patch.object(self.nb, "network_identity", lambda: ("aa:bb:cc:dd:ee:ff", ["127.0.0.1"])):
            self.assertTrue(attendre(lambda: isinstance(self.serveur._channels.get("pcneuf0001"), self.serveur.CanalV2)),
                            "nouvelle machine connectee en v2")
        self.assertTrue(attendre(lambda: self.compagnon.trusted()["pcneuf0001"].get("v2_autonome")),
                        "fiche notee autonome (ecrite juste apres le canal)")

    def test_12_appairage_v2_engagement_non_tenu_refuse(self):
        def tricher():
            # Un intermediaire remplace, dans la presentation de la machine, sa cle par la
            # sienne : elle ne tient plus l'engagement pris avant l'alea du Pi.
            vrai = self.nb._write

            def ecrire(chemin, data):
                if os.path.basename(chemin).startswith("revelation-"):
                    data = {"presentation": dict(data["presentation"],
                                                 identite=nearfield_pont.Coffre.generer("p256").cle_publique())}
                return vrai(chemin, data)
            p = mock.patch.object(self.nb, "_write", ecrire)
            p.start()
            self.addCleanup(p.stop)
        app, _ = self.appairer("pcneuf0002", tricher)
        self.assertTrue(attendre(lambda: app.etat() == "erreur", 10), app.etat())
        self.assertNotIn("pcneuf0002", self.compagnon.trusted())


    def test_12b_appairage_d_un_appareil_d_interface(self):
        """La coquille Prismaforge s'appaire avec role "interface" : certificat a ce role,
        sans capacite (aucun travail), role garde au renouvellement."""
        def tricher():
            vrai = self.nb._write

            def ecrire(chemin, data):
                if os.path.basename(chemin).startswith("engagement-"):
                    data = dict(data, role="interface")
                return vrai(chemin, data)
            p = mock.patch.object(self.nb, "_write", ecrire)
            p.start()
            self.addCleanup(p.stop)
        app, r = self.appairer("coquille-essai", tricher)
        self.assertEqual(app.etat(), "code")
        self.assertEqual(app.machine["role"], "interface")
        self.assertEqual(app.machine["capacites"], [])
        app.accepter()
        fiche = self.compagnon.trusted()["coquille-essai"]
        self.assertEqual(fiche["role"], "interface")
        dossier = os.path.join(self.compagnon.BRIDGE_DIR, "appairage", "v2")
        cert = self.compagnon._read(os.path.join(dossier, "certificat-%s.json" % app.n))["objet"]
        self.assertEqual(cert["charge"]["roles"], ["interface"])
        self.assertEqual(cert["charge"]["capacites"], [])
        self.assertEqual(self.pont_v2._roles(fiche), ["interface"])
        self.assertEqual(self.pont_v2._capacites(dict(fiche, capacites=["whisper"])), [])


    # --- coffre materiel, cle humaine, validation -------------------------------------

    def fausse_puce(self):
        """Programme au protocole de pf-enclave ; la cle est un PEM (essai seulement)."""
        import stat
        site = os.path.dirname(os.path.dirname(os.path.abspath(nearfield_pont.__file__)))
        chemin = os.path.join(TMP, "bin", "pf-faux")
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "w") as f:
            f.write("#!%s\n" % sys.executable + "\n".join([
                "import base64, os, sys",
                "sys.path.insert(0, %r)" % site,
                "import nearfield_pont as n",
                "a = sys.argv",
                "if a[1] == 'disponible': sys.exit(0)",
                "if a[1] == 'creer':",
                "    if os.path.exists(a[2]): sys.exit(2)",
                "    print(n.Coffre.charger_ou_creer(a[2], 'p256').cle_publique()['pub'])",
                "elif a[1] == 'publique': print(n.Coffre.ouvrir(a[2]).cle_publique()['pub'])",
                "elif a[1] == 'signer':",
                "    if '--raison' in a: open(a[2] + '.raison', 'w').write(a[a.index('--raison') + 1])",
                "    print(base64.b64encode(n.Coffre.ouvrir(a[2]).signer(sys.stdin.buffer.read())).decode())",
                "else: sys.exit(64)", ""]))
        os.chmod(chemin, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        p = mock.patch.object(self.nb, "_programme_puce", lambda: chemin)
        p.start()
        self.addCleanup(p.stop)
        return chemin

    def test_13_identite_passe_dans_la_puce_puis_validation_humaine(self):
        nb, v2 = self.nb, self.pont_v2
        avant = dict(self.fiche()["v2"])
        self.assertEqual(avant["niveau"], "logiciel")
        self.fausse_puce()
        r = nb.humain_creer()
        self.assertEqual(r[0], "OK", r)
        self.connecte_en_v2()
        # Rotation : la fiche passe a la nouvelle identite, materielle ; le Mac efface sa cle logicielle.
        self.assertTrue(attendre(lambda: self.fiche()["v2"]["niveau"] == "materiel", 20), "rotation acceptee")
        self.assertNotEqual(self.fiche()["v2"]["id"], avant["id"])
        self.assertTrue(attendre(lambda: os.path.exists(os.path.join(nb.V2_DIR, "identite.puce"))
                                 and not os.path.exists(os.path.join(nb.V2_DIR, "identite.pem")), 10))
        # Reconnecte avec la nouvelle identite : l'ancienne est retiree de la fiche.
        self.assertTrue(attendre(lambda: "precedente" not in self.fiche()["v2"] and v2.en_v2(self.mac_id), 20),
                        "session avec la nouvelle identite")
        self.assertEqual(self.certificat_garde()["charge"]["niveau"], "materiel")
        self.assertIsNone(v2._machine_de(avant["id"])[1], "l'ancienne identite ne vaut plus rien")
        # Cle humaine annoncee au Pi.
        self.assertTrue(attendre(lambda: any(h["machine"] == self.mac_id for h in v2.humains().values()), 10))
        # Validation demandee par le Pi : dialogue accepte, signature par la cle humaine.
        brouillon = {"type": "ticket", "parc": v2.parc(), "id": "essai-1", "titre": "Essai", "plan": "Faire.",
                     "mode": "strict", "budget": {"secondes": 60, "cout_centimes": 0}, "capacites": ["whisper"],
                     "zones": ["src/"]}
        with mock.patch.object(nb, "_dialogue_validation", lambda texte: "Essai" in texte):
            validation = v2.demander_validation("ticket", brouillon, 20)
        hid, h = next(iter(v2.humains().items()))
        self.assertEqual(validation["signataire"], hid)
        self.assertEqual(nearfield_pont.verifier_validation(validation, h["identite"], v2.parc(), brouillon), "accepte")
        with open(nb.HUMAIN + ".raison") as f:
            self.assertIn("Essai", f.read())
        ticket = nearfield_pont.emettre_ticket(v2.coffre(), brouillon, validation, [h["identite"]])
        self.assertEqual(ticket["charge"]["titre"], "Essai")
        # Arbitrage d'un desaccord : le dossier est montre, l'utilisateur choisit, sa signature
        # couvre son choix.
        dossier = {"type": "arbitrage", "parc": v2.parc(), "id": "arbitrage-essai-1-0", "ticket": "essai-1",
                   "titre": "Essai", "desaccord": "Cache contesté.", "impact": "un fichier",
                   "points": [{"sujet": "cache", "redacteur": "plus rapide", "verificateur": "fuite mémoire"}]}
        vus = []

        def choisir(texte, options):
            vus.append(texte)
            return "troisieme_avis"
        with mock.patch.object(nb, "_choisir_arbitrage", choisir):
            arbitrage, choix = v2.demander_arbitrage(dossier, ("redacteur", "verificateur", "troisieme_avis"), 20)
        self.assertEqual(choix, "troisieme_avis")
        self.assertIn("fuite mémoire", vus[0])
        self.assertIn("Impact : un fichier", vus[0])
        # Dialogue refuse : pas de validation.
        with mock.patch.object(nb, "_dialogue_validation", lambda texte: False):
            with self.assertRaises(nearfield_pont.ErreurPont):
                v2.demander_validation("ticket", brouillon, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
