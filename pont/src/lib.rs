//! Protocole de confiance v2 de nearfield.
//!
//! Spécification : `Prismaforge/specs/protocole-v2.md`. Ce crate est la seule
//! implémentation de la cryptographie du parc ; nearfield (Python) l'utilise par
//! une liaison, le cœur de l'ordonnateur Prismaforge (Rust) directement.
//!
//! - [`jcs`]         : forme canonique JSON (RFC 8785, entiers seulement).
//! - [`cles`]        : clés d'identité, coffres, identifiants de machine.
//! - [`objet`]       : objets signés déposés sur un stockage partagé, anti-rejeu.
//! - [`enveloppe`]   : chiffrement à destinataires multiples (clés de réception X25519).
//! - [`validation`]  : validations humaines (biométrie), 5 minutes, liées à une charge.
//! - [`ticket`]      : manifeste validé par l'humain, signé par l'ordonnateur.
//! - [`journal`]     : journal chaîné et signé d'un ticket.
//! - [`appairage`]   : engagement préalable et code vocal à 6 chiffres.
//! - [`certificat`]  : certificats de rôle et liste de révocation.
//! - [`migration`]   : passage de la v1 à la v2 sans nouvel appairage.
//! - [`messages`]    : liste fermée des messages du canal.
//! - [`transport`]   : session Noise XX authentifiée par certificat.

pub mod appairage;
pub mod certificat;
pub mod cles;
pub mod enveloppe;
pub mod erreur;
pub mod jcs;
pub mod journal;
pub mod messages;
pub mod migration;
pub mod objet;
pub mod ticket;
pub mod transport;
pub mod validation;

pub use erreur::{Erreur, Resultat};

/// Version du protocole portée par tous les objets et le prologue Noise.
pub const VERSION: u64 = 2;

/// Secondes écoulées depuis l'époque Unix.
pub fn maintenant() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Octets aléatoires du système.
pub fn aleatoire<const N: usize>() -> [u8; N] {
    use rand_core::RngCore;
    let mut octets = [0u8; N];
    rand_core::OsRng.fill_bytes(&mut octets);
    octets
}

pub(crate) fn b64(octets: &[u8]) -> String {
    data_encoding::BASE64.encode(octets)
}

pub(crate) fn de_b64(texte: &str) -> Resultat<Vec<u8>> {
    data_encoding::BASE64
        .decode(texte.as_bytes())
        .map_err(|_| Erreur::Format("base64 invalide".into()))
}

/// Écrit un secret dans un fichier en 0600 (fichier temporaire puis renommage).
pub(crate) fn ecrire_prive(chemin: &std::path::Path, octets: &[u8]) -> Resultat<()> {
    use std::io::Write;
    let tmp = chemin.with_extension("tmp");
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let _ = std::fs::remove_file(&tmp);
    let mut f = options.open(&tmp)?;
    f.write_all(octets)?;
    f.sync_all()?;
    std::fs::rename(tmp, chemin)?;
    Ok(())
}
