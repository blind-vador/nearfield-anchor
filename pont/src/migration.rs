//! Migration depuis la v1 sans nouvel appairage (spec §9).
//!
//! Une machine déjà appairée en v1 (clé Ed25519 dans `cle.pem`) annonce sa nouvelle
//! identité v2 par un objet `migration`, envoyé sur le canal v1 :
//!
//! - signé par sa **nouvelle** clé (objet signé ordinaire : elle prouve qu'elle la détient) ;
//! - contresigné par son **ancienne** clé v1 (`sig_v1`) : c'est ce qui transmet la confiance.
//!
//! La contresignature porte sur `DOMAINE || JCS(charge sans sig_v1)`. Le préfixe ne
//! commence pas par `{` : aucun message v1 signé (du JSON) ne peut servir de migration,
//! et réciproquement.
//!
//! Le même objet sert dans les deux sens : l'ordonnateur annonce d'abord sa clé v2
//! (les machines l'épinglent, son identifiant devient le `parc`), puis chaque machine
//! annonce la sienne et reçoit son premier certificat. Une ancienne clé ne migre
//! qu'une fois ([`RegistreMigrations`]).

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::cles::{Alg, CleIdentite, Coffre, Niveau};
use crate::objet::{ObjetSigne, RegistreNonces};
use crate::{aleatoire, b64, de_b64, jcs, maintenant, Erreur, Resultat, VERSION};

/// Préfixe de la contresignature v1 (séparation de domaine).
pub const DOMAINE: &[u8] = b"pf-migration-v1-v2\n";

/// Ce que transporte une migration vérifiée.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Migration {
    /// Clé v1 (Ed25519) qui transmet sa confiance.
    pub ancienne: CleIdentite,
    /// Nouvelle clé d'identité v2.
    pub identite: CleIdentite,
    /// Niveau déclaré par la machine pour sa nouvelle clé (pas d'attestation en v2).
    pub niveau: Niveau,
    pub nom: String,
    /// Clé X25519 de transport pour le premier certificat (absente pour l'ordonnateur).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transport: Option<String>,
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
    nom: String,
    #[serde(default)]
    transport: Option<String>,
    sig_v1: String,
}

fn verifier_nom(nom: &str) -> Resultat<()> {
    if nom.is_empty() || nom.len() > 128 || nom.chars().any(char::is_control) {
        return Err(Erreur::Migration("nom de machine invalide".into()));
    }
    Ok(())
}

fn message_v1(charge_sans_sig: &Value) -> Resultat<Vec<u8>> {
    let mut m = DOMAINE.to_vec();
    m.extend(jcs::canonique(charge_sans_sig)?);
    Ok(m)
}

/// Prépare l'objet `migration` : `ancien` est le coffre ouvert sur la `cle.pem` v1,
/// `nouveau` celui de l'identité v2 (puce de préférence).
pub fn preparer(
    ancien: &dyn Coffre,
    nouveau: &dyn Coffre,
    parc: &str,
    nom: &str,
    transport: Option<&[u8]>,
) -> Resultat<ObjetSigne> {
    let ancienne = ancien.cle_publique();
    let identite = nouveau.cle_publique();
    if ancienne.alg != Alg::Ed25519 {
        return Err(Erreur::Migration("l'ancienne clé doit être la clé Ed25519 de la v1".into()));
    }
    if ancienne == identite {
        return Err(Erreur::Migration("la nouvelle identité doit être une nouvelle clé".into()));
    }
    verifier_nom(nom)?;
    let mut charge = json!({
        "type": "migration",
        "v": VERSION,
        "parc": parc,
        "id": format!("migration-{}", identite.id()?),
        "emis": maintenant(),
        "nonce": b64(&aleatoire::<16>()),
        "ancienne": ancienne,
        "identite": identite,
        "niveau": nouveau.niveau(),
        "nom": nom,
    });
    if let Some(t) = transport {
        if t.len() != 32 {
            return Err(Erreur::Migration("clé de transport de 32 octets attendue".into()));
        }
        charge["transport"] = b64(t).into();
    }
    let sig_v1 = ancien.signer(&message_v1(&charge)?)?;
    charge["sig_v1"] = b64(&sig_v1).into();
    ObjetSigne::signer(nouveau, charge)
}

/// Lit, sans rien vérifier, l'ancienne clé annoncée : sert seulement à retrouver la
/// fiche v1 de la machine, dont la clé est ensuite passée à [`verifier`].
pub fn ancienne_annoncee(objet: &ObjetSigne) -> Resultat<CleIdentite> {
    let c = objet.charge.get("ancienne").cloned().ok_or_else(|| Erreur::Format("champ « ancienne »".into()))?;
    Ok(serde_json::from_value(c)?)
}

/// Vérifie une migration contre la clé v1 **déjà connue** de la machine (sa fiche
/// d'appairage v1). Rend la nouvelle identité.
pub fn verifier(
    objet: &ObjetSigne,
    parc: &str,
    ancienne_connue: &CleIdentite,
    registre: Option<&mut RegistreNonces>,
) -> Resultat<Migration> {
    let c: Charge = serde_json::from_value(objet.charge.clone())?;
    if c.ancienne != *ancienne_connue || c.ancienne.alg != Alg::Ed25519 {
        return Err(Erreur::Signataire("ancienne clé inconnue".into()));
    }
    if c.identite == c.ancienne {
        return Err(Erreur::Migration("la nouvelle identité doit être une nouvelle clé".into()));
    }
    // Signature par la nouvelle clé (preuve de possession), type, parc, version,
    // date et rejeu.
    objet.verifier(&c.identite, "migration", parc, registre)?;
    if c.id != format!("migration-{}", c.identite.id()?) {
        return Err(Erreur::Migration("identifiant d'objet".into()));
    }
    verifier_nom(&c.nom)?;
    if let Some(t) = &c.transport {
        if de_b64(t)?.len() != 32 {
            return Err(Erreur::Migration("clé de transport".into()));
        }
    }
    // Contresignature de l'ancienne clé : c'est elle qui transmet la confiance.
    let mut sans_sig = objet.charge.clone();
    sans_sig.as_object_mut().map(|m| m.remove("sig_v1"));
    c.ancienne
        .verifier(&message_v1(&sans_sig)?, &de_b64(&c.sig_v1)?)
        .map_err(|_| Erreur::Migration("contresignature de l'ancienne clé invalide".into()))?;
    Ok(Migration { ancienne: c.ancienne, identite: c.identite, niveau: c.niveau, nom: c.nom, transport: c.transport })
}

/// Migrations déjà faites : une ancienne clé ne migre qu'une fois. Après elle, la clé
/// v1 ne vaut plus rien, même volée.
#[derive(Default, Serialize, Deserialize)]
pub struct RegistreMigrations {
    /// Clé publique v1 (base64) → identifiant v2.
    faites: BTreeMap<String, String>,
}

impl RegistreMigrations {
    /// Consigne une migration vérifiée. Rejouer la même migration est sans effet ;
    /// migrer la même ancienne clé vers une autre identité est refusé.
    pub fn consigner(&mut self, m: &Migration) -> Resultat<()> {
        let nouvelle = m.identite.id()?;
        match self.faites.get(&m.ancienne.public) {
            Some(deja) if *deja == nouvelle => Ok(()),
            Some(_) => Err(Erreur::Migration("cette ancienne clé a déjà migré vers une autre identité".into())),
            None => {
                self.faites.insert(m.ancienne.public.clone(), nouvelle);
                Ok(())
            }
        }
    }

    /// Identifiant v2 vers lequel cette clé v1 a migré, s'il y en a un.
    pub fn migree_vers(&self, ancienne: &CleIdentite) -> Option<&str> {
        self.faites.get(&ancienne.public).map(String::as_str)
    }

    pub fn charger(chemin: &Path) -> Resultat<Self> {
        match std::fs::read(chemin) {
            Ok(o) => Ok(serde_json::from_slice(&o)?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e.into()),
        }
    }

    /// Écriture atomique (fichier temporaire puis renommage).
    pub fn enregistrer(&self, chemin: &Path) -> Resultat<()> {
        let tmp = chemin.with_extension("tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self)?)?;
        std::fs::rename(tmp, chemin)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cles::CoffreLogiciel;

    const PARC: &str = "parc-test";

    fn paire() -> (CoffreLogiciel, CoffreLogiciel) {
        (CoffreLogiciel::generer(Alg::Ed25519), CoffreLogiciel::generer(Alg::P256))
    }

    #[test]
    fn migre_dans_les_deux_sens() {
        let (v1, v2) = paire();
        // Machine : avec clé de transport.
        let o = preparer(&v1, &v2, PARC, "Mac mini", Some(&[7u8; 32])).unwrap();
        let mut r = RegistreNonces::default();
        assert_eq!(ancienne_annoncee(&o).unwrap(), v1.cle_publique());
        let m = verifier(&o, PARC, &v1.cle_publique(), Some(&mut r)).unwrap();
        assert_eq!(m.identite, v2.cle_publique());
        assert_eq!(m.niveau, Niveau::Logiciel);
        assert!(m.transport.is_some());
        // Ordonnateur : sans clé de transport.
        let o = preparer(&v1, &v2, PARC, "deepspace9", None).unwrap();
        assert!(verifier(&o, PARC, &v1.cle_publique(), None).unwrap().transport.is_none());
    }

    #[test]
    fn refuse_une_ancienne_cle_qui_n_est_pas_celle_de_la_fiche() {
        let (v1, v2) = paire();
        let autre = CoffreLogiciel::generer(Alg::Ed25519);
        let o = preparer(&v1, &v2, PARC, "Mac mini", None).unwrap();
        assert!(matches!(verifier(&o, PARC, &autre.cle_publique(), None), Err(Erreur::Signataire(_))));
    }

    #[test]
    fn un_intrus_sans_l_ancienne_cle_ne_peut_pas_detourner_l_identite() {
        // L'intrus intercepte une migration et remplace la nouvelle identité par la sienne,
        // en re-signant l'objet avec sa clé : la contresignature v1 ne correspond plus.
        let (v1, v2) = paire();
        let intrus = CoffreLogiciel::generer(Alg::P256);
        let o = preparer(&v1, &v2, PARC, "Mac mini", None).unwrap();
        let mut charge = o.charge.clone();
        charge["identite"] = serde_json::to_value(intrus.cle_publique()).unwrap();
        charge["id"] = format!("migration-{}", intrus.cle_publique().id().unwrap()).into();
        let falsifie = ObjetSigne::signer(&intrus, charge).unwrap();
        assert!(matches!(verifier(&falsifie, PARC, &v1.cle_publique(), None), Err(Erreur::Migration(_))));
        // Et sans re-signer : la signature de la nouvelle clé tombe.
        let mut brut = o.clone();
        brut.charge["nom"] = "Autre".into();
        assert!(verifier(&brut, PARC, &v1.cle_publique(), None).is_err());
    }

    #[test]
    fn refuse_rejeu_autre_parc_champ_en_trop_et_meme_cle() {
        let (v1, v2) = paire();
        let o = preparer(&v1, &v2, PARC, "Mac mini", None).unwrap();
        let mut r = RegistreNonces::default();
        verifier(&o, PARC, &v1.cle_publique(), Some(&mut r)).unwrap();
        assert!(matches!(verifier(&o, PARC, &v1.cle_publique(), Some(&mut r)), Err(Erreur::Rejeu)));
        assert!(verifier(&o, "autre-parc", &v1.cle_publique(), None).is_err());
        let mut charge = o.charge.clone();
        charge["commande"] = "rm -rf /".into();
        let o2 = ObjetSigne::signer(&v2, charge).unwrap();
        assert!(matches!(verifier(&o2, PARC, &v1.cle_publique(), None), Err(Erreur::Format(_))));
        assert!(preparer(&v1, &v1, PARC, "Mac mini", None).is_err());
        assert!(preparer(&v2, &v1, PARC, "Mac mini", None).is_err(), "l'ancienne clé doit être Ed25519");
        assert!(preparer(&v1, &v2, PARC, "Mac\nmini", None).is_err());
    }

    #[test]
    fn la_contresignature_v1_est_separee_par_domaine() {
        // Une signature v1 ordinaire (sur du JSON) ne vaut pas contresignature.
        let (v1, v2) = paire();
        let o = preparer(&v1, &v2, PARC, "Mac mini", None).unwrap();
        let mut sans_sig = o.charge.clone();
        sans_sig.as_object_mut().unwrap().remove("sig_v1");
        let sig_sans_domaine = v1.signer(&jcs::canonique(&sans_sig).unwrap()).unwrap();
        let mut charge = sans_sig;
        charge["sig_v1"] = b64(&sig_sans_domaine).into();
        let o2 = ObjetSigne::signer(&v2, charge).unwrap();
        assert!(matches!(verifier(&o2, PARC, &v1.cle_publique(), None), Err(Erreur::Migration(_))));
    }

    #[test]
    fn une_ancienne_cle_ne_migre_qu_une_fois() {
        let dossier = tempfile::tempdir().unwrap();
        let chemin = dossier.path().join("migrations.json");
        let (v1, v2) = paire();
        let m = verifier(&preparer(&v1, &v2, PARC, "Mac mini", None).unwrap(), PARC, &v1.cle_publique(), None).unwrap();
        let mut reg = RegistreMigrations::default();
        reg.consigner(&m).unwrap();
        reg.consigner(&m).unwrap(); // même migration : sans effet
        reg.enregistrer(&chemin).unwrap();
        let mut relu = RegistreMigrations::charger(&chemin).unwrap();
        assert_eq!(relu.migree_vers(&v1.cle_publique()), Some(v2.cle_publique().id().unwrap().as_str()));
        let vole = CoffreLogiciel::generer(Alg::P256);
        let m2 = verifier(&preparer(&v1, &vole, PARC, "Mac mini", None).unwrap(), PARC, &v1.cle_publique(), None).unwrap();
        assert!(matches!(relu.consigner(&m2), Err(Erreur::Migration(_))));
    }
}
