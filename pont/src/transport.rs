//! Transport chiffré : Noise XX authentifié par certificat (spec §5).
//!
//! Chaque côté présente son certificat dans la poignée de main ; l'autre vérifie
//! qu'il est signé par l'ordonnateur épinglé, valide, non révoqué, et que la clé de
//! transport certifiée est exactement la clé statique Noise reçue.

use std::io::{Read, Write};
use std::time::{Duration, Instant};

use crate::certificat::Certificat;
use crate::messages::Message;
use crate::objet::ObjetSigne;
use crate::{de_b64, Erreur, Resultat};

pub const MOTIF: &str = "Noise_XX_25519_ChaChaPoly_SHA256";
pub const PORT: u16 = 47812;
/// Une trame Noise ne dépasse jamais 65 535 octets ; 16 sont pris par l'étiquette.
pub const MAX_TRAME: usize = 65_535;
pub const MAX_CLAIR: usize = MAX_TRAME - 16;
/// Au-delà, il faut refaire une poignée de main.
pub const DUREE_SESSION: Duration = Duration::from_secs(24 * 3600);

fn parametres() -> snow::params::NoiseParams {
    MOTIF.parse().expect("motif Noise valide")
}

fn prologue(parc: &str) -> Vec<u8> {
    let mut p = b"nearfield-pont/2".to_vec();
    p.extend_from_slice(parc.as_bytes());
    p
}

/// Clé X25519 de transport, renouvelée avec chaque certificat.
pub struct CleTransport {
    prive: Vec<u8>,
    pub public: Vec<u8>,
}

impl CleTransport {
    pub fn generer() -> Resultat<Self> {
        let k = snow::Builder::new(parametres()).generate_keypair()?;
        Ok(CleTransport { prive: k.private, public: k.public })
    }
}

/// Ce dont chaque côté a besoin pour la poignée de main.
pub struct Contexte<'a> {
    pub parc: &'a str,
    pub cle: &'a CleTransport,
    pub mon_certificat: &'a ObjetSigne,
    /// Vérifie le certificat de l'autre (signature de l'ordonnateur, dates,
    /// révocation, rôle attendu) et rend son contenu.
    pub verifier_pair: &'a dyn Fn(&ObjetSigne) -> Resultat<Certificat>,
}

fn envoyer_trame<S: Write>(flux: &mut S, octets: &[u8]) -> Resultat<()> {
    if octets.len() > MAX_TRAME {
        return Err(Erreur::Transport("trame trop longue".into()));
    }
    flux.write_all(&(octets.len() as u16).to_be_bytes())?;
    flux.write_all(octets)?;
    flux.flush()?;
    Ok(())
}

/// `None` si l'autre a fermé proprement la connexion.
fn lire_trame<S: Read>(flux: &mut S) -> Resultat<Option<Vec<u8>>> {
    let mut taille = [0u8; 2];
    match flux.read_exact(&mut taille) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e.into()),
    }
    let mut octets = vec![0u8; u16::from_be_bytes(taille) as usize];
    flux.read_exact(&mut octets)?;
    Ok(Some(octets))
}

fn lire_trame_obligatoire<S: Read>(flux: &mut S) -> Resultat<Vec<u8>> {
    lire_trame(flux)?.ok_or_else(|| Erreur::Transport("connexion fermée pendant la poignée de main".into()))
}

fn controler_pair(charge: &[u8], hs: &snow::HandshakeState, ctx: &Contexte) -> Resultat<Certificat> {
    let objet: ObjetSigne = serde_json::from_slice(charge).map_err(|_| Erreur::Transport("certificat illisible".into()))?;
    let cert = (ctx.verifier_pair)(&objet)?;
    let statique = hs.get_remote_static().ok_or_else(|| Erreur::Transport("pas de clé statique".into()))?;
    if de_b64(&cert.transport)? != statique {
        return Err(Erreur::Transport("la clé Noise ne correspond pas au certificat".into()));
    }
    Ok(cert)
}

/// Côté machine qui se connecte (initiateur).
pub fn initier<S: Read + Write>(mut flux: S, ctx: &Contexte) -> Resultat<Session<S>> {
    let mut hs = snow::Builder::new(parametres())
        .local_private_key(&ctx.cle.prive)
        .prologue(&prologue(ctx.parc))
        .build_initiator()?;
    let mut tampon = vec![0u8; MAX_TRAME];
    let mut clair = vec![0u8; MAX_TRAME];

    let n = hs.write_message(&[], &mut tampon)?; // -> e
    envoyer_trame(&mut flux, &tampon[..n])?;

    let m2 = lire_trame_obligatoire(&mut flux)?; // <- e, ee, s, es + certificat
    let n = hs.read_message(&m2, &mut clair)?;
    let pair = controler_pair(&clair[..n], &hs, ctx)?;

    let charge = serde_json::to_vec(ctx.mon_certificat)?; // -> s, se + certificat
    let n = hs.write_message(&charge, &mut tampon)?;
    envoyer_trame(&mut flux, &tampon[..n])?;

    Ok(Session::nouvelle(flux, hs.into_transport_mode()?, pair))
}

/// Côté ordonnateur qui accepte une connexion (répondeur).
pub fn repondre<S: Read + Write>(mut flux: S, ctx: &Contexte) -> Resultat<Session<S>> {
    let mut hs = snow::Builder::new(parametres())
        .local_private_key(&ctx.cle.prive)
        .prologue(&prologue(ctx.parc))
        .build_responder()?;
    let mut tampon = vec![0u8; MAX_TRAME];
    let mut clair = vec![0u8; MAX_TRAME];

    let m1 = lire_trame_obligatoire(&mut flux)?;
    hs.read_message(&m1, &mut clair)?;

    let charge = serde_json::to_vec(ctx.mon_certificat)?;
    let n = hs.write_message(&charge, &mut tampon)?;
    envoyer_trame(&mut flux, &tampon[..n])?;

    let m3 = lire_trame_obligatoire(&mut flux)?;
    let n = hs.read_message(&m3, &mut clair)?;
    let pair = controler_pair(&clair[..n], &hs, ctx)?;

    Ok(Session::nouvelle(flux, hs.into_transport_mode()?, pair))
}

/// Session établie : messages chiffrés, compteurs anti-rejeu de Noise.
pub struct Session<S> {
    flux: S,
    noise: snow::TransportState,
    pair: Certificat,
    debut: Instant,
    tampon: Vec<u8>,
}

impl<S: Read + Write> Session<S> {
    fn nouvelle(flux: S, noise: snow::TransportState, pair: Certificat) -> Self {
        Session { flux, noise, pair, debut: Instant::now(), tampon: vec![0u8; MAX_TRAME] }
    }

    /// Le certificat vérifié de l'autre machine.
    pub fn pair(&self) -> &Certificat {
        &self.pair
    }

    fn encore_valide(&self) -> Resultat<()> {
        if self.debut.elapsed() > DUREE_SESSION {
            return Err(Erreur::Transport("session de plus de 24 h, à renouveler".into()));
        }
        Ok(())
    }

    pub fn envoyer(&mut self, message: &Message) -> Resultat<()> {
        self.encore_valide()?;
        let clair = message.encoder()?;
        if clair.len() > MAX_CLAIR {
            return Err(Erreur::Transport("message trop long : utiliser des morceaux".into()));
        }
        let n = self.noise.write_message(&clair, &mut self.tampon)?;
        envoyer_trame(&mut self.flux, &self.tampon[..n])
    }

    /// Message suivant, ou `None` si l'autre a fermé la connexion.
    pub fn recevoir(&mut self) -> Resultat<Option<Message>> {
        self.encore_valide()?;
        let Some(chiffre) = lire_trame(&mut self.flux)? else { return Ok(None) };
        let n = self.noise.read_message(&chiffre, &mut self.tampon)?;
        Message::decoder(&self.tampon[..n]).map(Some)
    }
}
