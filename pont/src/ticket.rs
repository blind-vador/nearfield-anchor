//! Ticket : un manifeste validé par l'humain puis signé par l'ordonnateur
//! (lot2-fondations §2).

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::cles::{CleIdentite, Coffre};
use crate::objet::{chemin_sur, nom_sur, ObjetSigne, AVANCE_MAX_SEC};
use crate::validation::{self, VALIDATION_MAX_SEC};
use crate::{maintenant, Erreur, Resultat, VERSION};

pub const MAX_TITRE: usize = 200;
pub const MAX_PLAN: usize = 20_000;
pub const MAX_LISTE: usize = 64;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Budget {
    pub secondes: u64,
    pub cout_centimes: u64,
}

/// Brouillon de ticket : ce que l'humain lit et valide.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Brouillon {
    #[serde(rename = "type")]
    pub type_: String,
    pub parc: String,
    pub id: String,
    pub titre: String,
    pub plan: String,
    pub budget: Budget,
    pub capacites: Vec<String>,
    pub zones: Vec<String>,
}

fn texte_ok(t: &str, max: usize, lignes: bool) -> bool {
    !t.trim().is_empty() && t.len() <= max && t.chars().all(|c| !c.is_control() || (lignes && (c == '\n' || c == '\t')))
}

impl Brouillon {
    pub fn controler(&self) -> Resultat<()> {
        let refus = |quoi: &str| Err(Erreur::Format(format!("ticket : {quoi}")));
        if self.type_ != "ticket" {
            return refus("type");
        }
        nom_sur(&self.id)?;
        if !texte_ok(&self.titre, MAX_TITRE, false) {
            return refus("titre");
        }
        if !texte_ok(&self.plan, MAX_PLAN, true) {
            return refus("plan");
        }
        if self.capacites.len() > MAX_LISTE || self.zones.len() > MAX_LISTE {
            return refus("listes trop longues");
        }
        for c in &self.capacites {
            if c.is_empty() || c.len() > 128 || c.chars().any(|x| x.is_control() || x == '/') {
                return refus("capacité");
            }
        }
        for z in &self.zones {
            chemin_sur(z.strip_suffix('/').unwrap_or(z))?;
        }
        Ok(())
    }

    fn valeur(&self) -> Resultat<Value> {
        Ok(serde_json::to_value(self)?)
    }
}

/// L'ordonnateur émet le ticket : le brouillon, plus la validation humaine (acceptée,
/// de moins de 5 minutes, pour exactement ce brouillon).
pub fn emettre(ordonnateur: &dyn Coffre, brouillon: &Brouillon, validation: &ObjetSigne, humains: &[CleIdentite]) -> Resultat<ObjetSigne> {
    brouillon.controler()?;
    let v = brouillon.valeur()?;
    validation::exiger(validation, humains, &brouillon.parc, &v, maintenant())?;
    let mut charge = v;
    charge["validation"] = serde_json::to_value(validation)?;
    ObjetSigne::signer(ordonnateur, charge)
}

/// Vérifie un ticket reçu : signé par l'ordonnateur épinglé, validé par un humain connu,
/// validation signée au plus 5 minutes avant le ticket. Rend le brouillon.
pub fn verifier(ticket: &ObjetSigne, ordonnateur: &CleIdentite, parc: &str, humains: &[CleIdentite]) -> Resultat<Brouillon> {
    ticket.verifier_signature(ordonnateur)?;
    if ticket.champ_texte("type")? != "ticket" || ticket.champ_texte("parc")? != parc || ticket.champ_entier("v")? != VERSION as i64 {
        return Err(Erreur::Format("ticket : type, parc ou version".into()));
    }
    let emis = ticket.champ_entier("emis")?;
    let mut charge = ticket.charge.clone();
    let obj = charge.as_object_mut().ok_or_else(|| Erreur::Format("ticket".into()))?;
    let validation: ObjetSigne =
        serde_json::from_value(obj.remove("validation").ok_or_else(|| Erreur::Format("ticket sans validation".into()))?)?;
    for champ in ["v", "emis", "nonce"] {
        obj.remove(champ);
    }
    let brouillon: Brouillon = serde_json::from_value(charge.clone())?;
    brouillon.controler()?;
    let v_emis = validation.champ_entier("emis")?;
    if v_emis > emis + AVANCE_MAX_SEC || v_emis < emis - VALIDATION_MAX_SEC {
        return Err(Erreur::Format("validation trop ancienne pour ce ticket".into()));
    }
    // La validation est vérifiée « au moment du ticket » : c'est là qu'elle a servi.
    let cle = humains
        .iter()
        .find(|c| c.id().map(|id| id == validation.signataire).unwrap_or(false))
        .ok_or_else(|| Erreur::Signataire(validation.signataire.clone()))?;
    match validation::verifier(&validation, cle, parc, &brouillon.valeur()?, emis)? {
        validation::Decision::Accepte => Ok(brouillon),
        validation::Decision::Refuse => Err(Erreur::Format("ticket refusé par l'humain".into())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cles::{Alg, CoffreLogiciel};
    use crate::validation::Decision;

    pub(crate) fn brouillon() -> Brouillon {
        Brouillon {
            type_: "ticket".into(),
            parc: "parc".into(),
            id: "t42".into(),
            titre: "Corriger la doc du pont".into(),
            plan: "1. lire\n2. corriger\n3. tester".into(),
            budget: Budget { secondes: 3600, cout_centimes: 200 },
            capacites: vec!["agent:code".into()],
            zones: vec!["pont/".into(), "docs/guide.md".into()],
        }
    }

    #[test]
    fn ticket_valide_emis_et_verifie() {
        let ord = CoffreLogiciel::generer(Alg::P256);
        let humain = CoffreLogiciel::generer(Alg::P256);
        let b = brouillon();
        let v = validation::preparer(&humain, "parc", &serde_json::to_value(&b).unwrap(), Decision::Accepte, None).unwrap();
        let t = emettre(&ord, &b, &v, &[humain.cle_publique()]).unwrap();
        assert_eq!(verifier(&t, &ord.cle_publique(), "parc", &[humain.cle_publique()]).unwrap(), b);
        // humain inconnu de la machine, faux ordonnateur
        let intrus = CoffreLogiciel::generer(Alg::P256);
        assert!(verifier(&t, &ord.cle_publique(), "parc", &[intrus.cle_publique()]).is_err());
        assert!(verifier(&t, &intrus.cle_publique(), "parc", &[humain.cle_publique()]).is_err());
    }

    #[test]
    fn plan_modifie_apres_validation_refuse() {
        let ord = CoffreLogiciel::generer(Alg::P256);
        let humain = CoffreLogiciel::generer(Alg::P256);
        let b = brouillon();
        let v = validation::preparer(&humain, "parc", &serde_json::to_value(&b).unwrap(), Decision::Accepte, None).unwrap();
        let mut pire = b.clone();
        pire.zones.push("/".into());
        assert!(emettre(&ord, &pire, &v, &[humain.cle_publique()]).is_err());
        let mut plus_large = b.clone();
        plus_large.capacites.push("secrets".into());
        assert!(emettre(&ord, &plus_large, &v, &[humain.cle_publique()]).is_err());
        // un ordonnateur compromis qui signe quand meme un ticket modifie : les machines le refusent
        let mut charge = serde_json::to_value(&plus_large).unwrap();
        charge["validation"] = serde_json::to_value(&v).unwrap();
        let faux = ObjetSigne::signer(&ord, charge).unwrap();
        assert!(verifier(&faux, &ord.cle_publique(), "parc", &[humain.cle_publique()]).is_err());
        // refus de l'humain
        let r = validation::preparer(&humain, "parc", &serde_json::to_value(&b).unwrap(), Decision::Refuse, None).unwrap();
        assert!(emettre(&ord, &b, &r, &[humain.cle_publique()]).is_err());
    }

    #[test]
    fn brouillon_borne() {
        let mut b = brouillon();
        b.zones = vec!["../etc/".into()];
        assert!(b.controler().is_err());
        let mut b = brouillon();
        b.titre = "a\u{7}b".into();
        assert!(b.controler().is_err());
        let mut b = brouillon();
        b.plan = "x".repeat(MAX_PLAN + 1);
        assert!(b.controler().is_err());
        let mut b = brouillon();
        b.id = "../t".into();
        assert!(b.controler().is_err());
    }
}
