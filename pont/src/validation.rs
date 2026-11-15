//! Validations humaines (protocole-v2 §7, lot2-fondations §1).
//!
//! Une validation est signée par la clé humaine d'un appareil (biométrie à chaque
//! signature quand le coffre est matériel). Elle porte sur l'empreinte d'une charge
//! précise et ne sert que 5 minutes.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::cles::{CleIdentite, Coffre};
use crate::objet::{ObjetSigne, AVANCE_MAX_SEC};
use crate::{jcs, maintenant, Erreur, Resultat, VERSION};

/// Durée d'usage d'une validation.
pub const VALIDATION_MAX_SEC: i64 = 300;
const MAX_MOTIF: usize = 1000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Decision {
    Accepte,
    Refuse,
}

impl Decision {
    fn nom(self) -> &'static str {
        match self {
            Decision::Accepte => "accepte",
            Decision::Refuse => "refuse",
        }
    }
}

/// Empreinte (sha256 hex) de la forme canonique d'une valeur.
pub fn empreinte_valeur(v: &Value) -> Resultat<String> {
    Ok(data_encoding::HEXLOWER.encode(&Sha256::digest(jcs::canonique(v)?)))
}

/// L'humain valide (ou refuse) exactement cette charge.
pub fn preparer(humain: &dyn Coffre, parc: &str, charge_validee: &Value, decision: Decision, motif: Option<&str>) -> Resultat<ObjetSigne> {
    let objet = empreinte_valeur(charge_validee)?;
    let mut c = json!({"type": "validation", "parc": parc, "id": format!("validation-{}-{}", &objet[..16], maintenant()),
                       "objet": objet, "decision": decision.nom()});
    if let Some(m) = motif {
        if m.len() > MAX_MOTIF || m.chars().any(char::is_control) {
            return Err(Erreur::Format("motif".into()));
        }
        c["motif"] = Value::from(m);
    }
    ObjetSigne::signer(humain, c)
}

/// Vérifie une validation pour cette charge, au moment `instant` : signée par `humain`,
/// du bon parc, pour cette empreinte, de moins de 5 minutes. Rend la décision.
pub fn verifier(validation: &ObjetSigne, humain: &CleIdentite, parc: &str, charge_validee: &Value, instant: i64) -> Resultat<Decision> {
    validation.verifier_signature(humain)?;
    if validation.champ_texte("type")? != "validation" || validation.champ_texte("parc")? != parc
        || validation.champ_entier("v")? != VERSION as i64
    {
        return Err(Erreur::Format("validation : type, parc ou version".into()));
    }
    if validation.champ_texte("objet")? != empreinte_valeur(charge_validee)? {
        return Err(Erreur::Format("validation d'une autre charge".into()));
    }
    let emis = validation.champ_entier("emis")?;
    if emis > instant + AVANCE_MAX_SEC || emis < instant - VALIDATION_MAX_SEC {
        return Err(Erreur::Date);
    }
    match validation.champ_texte("decision")? {
        "accepte" => Ok(Decision::Accepte),
        "refuse" => Ok(Decision::Refuse),
        _ => Err(Erreur::Format("décision".into())),
    }
}

/// Comme [`verifier`], mais exige `accepte` et la clé de l'un des humains connus.
pub fn exiger(validation: &ObjetSigne, humains: &[CleIdentite], parc: &str, charge_validee: &Value, instant: i64) -> Resultat<()> {
    let cle = humains
        .iter()
        .find(|c| c.id().map(|id| id == validation.signataire).unwrap_or(false))
        .ok_or_else(|| Erreur::Signataire(validation.signataire.clone()))?;
    match verifier(validation, cle, parc, charge_validee, instant)? {
        Decision::Accepte => Ok(()),
        Decision::Refuse => Err(Erreur::Format("l'humain a refusé".into())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cles::{Alg, CoffreLogiciel};

    #[test]
    fn validation_liee_a_sa_charge_et_limitee_dans_le_temps() {
        let humain = CoffreLogiciel::generer(Alg::P256);
        let autre = CoffreLogiciel::generer(Alg::P256);
        let plan = json!({"type": "ticket", "id": "t1", "plan": "corriger la doc"});
        let v = preparer(&humain, "parc", &plan, Decision::Accepte, None).unwrap();
        let t = maintenant();
        assert_eq!(verifier(&v, &humain.cle_publique(), "parc", &plan, t).unwrap(), Decision::Accepte);
        exiger(&v, &[autre.cle_publique(), humain.cle_publique()], "parc", &plan, t).unwrap();
        // autre charge, autre parc, autre cle, trop tard
        let autre_plan = json!({"type": "ticket", "id": "t1", "plan": "tout effacer"});
        assert!(verifier(&v, &humain.cle_publique(), "parc", &autre_plan, t).is_err());
        assert!(verifier(&v, &humain.cle_publique(), "parc2", &plan, t).is_err());
        assert!(exiger(&v, &[autre.cle_publique()], "parc", &plan, t).is_err());
        assert!(matches!(verifier(&v, &humain.cle_publique(), "parc", &plan, t + VALIDATION_MAX_SEC + 1), Err(Erreur::Date)));
        // un refus n'autorise rien
        let r = preparer(&humain, "parc", &plan, Decision::Refuse, Some("pas maintenant")).unwrap();
        assert_eq!(verifier(&r, &humain.cle_publique(), "parc", &plan, t).unwrap(), Decision::Refuse);
        assert!(exiger(&r, &[humain.cle_publique()], "parc", &plan, t).is_err());
        assert!(preparer(&humain, "parc", &plan, Decision::Accepte, Some("a\nb")).is_err());
    }
}
