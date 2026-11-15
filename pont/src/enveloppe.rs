//! Enveloppe chiffrée à destinataires multiples (spec §6.2).
//!
//! X25519 éphémère + HKDF-SHA256 pour chaque destinataire, qui enveloppe une clé de
//! contenu ; contenu en blocs ChaCha20-Poly1305 (construction STREAM) liés à l'entête.
//! Chaque machine reçoit sur une **clé de réception** X25519, publiée chaque semaine
//! dans un objet signé par sa clé d'identité (`cle_reception`).

use std::io::{Read, Write};
use std::path::Path;

use chacha20poly1305::aead::{Aead, KeyInit, Payload};
use chacha20poly1305::{ChaCha20Poly1305, Key, Nonce};
use hkdf::Hkdf;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use x25519_dalek::{PublicKey, StaticSecret};
use zeroize::Zeroizing;

use crate::cles::{CleIdentite, Coffre};
use crate::objet::ObjetSigne;
use crate::{aleatoire, b64, de_b64, ecrire_prive, jcs, maintenant, Erreur, Resultat};

pub const ALG: &str = "x25519-hkdf-sha256-chacha20poly1305";
pub const BLOC: usize = 64 * 1024;
pub const MAX_DESTINATAIRES: usize = 64;
pub const MAX_CONTEXTE: usize = 200;
const TAG: usize = 16;
const INFO_KEK: &[u8] = b"pf-enveloppe-v2 cle";
const AAD_DEST: &[u8] = b"pf-enveloppe-v2 dest";
const AAD_CONTENU: &[u8] = b"pf-enveloppe-v2\n";

// ------------------------------------------------------------------ clé de réception

/// Clé X25519 de réception d'une machine. La partie privée ne sort jamais d'ici.
pub struct CleReception {
    secret: StaticSecret,
    pub public: [u8; 32],
}

impl CleReception {
    pub fn generer() -> Self {
        let secret = StaticSecret::from(aleatoire::<32>());
        let public = PublicKey::from(&secret).to_bytes();
        CleReception { secret, public }
    }

    /// Relit la clé du fichier (32 octets bruts), ou la crée en 0600.
    pub fn charger_ou_creer(chemin: &Path) -> Resultat<Self> {
        match std::fs::read(chemin) {
            Ok(o) => {
                let o = Zeroizing::new(o);
                let octets: [u8; 32] =
                    o.as_slice().try_into().map_err(|_| Erreur::Cle("clé de réception : 32 octets attendus".into()))?;
                let secret = StaticSecret::from(octets);
                let public = PublicKey::from(&secret).to_bytes();
                Ok(CleReception { secret, public })
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                let cle = Self::generer();
                ecrire_prive(chemin, cle.secret.as_bytes())?;
                Ok(cle)
            }
            Err(e) => Err(e.into()),
        }
    }

    /// Empreinte publique (sha256 hex de la clé), qui désigne l'entrée d'un destinataire.
    pub fn empreinte(&self) -> String {
        empreinte_cle(&self.public)
    }

    /// Objet `cle_reception` signé par l'identité de la machine (vaut 7 jours, §6).
    pub fn publier(&self, coffre: &dyn Coffre, parc: &str) -> Resultat<ObjetSigne> {
        let id = coffre.cle_publique().id()?;
        ObjetSigne::signer(
            coffre,
            json!({"type": "cle_reception", "parc": parc, "id": format!("reception-{id}-{}", maintenant()),
                   "cle": b64(&self.public)}),
        )
    }
}

pub fn empreinte_cle(public: &[u8]) -> String {
    data_encoding::HEXLOWER.encode(&Sha256::digest(public))
}

/// Vérifie une publication de clé de réception (signature de `identite`, parc, 7 jours)
/// et rend la clé publique X25519.
pub fn verifier_cle_reception(objet: &ObjetSigne, identite: &CleIdentite, parc: &str) -> Resultat<[u8; 32]> {
    objet.verifier(identite, "cle_reception", parc, None)?;
    let cle = de_b64(objet.champ_texte("cle")?)?;
    cle.as_slice().try_into().map_err(|_| Erreur::Format("clé de réception : 32 octets".into()))
}

// ------------------------------------------------------------------ format

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Destinataire {
    pub id: String,
    pub cle: String,
    pub enc: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Enveloppe {
    pub alg: String,
    pub de: String,
    pub contexte: String,
    pub eph: String,
    pub destinataires: Vec<Destinataire>,
    pub prefixe: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub chiffre: Option<String>,
}

impl Enveloppe {
    /// Données associées du contenu : préfixe ‖ JCS(entête sans "chiffre").
    fn aad(&self) -> Resultat<Vec<u8>> {
        let mut entete = self.clone();
        entete.chiffre = None;
        let mut a = AAD_CONTENU.to_vec();
        a.extend(jcs::canonique(&serde_json::to_value(&entete)?)?);
        Ok(a)
    }

    fn controler(&self) -> Resultat<()> {
        if self.alg != ALG {
            return Err(Erreur::Format(format!("algorithme d'enveloppe : {}", self.alg)));
        }
        if self.destinataires.is_empty() || self.destinataires.len() > MAX_DESTINATAIRES {
            return Err(Erreur::Format("nombre de destinataires".into()));
        }
        if self.contexte.len() > MAX_CONTEXTE || self.contexte.chars().any(char::is_control) {
            return Err(Erreur::Format("contexte".into()));
        }
        Ok(())
    }
}

fn kek(eph_pub: &[u8; 32], dest: &[u8; 32], partage: &[u8; 32]) -> Resultat<Zeroizing<[u8; 32]>> {
    if partage.iter().all(|o| *o == 0) {
        return Err(Erreur::Format("clé de réception de faible ordre".into()));
    }
    let mut sel = [0u8; 64];
    sel[..32].copy_from_slice(eph_pub);
    sel[32..].copy_from_slice(dest);
    let mut sortie = Zeroizing::new([0u8; 32]);
    Hkdf::<Sha256>::new(Some(&sel), partage)
        .expand(INFO_KEK, sortie.as_mut())
        .map_err(|_| Erreur::Format("hkdf".into()))?;
    Ok(sortie)
}

fn aad_dest(eph_pub: &[u8; 32], dest: &[u8; 32]) -> Vec<u8> {
    [AAD_DEST, eph_pub, dest].concat()
}

fn nonce_bloc(prefixe: &[u8; 7], i: u32, dernier: bool) -> [u8; 12] {
    let mut n = [0u8; 12];
    n[..7].copy_from_slice(prefixe);
    n[7..11].copy_from_slice(&i.to_be_bytes());
    n[11] = dernier as u8;
    n
}

/// Prépare l'entête et la clé de contenu pour ces destinataires `(id, clé de réception)`.
fn entete(destinataires: &[(String, [u8; 32])], de: &str, contexte: &str) -> Resultat<(Enveloppe, Zeroizing<[u8; 32]>, [u8; 7])> {
    let contenu = Zeroizing::new(aleatoire::<32>());
    let eph = StaticSecret::from(aleatoire::<32>());
    let eph_pub = PublicKey::from(&eph).to_bytes();
    let prefixe = aleatoire::<7>();
    let mut liste = Vec::with_capacity(destinataires.len());
    for (id, cle) in destinataires {
        let partage = Zeroizing::new(eph.diffie_hellman(&PublicKey::from(*cle)).to_bytes());
        let k = kek(&eph_pub, cle, &partage)?;
        let enc = ChaCha20Poly1305::new(&Key::from(*k))
            .encrypt(&Nonce::from([0u8; 12]), Payload { msg: contenu.as_ref(), aad: &aad_dest(&eph_pub, cle) })
            .map_err(|_| Erreur::Format("chiffrement".into()))?;
        liste.push(Destinataire { id: id.clone(), cle: empreinte_cle(cle), enc: b64(&enc) });
    }
    let e = Enveloppe {
        alg: ALG.into(),
        de: de.into(),
        contexte: contexte.into(),
        eph: b64(&eph_pub),
        destinataires: liste,
        prefixe: b64(&prefixe),
        chiffre: None,
    };
    e.controler()?;
    Ok((e, contenu, prefixe))
}

/// Clé de contenu retrouvée par l'une de nos clés de réception.
fn cle_contenu(e: &Enveloppe, moi: &str, cles: &[&CleReception]) -> Resultat<(Zeroizing<[u8; 32]>, [u8; 7])> {
    e.controler()?;
    let eph_pub: [u8; 32] = de_b64(&e.eph)?.as_slice().try_into().map_err(|_| Erreur::Dechiffrement)?;
    let prefixe: [u8; 7] = de_b64(&e.prefixe)?.as_slice().try_into().map_err(|_| Erreur::Dechiffrement)?;
    for d in e.destinataires.iter().filter(|d| d.id == moi) {
        for cle in cles.iter().filter(|c| c.empreinte() == d.cle) {
            let partage = Zeroizing::new(cle.secret.diffie_hellman(&PublicKey::from(eph_pub)).to_bytes());
            let Ok(k) = kek(&eph_pub, &cle.public, &partage) else { continue };
            let enc = de_b64(&d.enc)?;
            if let Ok(c) = ChaCha20Poly1305::new(&Key::from(*k))
                .decrypt(&Nonce::from([0u8; 12]), Payload { msg: &enc, aad: &aad_dest(&eph_pub, &cle.public) })
            {
                let c = Zeroizing::new(c);
                let contenu: [u8; 32] = c.as_slice().try_into().map_err(|_| Erreur::Dechiffrement)?;
                return Ok((Zeroizing::new(contenu), prefixe));
            }
        }
    }
    Err(Erreur::Dechiffrement)
}

/// Chiffre un flux bloc par bloc ; rend le nombre d'octets clairs.
fn chiffrer_flux(cle: &[u8; 32], prefixe: &[u8; 7], aad: &[u8], source: &mut dyn Read, cible: &mut dyn Write) -> Resultat<u64> {
    let aead = ChaCha20Poly1305::new(&Key::from(*cle));
    let mut courant = lire_bloc(source)?;
    let mut i: u32 = 0;
    let mut total = 0u64;
    loop {
        let suivant = if courant.len() == BLOC { lire_bloc(source)? } else { Vec::new() };
        let dernier = courant.len() < BLOC || suivant.is_empty();
        let c = aead
            .encrypt(&Nonce::from(nonce_bloc(prefixe, i, dernier)), Payload { msg: &courant, aad })
            .map_err(|_| Erreur::Format("chiffrement".into()))?;
        cible.write_all(&c)?;
        total += courant.len() as u64;
        if dernier {
            return Ok(total);
        }
        i = i.checked_add(1).ok_or_else(|| Erreur::Format("contenu trop long".into()))?;
        courant = suivant;
    }
}

fn dechiffrer_flux(cle: &[u8; 32], prefixe: &[u8; 7], aad: &[u8], source: &mut dyn Read, cible: &mut dyn Write) -> Resultat<()> {
    let aead = ChaCha20Poly1305::new(&Key::from(*cle));
    let mut i: u32 = 0;
    let mut courant = lire_n(source, BLOC + TAG)?;
    loop {
        let suivant = if courant.len() == BLOC + TAG { lire_n(source, BLOC + TAG)? } else { Vec::new() };
        let dernier = suivant.is_empty();
        let clair = aead
            .decrypt(&Nonce::from(nonce_bloc(prefixe, i, dernier)), Payload { msg: &courant, aad })
            .map_err(|_| Erreur::Dechiffrement)?;
        cible.write_all(&clair)?;
        if dernier {
            return Ok(());
        }
        i = i.checked_add(1).ok_or(Erreur::Dechiffrement)?;
        courant = suivant;
    }
}

fn lire_bloc(source: &mut dyn Read) -> Resultat<Vec<u8>> {
    lire_n(source, BLOC)
}

fn lire_n(source: &mut dyn Read, n: usize) -> Resultat<Vec<u8>> {
    let mut v = Vec::with_capacity(n);
    source.take(n as u64).read_to_end(&mut v)?;
    Ok(v)
}

// ------------------------------------------------------------------ API

/// Scelle des octets pour ces destinataires `(id machine, clé de réception)`.
pub fn sceller(destinataires: &[(String, [u8; 32])], de: &str, contexte: &str, donnees: &[u8]) -> Resultat<Enveloppe> {
    let (mut e, cle, prefixe) = entete(destinataires, de, contexte)?;
    let mut chiffre = Vec::with_capacity(donnees.len() + TAG * (donnees.len() / BLOC + 1));
    chiffrer_flux(&cle, &prefixe, &e.aad()?, &mut &donnees[..], &mut chiffre)?;
    e.chiffre = Some(b64(&chiffre));
    Ok(e)
}

/// Ouvre une enveloppe d'octets. `moi` : notre identifiant ; `de` : le signataire de
/// l'objet qui la portait (déjà vérifié par l'appelant), qui doit être l'expéditeur.
pub fn ouvrir(e: &Enveloppe, moi: &str, de: &str, cles: &[&CleReception]) -> Resultat<Zeroizing<Vec<u8>>> {
    if e.de != de {
        return Err(Erreur::Dechiffrement);
    }
    let chiffre = de_b64(e.chiffre.as_deref().ok_or(Erreur::Dechiffrement)?)?;
    let (cle, prefixe) = cle_contenu(e, moi, cles)?;
    let mut clair = Zeroizing::new(Vec::with_capacity(chiffre.len()));
    dechiffrer_flux(&cle, &prefixe, &e.aad()?, &mut &chiffre[..], &mut *clair)?;
    Ok(clair)
}

/// Scelle un fichier : écrit la suite des blocs dans `cible` et rend l'entête (sans "chiffre").
pub fn sceller_fichier(
    destinataires: &[(String, [u8; 32])],
    de: &str,
    contexte: &str,
    source: &Path,
    cible: &Path,
) -> Resultat<Enveloppe> {
    let (e, cle, prefixe) = entete(destinataires, de, contexte)?;
    let mut entree = std::io::BufReader::new(std::fs::File::open(source)?);
    let tmp = cible.with_extension("tmp-scelle");
    let resultat = (|| {
        let mut sortie = std::io::BufWriter::new(std::fs::File::create(&tmp)?);
        chiffrer_flux(&cle, &prefixe, &e.aad()?, &mut entree, &mut sortie)?;
        sortie.into_inner().map_err(|e| e.into_error())?.sync_all()?;
        std::fs::rename(&tmp, cible)?;
        Ok(())
    })();
    if resultat.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    resultat.map(|()| e)
}

/// Ouvre un fichier scellé vers `cible`, qui n'apparaît qu'une fois tout authentifié.
pub fn ouvrir_fichier(e: &Enveloppe, moi: &str, de: &str, cles: &[&CleReception], source: &Path, cible: &Path) -> Resultat<()> {
    if e.de != de || e.chiffre.is_some() {
        return Err(Erreur::Dechiffrement);
    }
    let (cle, prefixe) = cle_contenu(e, moi, cles)?;
    let mut entree = std::io::BufReader::new(std::fs::File::open(source)?);
    let tmp = cible.with_extension("tmp-ouvert");
    let resultat = (|| {
        let mut sortie = std::io::BufWriter::new(std::fs::File::create(&tmp)?);
        dechiffrer_flux(&cle, &prefixe, &e.aad()?, &mut entree, &mut sortie)?;
        sortie.into_inner().map_err(|e| e.into_error())?.sync_all()?;
        std::fs::rename(&tmp, cible)?;
        Ok(())
    })();
    if resultat.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    resultat
}

/// Enveloppe lue depuis une valeur JSON (champ inconnu refusé).
pub fn depuis_valeur(v: &Value) -> Resultat<Enveloppe> {
    Ok(serde_json::from_value(v.clone())?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cles::{Alg, CoffreLogiciel};

    fn parc3() -> (CleReception, CleReception, CleReception, Vec<(String, [u8; 32])>) {
        let (a, b, c) = (CleReception::generer(), CleReception::generer(), CleReception::generer());
        let dest = vec![("mac".to_string(), a.public), ("ryzen".to_string(), b.public)];
        (a, b, c, dest)
    }

    #[test]
    fn aller_retour_plusieurs_destinataires_et_tailles() {
        let (a, b, c, dest) = parc3();
        for taille in [0, 1, BLOC - 1, BLOC, BLOC + 1, 3 * BLOC] {
            let donnees: Vec<u8> = (0..taille).map(|i| (i % 251) as u8).collect();
            let e = sceller(&dest, "pi", "ticket/42", &donnees).unwrap();
            assert_eq!(*ouvrir(&e, "mac", "pi", &[&a]).unwrap(), donnees);
            assert_eq!(*ouvrir(&e, "ryzen", "pi", &[&c, &b]).unwrap(), donnees);
            assert!(matches!(ouvrir(&e, "intrus", "pi", &[&c]), Err(Erreur::Dechiffrement)));
            assert!(matches!(ouvrir(&e, "mac", "pi", &[&c]), Err(Erreur::Dechiffrement)));
        }
    }

    #[test]
    fn entete_authentifie() {
        let (a, _b, c, dest) = parc3();
        let e = sceller(&dest, "pi", "ticket/42", b"secret").unwrap();
        // Autre expediteur annonce, autre contexte, destinataire retire ou ajoute.
        assert!(ouvrir(&e, "mac", "autre", &[&a]).is_err());
        let mut x = e.clone();
        x.de = "autre".into();
        assert!(ouvrir(&x, "mac", "autre", &[&a]).is_err());
        let mut x = e.clone();
        x.contexte = "ticket/43".into();
        assert!(ouvrir(&x, "mac", "pi", &[&a]).is_err());
        let mut x = e.clone();
        x.destinataires.pop();
        assert!(ouvrir(&x, "mac", "pi", &[&a]).is_err());
        let mut x = e.clone();
        x.destinataires.push(Destinataire { id: "c".into(), cle: empreinte_cle(&c.public), enc: x.destinataires[0].enc.clone() });
        assert!(ouvrir(&x, "mac", "pi", &[&a]).is_err());
        let mut x = e.clone();
        x.alg = "autre".into();
        assert!(ouvrir(&x, "mac", "pi", &[&a]).is_err());
    }

    #[test]
    fn blocs_coupes_deplaces_ou_modifies_refuses() {
        let (a, _b, _c, dest) = parc3();
        let donnees = vec![7u8; 2 * BLOC + 10];
        let e = sceller(&dest, "pi", "x", &donnees).unwrap();
        let chiffre = de_b64(e.chiffre.as_deref().unwrap()).unwrap();
        let bloc = BLOC + TAG;
        let variantes: Vec<Vec<u8>> = vec![
            chiffre[..2 * bloc].to_vec(),                                           // fin coupee
            [&chiffre[bloc..2 * bloc], &chiffre[..bloc], &chiffre[2 * bloc..]].concat(), // blocs echanges
            [&chiffre[..], &chiffre[..bloc]].concat(),                              // bloc ajoute
            { let mut v = chiffre.clone(); v[5] ^= 1; v },                         // octet modifie
            Vec::new(),
        ];
        for v in variantes {
            let mut x = e.clone();
            x.chiffre = Some(b64(&v));
            assert!(matches!(ouvrir(&x, "mac", "pi", &[&a]), Err(Erreur::Dechiffrement)));
        }
    }

    #[test]
    fn cle_de_faible_ordre_refusee() {
        let dest = vec![("x".to_string(), [0u8; 32])];
        assert!(sceller(&dest, "pi", "x", b"a").is_err());
    }

    #[test]
    fn limites() {
        let a = CleReception::generer();
        let trop: Vec<_> = (0..=MAX_DESTINATAIRES).map(|i| (format!("m{i}"), a.public)).collect();
        assert!(sceller(&trop, "pi", "x", b"a").is_err());
        assert!(sceller(&[], "pi", "x", b"a").is_err());
        assert!(sceller(&[("m".into(), a.public)], "pi", &"x".repeat(MAX_CONTEXTE + 1), b"a").is_err());
        assert!(sceller(&[("m".into(), a.public)], "pi", "a\nb", b"a").is_err());
        assert!(depuis_valeur(&json!({"alg": ALG, "inconnu": 1})).is_err());
    }

    #[test]
    fn fichier_aller_retour_et_fichier_modifie_sans_trace() {
        let (a, _b, _c, dest) = parc3();
        let d = tempfile::tempdir().unwrap();
        let clair: Vec<u8> = (0..(BLOC * 2 + 123)).map(|i| (i * 7 % 256) as u8).collect();
        std::fs::write(d.path().join("iq.bin"), &clair).unwrap();
        let e = sceller_fichier(&dest, "pi", "travail/1", &d.path().join("iq.bin"), &d.path().join("iq.scelle")).unwrap();
        assert!(e.chiffre.is_none());
        ouvrir_fichier(&e, "mac", "pi", &[&a], &d.path().join("iq.scelle"), &d.path().join("iq.ouvert")).unwrap();
        assert_eq!(std::fs::read(d.path().join("iq.ouvert")).unwrap(), clair);
        let mut v = std::fs::read(d.path().join("iq.scelle")).unwrap();
        let n = v.len();
        v[n - 1] ^= 1;
        std::fs::write(d.path().join("iq.scelle"), &v).unwrap();
        let r = ouvrir_fichier(&e, "mac", "pi", &[&a], &d.path().join("iq.scelle"), &d.path().join("iq.mauvais"));
        assert!(matches!(r, Err(Erreur::Dechiffrement)));
        assert!(!d.path().join("iq.mauvais").exists());
        assert!(std::fs::read_dir(d.path()).unwrap().all(|f| !f.unwrap().file_name().to_string_lossy().contains("tmp")));
    }

    #[test]
    fn cle_de_reception_publiee_signee_et_relue() {
        let d = tempfile::tempdir().unwrap();
        let chemin = d.path().join("reception.cle");
        let cle = CleReception::charger_ou_creer(&chemin).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(&chemin).unwrap().permissions().mode() & 0o777, 0o600);
        }
        assert_eq!(CleReception::charger_ou_creer(&chemin).unwrap().public, cle.public);
        let identite = CoffreLogiciel::generer(Alg::P256);
        let objet = cle.publier(&identite, "parc1").unwrap();
        assert_eq!(verifier_cle_reception(&objet, &identite.cle_publique(), "parc1").unwrap(), cle.public);
        let intrus = CoffreLogiciel::generer(Alg::P256);
        assert!(verifier_cle_reception(&objet, &intrus.cle_publique(), "parc1").is_err());
        assert!(verifier_cle_reception(&objet, &identite.cle_publique(), "parc2").is_err());
    }
}
