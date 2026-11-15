//! Objets signés déposés sur un stockage partagé (spec §6).
//!
//! Le NAS, le VPS ou l'hébergeur Git n'ont aucune autorité : un objet n'est cru que
//! si sa signature est valide, que son signataire est connu, qu'il est récent et
//! qu'il n'a pas déjà été consommé.

use std::collections::HashMap;
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::cles::{Alg, CleIdentite, Coffre};
use crate::{aleatoire, b64, de_b64, jcs, maintenant, Erreur, Resultat, VERSION};

/// Âge maximal d'un objet accepté hors ligne.
pub const AGE_MAX_SEC: i64 = 7 * 24 * 3600;
/// Écart d'horloge toléré vers le futur.
pub const AVANCE_MAX_SEC: i64 = 300;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObjetSigne {
    pub charge: Value,
    pub signataire: String,
    pub alg: Alg,
    pub sig: String,
}

impl ObjetSigne {
    /// Signe une charge. `type`, `parc` et `id` sont obligatoires ; `v`, `emis` et
    /// `nonce` sont ajoutés s'ils manquent.
    pub fn signer(coffre: &dyn Coffre, charge: Value) -> Resultat<Self> {
        let Value::Object(mut champs) = charge else {
            return Err(Erreur::Format("la charge doit être un objet".into()));
        };
        for requis in ["type", "parc", "id"] {
            if !champs.get(requis).map_or(false, Value::is_string) {
                return Err(Erreur::Format(format!("champ « {requis} » manquant")));
            }
        }
        champs.entry("v").or_insert(Value::from(VERSION));
        champs.entry("emis").or_insert(Value::from(maintenant()));
        champs.entry("nonce").or_insert(Value::from(b64(&aleatoire::<16>())));
        let charge = Value::Object(champs);
        let cle = coffre.cle_publique();
        let sig = coffre.signer(&jcs::canonique(&charge)?)?;
        Ok(ObjetSigne { charge, signataire: cle.id()?, alg: cle.alg, sig: b64(&sig) })
    }

    /// Vérifie la signature avec la clé attendue du signataire.
    pub fn verifier_signature(&self, cle: &CleIdentite) -> Resultat<()> {
        if cle.id()? != self.signataire || cle.alg != self.alg {
            return Err(Erreur::Signataire(self.signataire.clone()));
        }
        cle.verifier(&jcs::canonique(&self.charge)?, &de_b64(&self.sig)?)
    }

    pub fn champ_texte(&self, nom: &str) -> Resultat<&str> {
        self.charge.get(nom).and_then(Value::as_str).ok_or_else(|| Erreur::Format(format!("champ « {nom} »")))
    }

    pub fn champ_entier(&self, nom: &str) -> Resultat<i64> {
        self.charge.get(nom).and_then(Value::as_i64).ok_or_else(|| Erreur::Format(format!("champ « {nom} »")))
    }

    /// Vérification complète : signature, type, parc, date, anti-rejeu.
    pub fn verifier(
        &self,
        cle: &CleIdentite,
        type_attendu: &str,
        parc: &str,
        registre: Option<&mut RegistreNonces>,
    ) -> Resultat<()> {
        self.verifier_signature(cle)?;
        if self.champ_texte("type")? != type_attendu {
            return Err(Erreur::Format(format!("type attendu : {type_attendu}")));
        }
        if self.champ_texte("parc")? != parc {
            return Err(Erreur::Format("objet d'un autre parc".into()));
        }
        if self.champ_entier("v")? != VERSION as i64 {
            return Err(Erreur::Format("version de protocole".into()));
        }
        let emis = self.champ_entier("emis")?;
        if let Some(r) = registre {
            r.accepter(&self.signataire, self.champ_texte("nonce")?, emis, maintenant())?;
        } else {
            verifier_date(emis, maintenant())?;
        }
        Ok(())
    }

    /// Vérifie les fichiers joints (`fichiers: {chemin: sha256 hex}`) dans `dossier`.
    /// Un chemin est relatif au dossier (`chemin_sur`) et ne traverse aucun lien
    /// symbolique : on ne lit jamais hors du dossier.
    pub fn verifier_fichiers(&self, dossier: &Path) -> Resultat<()> {
        let Some(fichiers) = self.charge.get("fichiers") else { return Ok(()) };
        let fichiers = fichiers.as_object().ok_or_else(|| Erreur::Format("fichiers".into()))?;
        for (nom, attendu) in fichiers {
            chemin_sur(nom)?;
            let mut chemin = dossier.to_path_buf();
            for morceau in nom.split('/') {
                chemin.push(morceau);
                if std::fs::symlink_metadata(&chemin)?.file_type().is_symlink() {
                    return Err(Erreur::Chemin(nom.clone()));
                }
            }
            let attendu = attendu.as_str().ok_or_else(|| Erreur::Format("empreinte".into()))?;
            if empreinte_fichier(&chemin)? != attendu {
                return Err(Erreur::Empreinte(nom.clone()));
            }
        }
        Ok(())
    }
}

fn verifier_date(emis: i64, maintenant: i64) -> Resultat<()> {
    if emis > maintenant + AVANCE_MAX_SEC || emis < maintenant - AGE_MAX_SEC {
        return Err(Erreur::Date);
    }
    Ok(())
}

/// SHA-256 (hexadécimal) d'un fichier.
pub fn empreinte_fichier(chemin: &Path) -> Resultat<String> {
    use std::io::Read;
    let mut f = std::fs::File::open(chemin)?;
    let mut h = Sha256::new();
    let mut bloc = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut bloc)?;
        if n == 0 {
            break;
        }
        h.update(&bloc[..n]);
    }
    Ok(data_encoding::HEXLOWER.encode(&h.finalize()))
}

/// Un nom de fichier simple : ni dossier, ni remontée, ni fichier caché.
pub fn nom_sur(nom: &str) -> Resultat<()> {
    let refuse = nom.is_empty()
        || nom.len() > 200
        || nom.starts_with('.')
        || nom.contains(['/', '\\', '\0', ':'])
        || nom.chars().any(char::is_control);
    if refuse {
        return Err(Erreur::Chemin(nom.to_string()));
    }
    Ok(())
}

/// Un chemin relatif sûr : 1 à 4 noms simples (`nom_sur`) séparés par `/`.
pub fn chemin_sur(chemin: &str) -> Resultat<()> {
    let morceaux: Vec<&str> = chemin.split('/').collect();
    if chemin.len() > 400 || morceaux.len() > 4 {
        return Err(Erreur::Chemin(chemin.to_string()));
    }
    morceaux.into_iter().try_for_each(nom_sur)
}

/// Registre des `(signataire, nonce)` déjà consommés, gardés 7 jours (spec §6).
#[derive(Default, Serialize, Deserialize)]
pub struct RegistreNonces {
    vus: HashMap<String, i64>,
}

impl RegistreNonces {
    pub fn accepter(&mut self, signataire: &str, nonce: &str, emis: i64, maintenant: i64) -> Resultat<()> {
        verifier_date(emis, maintenant)?;
        self.vus.retain(|_, e| *e >= maintenant - AGE_MAX_SEC);
        let cle = format!("{signataire}:{nonce}");
        if self.vus.contains_key(&cle) {
            return Err(Erreur::Rejeu);
        }
        self.vus.insert(cle, emis);
        Ok(())
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
        std::fs::write(&tmp, serde_json::to_vec(self)?)?;
        std::fs::rename(tmp, chemin)?;
        Ok(())
    }
}

/// Construit une charge à partir de paires (aide pour les appelants).
pub fn charge(type_: &str, parc: &str, id: &str, autres: Value) -> Value {
    let mut m = Map::new();
    m.insert("type".into(), type_.into());
    m.insert("parc".into(), parc.into());
    m.insert("id".into(), id.into());
    if let Value::Object(a) = autres {
        m.extend(a);
    }
    Value::Object(m)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cles::CoffreLogiciel;
    use serde_json::json;

    fn travail(c: &CoffreLogiciel) -> ObjetSigne {
        ObjetSigne::signer(c, charge("travail", "parc1", "t1", json!({"genre": "whisper"}))).unwrap()
    }

    #[test]
    fn signe_et_verifie() {
        let c = CoffreLogiciel::generer(Alg::P256);
        let o = travail(&c);
        let mut r = RegistreNonces::default();
        o.verifier(&c.cle_publique(), "travail", "parc1", Some(&mut r)).unwrap();
    }

    #[test]
    fn refuse_le_rejeu() {
        let c = CoffreLogiciel::generer(Alg::Ed25519);
        let o = travail(&c);
        let mut r = RegistreNonces::default();
        o.verifier(&c.cle_publique(), "travail", "parc1", Some(&mut r)).unwrap();
        assert!(matches!(o.verifier(&c.cle_publique(), "travail", "parc1", Some(&mut r)), Err(Erreur::Rejeu)));
    }

    #[test]
    fn refuse_une_charge_modifiee() {
        let c = CoffreLogiciel::generer(Alg::P256);
        let mut o = travail(&c);
        o.charge["genre"] = json!("shell");
        assert!(matches!(o.verifier_signature(&c.cle_publique()), Err(Erreur::Signature)));
    }

    #[test]
    fn refuse_autre_signataire_autre_parc_autre_type() {
        let c = CoffreLogiciel::generer(Alg::P256);
        let intrus = CoffreLogiciel::generer(Alg::P256);
        let o = travail(&c);
        assert!(o.verifier(&intrus.cle_publique(), "travail", "parc1", None).is_err());
        assert!(o.verifier(&c.cle_publique(), "travail", "parc2", None).is_err());
        assert!(o.verifier(&c.cle_publique(), "resultat", "parc1", None).is_err());
    }

    #[test]
    fn refuse_les_objets_trop_vieux_ou_futurs() {
        let mut r = RegistreNonces::default();
        let t = 1_000_000_000;
        assert!(matches!(r.accepter("a", "n1", t - AGE_MAX_SEC - 1, t), Err(Erreur::Date)));
        assert!(matches!(r.accepter("a", "n2", t + AVANCE_MAX_SEC + 1, t), Err(Erreur::Date)));
        r.accepter("a", "n3", t, t).unwrap();
    }

    #[test]
    fn verifie_les_fichiers_joints_et_les_noms() {
        let dossier = tempfile::tempdir().unwrap();
        std::fs::write(dossier.path().join("son.wav"), b"abc").unwrap();
        let empreinte = empreinte_fichier(&dossier.path().join("son.wav")).unwrap();
        let c = CoffreLogiciel::generer(Alg::P256);
        let o = ObjetSigne::signer(&c, charge("resultat", "p", "r1", json!({"fichiers": {"son.wav": empreinte}}))).unwrap();
        o.verifier_fichiers(dossier.path()).unwrap();
        std::fs::write(dossier.path().join("son.wav"), b"abd").unwrap();
        assert!(matches!(o.verifier_fichiers(dossier.path()), Err(Erreur::Empreinte(_))));
        for mauvais in ["../x", "a/b", ".cache", "", "a\\b", "c:d"] {
            assert!(nom_sur(mauvais).is_err(), "{mauvais}");
        }
    }

    #[test]
    fn chemins_relatifs_surs_et_liens_refuses() {
        for bon in ["a.wav", "audio/a.wav", "a/b/c/d"] {
            chemin_sur(bon).unwrap();
        }
        for mauvais in ["../x", "a/../b", "/a", "a/", "a//b", "a/.b", "a/b/c/d/e", "a\\b", ""] {
            assert!(chemin_sur(mauvais).is_err(), "{mauvais}");
        }
        let dossier = tempfile::tempdir().unwrap();
        std::fs::create_dir(dossier.path().join("audio")).unwrap();
        std::fs::write(dossier.path().join("audio/a.wav"), b"abc").unwrap();
        let e = empreinte_fichier(&dossier.path().join("audio/a.wav")).unwrap();
        let c = CoffreLogiciel::generer(Alg::P256);
        let o = ObjetSigne::signer(&c, charge("resultat", "p", "r2", json!({"fichiers": {"audio/a.wav": e}}))).unwrap();
        o.verifier_fichiers(dossier.path()).unwrap();
        // Un lien vers un fichier hors du dossier, de même contenu, est refusé.
        let dehors = tempfile::tempdir().unwrap();
        std::fs::write(dehors.path().join("a.wav"), b"abc").unwrap();
        std::fs::remove_file(dossier.path().join("audio/a.wav")).unwrap();
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(dehors.path().join("a.wav"), dossier.path().join("audio/a.wav")).unwrap();
            assert!(matches!(o.verifier_fichiers(dossier.path()), Err(Erreur::Chemin(_))));
            std::fs::remove_file(dossier.path().join("audio/a.wav")).unwrap();
            std::fs::remove_dir(dossier.path().join("audio")).unwrap();
            std::os::unix::fs::symlink(dehors.path(), dossier.path().join("audio")).unwrap();
            assert!(matches!(o.verifier_fichiers(dossier.path()), Err(Erreur::Chemin(_))));
        }
    }

    #[test]
    fn registre_survit_au_redemarrage() {
        let dossier = tempfile::tempdir().unwrap();
        let chemin = dossier.path().join("nonces.json");
        let mut r = RegistreNonces::default();
        let t = maintenant();
        r.accepter("a", "n", t, t).unwrap();
        r.enregistrer(&chemin).unwrap();
        let mut relu = RegistreNonces::charger(&chemin).unwrap();
        assert!(matches!(relu.accepter("a", "n", t, t), Err(Erreur::Rejeu)));
    }
}
