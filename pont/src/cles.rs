//! Clés d'identité, coffres et identifiants de machine (spec §2).
//!
//! Une clé d'identité vit dans un [`Coffre`] : la puce de sécurité quand la machine
//! en a une (Secure Enclave, TPM : [`crate::puce`]), sinon un fichier
//! (`CoffreLogiciel`, niveau [`Niveau::Logiciel`]).

use std::path::Path;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::{b64, de_b64, Erreur, Resultat};

/// Algorithme de signature d'une clé d'identité.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Alg {
    /// Ed25519 : clés logicielles, et clés héritées de la v1.
    Ed25519,
    /// ECDSA P-256 / SHA-256 : ce que savent faire Secure Enclave et TPM.
    P256,
}

impl Alg {
    pub fn nom(self) -> &'static str {
        match self {
            Alg::Ed25519 => "ed25519",
            Alg::P256 => "p256",
        }
    }
}

/// Où vit la clé : décide de ce que la machine a le droit de faire (spec §4).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Niveau {
    Materiel,
    Logiciel,
}

/// Clé publique d'identité, telle qu'elle circule dans les objets.
/// `public` : 32 octets (Ed25519) ou point SEC1 non compressé de 65 octets (P-256).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CleIdentite {
    pub alg: Alg,
    #[serde(rename = "pub")]
    pub public: String,
}

impl CleIdentite {
    pub fn octets(&self) -> Resultat<Vec<u8>> {
        let o = de_b64(&self.public)?;
        let attendu = match self.alg {
            Alg::Ed25519 => 32,
            Alg::P256 => 65,
        };
        if o.len() != attendu {
            return Err(Erreur::Cle(format!("clé {} de {} octets", self.alg.nom(), o.len())));
        }
        Ok(o)
    }

    /// Identifiant stable de la machine, dérivé de sa clé (spec §2.2).
    pub fn id(&self) -> Resultat<String> {
        let mut h = Sha256::new();
        h.update(self.alg.nom().as_bytes());
        h.update(self.octets()?);
        let texte = data_encoding::BASE32_NOPAD.encode(&h.finalize());
        Ok(texte[..16].to_ascii_lowercase())
    }

    /// Vérifie une signature `r||s` (P-256) ou Ed25519 de 64 octets.
    pub fn verifier(&self, message: &[u8], signature: &[u8]) -> Resultat<()> {
        let public = self.octets()?;
        let sig: [u8; 64] = signature.try_into().map_err(|_| Erreur::Signature)?;
        match self.alg {
            Alg::Ed25519 => {
                let cle = ed25519_dalek::VerifyingKey::from_bytes(public.as_slice().try_into().unwrap())
                    .map_err(|_| Erreur::Cle("Ed25519 invalide".into()))?;
                cle.verify_strict(message, &ed25519_dalek::Signature::from_bytes(&sig))
                    .map_err(|_| Erreur::Signature)
            }
            Alg::P256 => {
                use p256::ecdsa::signature::Verifier;
                let cle = p256::ecdsa::VerifyingKey::from_sec1_bytes(&public)
                    .map_err(|_| Erreur::Cle("P-256 invalide".into()))?;
                let s = p256::ecdsa::Signature::from_slice(&sig).map_err(|_| Erreur::Signature)?;
                cle.verify(message, &s).map_err(|_| Erreur::Signature)
            }
        }
    }
}

/// Ce qui tient une clé privée d'identité sans jamais la donner.
pub trait Coffre: Send + Sync {
    fn cle_publique(&self) -> CleIdentite;
    /// Signature de 64 octets (`r||s` pour P-256).
    fn signer(&self, message: &[u8]) -> Resultat<Vec<u8>>;
    fn niveau(&self) -> Niveau;
    /// Signature demandée à l'humain : une puce qui exige sa présence (biométrie ou mot
    /// de passe) affiche `raison`. Les autres coffres l'ignorent.
    fn signer_avec_raison(&self, message: &[u8], _raison: &str) -> Resultat<Vec<u8>> {
        self.signer(message)
    }
}

impl<T: Coffre + ?Sized> Coffre for Box<T> {
    fn cle_publique(&self) -> CleIdentite {
        (**self).cle_publique()
    }
    fn signer(&self, message: &[u8]) -> Resultat<Vec<u8>> {
        (**self).signer(message)
    }
    fn niveau(&self) -> Niveau {
        (**self).niveau()
    }
    fn signer_avec_raison(&self, message: &[u8], raison: &str) -> Resultat<Vec<u8>> {
        (**self).signer_avec_raison(message, raison)
    }
}

/// Un coffre dont chaque signature est demandée à l'humain avec cette raison.
pub struct AvecRaison<'a> {
    pub coffre: &'a dyn Coffre,
    pub raison: &'a str,
}

impl Coffre for AvecRaison<'_> {
    fn cle_publique(&self) -> CleIdentite {
        self.coffre.cle_publique()
    }
    fn signer(&self, message: &[u8]) -> Resultat<Vec<u8>> {
        self.coffre.signer_avec_raison(message, self.raison)
    }
    fn niveau(&self) -> Niveau {
        self.coffre.niveau()
    }
}

enum Prive {
    Ed25519(ed25519_dalek::SigningKey),
    P256(p256::ecdsa::SigningKey),
}

/// Clé gardée dans un fichier PKCS#8 (PEM, droits 0600). Niveau logiciel.
/// Lit aussi la `cle.pem` Ed25519 de la v1 : la migration garde l'identité.
pub struct CoffreLogiciel {
    prive: Prive,
}

/// Clé P-256 tirée de l'aléa du système (32 octets ; un tirage hors de l'intervalle
/// valide, probabilité de l'ordre de 2^-128, est simplement refait).
fn p256_aleatoire() -> p256::ecdsa::SigningKey {
    loop {
        let mut octets = crate::aleatoire::<32>();
        let k = p256::ecdsa::SigningKey::from_slice(&octets);
        zeroize::Zeroize::zeroize(&mut octets);
        if let Ok(k) = k {
            return k;
        }
    }
}

impl CoffreLogiciel {
    pub fn generer(alg: Alg) -> Self {
        let prive = match alg {
            Alg::Ed25519 => Prive::Ed25519(ed25519_dalek::SigningKey::generate(&mut rand_core::OsRng)),
            Alg::P256 => Prive::P256(p256_aleatoire()),
        };
        CoffreLogiciel { prive }
    }

    pub fn depuis_pem(pem: &str) -> Resultat<Self> {
        use ed25519_dalek::pkcs8::DecodePrivateKey as _;
        if let Ok(k) = ed25519_dalek::SigningKey::from_pkcs8_pem(pem) {
            return Ok(CoffreLogiciel { prive: Prive::Ed25519(k) });
        }
        use p256::pkcs8::DecodePrivateKey as _;
        if let Ok(k) = p256::SecretKey::from_pkcs8_pem(pem) {
            return Ok(CoffreLogiciel { prive: Prive::P256(k.into()) });
        }
        Err(Erreur::Cle("PEM illisible (ni Ed25519 ni P-256)".into()))
    }

    pub fn en_pem(&self) -> Resultat<String> {
        match &self.prive {
            Prive::Ed25519(k) => {
                use ed25519_dalek::pkcs8::EncodePrivateKey as _;
                k.to_pkcs8_pem(Default::default()).map(|z| z.to_string()).map_err(|e| Erreur::Cle(e.to_string()))
            }
            Prive::P256(k) => {
                use p256::pkcs8::EncodePrivateKey as _;
                p256::SecretKey::from(k)
                    .to_pkcs8_pem(Default::default())
                    .map(|z| z.to_string())
                    .map_err(|e| Erreur::Cle(e.to_string()))
            }
        }
    }

    /// Charge la clé du fichier, ou la crée (droits 0600, création exclusive).
    pub fn charger_ou_creer(chemin: &Path, alg: Alg) -> Resultat<Self> {
        match std::fs::read_to_string(chemin) {
            Ok(pem) => Self::depuis_pem(&pem),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                let coffre = Self::generer(alg);
                if let Some(parent) = chemin.parent() {
                    std::fs::create_dir_all(parent)?;
                }
                let mut options = std::fs::OpenOptions::new();
                options.write(true).create_new(true);
                #[cfg(unix)]
                {
                    use std::os::unix::fs::OpenOptionsExt;
                    options.mode(0o600);
                }
                use std::io::Write;
                options.open(chemin)?.write_all(coffre.en_pem()?.as_bytes())?;
                Ok(coffre)
            }
            Err(e) => Err(e.into()),
        }
    }
}

impl Coffre for CoffreLogiciel {
    fn cle_publique(&self) -> CleIdentite {
        match &self.prive {
            Prive::Ed25519(k) => CleIdentite { alg: Alg::Ed25519, public: b64(k.verifying_key().as_bytes()) },
            Prive::P256(k) => CleIdentite {
                alg: Alg::P256,
                public: b64(k.verifying_key().to_sec1_point(false).as_bytes()),
            },
        }
    }

    fn signer(&self, message: &[u8]) -> Resultat<Vec<u8>> {
        Ok(match &self.prive {
            Prive::Ed25519(k) => {
                use ed25519_dalek::Signer;
                k.sign(message).to_bytes().to_vec()
            }
            Prive::P256(k) => {
                use p256::ecdsa::signature::Signer;
                let s: p256::ecdsa::Signature = k.sign(message);
                s.to_bytes().to_vec()
            }
        })
    }

    fn niveau(&self) -> Niveau {
        Niveau::Logiciel
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signe_et_verifie_les_deux_algorithmes() {
        for alg in [Alg::Ed25519, Alg::P256] {
            let c = CoffreLogiciel::generer(alg);
            let sig = c.signer(b"bonjour").unwrap();
            assert_eq!(sig.len(), 64);
            let pubk = c.cle_publique();
            pubk.verifier(b"bonjour", &sig).unwrap();
            assert!(pubk.verifier(b"bonsoir", &sig).is_err());
            assert_eq!(pubk.id().unwrap().len(), 16);
        }
    }

    #[test]
    fn pem_aller_retour_et_fichier_0600() {
        let dossier = tempfile::tempdir().unwrap();
        let chemin = dossier.path().join("cles/identite.pem");
        let a = CoffreLogiciel::charger_ou_creer(&chemin, Alg::P256).unwrap();
        let b = CoffreLogiciel::charger_ou_creer(&chemin, Alg::Ed25519).unwrap();
        assert_eq!(a.cle_publique(), b.cle_publique(), "la clé existante doit être relue");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&chemin).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }
    }

    #[test]
    fn lit_une_cle_v1_ed25519() {
        let v1 = CoffreLogiciel::generer(Alg::Ed25519);
        let pem = v1.en_pem().unwrap();
        assert!(pem.contains("BEGIN PRIVATE KEY"));
        let relu = CoffreLogiciel::depuis_pem(&pem).unwrap();
        assert_eq!(relu.cle_publique(), v1.cle_publique());
    }

    #[test]
    fn refuse_une_cle_de_mauvaise_taille() {
        let c = CleIdentite { alg: Alg::P256, public: b64(&[1u8; 32]) };
        assert!(c.id().is_err());
    }

    /// Clé et signature produites par p256 0.13 (avant la migration du 2026-11-16, clé
    /// d'essai jetable) : la 0.14 relit la même clé et vérifie la même signature. Les
    /// identités P-256 déjà en service (fichiers identite.pem) restent donc valables.
    #[test]
    fn compatible_avec_les_cles_de_p256_0_13() {
        const PEM: &str = "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgLJXEdOyi7CgyVOMM\nky4aLY2Ayj8EREb8/QJTV8aEcO2hRANCAAQhAay32OVlq0ZyKSuO2mUX+9wxjB8o\nJNgA2Iu0B00GjfD0gojPkdCsARtzvoVqqhYqKGx5WjkIzDBd6nxsnq0f\n-----END PRIVATE KEY-----\n";
        const PUB: &str = "BCEBrLfY5WWrRnIpK47aZRf73DGMHygk2ADYi7QHTQaN8PSCiM+R0KwBG3O+hWqqFioobHlaOQjMMF3qfGyerR8=";
        const SIG: &str = "P7sbDSay3BueQwVQo6FDJpa52Yvgt8PvfsIo9+A52xHve4KIMKqqf/AX/1eBwPB98sNoqV0qM5389Lcbh4OckA==";
        let c = CoffreLogiciel::depuis_pem(PEM).unwrap();
        assert_eq!(c.cle_publique(), CleIdentite { alg: Alg::P256, public: PUB.into() });
        let sig = crate::de_b64(SIG).unwrap();
        c.cle_publique().verifier(b"compatibilite p256 0.13 vers 0.14", &sig).unwrap();
        // Et la nouvelle version signe d'une façon que la clé publique accepte.
        let neuve = c.signer(b"x").unwrap();
        c.cle_publique().verifier(b"x", &neuve).unwrap();
        assert_eq!(CoffreLogiciel::depuis_pem(&c.en_pem().unwrap()).unwrap().cle_publique(), c.cle_publique());
    }

}
