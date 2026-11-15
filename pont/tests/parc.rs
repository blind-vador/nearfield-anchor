//! Scénario complet : appairage, certificats, session Noise sur TCP, messages,
//! et les attaques que la v2 doit repousser.

use std::net::{TcpListener, TcpStream};
use std::thread;

use nearfield_pont::appairage::{Accueil, Rejoignant};
use nearfield_pont::certificat::{self, Demande, ListeRevocations, Role};
use nearfield_pont::cles::{Alg, Coffre, CoffreLogiciel, Niveau};
use nearfield_pont::messages::{Etat, Message};
use nearfield_pont::objet::ObjetSigne;
use nearfield_pont::transport::{initier, repondre, CleTransport, Contexte};
use nearfield_pont::{maintenant, Resultat};

struct Machine {
    coffre: CoffreLogiciel,
    transport: CleTransport,
    certificat: ObjetSigne,
}

fn certifier(ord: &CoffreLogiciel, parc: &str, coffre: &CoffreLogiciel, transport: &CleTransport, roles: &[Role]) -> ObjetSigne {
    certificat::emettre(
        ord,
        parc,
        Demande {
            nom: "machine",
            identite: coffre.cle_publique(),
            niveau: Niveau::Logiciel,
            transport: &transport.public,
            roles,
            capacites: &["whisper"],
            serie: 1,
        },
        true,
    )
    .unwrap()
}

fn machine(ord: &CoffreLogiciel, parc: &str, roles: &[Role]) -> Machine {
    let coffre = CoffreLogiciel::generer(Alg::P256);
    let transport = CleTransport::generer().unwrap();
    let certificat = certifier(ord, parc, &coffre, &transport, roles);
    Machine { coffre, transport, certificat }
}

/// Lance l'ordonnateur sur un port libre ; rend le port et le fil qui renvoie
/// ce que la session côté ordonnateur a reçu (ou l'erreur).
fn ordonnateur(
    ord_coffre: CoffreLogiciel,
    moi: Machine,
    parc: &'static str,
) -> (u16, thread::JoinHandle<Resultat<Vec<Message>>>) {
    let ecoute = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = ecoute.local_addr().unwrap().port();
    let fil = thread::spawn(move || {
        let (flux, _) = ecoute.accept().unwrap();
        let cle_ord = ord_coffre.cle_publique();
        let verifier = move |o: &ObjetSigne| {
            certificat::verifier(o, &cle_ord, parc, &ListeRevocations::default(), true, maintenant())
        };
        let ctx = Contexte { parc, cle: &moi.transport, mon_certificat: &moi.certificat, verifier_pair: &verifier };
        let mut session = repondre(flux, &ctx)?;
        let mut recus = vec![];
        while let Some(m) = session.recevoir()? {
            if matches!(m, Message::Battement { .. }) {
                session.envoyer(&Message::Dire { id: "r1".into(), texte: "Bien reçu.".into(), voix: None, debit: None })?;
            }
            recus.push(m);
        }
        Ok(recus)
    });
    (port, fil)
}

fn parc_de_test(parc: &'static str) -> (CoffreLogiciel, Machine, Machine) {
    let ord = CoffreLogiciel::generer(Alg::P256);
    let m_ord = machine(&ord, parc, &[Role::Ordonnateur]);
    let m = machine(&ord, parc, &[Role::Travailleur]);
    (ord, m_ord, m)
}

#[test]
fn appairage_puis_session_chiffree() {
    let parc = "parc-test";
    let (ord, m_ord, m) = parc_de_test(parc);

    // Appairage vocal : les deux machines obtiennent le même code.
    let (a, eng) = Rejoignant::nouveau(m.coffre.cle_publique(), &m.transport.public).unwrap();
    let (b, pres_b) = Accueil::nouveau(&eng, ord.cle_publique(), &m_ord.transport.public).unwrap();
    let (pres_a, code_a) = a.recevoir(&pres_b).unwrap();
    assert_eq!(code_a, b.verifier(&pres_a).unwrap());

    let cle_ord = ord.cle_publique();
    let (port, fil) = ordonnateur(ord, m_ord, parc);
    let verifier = move |o: &ObjetSigne| certificat::verifier(o, &cle_ord, parc, &ListeRevocations::default(), true, maintenant());
    let ctx = Contexte { parc, cle: &m.transport, mon_certificat: &m.certificat, verifier_pair: &verifier };
    let mut session = initier(TcpStream::connect(("127.0.0.1", port)).unwrap(), &ctx).unwrap();
    assert!(session.pair().roles.contains(&Role::Ordonnateur));

    session
        .envoyer(&Message::Battement { id: "b1".into(), etat: Etat::Libre, capacites: vec!["whisper".into()], puissance_libre: 80, voix: None, session: None, puissance: None, reseau: None })
        .unwrap();
    match session.recevoir().unwrap() {
        Some(Message::Dire { texte, .. }) => assert_eq!(texte, "Bien reçu."),
        autre => panic!("réponse inattendue : {autre:?}"),
    }
    drop(session);
    let recus = fil.join().unwrap().unwrap();
    assert_eq!(recus.len(), 1);
}

#[test]
fn refuse_une_machine_dont_le_certificat_ne_correspond_pas_a_sa_cle_noise() {
    let parc = "parc-test";
    let (ord, m_ord, mut m) = parc_de_test(parc);
    m.transport = CleTransport::generer().unwrap(); // clé volée ou remplacée : plus celle du certificat
    let cle_ord = ord.cle_publique();
    let (port, fil) = ordonnateur(ord, m_ord, parc);
    let verifier = move |o: &ObjetSigne| certificat::verifier(o, &cle_ord, parc, &ListeRevocations::default(), true, maintenant());
    let ctx = Contexte { parc, cle: &m.transport, mon_certificat: &m.certificat, verifier_pair: &verifier };
    let _ = initier(TcpStream::connect(("127.0.0.1", port)).unwrap(), &ctx);
    assert!(fil.join().unwrap().is_err(), "l'ordonnateur doit refuser");
}

#[test]
fn refuse_un_certificat_signe_par_un_autre_ordonnateur() {
    let parc = "parc-test";
    let (ord, m_ord, _) = parc_de_test(parc);
    let faux_ord = CoffreLogiciel::generer(Alg::P256);
    let intrus = machine(&faux_ord, parc, &[Role::Travailleur]);
    let cle_ord = ord.cle_publique();
    let (port, fil) = ordonnateur(ord, m_ord, parc);
    let verifier = move |o: &ObjetSigne| certificat::verifier(o, &cle_ord, parc, &ListeRevocations::default(), true, maintenant());
    let ctx = Contexte { parc, cle: &intrus.transport, mon_certificat: &intrus.certificat, verifier_pair: &verifier };
    let _ = initier(TcpStream::connect(("127.0.0.1", port)).unwrap(), &ctx);
    assert!(fil.join().unwrap().is_err());
}

#[test]
fn deux_parcs_differents_ne_se_parlent_pas() {
    let (ord, m_ord, m) = parc_de_test("parc-a");
    let cle_ord = ord.cle_publique();
    let (port, fil) = ordonnateur(ord, m_ord, "parc-a");
    // La machine croit être dans « parc-b » : le prologue Noise diffère.
    let verifier = move |o: &ObjetSigne| certificat::verifier(o, &cle_ord, "parc-a", &ListeRevocations::default(), true, maintenant());
    let ctx = Contexte { parc: "parc-b", cle: &m.transport, mon_certificat: &m.certificat, verifier_pair: &verifier };
    assert!(initier(TcpStream::connect(("127.0.0.1", port)).unwrap(), &ctx).is_err());
    assert!(fil.join().unwrap().is_err());
}

#[test]
fn session_separee_envoie_et_recoit_depuis_deux_fils() {
    let parc = "parc-test";
    let (ord, m_ord, m) = parc_de_test(parc);
    let cle_ord = ord.cle_publique();
    let (port, fil) = ordonnateur(ord, m_ord, parc);
    let verifier = move |o: &ObjetSigne| certificat::verifier(o, &cle_ord, parc, &ListeRevocations::default(), true, maintenant());
    let ctx = Contexte { parc, cle: &m.transport, mon_certificat: &m.certificat, verifier_pair: &verifier };
    let session = initier(TcpStream::connect(("127.0.0.1", port)).unwrap(), &ctx).unwrap();
    let (mut emetteur, mut recepteur) = session.separer().unwrap();
    // Trois battements, trois réponses : on les attend toutes avant de fermer.
    let ecoute = thread::spawn(move || (0..3).map(|_| recepteur.recevoir()).collect::<Vec<_>>());
    for i in 0..3 {
        let b = Message::Battement { id: format!("b{i}"), etat: Etat::Occupe, capacites: vec![], puissance_libre: 10, voix: None, session: None, puissance: None, reseau: None };
        emetteur.envoyer(&b).unwrap();
    }
    for reponse in ecoute.join().unwrap() {
        assert!(matches!(reponse.unwrap(), Some(Message::Dire { .. })));
    }
    emetteur.fermer();
    assert_eq!(fil.join().unwrap().unwrap().len(), 3);
}
