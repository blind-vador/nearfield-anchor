//! Rotation d'identité : une machine v2 passe à une nouvelle clé (typiquement de sa clé
//! logicielle à sa puce) sans nouvel appairage.
//!
//! L'objet `rotation` est porté par la demande de certificat (`certificat_demande.preuve`,
//! à la place de l'objet `renouvellement`) :
//! - signé par la **nouvelle** clé (preuve de possession) ;
//! - contresigné par l'**ancienne** (`sig_ancienne`, sur `DOMAINE || JCS(charge sans
//!   sig_ancienne)`) : c'est elle, connue de l'ordonnateur, qui transmet la confiance ;
//! - lié à la nouvelle clé de transport, comme un renouvellement.
//!
//! Jamais de retour en arrière : une identité matérielle ne passe pas à une clé logicielle.

use serde::Deserialize;
use serde_json::{json, Value};

use crate::cles::{Alg, CleIdentite, Coffre, Niveau};
use crate::objet::{ObjetSigne, RegistreNonces};
use crate::{aleatoire, b64, de_b64, jcs, maintenant, Erreur, Resultat, VERSION};

/// Préfixe de la contresignature (séparation de domaine).
pub const DOMAINE: &[u8] = b"pf-rotation-v2\n";

/// Ce que transporte une rotation vérifiée.
#[derive(Clone, Debug, PartialEq)]
pub struct Rotation {
    pub ancienne: CleIdentite,
    pub identite: CleIdentite,
    pub niveau: Niveau,
    /// Clé X25519 de transport du nouveau certificat.
    pub transport: Vec<u8>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Charge {
    #[serde(rename = "type")]
    type_: String,
    v: u64,
    parc: String,
    id: String,
    emis: i64,
    nonce: String,
    ancienne: CleIdentite,
    identite: CleIdentite,
    niveau: Niveau,
    transport: String,
    sig_ancienne: String,
}

fn message(charge_sans_sig: &Value) -> Resultat<Vec<u8>> {
    let mut m = DOMAINE.to_vec();
    m.extend(jcs::canonique(charge_sans_sig)?);
    Ok(m)
}

fn refus(texte: &str) -> Erreur {
    Erreur::Certificat(format!("rotation : {texte}"))
}

/// Prépare la rotation de `ancien` vers `nouveau`, pour la clé de transport `transport`.
pub fn preparer(ancien: &dyn Coffre, nouveau: &dyn Coffre, parc: &str, transport: &[u8]) -> Resultat<ObjetSigne> {
    let ancienne = ancien.cle_publique();
    let identite = nouveau.cle_publique();
    if ancienne == identite {
        return Err(refus("la nouvelle identité doit être une nouvelle clé"));
    }
    if identite.alg != Alg::P256 {
        return Err(refus("la nouvelle identité doit être une clé P-256"));
    }
    if ancien.niveau() == Niveau::Materiel && nouveau.niveau() == Niveau::Logiciel {
        return Err(refus("pas de retour d'une puce vers une clé logicielle"));
    }
    if transport.len() != 32 {
        return Err(refus("clé de transport de 32 octets attendue"));
    }
    let mut charge = json!({
        "type": "rotation",
        "v": VERSION,
        "parc": parc,
        "id": format!("rotation-{}", identite.id()?),
        "emis": maintenant(),
        "nonce": b64(&aleatoire::<16>()),
        "ancienne": ancienne,
        "identite": identite,
        "niveau": nouveau.niveau(),
        "transport": b64(transport),
    });
    charge["sig_ancienne"] = b64(&ancien.signer(&message(&charge)?)?).into();
    ObjetSigne::signer(nouveau, charge)
}

/// Le type d'une preuve jointe à une demande de certificat (`renouvellement` ou `rotation`).
pub fn type_de(objet: &ObjetSigne) -> Option<&str> {
    objet.charge.get("type").and_then(Value::as_str)
}

/// Vérifie une rotation avec l'identité **déjà connue** de la machine et son niveau,
/// pour la clé de transport annoncée dans la demande (base64).
pub fn verifier(
    objet: &ObjetSigne,
    parc: &str,
    ancienne_connue: &CleIdentite,
    niveau_connu: Niveau,
    transport_annonce: &str,
    registre: Option<&mut RegistreNonces>,
) -> Resultat<Rotation> {
    let c: Charge = serde_json::from_value(objet.charge.clone())?;
    if c.ancienne != *ancienne_connue {
        return Err(Erreur::Signataire("rotation d'une identité qui n'est pas celle de la machine".into()));
    }
    if c.identite == c.ancienne || c.identite.alg != Alg::P256 {
        return Err(refus("nouvelle identité invalide"));
    }
    if niveau_connu == Niveau::Materiel && c.niveau == Niveau::Logiciel {
        return Err(refus("pas de retour d'une puce vers une clé logicielle"));
    }
    if c.transport != transport_annonce {
        return Err(refus("autre clé de transport que celle de la demande"));
    }
    let transport = de_b64(&c.transport)?;
    if transport.len() != 32 {
        return Err(refus("clé de transport"));
    }
    objet.verifier(&c.identite, "rotation", parc, registre)?;
    if c.id != format!("rotation-{}", c.identite.id()?) {
        return Err(refus("identifiant d'objet"));
    }
    let mut sans_sig = objet.charge.clone();
    sans_sig.as_object_mut().map(|m| m.remove("sig_ancienne"));
    c.ancienne
        .verifier(&message(&sans_sig)?, &de_b64(&c.sig_ancienne)?)
        .map_err(|_| refus("contresignature de l'ancienne clé invalide"))?;
    Ok(Rotation { ancienne: c.ancienne, identite: c.identite, niveau: c.niveau, transport })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cles::CoffreLogiciel;

    const PARC: &str = "parc-test";

    #[test]
    fn passe_a_une_nouvelle_cle() {
        let a = CoffreLogiciel::generer(Alg::P256);
        let b = CoffreLogiciel::generer(Alg::P256);
        let o = preparer(&a, &b, PARC, &[5u8; 32]).unwrap();
        assert_eq!(type_de(&o), Some("rotation"));
        let mut reg = RegistreNonces::default();
        let t = b64(&[5u8; 32]);
        let r = verifier(&o, PARC, &a.cle_publique(), Niveau::Logiciel, &t, Some(&mut reg)).unwrap();
        assert_eq!(r.identite, b.cle_publique());
        assert_eq!(r.transport, vec![5u8; 32]);
        // Rejouée : refusée.
        assert!(verifier(&o, PARC, &a.cle_publique(), Niveau::Logiciel, &t, Some(&mut reg)).is_err());
    }

    #[test]
    fn refuse_autre_ancienne_autre_transport_autre_parc() {
        let a = CoffreLogiciel::generer(Alg::P256);
        let b = CoffreLogiciel::generer(Alg::P256);
        let intrus = CoffreLogiciel::generer(Alg::P256);
        let o = preparer(&a, &b, PARC, &[5u8; 32]).unwrap();
        let t = b64(&[5u8; 32]);
        assert!(verifier(&o, PARC, &intrus.cle_publique(), Niveau::Logiciel, &t, None).is_err());
        assert!(verifier(&o, PARC, &a.cle_publique(), Niveau::Logiciel, &b64(&[6u8; 32]), None).is_err());
        assert!(verifier(&o, "autre", &a.cle_publique(), Niveau::Logiciel, &t, None).is_err());
    }

    #[test]
    fn un_intrus_ne_peut_pas_detourner_la_rotation() {
        // L'intrus remplace la nouvelle identité par la sienne et re-signe : la
        // contresignature de l'ancienne clé ne correspond plus.
        let a = CoffreLogiciel::generer(Alg::P256);
        let b = CoffreLogiciel::generer(Alg::P256);
        let intrus = CoffreLogiciel::generer(Alg::P256);
        let o = preparer(&a, &b, PARC, &[5u8; 32]).unwrap();
        let mut charge = o.charge.clone();
        charge.as_object_mut().unwrap().remove("sig");
        charge["identite"] = serde_json::to_value(intrus.cle_publique()).unwrap();
        charge["id"] = format!("rotation-{}", intrus.cle_publique().id().unwrap()).into();
        let faux = ObjetSigne::signer(&intrus, charge).unwrap();
        assert!(verifier(&faux, PARC, &a.cle_publique(), Niveau::Logiciel, &b64(&[5u8; 32]), None).is_err());
    }

    #[test]
    fn pas_de_retour_vers_le_logiciel() {
        let a = CoffreLogiciel::generer(Alg::P256);
        let b = CoffreLogiciel::generer(Alg::P256);
        let o = preparer(&a, &b, PARC, &[5u8; 32]).unwrap();
        // L'ordonnateur sait la machine matérielle : une rotation vers du logiciel est refusée.
        assert!(verifier(&o, PARC, &a.cle_publique(), Niveau::Materiel, &b64(&[5u8; 32]), None).is_err());
    }
}
