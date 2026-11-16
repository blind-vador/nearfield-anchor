//! Coffres matériels : la clé d'identité reste dans la puce de la machine (spec §2.1).
//!
//! La puce n'est pas pilotée par ce crate mais par un **programme local** propre à
//! chaque plate-forme, au protocole fermé :
//!
//! | commande                                  | sortie (une ligne)                      |
//! |-------------------------------------------|-----------------------------------------|
//! | `disponible`                              | code de sortie 0 si la puce répond      |
//! | `creer <poignee> [--presence]`            | clé publique P-256, SEC1 non compressée, base64 |
//! | `publique <poignee>`                      | idem                                    |
//! | `signer <poignee> [--raison <texte>]`     | signature ECDSA P-256/SHA-256 (r‖s ou DER), base64 ; le message arrive sur l'entrée standard |
//!
//! - Mac : `pf-enclave` (Swift, CryptoKit) ; la poignée est le blob chiffré que seule
//!   *cette* Secure Enclave sait utiliser. Avec `--presence`, chaque signature exige
//!   Touch ID ou le mot de passe de la session : c'est la clé humaine.
//! - Linux : `pf-tpm` (outils tpm2) ; la poignée est un dossier qui contient la clé
//!   enveloppée par le TPM.
//! - Sans puce (Raspberry Pi) : repli sur [`CoffreLogiciel`], niveau logiciel.
//!
//! Le programme reçoit des arguments fixes (jamais de shell), un environnement réduit,
//! et doit appartenir à l'utilisateur ou à root sans être modifiable par d'autres.
//! Chaque signature rendue est vérifiée avec la clé publique avant d'être utilisée.

use std::ffi::OsString;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use crate::cles::{Alg, CleIdentite, Coffre, CoffreLogiciel, Niveau};
use crate::{de_b64, Erreur, Resultat};

/// Plus grand message accepté à signer (les objets signés sont bien plus petits).
pub const MAX_MESSAGE: usize = 1 << 20;
/// Une signature avec présence attend l'humain (Touch ID, mot de passe).
pub const DELAI_PRESENCE: Duration = Duration::from_secs(120);
pub const DELAI_ORDINAIRE: Duration = Duration::from_secs(30);

/// Noms de fichier de l'identité dans le dossier d'une machine.
pub const FICHIER_PUCE: &str = "identite.puce";
pub const FICHIER_LOGICIEL: &str = "identite.pem";

/// Clé tenue par la puce, par l'intermédiaire de son programme local.
pub struct CoffreExterne {
    programme: PathBuf,
    poignee: PathBuf,
    publique: CleIdentite,
    presence: bool,
}

fn uid_courant() -> u32 {
    #[cfg(unix)]
    {
        // SAFETY: geteuid n'a pas de précondition et ne peut pas échouer.
        unsafe { libc::geteuid() }
    }
    #[cfg(not(unix))]
    {
        0
    }
}

/// Le programme doit être un vrai fichier, désigné par un chemin absolu, à nous ou à
/// root, et modifiable par personne d'autre.
pub fn verifier_programme(programme: &Path) -> Resultat<()> {
    if !programme.is_absolute() {
        return Err(Erreur::Cle("programme de puce : chemin absolu exigé".into()));
    }
    let m = std::fs::symlink_metadata(programme)
        .map_err(|e| Erreur::Cle(format!("programme de puce introuvable : {e}")))?;
    if !m.file_type().is_file() {
        return Err(Erreur::Cle("programme de puce : fichier ordinaire exigé (pas de lien)".into()));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if m.mode() & 0o022 != 0 {
            return Err(Erreur::Cle("programme de puce modifiable par d'autres".into()));
        }
        if m.uid() != 0 && m.uid() != uid_courant() {
            return Err(Erreur::Cle("programme de puce d'un autre utilisateur".into()));
        }
        if m.mode() & 0o100 == 0 {
            return Err(Erreur::Cle("programme de puce non exécutable".into()));
        }
    }
    Ok(())
}

fn raison_sure(raison: &str) -> Resultat<()> {
    if raison.is_empty() || raison.len() > 400 || raison.chars().any(char::is_control) {
        return Err(Erreur::Cle("raison de signature : 1 à 400 octets, sans caractère de contrôle".into()));
    }
    Ok(())
}

/// Lance le programme avec ces arguments, le message éventuel sur l'entrée standard ;
/// rend sa sortie standard (4 Kio au plus).
fn lancer(programme: &Path, args: &[OsString], entree: Option<&[u8]>, delai: Duration) -> Resultat<Vec<u8>> {
    verifier_programme(programme)?;
    let mut cmd = Command::new(programme);
    cmd.args(args)
        .env_clear()
        .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        .env("LANG", "C.UTF-8")
        .stdin(if entree.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Seule exception : le canal vers un TPM simulé (essais) ou un gestionnaire tpm2.
    if let Some(t) = std::env::var_os("TPM2TOOLS_TCTI") {
        cmd.env("TPM2TOOLS_TCTI", t);
    }
    let mut enfant = cmd.spawn().map_err(|e| Erreur::Cle(format!("programme de puce : {e}")))?;
    if let (Some(m), Some(mut sin)) = (entree, enfant.stdin.take()) {
        let m = m.to_vec();
        std::thread::spawn(move || {
            let _ = sin.write_all(&m);
        });
    }
    let mut sout = enfant.stdout.take().expect("stdout");
    let mut serr = enfant.stderr.take().expect("stderr");
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut sortie = Vec::new();
        let _ = (&mut sout).take(4096).read_to_end(&mut sortie);
        let mut erreur = Vec::new();
        let _ = (&mut serr).take(2048).read_to_end(&mut erreur);
        let _ = tx.send((sortie, erreur));
    });
    let (sortie, erreur) = match rx.recv_timeout(delai) {
        Ok(r) => r,
        Err(_) => {
            let _ = enfant.kill();
            let _ = enfant.wait();
            return Err(Erreur::Cle("la puce n'a pas répondu à temps".into()));
        }
    };
    let etat = enfant.wait()?;
    if !etat.success() {
        let texte = String::from_utf8_lossy(&erreur);
        let ligne = texte.lines().last().unwrap_or("").chars().take(200).collect::<String>();
        return Err(Erreur::Cle(format!("la puce a refusé ({}) {ligne}", etat.code().unwrap_or(-1))));
    }
    Ok(sortie)
}

fn cle_de_sortie(sortie: &[u8]) -> Resultat<CleIdentite> {
    let texte = std::str::from_utf8(sortie).map_err(|_| Erreur::Cle("sortie de la puce illisible".into()))?;
    let cle = CleIdentite { alg: Alg::P256, public: texte.trim().to_string() };
    cle.octets()?; // 65 octets, point valide vérifié à la première signature
    p256::ecdsa::VerifyingKey::from_sec1_bytes(&cle.octets()?).map_err(|_| Erreur::Cle("point P-256 invalide".into()))?;
    Ok(cle)
}

fn poignee_sure(poignee: &Path) -> Resultat<()> {
    if !poignee.is_absolute() {
        return Err(Erreur::Cle("poignée de puce : chemin absolu exigé".into()));
    }
    if let Ok(m) = std::fs::symlink_metadata(poignee) {
        if m.file_type().is_symlink() {
            return Err(Erreur::Cle("poignée de puce : lien symbolique refusé".into()));
        }
    }
    Ok(())
}

impl CoffreExterne {
    /// La puce répond-elle ? (Mac avec Secure Enclave, machine avec TPM 2.0.)
    pub fn disponible(programme: &Path) -> bool {
        lancer(programme, &["disponible".into()], None, DELAI_ORDINAIRE).is_ok()
    }

    /// Crée une clé dans la puce. `presence` : chaque signature exige l'humain.
    pub fn creer(programme: &Path, poignee: &Path, presence: bool) -> Resultat<Self> {
        poignee_sure(poignee)?;
        if poignee.exists() {
            return Err(Erreur::Cle("une clé existe déjà à cet endroit".into()));
        }
        let mut args: Vec<OsString> = vec!["creer".into(), poignee.into()];
        if presence {
            args.push("--presence".into());
        }
        let publique = cle_de_sortie(&lancer(programme, &args, None, DELAI_ORDINAIRE)?)?;
        Ok(CoffreExterne { programme: programme.into(), poignee: poignee.into(), publique, presence })
    }

    /// Ouvre une clé existante (la puce redonne sa clé publique).
    pub fn ouvrir(programme: &Path, poignee: &Path) -> Resultat<Self> {
        poignee_sure(poignee)?;
        let publique =
            cle_de_sortie(&lancer(programme, &["publique".into(), poignee.into()], None, DELAI_ORDINAIRE)?)?;
        Ok(CoffreExterne { programme: programme.into(), poignee: poignee.into(), publique, presence: false })
    }

    /// Indique que les signatures de cette clé attendront l'humain (délai plus long).
    pub fn avec_presence(mut self) -> Self {
        self.presence = true;
        self
    }

    fn signer_interne(&self, message: &[u8], raison: Option<&str>) -> Resultat<Vec<u8>> {
        if message.len() > MAX_MESSAGE {
            return Err(Erreur::Cle("message trop long pour la puce".into()));
        }
        let mut args: Vec<OsString> = vec!["signer".into(), self.poignee.clone().into()];
        if let Some(r) = raison {
            raison_sure(r)?;
            args.push("--raison".into());
            args.push(r.into());
        }
        let delai = if self.presence || raison.is_some() { DELAI_PRESENCE } else { DELAI_ORDINAIRE };
        let sortie = lancer(&self.programme, &args, Some(message), delai)?;
        let texte = std::str::from_utf8(&sortie).map_err(|_| Erreur::Cle("signature illisible".into()))?;
        let brute = de_b64(texte.trim())?;
        let sig = match brute.len() {
            64 => p256::ecdsa::Signature::from_slice(&brute),
            _ => p256::ecdsa::Signature::from_der(&brute),
        }
        .map_err(|_| Erreur::Cle("signature de la puce mal formée".into()))?;
        let sig = sig.normalize_s();
        let octets = sig.to_bytes().to_vec();
        // Une puce défaillante (ou un programme remplacé) ne fait pas passer une signature fausse.
        self.publique.verifier(message, &octets)?;
        Ok(octets)
    }
}

impl Coffre for CoffreExterne {
    fn cle_publique(&self) -> CleIdentite {
        self.publique.clone()
    }
    fn signer(&self, message: &[u8]) -> Resultat<Vec<u8>> {
        self.signer_interne(message, None)
    }
    fn niveau(&self) -> Niveau {
        Niveau::Materiel
    }
    fn signer_avec_raison(&self, message: &[u8], raison: &str) -> Resultat<Vec<u8>> {
        self.signer_interne(message, Some(raison))
    }
}

/// Clé d'identité de la machine dans `dossier` :
/// 1. `identite.puce` existe : la puce, obligatoirement (jamais de repli silencieux :
///    une machine dont la puce ne répond plus doit le dire) ;
/// 2. sinon `identite.pem` existe : la clé logicielle (machine d'avant la puce, ou sans) ;
/// 3. sinon on crée la clé : dans la puce si `programme` est donné et qu'elle répond,
///    sinon en logiciel (repli, niveau logiciel).
pub fn identite_machine(dossier: &Path, programme: Option<&Path>) -> Resultat<Box<dyn Coffre>> {
    let puce = dossier.join(FICHIER_PUCE);
    let pem = dossier.join(FICHIER_LOGICIEL);
    if std::fs::symlink_metadata(&puce).is_ok() {
        let p = programme.ok_or_else(|| Erreur::Cle("clé dans la puce, mais aucun programme pour la joindre".into()))?;
        return Ok(Box::new(CoffreExterne::ouvrir(p, &puce)?));
    }
    if pem.exists() {
        return Ok(Box::new(CoffreLogiciel::charger_ou_creer(&pem, Alg::P256)?));
    }
    std::fs::create_dir_all(dossier)?;
    if let Some(p) = programme {
        if CoffreExterne::disponible(p) {
            return Ok(Box::new(CoffreExterne::creer(p, &puce, false)?));
        }
    }
    Ok(Box::new(CoffreLogiciel::charger_ou_creer(&pem, Alg::P256)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refuse_un_programme_relatif_ou_absent() {
        assert!(verifier_programme(Path::new("pf-enclave")).is_err());
        assert!(verifier_programme(Path::new("/nexiste/pas/pf-enclave")).is_err());
        assert!(!CoffreExterne::disponible(Path::new("/nexiste/pas")));
    }

    #[test]
    fn refuse_un_programme_modifiable_par_tous() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("prog");
        std::fs::write(&p, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o777)).unwrap();
            assert!(verifier_programme(&p).is_err());
            std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
            verifier_programme(&p).unwrap();
            assert!(CoffreExterne::disponible(&p));
        }
    }

    #[test]
    fn repli_logiciel_sans_programme_et_pas_de_repli_si_la_puce_manque() {
        let d = tempfile::tempdir().unwrap();
        let c = identite_machine(d.path(), None).unwrap();
        assert_eq!(c.niveau(), Niveau::Logiciel);
        assert!(d.path().join(FICHIER_LOGICIEL).exists());
        // Relu à l'identique.
        assert_eq!(identite_machine(d.path(), None).unwrap().cle_publique(), c.cle_publique());
        // Une poignée de puce sans programme : erreur, jamais la clé logicielle.
        std::fs::write(d.path().join(FICHIER_PUCE), b"x").unwrap();
        assert!(identite_machine(d.path(), None).is_err());
    }

    #[test]
    fn raison_bornee() {
        assert!(raison_sure("Valider le ticket « essai »").is_ok());
        assert!(raison_sure("").is_err());
        assert!(raison_sure("a\nb").is_err());
        assert!(raison_sure(&"x".repeat(401)).is_err());
    }
}
