//! Certificats de rôle et liste de révocation (spec §4).
//!
//! L'ordonnateur signe, pour chaque machine, un certificat de 12 h qui lie sa clé
//! d'identité, sa clé de transport du jour, ses rôles, ses capacités et son niveau.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::cles::{CleIdentite, Coffre, Niveau};
use crate::objet::{ObjetSigne, AVANCE_MAX_SEC};
use crate::{de_b64, maintenant, Erreur, Resultat, VERSION};

/// Durée normale d'un certificat.
pub const DUREE_SEC: i64 = 12 * 3600;
/// Aucun certificat n'est accepté au-delà de cette durée de vie.
pub const DUREE_MAX_SEC: i64 = 24 * 3600;
/// Capacité qui permet de détenir des secrets (clés d'API…).
pub const CAPACITE_SECRETS: &str = "secrets";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Ordonnateur,
    Travailleur,
    Interface,
    /// Clé biométrique d'un appareil, qui signe les validations de l'humain.
    Humain,
}

/// Ce que l'ordonnateur certifie d'une machine.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Certificat {
    #[serde(rename = "type")]
    pub type_: String,
    pub v: u64,
    pub parc: String,
    pub id: String,
    pub emis: i64,
    pub nonce: String,
    pub sujet: String,
    pub nom: String,
    pub identite: CleIdentite,
    /// Clé publique X25519 de transport (base64), celle que Noise doit présenter.
    pub transport: String,
    pub roles: Vec<Role>,
    pub capacites: Vec<String>,
    pub niveau: Niveau,
    pub expire: i64,
    pub serie: u64,
}

/// Demande de certificat, préparée par l'ordonnateur.
pub struct Demande<'a> {
    pub nom: &'a str,
    pub identite: CleIdentite,
    pub niveau: Niveau,
    pub transport: &'a [u8],
    pub roles: &'a [Role],
    pub capacites: &'a [&'a str],
    pub serie: u64,
}

/// Le rôle d'ordonnateur et la détention de secrets exigent une clé dans une puce,
/// sauf tolérance explicite (ordonnateur logiciel de transition, annoncé à voix haute).
pub fn politique(roles: &[Role], capacites: &[String], niveau: Niveau, tolerance_logiciel: bool) -> Resultat<()> {
    let exigeant = roles.contains(&Role::Ordonnateur) || capacites.iter().any(|c| c == CAPACITE_SECRETS);
    if exigeant && niveau == Niveau::Logiciel && !tolerance_logiciel {
        return Err(Erreur::Certificat("ce rôle exige une clé dans une puce de sécurité".into()));
    }
    Ok(())
}

/// L'ordonnateur émet un certificat signé.
pub fn emettre(ordonnateur: &dyn Coffre, parc: &str, d: Demande, tolerance_logiciel: bool) -> Resultat<ObjetSigne> {
    let capacites: Vec<String> = d.capacites.iter().map(|c| c.to_string()).collect();
    politique(d.roles, &capacites, d.niveau, tolerance_logiciel)?;
    if d.transport.len() != 32 {
        return Err(Erreur::Certificat("clé de transport de 32 octets attendue".into()));
    }
    let sujet = d.identite.id()?;
    let charge = json!({
        "type": "certificat",
        "parc": parc,
        "id": format!("cert-{sujet}-{}", d.serie),
        "sujet": sujet,
        "nom": d.nom,
        "identite": d.identite,
        "transport": crate::b64(d.transport),
        "roles": d.roles,
        "capacites": capacites,
        "niveau": d.niveau,
        "expire": maintenant() + DUREE_SEC,
        "serie": d.serie,
    });
    ObjetSigne::signer(ordonnateur, charge)
}

/// Vérifie un certificat présenté par une machine ; rend son contenu s'il est valide.
pub fn verifier(
    objet: &ObjetSigne,
    ordonnateur: &CleIdentite,
    parc: &str,
    revocations: &ListeRevocations,
    tolerance_logiciel: bool,
    instant: i64,
) -> Resultat<Certificat> {
    objet.verifier_signature(ordonnateur).map_err(|_| Erreur::Certificat("pas signé par l'ordonnateur".into()))?;
    let c: Certificat = serde_json::from_value(objet.charge.clone())?;
    if c.type_ != "certificat" || c.v != VERSION {
        return Err(Erreur::Certificat("type ou version".into()));
    }
    if c.parc != parc {
        return Err(Erreur::Certificat("certificat d'un autre parc".into()));
    }
    if c.sujet != c.identite.id()? {
        return Err(Erreur::Certificat("le sujet ne correspond pas à la clé".into()));
    }
    if c.emis > instant + AVANCE_MAX_SEC || c.expire <= instant || c.expire - c.emis > DUREE_MAX_SEC {
        return Err(Erreur::Certificat("expiré ou durée invalide".into()));
    }
    if revocations.est_revoque(&c.sujet) {
        return Err(Erreur::Certificat(format!("machine révoquée : {}", c.nom)));
    }
    if de_b64(&c.transport)?.len() != 32 {
        return Err(Erreur::Certificat("clé de transport".into()));
    }
    politique(&c.roles, &c.capacites, c.niveau, tolerance_logiciel)?;
    Ok(c)
}

/// Délai maximal pendant lequel un certificat expiré permet encore de demander un
/// renouvellement (machine restée en veille), dans une session limitée à cela (§4.1).
pub const GRACE_MAX_SEC: i64 = 30 * 24 * 3600;

/// Comme [`verifier`], mais accepte un certificat **expiré depuis moins de `grace`**
/// secondes (signature, parc, révocation, politique toujours vérifiés). Rend
/// `(certificat, perime)` : une session ouverte avec `perime` ne doit servir qu'à
/// renouveler le certificat.
pub fn verifier_avec_grace(
    objet: &ObjetSigne,
    ordonnateur: &CleIdentite,
    parc: &str,
    revocations: &ListeRevocations,
    tolerance_logiciel: bool,
    instant: i64,
    grace: i64,
) -> Resultat<(Certificat, bool)> {
    match verifier(objet, ordonnateur, parc, revocations, tolerance_logiciel, instant) {
        Ok(c) => Ok((c, false)),
        Err(e) if grace > 0 => {
            objet.verifier_signature(ordonnateur).map_err(|_| Erreur::Certificat("pas signé par l'ordonnateur".into()))?;
            let c: Certificat = serde_json::from_value(objet.charge.clone())?;
            let grace = grace.min(GRACE_MAX_SEC);
            if c.expire <= instant && c.expire > instant - grace {
                Ok((verifier(objet, ordonnateur, parc, revocations, tolerance_logiciel, c.expire - 1)?, true))
            } else {
                Err(e)
            }
        }
        Err(e) => Err(e),
    }
}

/// Preuve de renouvellement : la clé d'identité de la machine (dans sa puce quand elle
/// en a une) signe sa nouvelle clé de transport. Exigée par l'ordonnateur pour tout
/// certificat demandé dans une session v2 (§4.1).
pub fn preparer_renouvellement(identite: &dyn Coffre, parc: &str, transport: &[u8]) -> Resultat<ObjetSigne> {
    if transport.len() != 32 {
        return Err(Erreur::Certificat("clé de transport de 32 octets attendue".into()));
    }
    let sujet = identite.cle_publique().id()?;
    ObjetSigne::signer(
        identite,
        json!({"type": "renouvellement", "parc": parc, "id": format!("renouvellement-{sujet}-{}", maintenant()),
               "transport": crate::b64(transport)}),
    )
}

/// Vérifie une preuve de renouvellement avec la clé d'identité connue de la machine et
/// rend la clé de transport qu'elle certifie, qui doit être celle de la demande.
pub fn verifier_renouvellement(
    preuve: &ObjetSigne,
    identite: &CleIdentite,
    parc: &str,
    transport_demande: &str,
    registre: Option<&mut crate::objet::RegistreNonces>,
) -> Resultat<Vec<u8>> {
    preuve.verifier(identite, "renouvellement", parc, registre)?;
    let t = preuve.champ_texte("transport")?;
    if t != transport_demande {
        return Err(Erreur::Certificat("la preuve porte une autre clé de transport".into()));
    }
    let octets = de_b64(t)?;
    if octets.len() != 32 {
        return Err(Erreur::Certificat("clé de transport".into()));
    }
    Ok(octets)
}

/// Liste de révocation : son numéro ne fait que croître.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ListeRevocations {
    pub numero: u64,
    pub sujets: Vec<String>,
}

impl ListeRevocations {
    pub fn est_revoque(&self, sujet: &str) -> bool {
        self.sujets.iter().any(|s| s == sujet)
    }

    /// L'ordonnateur publie une nouvelle liste (numéro suivant).
    pub fn publier(&self, ordonnateur: &dyn Coffre, parc: &str, sujets: Vec<String>) -> Resultat<(Self, ObjetSigne)> {
        let suivante = ListeRevocations { numero: self.numero + 1, sujets };
        let charge = json!({
            "type": "revocations",
            "parc": parc,
            "id": format!("revocations-{}", suivante.numero),
            "numero": suivante.numero,
            "sujets": suivante.sujets,
        });
        Ok((suivante.clone(), ObjetSigne::signer(ordonnateur, charge)?))
    }

    /// Une machine reçoit une liste : refusée si elle est plus ancienne que la sienne.
    pub fn recevoir(&mut self, objet: &ObjetSigne, ordonnateur: &CleIdentite, parc: &str) -> Resultat<()> {
        objet.verifier(ordonnateur, "revocations", parc, None)?;
        let numero = objet.champ_entier("numero")? as u64;
        if numero < self.numero {
            return Err(Erreur::Certificat("liste de révocation plus ancienne que la nôtre".into()));
        }
        let sujets: Vec<String> = serde_json::from_value(objet.charge.get("sujets").cloned().unwrap_or(Value::Null))?;
        *self = ListeRevocations { numero, sujets };
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cles::{Alg, CoffreLogiciel};

    struct Parc {
        ord: CoffreLogiciel,
        machine: CoffreLogiciel,
    }

    fn parc() -> Parc {
        Parc { ord: CoffreLogiciel::generer(Alg::P256), machine: CoffreLogiciel::generer(Alg::P256) }
    }

    fn demande<'a>(p: &Parc, roles: &'a [Role], niveau: Niveau) -> Demande<'a> {
        Demande {
            nom: "Mac mini",
            identite: p.machine.cle_publique(),
            niveau,
            transport: &[7; 32],
            roles,
            capacites: &["whisper"],
            serie: 1,
        }
    }

    #[test]
    fn emet_et_verifie() {
        let p = parc();
        let o = emettre(&p.ord, "parc1", demande(&p, &[Role::Travailleur], Niveau::Logiciel), false).unwrap();
        let c = verifier(&o, &p.ord.cle_publique(), "parc1", &ListeRevocations::default(), false, maintenant()).unwrap();
        assert_eq!(c.nom, "Mac mini");
        assert_eq!(c.sujet, p.machine.cle_publique().id().unwrap());
    }

    #[test]
    fn refuse_expire_autre_parc_et_faux_ordonnateur() {
        let p = parc();
        let o = emettre(&p.ord, "parc1", demande(&p, &[Role::Travailleur], Niveau::Logiciel), false).unwrap();
        let r = ListeRevocations::default();
        let k = p.ord.cle_publique();
        assert!(verifier(&o, &k, "parc1", &r, false, maintenant() + DUREE_SEC + 1).is_err());
        assert!(verifier(&o, &k, "parc2", &r, false, maintenant()).is_err());
        let intrus = CoffreLogiciel::generer(Alg::P256);
        assert!(verifier(&o, &intrus.cle_publique(), "parc1", &r, false, maintenant()).is_err());
    }

    #[test]
    fn ordonnateur_logiciel_refuse_sauf_tolerance() {
        let p = parc();
        assert!(emettre(&p.ord, "parc1", demande(&p, &[Role::Ordonnateur], Niveau::Logiciel), false).is_err());
        let o = emettre(&p.ord, "parc1", demande(&p, &[Role::Ordonnateur], Niveau::Logiciel), true).unwrap();
        let k = p.ord.cle_publique();
        let r = ListeRevocations::default();
        assert!(verifier(&o, &k, "parc1", &r, false, maintenant()).is_err());
        assert!(verifier(&o, &k, "parc1", &r, true, maintenant()).is_ok());
    }

    #[test]
    fn revocation_et_liste_plus_ancienne() {
        let p = parc();
        let o = emettre(&p.ord, "parc1", demande(&p, &[Role::Travailleur], Niveau::Logiciel), false).unwrap();
        let sujet = p.machine.cle_publique().id().unwrap();
        let (l1, obj1) = ListeRevocations::default().publier(&p.ord, "parc1", vec![]).unwrap();
        let (_l2, obj2) = l1.publier(&p.ord, "parc1", vec![sujet]).unwrap();
        let mut chez_machine = ListeRevocations::default();
        chez_machine.recevoir(&obj2, &p.ord.cle_publique(), "parc1").unwrap();
        assert!(verifier(&o, &p.ord.cle_publique(), "parc1", &chez_machine, false, maintenant()).is_err());
        assert!(chez_machine.recevoir(&obj1, &p.ord.cle_publique(), "parc1").is_err(), "retour en arrière refusé");
    }

    #[test]
    fn refuse_un_champ_inconnu_ajoute() {
        let p = parc();
        let mut charge = emettre(&p.ord, "parc1", demande(&p, &[Role::Travailleur], Niveau::Logiciel), false)
            .unwrap()
            .charge;
        charge["root"] = true.into();
        let o = ObjetSigne::signer(&p.ord, charge).unwrap();
        assert!(verifier(&o, &p.ord.cle_publique(), "parc1", &ListeRevocations::default(), false, maintenant()).is_err());
    }

    #[test]
    fn grace_pour_un_certificat_expire_et_preuve_de_renouvellement() {
        use crate::cles::{Alg, CoffreLogiciel};
        let ord = CoffreLogiciel::generer(Alg::P256);
        let mac = CoffreLogiciel::generer(Alg::P256);
        let roles = [Role::Travailleur];
        let o = emettre(&ord, "parc", Demande { nom: "Mac", identite: mac.cle_publique(), niveau: Niveau::Logiciel,
                        transport: &[1u8; 32], roles: &roles, capacites: &[], serie: 1 }, false).unwrap();
        let rev = ListeRevocations::default();
        let t = maintenant();
        let (_, perime) = verifier_avec_grace(&o, &ord.cle_publique(), "parc", &rev, false, t, 3600).unwrap();
        assert!(!perime);
        // Expire depuis 1 h : refuse sans grace, accepte (perime) avec 2 h de grace.
        let plus_tard = t + DUREE_SEC + 3600;
        assert!(verifier(&o, &ord.cle_publique(), "parc", &rev, false, plus_tard).is_err());
        let (_, perime) = verifier_avec_grace(&o, &ord.cle_publique(), "parc", &rev, false, plus_tard, 7200).unwrap();
        assert!(perime);
        assert!(verifier_avec_grace(&o, &ord.cle_publique(), "parc", &rev, false, plus_tard, 1800).is_err());
        // Jamais au-dela de GRACE_MAX_SEC, jamais pour une machine revoquee ou un faux ordonnateur.
        let bien_plus_tard = t + DUREE_SEC + GRACE_MAX_SEC + 10;
        assert!(verifier_avec_grace(&o, &ord.cle_publique(), "parc", &rev, false, bien_plus_tard, i64::MAX).is_err());
        let mut revoque = ListeRevocations::default();
        revoque.sujets.push(mac.cle_publique().id().unwrap());
        assert!(verifier_avec_grace(&o, &ord.cle_publique(), "parc", &revoque, false, plus_tard, 7200).is_err());
        let intrus = CoffreLogiciel::generer(Alg::P256);
        assert!(verifier_avec_grace(&o, &intrus.cle_publique(), "parc", &rev, false, plus_tard, 7200).is_err());
        // Preuve : signee par l'identite de la machine, pour CETTE cle de transport.
        let p = preparer_renouvellement(&mac, "parc", &[2u8; 32]).unwrap();
        let t2 = crate::b64(&[2u8; 32]);
        assert_eq!(verifier_renouvellement(&p, &mac.cle_publique(), "parc", &t2, None).unwrap(), vec![2u8; 32]);
        assert!(verifier_renouvellement(&p, &mac.cle_publique(), "parc", &crate::b64(&[3u8; 32]), None).is_err());
        assert!(verifier_renouvellement(&p, &intrus.cle_publique(), "parc", &t2, None).is_err());
        let mut reg = crate::objet::RegistreNonces::default();
        verifier_renouvellement(&p, &mac.cle_publique(), "parc", &t2, Some(&mut reg)).unwrap();
        assert!(verifier_renouvellement(&p, &mac.cle_publique(), "parc", &t2, Some(&mut reg)).is_err());
    }
}
