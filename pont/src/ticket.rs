//! Ticket : un manifeste validé par l'humain puis signé par l'ordonnateur
//! (lot2-fondations §2).
//!
//! Deux modes, choisis à l'ouverture (lot2-fondations §6) :
//! - `strict` : chaque sous-ticket (bloc confié à un agent) est validé par l'humain ;
//! - `lax` : la validation du ticket parent couvre ses sous-tickets, tant qu'ils restent
//!   dans ses bornes (zones, capacités, budget), vérifiées ici mécaniquement.

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

/// Mode de validation des sous-tickets.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Lax,
    Strict,
}

/// Rattachement d'un sous-ticket à son ticket parent (émis).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Parent {
    pub ticket: String,
    /// Empreinte de l'objet ticket parent complet (`journal::empreinte_objet`).
    pub empreinte: String,
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
    pub mode: Mode,
    pub budget: Budget,
    pub capacites: Vec<String>,
    pub zones: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent: Option<Parent>,
}

/// Un chemin (fichier ou dossier avec `/` final) est-il dans ces zones ? Une zone qui
/// finit par `/` couvre tout ce qui est dessous ; sinon elle ne couvre qu'elle-même.
pub fn dans_les_zones(zones: &[String], chemin: &str) -> bool {
    zones.iter().any(|z| z == chemin || (z.ends_with('/') && chemin.starts_with(z.as_str())))
}

/// Bornes d'un sous-ticket : même parc, zones et capacités incluses dans celles du
/// parent, budget au plus le sien, pas de mode plus lâche, un seul niveau.
pub fn controler_sous_ticket(parent: &Brouillon, sous: &Brouillon) -> Resultat<()> {
    let refus = |quoi: &str| Err(Erreur::Format(format!("sous-ticket : {quoi}")));
    sous.controler()?;
    if parent.parent.is_some() {
        return refus("un sous-ticket n'a pas de sous-ticket");
    }
    match &sous.parent {
        Some(p) if p.ticket == parent.id => {}
        _ => return refus("parent absent ou différent"),
    }
    if sous.parc != parent.parc || sous.id == parent.id {
        return refus("parc ou identifiant");
    }
    if let Some(z) = sous.zones.iter().find(|z| !dans_les_zones(&parent.zones, z)) {
        return Err(Erreur::Format(format!("sous-ticket : zone hors du ticket parent : {z}")));
    }
    if let Some(c) = sous.capacites.iter().find(|c| !parent.capacites.contains(c)) {
        return Err(Erreur::Format(format!("sous-ticket : capacité que le parent n'a pas : {c}")));
    }
    if sous.budget.secondes > parent.budget.secondes || sous.budget.cout_centimes > parent.budget.cout_centimes {
        return refus("budget plus grand que celui du parent");
    }
    if parent.mode == Mode::Strict && sous.mode == Mode::Lax {
        return refus("mode plus lâche que le parent");
    }
    Ok(())
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
/// de moins de 5 minutes, pour exactement ce brouillon). Un sous-ticket passe par
/// [`emettre_sous_ticket`].
pub fn emettre(ordonnateur: &dyn Coffre, brouillon: &Brouillon, validation: &ObjetSigne, humains: &[CleIdentite]) -> Resultat<ObjetSigne> {
    if brouillon.parent.is_some() {
        return Err(Erreur::Format("sous-ticket : à émettre avec son parent".into()));
    }
    emettre_valide(ordonnateur, brouillon, validation, humains)
}

fn emettre_valide(ordonnateur: &dyn Coffre, brouillon: &Brouillon, validation: &ObjetSigne, humains: &[CleIdentite]) -> Resultat<ObjetSigne> {
    brouillon.controler()?;
    let v = brouillon.valeur()?;
    validation::exiger(validation, humains, &brouillon.parc, &v, maintenant())?;
    let mut charge = v;
    charge["validation"] = serde_json::to_value(validation)?;
    ObjetSigne::signer(ordonnateur, charge)
}

/// Sous-ticket : vérifie ses bornes face au parent (ticket émis, vérifié). Avec une
/// validation humaine (obligatoire si le parent est strict ou si le sous-ticket l'est),
/// il est émis comme un ticket ; en mode lax sans validation, il porte `couvert_par`
/// (l'empreinte du parent) : c'est la validation du parent qui vaut pour lui.
pub fn emettre_sous_ticket(
    ordonnateur: &dyn Coffre,
    sous: &Brouillon,
    parent: &ObjetSigne,
    validation: Option<&ObjetSigne>,
    humains: &[CleIdentite],
) -> Resultat<ObjetSigne> {
    let p = verifier(parent, &ordonnateur.cle_publique(), &sous.parc, humains)?;
    controler_sous_ticket(&p, sous)?;
    if sous.parent.as_ref().map(|x| x.empreinte.as_str()) != Some(crate::journal::empreinte_objet(parent)?.as_str()) {
        return Err(Erreur::Format("sous-ticket : empreinte du parent".into()));
    }
    match validation {
        Some(v) => emettre_valide(ordonnateur, sous, v, humains),
        None if p.mode == Mode::Lax && sous.mode == Mode::Lax => {
            let mut charge = sous.valeur()?;
            charge["couvert_par"] = sous.parent.as_ref().unwrap().empreinte.clone().into();
            ObjetSigne::signer(ordonnateur, charge)
        }
        None => Err(Erreur::Format("sous-ticket en mode strict : validation humaine exigée".into())),
    }
}

/// Vérifie un sous-ticket avec son parent : parent valide, bornes, et soit sa propre
/// validation humaine, soit (mode lax) la couverture par le parent. Rend son brouillon.
pub fn verifier_sous_ticket(
    sous: &ObjetSigne,
    parent: &ObjetSigne,
    ordonnateur: &CleIdentite,
    parc: &str,
    humains: &[CleIdentite],
) -> Resultat<Brouillon> {
    let p = verifier(parent, ordonnateur, parc, humains)?;
    let empreinte = crate::journal::empreinte_objet(parent)?;
    let b = if sous.charge.get("validation").is_some() {
        verifier_valide(sous, ordonnateur, parc, humains)?
    } else {
        sous.verifier_signature(ordonnateur)?;
        if sous.champ_texte("type")? != "ticket" || sous.champ_texte("parc")? != parc || sous.champ_entier("v")? != VERSION as i64 {
            return Err(Erreur::Format("ticket : type, parc ou version".into()));
        }
        if sous.champ_texte("couvert_par")? != empreinte {
            return Err(Erreur::Format("sous-ticket : couvert par un autre parent".into()));
        }
        let mut charge = sous.charge.clone();
        let obj = charge.as_object_mut().ok_or_else(|| Erreur::Format("ticket".into()))?;
        for champ in ["v", "emis", "nonce", "couvert_par"] {
            obj.remove(champ);
        }
        let b: Brouillon = serde_json::from_value(charge)?;
        if p.mode != Mode::Lax || b.mode != Mode::Lax {
            return Err(Erreur::Format("sous-ticket strict sans sa propre validation".into()));
        }
        b
    };
    if b.parent.as_ref().map(|x| x.empreinte.as_str()) != Some(empreinte.as_str()) {
        return Err(Erreur::Format("sous-ticket : empreinte du parent".into()));
    }
    controler_sous_ticket(&p, &b)?;
    Ok(b)
}

/// Vérifie un ticket reçu : signé par l'ordonnateur épinglé, validé par un humain connu,
/// validation signée au plus 5 minutes avant le ticket. Rend le brouillon. Un sous-ticket
/// se vérifie avec son parent ([`verifier_sous_ticket`]).
pub fn verifier(ticket: &ObjetSigne, ordonnateur: &CleIdentite, parc: &str, humains: &[CleIdentite]) -> Resultat<Brouillon> {
    let b = verifier_valide(ticket, ordonnateur, parc, humains)?;
    if b.parent.is_some() {
        return Err(Erreur::Format("sous-ticket : à vérifier avec son parent".into()));
    }
    Ok(b)
}

fn verifier_valide(ticket: &ObjetSigne, ordonnateur: &CleIdentite, parc: &str, humains: &[CleIdentite]) -> Resultat<Brouillon> {
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
            mode: Mode::Lax,
            budget: Budget { secondes: 3600, cout_centimes: 200 },
            capacites: vec!["agent:code".into()],
            zones: vec!["pont/".into(), "docs/guide.md".into()],
            parent: None,
        }
    }

    fn emis(ord: &CoffreLogiciel, humain: &CoffreLogiciel, b: &Brouillon) -> ObjetSigne {
        let v = validation::preparer(humain, "parc", &serde_json::to_value(b).unwrap(), Decision::Accepte, None).unwrap();
        emettre(ord, b, &v, &[humain.cle_publique()]).unwrap()
    }

    fn sous(parent: &ObjetSigne, id: &str, zones: &[&str], mode: Mode) -> Brouillon {
        Brouillon {
            id: id.into(),
            titre: "Bloc".into(),
            plan: "Faire le bloc.".into(),
            mode,
            budget: Budget { secondes: 600, cout_centimes: 20 },
            zones: zones.iter().map(|z| z.to_string()).collect(),
            parent: Some(Parent { ticket: "t42".into(), empreinte: crate::journal::empreinte_objet(parent).unwrap() }),
            ..brouillon()
        }
    }

    #[test]
    fn zones() {
        let z = vec!["pont/".to_string(), "docs/guide.md".to_string()];
        assert!(dans_les_zones(&z, "pont/src/lib.rs"));
        assert!(dans_les_zones(&z, "docs/guide.md"));
        assert!(!dans_les_zones(&z, "docs/autre.md"));
        assert!(!dans_les_zones(&z, "pontage/x"));
    }

    #[test]
    fn sous_ticket_lax_couvert_par_le_parent() {
        let ord = CoffreLogiciel::generer(Alg::P256);
        let humain = CoffreLogiciel::generer(Alg::P256);
        let h = [humain.cle_publique()];
        let parent = emis(&ord, &humain, &brouillon());
        let s = sous(&parent, "t42-1", &["pont/src/"], Mode::Lax);
        let t = emettre_sous_ticket(&ord, &s, &parent, None, &h).unwrap();
        assert_eq!(verifier_sous_ticket(&t, &parent, &ord.cle_publique(), "parc", &h).unwrap(), s);
        assert!(verifier(&t, &ord.cle_publique(), "parc", &h).is_err(), "sans son parent : refusé");
        // Hors des zones, capacité ou budget de plus : refusé.
        assert!(emettre_sous_ticket(&ord, &sous(&parent, "t42-2", &["lib/"], Mode::Lax), &parent, None, &h).is_err());
        let mut plus = sous(&parent, "t42-3", &["pont/"], Mode::Lax);
        plus.capacites.push("reseau".into());
        assert!(emettre_sous_ticket(&ord, &plus, &parent, None, &h).is_err());
        let mut cher = sous(&parent, "t42-4", &["pont/"], Mode::Lax);
        cher.budget.cout_centimes = 1000;
        assert!(emettre_sous_ticket(&ord, &cher, &parent, None, &h).is_err());
        // Un autre parent (même id, autre ticket) : refusé.
        let autre = emis(&ord, &humain, &Brouillon { titre: "Autre".into(), ..brouillon() });
        assert!(verifier_sous_ticket(&t, &autre, &ord.cle_publique(), "parc", &h).is_err());
    }

    #[test]
    fn sous_ticket_strict_exige_sa_validation() {
        let ord = CoffreLogiciel::generer(Alg::P256);
        let humain = CoffreLogiciel::generer(Alg::P256);
        let h = [humain.cle_publique()];
        let parent = emis(&ord, &humain, &Brouillon { mode: Mode::Strict, ..brouillon() });
        let s = sous(&parent, "t42-1", &["pont/"], Mode::Strict);
        assert!(emettre_sous_ticket(&ord, &s, &parent, None, &h).is_err());
        let lache = sous(&parent, "t42-2", &["pont/"], Mode::Lax);
        let v = validation::preparer(&humain, "parc", &serde_json::to_value(&lache).unwrap(), Decision::Accepte, None).unwrap();
        assert!(emettre_sous_ticket(&ord, &lache, &parent, Some(&v), &h).is_err(), "mode plus lâche");
        let v = validation::preparer(&humain, "parc", &serde_json::to_value(&s).unwrap(), Decision::Accepte, None).unwrap();
        let t = emettre_sous_ticket(&ord, &s, &parent, Some(&v), &h).unwrap();
        assert_eq!(verifier_sous_ticket(&t, &parent, &ord.cle_publique(), "parc", &h).unwrap(), s);
        // Un ordonnateur compromis qui le signe « couvert » sans validation : refusé.
        let mut charge = serde_json::to_value(&s).unwrap();
        charge["couvert_par"] = crate::journal::empreinte_objet(&parent).unwrap().into();
        let faux = ObjetSigne::signer(&ord, charge).unwrap();
        assert!(verifier_sous_ticket(&faux, &parent, &ord.cle_publique(), "parc", &h).is_err());
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
