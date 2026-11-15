"""Tests de la liaison Python : le même scénario que les tests Rust, vu de Python.

Lancer : `maturin develop` puis `python -m pytest tests`.
"""

import base64
import json
import os
import threading
import time
from pathlib import Path

import pytest

import nearfield_pont as np_

VECTEURS = json.loads((Path(__file__).resolve().parents[2] / "vecteurs.json").read_text())
PARC = "parc-test"


# ------------------------------------------------------------------ clés et objets

def test_vecteurs_publies():
    v = VECTEURS
    cle = {"alg": "ed25519", "pub": base64.b64encode(bytes.fromhex(v["identifiant"]["entree"]["pub_hex"])).decode()}
    assert np_.identifiant(cle) == v["identifiant"]["attendu"]
    assert np_.canonique(v["jcs"]["entree"]).decode() == v["jcs"]["attendu"]


def test_canonique_refuse_les_flottants():
    with pytest.raises(np_.ErreurFormat):
        np_.canonique({"x": 1.5})


@pytest.mark.parametrize("alg", ["ed25519", "p256"])
def test_signe_et_verifie(alg):
    c = np_.Coffre.generer(alg)
    assert c.niveau == "logiciel" and len(c.id) == 16
    sig = c.signer(b"bonjour")
    np_.verifier_signature_brute(c.cle_publique(), b"bonjour", sig)
    with pytest.raises(np_.SignatureInvalide):
        np_.verifier_signature_brute(c.cle_publique(), b"bonsoir", sig)


def test_la_cle_privee_ne_sort_pas():
    c = np_.Coffre.generer()
    assert not [n for n in dir(c) if "pem" in n or "prive" in n or "secret" in n]
    t = np_.CleTransport()
    assert len(t.public) == 32
    assert not [n for n in dir(t) if "prive" in n or "secret" in n]


def test_fichier_de_cle_0600_et_relu(tmp_path):
    chemin = tmp_path / "cles" / "identite.pem"
    a = np_.Coffre.charger_ou_creer(chemin, "ed25519")
    b = np_.Coffre.charger_ou_creer(str(chemin), "p256")
    assert a.cle_publique() == b.cle_publique()
    assert a.cle_publique()["alg"] == "ed25519"
    assert os.stat(chemin).st_mode & 0o777 == 0o600


def objet_travail(c, **autres):
    return c.signer_objet({"type": "travail", "parc": PARC, "id": "t1", "genre": "whisper", **autres})


def test_objet_signe_rejeu_et_alteration():
    c = np_.Coffre.generer()
    o = objet_travail(c)
    r = np_.RegistreNonces()
    charge = np_.verifier_objet(o, c.cle_publique(), "travail", PARC, r)
    assert charge["genre"] == "whisper" and charge["v"] == np_.VERSION
    with pytest.raises(np_.Rejeu):
        np_.verifier_objet(o, c.cle_publique(), "travail", PARC, r)
    o["charge"]["genre"] = "shell"
    with pytest.raises(np_.SignatureInvalide):
        np_.verifier_signature(o, c.cle_publique())


def test_objet_refuse_autre_signataire_parc_type_et_champ_en_trop():
    c, intrus = np_.Coffre.generer(), np_.Coffre.generer()
    o = objet_travail(c)
    with pytest.raises(np_.SignataireInconnu):
        np_.verifier_objet(o, intrus.cle_publique(), "travail", PARC)
    with pytest.raises(np_.ErreurFormat):
        np_.verifier_objet(o, c.cle_publique(), "travail", "autre-parc")
    with pytest.raises(np_.ErreurFormat):
        np_.verifier_objet(o, c.cle_publique(), "resultat", PARC)
    with pytest.raises(np_.ErreurFormat):
        np_.verifier_objet({**o, "commande": "rm -rf /"}, c.cle_publique(), "travail", PARC)


def test_objet_perime():
    c = np_.Coffre.generer()
    o = objet_travail(c, emis=int(time.time()) - 8 * 24 * 3600)
    with pytest.raises(np_.DateInvalide):
        np_.verifier_objet(o, c.cle_publique(), "travail", PARC)


def test_registre_survit_au_redemarrage(tmp_path):
    c = np_.Coffre.generer()
    o = objet_travail(c)
    r = np_.RegistreNonces()
    np_.verifier_objet(o, c.cle_publique(), "travail", PARC, r)
    r.enregistrer(tmp_path / "nonces.json")
    relu = np_.RegistreNonces.charger(tmp_path / "nonces.json")
    with pytest.raises(np_.Rejeu):
        np_.verifier_objet(o, c.cle_publique(), "travail", PARC, relu)


def test_fichiers_joints_et_noms(tmp_path):
    (tmp_path / "son.wav").write_bytes(b"abc")
    empreinte = np_.empreinte_fichier(tmp_path / "son.wav")
    c = np_.Coffre.generer()
    o = c.signer_objet({"type": "resultat", "parc": PARC, "id": "r1", "fichiers": {"son.wav": empreinte}})
    np_.verifier_fichiers(o, tmp_path)
    (tmp_path / "son.wav").write_bytes(b"abd")
    with pytest.raises(np_.EmpreinteDifferente):
        np_.verifier_fichiers(o, tmp_path)
    for mauvais in ["../x", "a/b", ".cache", "", "c:d"]:
        with pytest.raises(np_.CheminRefuse):
            np_.nom_sur(mauvais)


def test_toutes_les_erreurs_heritent_de_erreur_pont():
    for nom in ["ErreurFormat", "SignatureInvalide", "Rejeu", "CertificatRefuse", "ErreurTransport", "MessageRefuse"]:
        assert issubclass(getattr(np_, nom), np_.ErreurPont)


# ------------------------------------------------------------------ appairage

def test_appairage_meme_code_des_deux_cotes():
    a, b = np_.Coffre.generer(), np_.Coffre.generer()
    ta, tb = np_.CleTransport(), np_.CleTransport()
    rejoignant = np_.Rejoignant(a.cle_publique(), ta.public)
    accueil = np_.Accueil(rejoignant.engagement, b.cle_publique(), tb.public)
    pres_a, code_a = rejoignant.recevoir(accueil.presentation)
    assert len(code_a) == 6 and code_a == accueil.verifier(pres_a)
    assert np_.code_parle(code_a) == f"{code_a[:2]}, {code_a[2:4]}, {code_a[4:]}"
    with pytest.raises(np_.AppairageRefuse):
        rejoignant.recevoir(accueil.presentation)  # usage unique


def test_appairage_refuse_une_presentation_changee_apres_engagement():
    a, b, m = np_.Coffre.generer(), np_.Coffre.generer(), np_.Coffre.generer()
    rejoignant = np_.Rejoignant(a.cle_publique(), np_.CleTransport().public)
    accueil = np_.Accueil(rejoignant.engagement, b.cle_publique(), np_.CleTransport().public)
    pres_a, _ = rejoignant.recevoir(accueil.presentation)
    pres_a["identite"] = m.cle_publique()  # un intermédiaire substitue sa clé
    with pytest.raises(np_.AppairageRefuse):
        accueil.verifier(pres_a)


# ------------------------------------------------------------------ certificats

class Parc:
    def __init__(self, nom=PARC):
        self.nom = nom
        self.ord = np_.Coffre.generer()
        self.cle_ord = self.ord.cle_publique()
        self.revocations = np_.Revocations()
        self.serie = 0

    def machine(self, roles, nom="machine"):
        coffre, transport = np_.Coffre.generer(), np_.CleTransport()
        self.serie += 1
        cert = np_.emettre_certificat(self.ord, self.nom, nom, coffre.cle_publique(), "logiciel", transport.public,
                                      roles, ["whisper"], self.serie, tolerance_logiciel=True)
        return coffre, transport, cert


def test_certificat_valide_puis_revoque():
    p = Parc()
    coffre, _, cert = p.machine(["travailleur"], "mac-mini")
    contenu = np_.verifier_certificat(cert, p.cle_ord, PARC, p.revocations, tolerance_logiciel=True)
    assert contenu["nom"] == "mac-mini" and contenu["sujet"] == coffre.id
    liste = p.revocations.publier(p.ord, PARC, [coffre.id])
    recue = np_.Revocations()
    recue.recevoir(liste, p.cle_ord, PARC)
    assert recue.numero == 1 and recue.est_revoque(coffre.id)
    with pytest.raises(np_.CertificatRefuse):
        np_.verifier_certificat(cert, p.cle_ord, PARC, recue, tolerance_logiciel=True)


def test_liste_de_revocation_ne_recule_pas():
    p = Parc()
    ancienne = p.revocations.publier(p.ord, PARC, [])
    p.revocations.publier(p.ord, PARC, ["x"])
    recue = np_.Revocations()
    recue.recevoir(p.revocations.publier(p.ord, PARC, ["x", "y"]), p.cle_ord, PARC)
    with pytest.raises(np_.CertificatRefuse):
        recue.recevoir(ancienne, p.cle_ord, PARC)


def test_ordonnateur_logiciel_refuse_sans_tolerance():
    p = Parc()
    with pytest.raises(np_.CertificatRefuse):
        np_.emettre_certificat(p.ord, PARC, "ord", p.cle_ord, "logiciel", np_.CleTransport().public,
                               ["ordonnateur"], [], 1)


def test_certificat_expire():
    p = Parc()
    _, _, cert = p.machine(["travailleur"])
    with pytest.raises(np_.CertificatRefuse):
        np_.verifier_certificat(cert, p.cle_ord, PARC, p.revocations, True, instant=int(time.time()) + 13 * 3600)


# ------------------------------------------------------------------ messages

def test_messages_liste_fermee():
    m = {"type": "dire", "id": "d1", "texte": "Bonjour."}
    assert np_.decoder_message(np_.encoder_message(m)) == m
    for refuse in [
        {"type": "executer", "id": "x", "commande": "rm -rf /"},
        {"type": "dire", "id": "d1", "texte": "a", "commande": "ls"},
        {"type": "dire", "id": "d1", "texte": "a" * 5000},
        {"type": "travail_annonce", "id": "t", "travail": "../../etc/passwd"},
    ]:
        with pytest.raises((np_.MessageRefuse, np_.CheminRefuse)):
            np_.encoder_message(refuse)
    with pytest.raises(np_.MessageRefuse):
        np_.decoder_message(b'{"type":"executer","id":"x"}')


# ------------------------------------------------------------------ transport

def lancer_ordonnateur(p, nb_connexions=1, **options):
    """Ordonnateur sur un port libre ; rend (port, fil, résultats)."""
    _, transport, cert = p.machine(["ordonnateur"], "ordonnateur")
    ecouteur = np_.Ecouteur("127.0.0.1:0")
    resultats = []

    def servir():
        for _ in range(nb_connexions):
            entrante = ecouteur.accepter()
            try:
                s = entrante.repondre(p.nom, transport, cert, p.cle_ord, p.revocations, True, **options)
            except np_.ErreurPont as e:
                resultats.append(e)
                continue
            recus = []
            while (m := s.recevoir()) is not None:
                recus.append(m)
                if m["type"] == "battement":
                    s.envoyer({"type": "dire", "id": "r1", "texte": "Bien reçu."})
            resultats.append((s.pair, recus))

    fil = threading.Thread(target=servir, daemon=True)
    fil.start()
    return ecouteur.port, fil, resultats


BATTEMENT = {"type": "battement", "id": "b1", "etat": "libre", "capacites": ["whisper"], "puissance_libre": 80}


def test_session_chiffree_de_bout_en_bout():
    p = Parc()
    port, fil, resultats = lancer_ordonnateur(p)
    _, transport, cert = p.machine(["travailleur"], "mac-mini")
    s = np_.connecter(f"127.0.0.1:{port}", PARC, transport, cert, p.cle_ord, p.revocations, True)
    assert "ordonnateur" in s.pair["roles"]
    s.envoyer(BATTEMENT)
    assert s.recevoir()["texte"] == "Bien reçu."
    s.fermer()
    fil.join(5)
    pair, recus = resultats[0]
    assert pair["nom"] == "mac-mini" and recus == [BATTEMENT]


def test_envoi_et_reception_depuis_deux_fils_et_fermer_debloque():
    p = Parc()
    port, fil, resultats = lancer_ordonnateur(p)
    _, transport, cert = p.machine(["travailleur"])
    s = np_.connecter(f"127.0.0.1:{port}", PARC, transport, cert, p.cle_ord, p.revocations, True)
    recus = []
    lecteur = threading.Thread(target=lambda: recus.extend(iter(s.recevoir, None)), daemon=True)
    lecteur.start()
    for i in range(5):
        s.envoyer({**BATTEMENT, "id": f"b{i}"})
    attente = time.time() + 5
    while len(recus) < 5 and time.time() < attente:
        time.sleep(0.02)
    s.fermer()  # débloque le lecteur
    lecteur.join(5)
    assert not lecteur.is_alive() and len(recus) == 5
    fil.join(5)
    assert len(resultats[0][1]) == 5


def test_session_silencieuse_plus_longue_que_le_delai_de_poignee():
    # 2026-11-15 : le délai de la poignée de main restait sur la lecture, la
    # session tombait après delai_sec sans trafic (battements toutes les 30 s).
    p = Parc()
    port, fil, resultats = lancer_ordonnateur(p, delai_sec=0.5)
    _, transport, cert = p.machine(["travailleur"])
    s = np_.connecter(f"127.0.0.1:{port}", PARC, transport, cert, p.cle_ord, p.revocations, True, delai_sec=0.5)
    time.sleep(1.5)
    s.envoyer(BATTEMENT)
    assert s.recevoir()["texte"] == "Bien reçu."
    s.fermer()
    fil.join(5)
    assert resultats[0][1] == [BATTEMENT]


def test_le_travailleur_refuse_un_pair_qui_n_est_pas_ordonnateur():
    p = Parc()
    _, t_faux, c_faux = p.machine(["travailleur"], "faux")
    ecouteur = np_.Ecouteur("127.0.0.1:0")
    fil = threading.Thread(
        target=lambda: pytest.raises(np_.ErreurPont, ecouteur.accepter().repondre, PARC, t_faux, c_faux,
                                     p.cle_ord, p.revocations, True), daemon=True)
    fil.start()
    _, transport, cert = p.machine(["travailleur"])
    with pytest.raises(np_.CertificatRefuse):
        np_.connecter(f"127.0.0.1:{ecouteur.port}", PARC, transport, cert, p.cle_ord, p.revocations, True)
    fil.join(5)


def tentative_refusee(port, transport, cert, p):
    """Avec Noise XX, l'initiateur envoie son certificat dans le dernier message :
    c'est l'ordonnateur qui refuse et ferme ; côté machine, la connexion est morte."""
    try:
        s = np_.connecter(f"127.0.0.1:{port}", PARC, transport, cert, p.cle_ord, p.revocations, True)
    except np_.ErreurPont:
        return
    try:
        assert s.recevoir() is None
    except np_.ErreurPont:
        pass


def test_l_ordonnateur_refuse_une_cle_de_transport_volee_et_un_autre_ordonnateur():
    p = Parc()
    port, fil, resultats = lancer_ordonnateur(p, nb_connexions=2)
    _, _, cert = p.machine(["travailleur"])
    tentative_refusee(port, np_.CleTransport(), cert, p)  # clé Noise qui n'est pas celle du certificat
    autre = Parc()
    _, t_intrus, c_intrus = autre.machine(["travailleur"])
    tentative_refusee(port, t_intrus, c_intrus, p)  # certificat signé par un autre ordonnateur
    fil.join(5)
    assert len(resultats) == 2 and all(isinstance(r, np_.ErreurPont) for r in resultats)
    assert "correspond pas" in str(resultats[0]) and "ordonnateur" in str(resultats[1])


def test_pair_muet_ne_bloque_pas_la_poignee_de_main():
    import socket
    ecoute = socket.socket()
    ecoute.bind(("127.0.0.1", 0))
    ecoute.listen()
    p = Parc()
    _, transport, cert = p.machine(["travailleur"])
    debut = time.time()
    with pytest.raises(np_.ErreurPont):
        np_.connecter(f"127.0.0.1:{ecoute.getsockname()[1]}", PARC, transport, cert, p.cle_ord, p.revocations,
                      True, delai_sec=0.5)
    assert time.time() - debut < 3
    ecoute.close()


# ------------------------------------------------------------------ migration v1 → v2

def charger_v1():
    """Le module du pont v1, tel qu'il tourne aujourd'hui (bibliothèque cryptography)."""
    pytest.importorskip("cryptography")
    import importlib.util
    chemin = Path(__file__).resolve().parents[3] / "bridge" / "pont_protocole.py"
    spec = importlib.util.spec_from_file_location("pont_protocole_v1", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_complete_depuis_de_vraies_cles_v1(tmp_path):
    import socket
    v1 = charger_v1()
    # Parc v1 existant : chaque côté a sa cle.pem et connaît la clé publique de l'autre.
    cle_pi = v1.load_or_create_key(str(tmp_path / "pi" / "cle.pem"))
    cle_mac = v1.load_or_create_key(str(tmp_path / "mac" / "cle.pem"))
    fiche_pi_vue_du_mac = {"alg": "ed25519", "pub": v1.public_b64(cle_pi)}
    fiche_mac_vue_du_pi = {"alg": "ed25519", "pub": v1.public_b64(cle_mac)}
    a, b = socket.socketpair()
    canal_pi, canal_mac = v1.Channel(a, cle_pi), v1.Channel(b, cle_mac)

    # 1. L'ordonnateur annonce sa clé v2, contresignée par sa clé v1, sur le canal v1.
    ancien_pi = np_.Coffre.ouvrir(tmp_path / "pi" / "cle.pem")
    assert ancien_pi.cle_publique() == fiche_pi_vue_du_mac  # la v2 relit bien la clé v1
    ord_v2 = np_.Coffre.generer("p256")
    parc = ord_v2.id
    canal_pi.send({"type": "migration", "objet": np_.preparer_migration(ancien_pi, ord_v2, parc, "pi-ordonnateur")})

    # 2. La machine vérifie avec la clé v1 du Pi qu'elle connaît, et épingle la v2.
    recu = canal_mac.receive(fiche_pi_vue_du_mac["pub"])
    m_ord = np_.verifier_migration(recu["objet"], recu["objet"]["charge"]["parc"], fiche_pi_vue_du_mac)
    assert np_.identifiant(m_ord["identite"]) == recu["objet"]["charge"]["parc"]  # parc = id de l'ordonnateur
    cle_ord_epinglee = m_ord["identite"]

    # 3. La machine annonce à son tour sa clé v2 et sa clé de transport.
    mac_v2, transport_mac = np_.Coffre.generer("p256"), np_.CleTransport()
    ancien_mac = np_.Coffre.ouvrir(tmp_path / "mac" / "cle.pem")
    canal_mac.send({"type": "migration",
                    "objet": np_.preparer_migration(ancien_mac, mac_v2, parc, "Mac mini", transport_mac.public)})

    # 4. L'ordonnateur retrouve la fiche v1, vérifie, consigne, délivre le certificat.
    recu = canal_pi.receive(fiche_mac_vue_du_pi["pub"])
    assert np_.ancienne_annoncee(recu["objet"]) == fiche_mac_vue_du_pi
    registre, migrations = np_.RegistreNonces(), np_.RegistreMigrations()
    m = np_.verifier_migration(recu["objet"], parc, fiche_mac_vue_du_pi, registre)
    migrations.consigner(m)
    assert migrations.migree_vers(fiche_mac_vue_du_pi) == mac_v2.id
    cert_mac = np_.emettre_certificat(ord_v2, parc, m["nom"], m["identite"], m["niveau"],
                                      base64.b64decode(m["transport"]), ["travailleur"], ["whisper"], 1, True)
    canal_pi.send({"type": "certificat", "objet": cert_mac})
    cert_recu = canal_mac.receive(fiche_pi_vue_du_mac["pub"])["objet"]
    canal_pi.close()
    canal_mac.close()

    # 5. Bascule sur le port v2 : session Noise avec la clé de l'ordonnateur épinglée.
    t_ord = np_.CleTransport()
    cert_ord = np_.emettre_certificat(ord_v2, parc, "pi-ordonnateur", cle_ord_epinglee, "logiciel", t_ord.public,
                                      ["ordonnateur"], [], 2, True)
    ecouteur = np_.Ecouteur("127.0.0.1:0")
    recus = []

    def servir():
        s = ecouteur.accepter().repondre(parc, t_ord, cert_ord, ord_v2.cle_publique(), np_.Revocations(), True)
        recus.append((s.pair["nom"], s.recevoir()))
        s.fermer()

    fil = threading.Thread(target=servir, daemon=True)
    fil.start()
    s = np_.connecter(f"127.0.0.1:{ecouteur.port}", parc, transport_mac, cert_recu, cle_ord_epinglee,
                      np_.Revocations(), True)
    s.envoyer(BATTEMENT)
    fil.join(5)
    assert recus == [("Mac mini", BATTEMENT)]
    assert s.recevoir() is None


def test_migration_refusee_si_la_cle_v1_ne_correspond_pas_ou_a_deja_migre(tmp_path):
    v1 = np_.Coffre.charger_ou_creer(tmp_path / "cle.pem", "ed25519")
    autre_v1 = np_.Coffre.generer("ed25519")
    v2, vole = np_.Coffre.generer(), np_.Coffre.generer()
    o = np_.preparer_migration(v1, v2, PARC, "Mac mini")
    with pytest.raises(np_.SignataireInconnu):
        np_.verifier_migration(o, PARC, autre_v1.cle_publique())
    migrations = np_.RegistreMigrations()
    migrations.consigner(np_.verifier_migration(o, PARC, v1.cle_publique()))
    migrations.enregistrer(tmp_path / "migrations.json")
    relu = np_.RegistreMigrations.charger(tmp_path / "migrations.json")
    m2 = np_.verifier_migration(np_.preparer_migration(v1, vole, PARC, "Mac mini"), PARC, v1.cle_publique())
    with pytest.raises(np_.MigrationRefusee):
        relu.consigner(m2)


def test_migration_detournee_par_un_intrus(tmp_path):
    v1, v2, intrus = np_.Coffre.generer("ed25519"), np_.Coffre.generer(), np_.Coffre.generer()
    o = np_.preparer_migration(v1, v2, PARC, "Mac mini")
    charge = dict(o["charge"], identite=intrus.cle_publique(), id=f"migration-{intrus.id}")
    with pytest.raises(np_.MigrationRefusee):
        np_.verifier_migration(intrus.signer_objet(charge), PARC, v1.cle_publique())


def test_ouvrir_ne_cree_jamais_de_cle(tmp_path):
    with pytest.raises(np_.ErreurEntreeSortie):
        np_.Coffre.ouvrir(tmp_path / "absente.pem")
    assert not (tmp_path / "absente.pem").exists()


# --- enveloppe chiffree (spec §6.2) -----------------------------------------------

def test_enveloppe_aller_retour_et_refus(tmp_path):
    identite_pi = np_.Coffre.generer("p256")
    mac = np_.CleReception.charger_ou_creer(tmp_path / "reception.cle")
    assert (tmp_path / "reception.cle").stat().st_mode & 0o777 == 0o600
    ryzen = np_.CleReception.generer()
    identite_mac = np_.Coffre.generer("p256")
    publication = mac.publier(identite_mac, "parc1")
    assert np_.verifier_cle_reception(publication, identite_mac.cle_publique(), "parc1") == mac.public
    with pytest.raises(np_.ErreurPont):
        np_.verifier_cle_reception(publication, identite_pi.cle_publique(), "parc1")

    dest = [("mac", mac.public), ("ryzen", ryzen.public)]
    env = np_.sceller(dest, identite_pi.id, "ticket/42", b"code secret")
    assert "secret" not in json.dumps(env)
    # L'enveloppe voyage dans un objet signe par l'expediteur.
    objet = identite_pi.signer_objet({"type": "ticket", "parc": "parc1", "id": "t42", "enveloppe": env})
    charge = np_.verifier_objet(objet, identite_pi.cle_publique(), "ticket", "parc1")
    assert np_.ouvrir(charge["enveloppe"], "mac", objet["signataire"], [mac]) == b"code secret"
    assert np_.ouvrir(env, "ryzen", identite_pi.id, [mac, ryzen]) == b"code secret"
    with pytest.raises(np_.DechiffrementImpossible):
        np_.ouvrir(env, "mac", identite_pi.id, [ryzen])
    with pytest.raises(np_.DechiffrementImpossible):
        np_.ouvrir(env, "mac", "un-autre-signataire", [mac])
    env2 = dict(env, contexte="ticket/43")
    with pytest.raises(np_.DechiffrementImpossible):
        np_.ouvrir(env2, "mac", identite_pi.id, [mac])
    with pytest.raises(np_.ErreurFormat):
        np_.ouvrir(dict(env, inconnu=1), "mac", identite_pi.id, [mac])


def test_enveloppe_fichier(tmp_path):
    mac = np_.CleReception.generer()
    clair = os.urandom(200_000)
    (tmp_path / "iq.bin").write_bytes(clair)
    entete = np_.sceller_fichier([("mac", mac.public)], "pi", "travail/1", tmp_path / "iq.bin", tmp_path / "iq.scelle")
    assert "chiffre" not in entete
    np_.ouvrir_fichier(entete, "mac", "pi", [mac], tmp_path / "iq.scelle", tmp_path / "iq.ouvert")
    assert (tmp_path / "iq.ouvert").read_bytes() == clair
    abime = bytearray((tmp_path / "iq.scelle").read_bytes())
    abime[70_000] ^= 1
    (tmp_path / "iq.scelle").write_bytes(bytes(abime))
    with pytest.raises(np_.DechiffrementImpossible):
        np_.ouvrir_fichier(entete, "mac", "pi", [mac], tmp_path / "iq.scelle", tmp_path / "iq.mauvais")
    assert not (tmp_path / "iq.mauvais").exists()
