//! Transport chiffré : Noise XX authentifié par certificat (spec §5).
//!
//! Chaque côté présente son certificat dans la poignée de main ; l'autre vérifie
//! qu'il est signé par l'ordonnateur épinglé, valide, non révoqué, et que la clé de
//! transport certifiée est exactement la clé statique Noise reçue.

use std::io::{Read, Write};
use std::net::{Shutdown, TcpStream};
use std::sync::Arc;
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

    /// Garde la clé (32 octets bruts, fichier 0600) : la machine se reconnecte après un
    /// redémarrage avec le certificat qui la porte, sans repasser par la v1.
    pub fn enregistrer(&self, chemin: &std::path::Path) -> Resultat<()> {
        crate::ecrire_prive(chemin, &self.prive)
    }

    pub fn charger(chemin: &std::path::Path) -> Resultat<Self> {
        let prive = zeroize::Zeroizing::new(std::fs::read(chemin)?);
        let octets: [u8; 32] =
            prive.as_slice().try_into().map_err(|_| Erreur::Cle("clé de transport : 32 octets attendus".into()))?;
        let public = x25519_dalek::PublicKey::from(&x25519_dalek::StaticSecret::from(octets)).to_bytes().to_vec();
        Ok(CleTransport { prive: octets.to_vec(), public })
    }
}

impl Drop for CleTransport {
    fn drop(&mut self) {
        use zeroize::Zeroize;
        self.prive.zeroize();
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

    Ok(Session::nouvelle(flux, hs.into_stateless_transport_mode()?, pair))
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

    Ok(Session::nouvelle(flux, hs.into_stateless_transport_mode()?, pair))
}

/// Session établie : messages chiffrés, compteurs anti-rejeu de Noise.
///
/// Noise est utilisé en mode « sans état » avec nos propres compteurs : une session
/// TCP peut ainsi être séparée en une moitié qui envoie et une moitié qui reçoit,
/// utilisables depuis deux fils d'exécution (voir [`Session::separer`]).
pub struct Session<S> {
    flux: S,
    noise: Arc<snow::StatelessTransportState>,
    pair: Certificat,
    debut: Instant,
    envoi: u64,
    reception: u64,
}

fn encore_valide(debut: Instant) -> Resultat<()> {
    if debut.elapsed() > DUREE_SESSION {
        return Err(Erreur::Transport("session de plus de 24 h, à renouveler".into()));
    }
    Ok(())
}

fn suivant(compteur: &mut u64) -> Resultat<u64> {
    let n = *compteur;
    *compteur = n.checked_add(1).ok_or_else(|| Erreur::Transport("compteur épuisé".into()))?;
    Ok(n)
}

fn chiffrer_envoyer<S: Write>(
    flux: &mut S,
    noise: &snow::StatelessTransportState,
    compteur: &mut u64,
    debut: Instant,
    message: &Message,
) -> Resultat<()> {
    encore_valide(debut)?;
    let clair = message.encoder()?;
    if clair.len() > MAX_CLAIR {
        return Err(Erreur::Transport("message trop long : utiliser des morceaux".into()));
    }
    let mut tampon = vec![0u8; MAX_TRAME];
    let n = noise.write_message(suivant(compteur)?, &clair, &mut tampon)?;
    envoyer_trame(flux, &tampon[..n])
}

fn recevoir_dechiffrer<S: Read>(
    flux: &mut S,
    noise: &snow::StatelessTransportState,
    compteur: &mut u64,
    debut: Instant,
) -> Resultat<Option<Message>> {
    encore_valide(debut)?;
    let Some(chiffre) = lire_trame(flux)? else { return Ok(None) };
    let mut tampon = vec![0u8; MAX_TRAME];
    // Le compteur attendu est imposé : une trame rejouée, supprimée ou réordonnée
    // ne se déchiffre pas.
    let n = noise.read_message(suivant(compteur)?, &chiffre, &mut tampon)?;
    Message::decoder(&tampon[..n]).map(Some)
}

impl<S: Read + Write> Session<S> {
    fn nouvelle(flux: S, noise: snow::StatelessTransportState, pair: Certificat) -> Self {
        Session { flux, noise: Arc::new(noise), pair, debut: Instant::now(), envoi: 0, reception: 0 }
    }

    /// Le certificat vérifié de l'autre machine.
    pub fn pair(&self) -> &Certificat {
        &self.pair
    }

    pub fn envoyer(&mut self, message: &Message) -> Resultat<()> {
        chiffrer_envoyer(&mut self.flux, &self.noise, &mut self.envoi, self.debut, message)
    }

    /// Message suivant, ou `None` si l'autre a fermé la connexion.
    pub fn recevoir(&mut self) -> Resultat<Option<Message>> {
        recevoir_dechiffrer(&mut self.flux, &self.noise, &mut self.reception, self.debut)
    }
}

impl Session<TcpStream> {
    /// Sépare la session en deux moitiés indépendantes (envoi, réception).
    pub fn separer(self) -> Resultat<(Emetteur, Recepteur)> {
        let lecture = self.flux.try_clone()?;
        Ok((
            Emetteur { flux: self.flux, noise: self.noise.clone(), compteur: self.envoi, debut: self.debut },
            Recepteur { flux: lecture, noise: self.noise, compteur: self.reception, debut: self.debut },
        ))
    }
}

/// Moitié d'une session qui envoie.
pub struct Emetteur {
    flux: TcpStream,
    noise: Arc<snow::StatelessTransportState>,
    compteur: u64,
    debut: Instant,
}

impl Emetteur {
    pub fn envoyer(&mut self, message: &Message) -> Resultat<()> {
        chiffrer_envoyer(&mut self.flux, &self.noise, &mut self.compteur, self.debut, message)
    }

    /// Ferme la connexion dans les deux sens.
    pub fn fermer(&self) {
        let _ = self.flux.shutdown(Shutdown::Both);
    }
}

/// Moitié d'une session qui reçoit.
pub struct Recepteur {
    flux: TcpStream,
    noise: Arc<snow::StatelessTransportState>,
    compteur: u64,
    debut: Instant,
}

impl Recepteur {
    /// Message suivant, ou `None` si l'autre a fermé la connexion.
    pub fn recevoir(&mut self) -> Resultat<Option<Message>> {
        recevoir_dechiffrer(&mut self.flux, &self.noise, &mut self.compteur, self.debut)
    }
}
