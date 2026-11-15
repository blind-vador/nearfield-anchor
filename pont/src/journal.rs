//! Journal chaîné d'un ticket (lot2-fondations §3).
//!
//! Chaque entrée est signée par qui l'écrit et pointe sur l'empreinte de la
//! précédente (la première, sur celle du ticket). On ne peut que compléter le
//! journal : une entrée changée, retirée, ajoutée au milieu ou en double se voit.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::cles::{CleIdentite, Coffre};
use crate::objet::{nom_sur, ObjetSigne, AVANCE_MAX_SEC};
use crate::{jcs, Erreur, Resultat, VERSION};

pub const MAX_TEXTE: usize = 4000;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Genre {
    Prise,
    Progression,
    Resultat,
    Desaccord,
    ContreEpreuve,
    Validation,
    Note,
    Cloture,
}

/// Une entrée vérifiée.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Entree {
    #[serde(rename = "type")]
    pub type_: String,
    pub v: u64,
    pub parc: String,
    pub id: String,
    pub emis: i64,
    pub nonce: String,
    pub ticket: String,
    pub n: u64,
    pub precedent: String,
    pub genre: Genre,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub texte: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub donnees: Option<Value>,
}

/// Empreinte d'un objet signé complet (signature comprise) : sha256 hex de sa forme canonique.
pub fn empreinte_objet(o: &ObjetSigne) -> Resultat<String> {
    Ok(data_encoding::HEXLOWER.encode(&Sha256::digest(jcs::canonique(&serde_json::to_value(o)?)?)))
}

fn controler_contenu(texte: &Option<String>, donnees: &Option<Value>) -> Resultat<()> {
    if let Some(t) = texte {
        if t.len() > MAX_TEXTE || t.chars().any(|c| c.is_control() && c != '\n') {
            return Err(Erreur::Format("journal : texte".into()));
        }
    }
    crate::messages::donnees("donnees", donnees).map_err(|_| Erreur::Format("journal : données hors limites".into()))
}

/// Nouvelle entrée, à la suite de `precedente` (ou du ticket pour la première).
pub fn ecrire(
    auteur: &dyn Coffre,
    parc: &str,
    ticket: &ObjetSigne,
    precedente: Option<&ObjetSigne>,
    genre: Genre,
    texte: Option<&str>,
    donnees: Option<Value>,
) -> Resultat<ObjetSigne> {
    let id_ticket = ticket.champ_texte("id")?.to_string();
    nom_sur(&id_ticket)?;
    let (n, precedent) = match precedente {
        None => (0u64, empreinte_objet(ticket)?),
        Some(p) => (p.champ_entier("n")? as u64 + 1, empreinte_objet(p)?),
    };
    let texte = texte.map(str::to_string);
    controler_contenu(&texte, &donnees)?;
    let mut c = json!({"type": "journal", "parc": parc, "id": format!("{id_ticket}-{n}"), "ticket": id_ticket,
                       "n": n, "precedent": precedent, "genre": genre});
    if let Some(t) = texte {
        c["texte"] = Value::from(t);
    }
    if let Some(d) = donnees {
        c["donnees"] = d;
    }
    ObjetSigne::signer(auteur, c)
}

/// Vérifie un journal complet (entrées dans l'ordre de `n`). `cle_de` donne la clé
/// d'identité d'un signataire connu et non révoqué. Rend les entrées vérifiées.
pub fn verifier(
    ticket: &ObjetSigne,
    entrees: &[ObjetSigne],
    parc: &str,
    cle_de: &dyn Fn(&str) -> Option<CleIdentite>,
) -> Resultat<Vec<Entree>> {
    let id_ticket = ticket.champ_texte("id")?;
    let mut attendu = empreinte_objet(ticket)?;
    let mut dernier_emis = i64::MIN;
    let mut sortie = Vec::with_capacity(entrees.len());
    for (i, o) in entrees.iter().enumerate() {
        let cle = cle_de(&o.signataire).ok_or_else(|| Erreur::Signataire(o.signataire.clone()))?;
        o.verifier_signature(&cle)?;
        let e: Entree = serde_json::from_value(o.charge.clone())?;
        let faux = |quoi: &str| Err(Erreur::Format(format!("journal {id_ticket}, entrée {i} : {quoi}")));
        if e.type_ != "journal" || e.v != VERSION || e.parc != parc || e.ticket != id_ticket {
            return faux("type, version, parc ou ticket");
        }
        if e.n != i as u64 {
            return faux("numéro manquant ou en double (fourche)");
        }
        if e.id != format!("{id_ticket}-{}", e.n) {
            return faux("identifiant");
        }
        if e.precedent != attendu {
            return faux("chaîne rompue");
        }
        if e.emis + AVANCE_MAX_SEC < dernier_emis {
            return faux("date qui recule");
        }
        if sortie.last().map_or(false, |p: &Entree| p.genre == Genre::Cloture) {
            return faux("entrée après la clôture");
        }
        controler_contenu(&e.texte, &e.donnees)?;
        dernier_emis = dernier_emis.max(e.emis);
        attendu = empreinte_objet(o)?;
        sortie.push(e);
    }
    Ok(sortie)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cles::{Alg, CoffreLogiciel};

    struct Parc {
        ord: CoffreLogiciel,
        mac: CoffreLogiciel,
        ticket: ObjetSigne,
    }

    fn parc() -> Parc {
        let ord = CoffreLogiciel::generer(Alg::P256);
        let mac = CoffreLogiciel::generer(Alg::P256);
        let ticket = ObjetSigne::signer(&ord, json!({"type": "ticket", "parc": "p", "id": "t42"})).unwrap();
        Parc { ord, mac, ticket }
    }

    fn cles(p: &Parc) -> impl Fn(&str) -> Option<CleIdentite> + '_ {
        move |id| [p.ord.cle_publique(), p.mac.cle_publique()].into_iter().find(|c| c.id().unwrap() == id)
    }

    fn trois(p: &Parc) -> Vec<ObjetSigne> {
        let a = ecrire(&p.mac, "p", &p.ticket, None, Genre::Prise, Some("Le Mac prend le ticket."), None).unwrap();
        let b = ecrire(&p.mac, "p", &p.ticket, Some(&a), Genre::Progression, Some("Moitié faite."), Some(json!({"pct": 50}))).unwrap();
        let c = ecrire(&p.ord, "p", &p.ticket, Some(&b), Genre::Cloture, Some("Ticket clos."), None).unwrap();
        vec![a, b, c]
    }

    #[test]
    fn journal_continu_verifie() {
        let p = parc();
        let j = trois(&p);
        let e = verifier(&p.ticket, &j, "p", &cles(&p)).unwrap();
        assert_eq!(e.iter().map(|x| x.genre).collect::<Vec<_>>(), [Genre::Prise, Genre::Progression, Genre::Cloture]);
        assert_eq!(e[1].texte.as_deref(), Some("Moitié faite."));
    }

    #[test]
    fn retrait_echange_fourche_et_ajout_apres_cloture_refuses() {
        let p = parc();
        let j = trois(&p);
        assert!(verifier(&p.ticket, &[j[0].clone(), j[2].clone()], "p", &cles(&p)).is_err()); // trou
        assert!(verifier(&p.ticket, &[j[1].clone(), j[0].clone()], "p", &cles(&p)).is_err()); // echange
        // fourche : deux entrees n = 1
        let autre = ecrire(&p.mac, "p", &p.ticket, Some(&j[0]), Genre::Note, Some("Autre version."), None).unwrap();
        assert!(verifier(&p.ticket, &[j[0].clone(), j[1].clone(), autre], "p", &cles(&p)).is_err());
        // rien apres la cloture
        let apres = ecrire(&p.mac, "p", &p.ticket, Some(&j[2]), Genre::Note, Some("Encore."), None).unwrap();
        let mut long = j.clone();
        long.push(apres);
        assert!(verifier(&p.ticket, &long, "p", &cles(&p)).is_err());
    }

    #[test]
    fn entree_modifiee_inconnue_ou_d_un_autre_ticket_refusee() {
        let p = parc();
        let mut j = trois(&p);
        j[1].charge["texte"] = json!("Tout est fini.");
        assert!(verifier(&p.ticket, &j, "p", &cles(&p)).is_err());
        let p2 = parc();
        let j2 = trois(&p2);
        assert!(verifier(&p.ticket, &j2, "p", &cles(&p)).is_err()); // signataires inconnus
        let autre_ticket = ObjetSigne::signer(&p.ord, json!({"type": "ticket", "parc": "p", "id": "t43"})).unwrap();
        let j = trois(&p);
        assert!(verifier(&autre_ticket, &j, "p", &cles(&p)).is_err());
        assert!(ecrire(&p.mac, "p", &p.ticket, None, Genre::Note, Some("a\u{1}b"), None).is_err());
    }
}
