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
