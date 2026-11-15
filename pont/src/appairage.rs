//! Appairage : engagement préalable et code vocal à 6 chiffres (spec §3).
//!
//! A (la machine qui rejoint) s'engage sur sa clé et son aléa AVANT de connaître
//! l'aléa de B (l'ordonnateur). Un intermédiaire ne peut donc plus essayer des clés
//! jusqu'à retomber sur le même code : il a une chance sur un million, et un échec
//! s'entend. Le canal peut être quelconque (TCP direct, fichiers sur le NAS).
//!
//! Déroulé : `Rejoignant::nouveau` → engagement → B : `Accueil::nouveau` → réponse →
//! A : `Rejoignant::recevoir` → offre + code → B : `Accueil::verifier` → code.
//! Les deux machines disent leur code ; l'humain confirme des deux côtés.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::cles::CleIdentite;
use crate::{aleatoire, b64, de_b64, Erreur, Resultat};

/// Ce qu'une machine dévoile d'elle-même pendant l'appairage.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Presentation {
    pub identite: CleIdentite,
    /// Clé publique X25519 de transport (32 octets, base64).
    pub transport: String,
    /// Aléa de 32 octets (base64).
    pub n: String,
}

impl Presentation {
    fn nouvelle(identite: CleIdentite, transport: &[u8]) -> Resultat<Self> {
        if transport.len() != 32 {
            return Err(Erreur::Appairage("clé de transport de 32 octets attendue".into()));
        }
        Ok(Presentation { identite, transport: b64(transport), n: b64(&aleatoire::<32>()) })
    }

    fn verifier_tailles(&self) -> Resultat<()> {
        self.identite.octets()?;
        if de_b64(&self.transport)?.len() != 32 || de_b64(&self.n)?.len() != 32 {
            return Err(Erreur::Appairage("tailles invalides".into()));
        }
        Ok(())
    }

    fn hacher(&self, h: &mut Sha256) -> Resultat<()> {
        champ(h, self.identite.alg.nom().as_bytes());
        champ(h, &self.identite.octets()?);
        champ(h, &de_b64(&self.transport)?);
        Ok(())
    }
}

/// Chaque champ est préfixé par sa longueur : aucune ambiguïté de découpage.
fn champ(h: &mut Sha256, octets: &[u8]) {
    h.update((octets.len() as u32).to_be_bytes());
    h.update(octets);
}

/// Engagement de A : SHA-256 hexadécimal de sa présentation complète.
pub fn engagement(p: &Presentation) -> Resultat<String> {
    let mut h = Sha256::new();
    champ(&mut h, b"pf-appairage-v2");
    p.hacher(&mut h)?;
    champ(&mut h, &de_b64(&p.n)?);
    Ok(data_encoding::HEXLOWER.encode(&h.finalize()))
}

/// Code à 6 chiffres, identique sur les deux machines si personne ne s'est interposé.
pub fn code(a: &Presentation, b: &Presentation) -> Resultat<String> {
    let mut h = Sha256::new();
    champ(&mut h, b"pf-code-v2");
    a.hacher(&mut h)?;
    b.hacher(&mut h)?;
    champ(&mut h, &de_b64(&a.n)?);
    champ(&mut h, &de_b64(&b.n)?);
    let d = h.finalize();
    let nombre = u32::from_be_bytes([d[0], d[1], d[2], d[3]]) % 1_000_000;
    Ok(format!("{nombre:06}"))
}

/// Le code tel qu'il se dit : trois paires, « 48, 21, 07 ».
pub fn code_parle(code: &str) -> String {
    let c: Vec<char> = code.chars().collect();
    c.chunks(2).map(|p| p.iter().collect::<String>()).collect::<Vec<_>>().join(", ")
}

/// Côté A, la machine qui rejoint le parc.
pub struct Rejoignant {
    moi: Presentation,
}

impl Rejoignant {
    /// Rend l'état de A et l'engagement à envoyer à B.
    pub fn nouveau(identite: CleIdentite, transport: &[u8]) -> Resultat<(Self, String)> {
        let moi = Presentation::nouvelle(identite, transport)?;
        let e = engagement(&moi)?;
        Ok((Rejoignant { moi }, e))
    }

    /// Reçoit la présentation de B ; rend la présentation de A à envoyer et le code à dire.
    pub fn recevoir(self, b: &Presentation) -> Resultat<(Presentation, String)> {
        b.verifier_tailles()?;
        let c = code(&self.moi, b)?;
        Ok((self.moi, c))
    }
}

/// Côté B, l'ordonnateur.
pub struct Accueil {
    engagement: String,
    moi: Presentation,
}

impl Accueil {
    /// Reçoit l'engagement de A ; rend l'état de B et sa présentation à envoyer.
    pub fn nouveau(engagement_a: &str, identite: CleIdentite, transport: &[u8]) -> Resultat<(Self, Presentation)> {
        if engagement_a.len() != 64 || !engagement_a.bytes().all(|o| o.is_ascii_hexdigit()) {
            return Err(Erreur::Appairage("engagement mal formé".into()));
        }
        let moi = Presentation::nouvelle(identite, transport)?;
        Ok((Accueil { engagement: engagement_a.to_ascii_lowercase(), moi: moi.clone() }, moi))
    }

    /// Reçoit la présentation de A : elle doit correspondre à son engagement.
    /// Rend le code à dire.
    pub fn verifier(self, a: &Presentation) -> Resultat<String> {
        a.verifier_tailles()?;
        if engagement(a)? != self.engagement {
            return Err(Erreur::Appairage("la présentation ne correspond pas à l'engagement".into()));
        }
        code(a, &self.moi)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cles::{Alg, Coffre, CoffreLogiciel};

    fn identite(alg: Alg) -> CleIdentite {
        CoffreLogiciel::generer(alg).cle_publique()
    }

    #[test]
    fn les_deux_cotes_obtiennent_le_meme_code() {
        let (a, eng) = Rejoignant::nouveau(identite(Alg::P256), &[1; 32]).unwrap();
        let (b, pres_b) = Accueil::nouveau(&eng, identite(Alg::Ed25519), &[2; 32]).unwrap();
        let (pres_a, code_a) = a.recevoir(&pres_b).unwrap();
        let code_b = b.verifier(&pres_a).unwrap();
        assert_eq!(code_a, code_b);
        assert_eq!(code_a.len(), 6);
    }

    #[test]
    fn refuse_une_cle_changee_apres_engagement() {
        let (a, eng) = Rejoignant::nouveau(identite(Alg::P256), &[1; 32]).unwrap();
        let (b, pres_b) = Accueil::nouveau(&eng, identite(Alg::P256), &[2; 32]).unwrap();
        let (mut pres_a, _) = a.recevoir(&pres_b).unwrap();
        pres_a.identite = identite(Alg::P256); // un intermédiaire substitue sa clé
        assert!(b.verifier(&pres_a).is_err());
    }

    #[test]
    fn un_intermediaire_obtient_des_codes_differents() {
        // M se place entre A et B : il doit s'engager auprès de B avant de voir nB.
        let (a, eng_a) = Rejoignant::nouveau(identite(Alg::P256), &[1; 32]).unwrap();
        let (m_vers_b, eng_m) = Rejoignant::nouveau(identite(Alg::P256), &[3; 32]).unwrap();
        let (b, pres_b) = Accueil::nouveau(&eng_m, identite(Alg::P256), &[2; 32]).unwrap();
        let (m_accueil, pres_m) = Accueil::nouveau(&eng_a, identite(Alg::P256), &[4; 32]).unwrap();
        let (pres_a, code_a) = a.recevoir(&pres_m).unwrap();
        let (pres_m_vers_b, code_mb) = m_vers_b.recevoir(&pres_b).unwrap();
        let code_b = b.verifier(&pres_m_vers_b).unwrap();
        let code_ma = m_accueil.verifier(&pres_a).unwrap();
        assert_eq!(code_a, code_ma);
        assert_eq!(code_b, code_mb);
        // Les aléas sont indépendants : les deux codes entendus par l'humain diffèrent
        // (sauf une chance sur un million).
        assert_ne!(code_a, code_b);
    }

    #[test]
    fn code_parle_en_paires() {
        assert_eq!(code_parle("482107"), "48, 21, 07");
    }

    #[test]
    fn vecteur_fixe() {
        // Vecteur de test publié (specs/vecteurs) : toute implémentation doit le retrouver.
        let a = Presentation {
            identite: CleIdentite { alg: Alg::Ed25519, public: b64(&[0x11; 32]) },
            transport: b64(&[0x22; 32]),
            n: b64(&[0x33; 32]),
        };
        let b = Presentation {
            identite: CleIdentite { alg: Alg::Ed25519, public: b64(&[0x44; 32]) },
            transport: b64(&[0x55; 32]),
            n: b64(&[0x66; 32]),
        };
        // Valeurs recalculées indépendamment (Python, hashlib) : voir vecteurs.json.
        assert_eq!(engagement(&a).unwrap(), "c0bf9ed4deba33797f4a5f37618255cec8ee5a0185c2d2da3bd62a2f0c399035");
        assert_eq!(code(&a, &b).unwrap(), "719237");
        assert_eq!(a.identite.id().unwrap(), "ytymkn3ulb3jyfun");
    }
}
