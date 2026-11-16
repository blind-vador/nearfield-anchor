//! Coffre externe avec une fausse puce (exemple `puce_factice`).
use std::path::PathBuf;

use nearfield_pont::cles::{Coffre, Niveau};
use nearfield_pont::puce::{identite_machine, CoffreExterne, FICHIER_PUCE};

fn programme() -> PathBuf {
    // L'exemple est construit par `cargo test` dans <cible>/<profil>/examples ; le
    // binaire de test peut vivre ailleurs (dossier de construction) : on remonte.
    let exe = std::env::current_exe().unwrap();
    for dossier in exe.ancestors() {
        for profil in ["debug", "release"] {
            let p = dossier.join(profil).join("examples").join("puce_factice");
            if p.is_file() {
                return p;
            }
        }
        let p = dossier.join("examples").join("puce_factice");
        if p.is_file() {
            return p;
        }
    }
    panic!("exemple puce_factice introuvable depuis {exe:?}");
}

#[test]
fn cree_ouvre_et_signe_par_la_puce() {
    let prog = programme();
    assert!(prog.exists(), "construire d'abord l'exemple : {prog:?}");
    let d = tempfile::tempdir().unwrap();
    let poignee = d.path().join("cle.puce");
    let c = CoffreExterne::creer(&prog, &poignee, false).unwrap();
    assert_eq!(c.niveau(), Niveau::Materiel);
    assert!(CoffreExterne::creer(&prog, &poignee, false).is_err(), "jamais écraser une clé");
    let sig = c.signer(b"bonjour").unwrap();
    assert_eq!(sig.len(), 64);
    c.cle_publique().verifier(b"bonjour", &sig).unwrap();
    let relu = CoffreExterne::ouvrir(&prog, &poignee).unwrap();
    assert_eq!(relu.cle_publique(), c.cle_publique());
    relu.signer_avec_raison(b"x", "Valider le ticket essai").unwrap();
    assert_eq!(std::fs::read_to_string(d.path().join("cle.puce.raison")).unwrap(), "Valider le ticket essai");
    assert!(relu.signer_avec_raison(b"x", "a\nb").is_err());
}

#[test]
fn identite_dans_la_puce_quand_elle_repond() {
    let prog = programme();
    let d = tempfile::tempdir().unwrap();
    let c = identite_machine(d.path(), Some(&prog)).unwrap();
    assert_eq!(c.niveau(), Niveau::Materiel);
    assert!(d.path().join(FICHIER_PUCE).exists());
    assert_eq!(identite_machine(d.path(), Some(&prog)).unwrap().cle_publique(), c.cle_publique());
}

#[test]
fn une_puce_qui_ment_est_refusee() {
    // La poignée change de clé derrière le dos du coffre : la signature ne correspond
    // plus à la clé publique connue, elle est refusée.
    let prog = programme();
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("cle.puce");
    let c = CoffreExterne::creer(&prog, &p, false).unwrap();
    std::fs::remove_file(&p).unwrap();
    CoffreExterne::creer(&prog, &p, false).unwrap();
    assert!(c.signer(b"bonjour").is_err());
}

/// Vraie puce (TPM simulé par swtpm, ou Secure Enclave) : seulement si
/// `PF_PUCE_PROGRAMME` désigne son programme (essai manuel, voir pont/LISEZMOI.md).
#[test]
fn vraie_puce_si_demandee() {
    let Some(prog) = std::env::var_os("PF_PUCE_PROGRAMME") else { return };
    let prog = PathBuf::from(prog);
    assert!(CoffreExterne::disponible(&prog), "la puce ne répond pas");
    let d = tempfile::tempdir().unwrap();
    let c = identite_machine(d.path(), Some(&prog)).unwrap();
    assert_eq!(c.niveau(), Niveau::Materiel);
    for i in 0..5u8 {
        let m = vec![i; 1000 + i as usize];
        let sig = c.signer(&m).unwrap();
        c.cle_publique().verifier(&m, &sig).unwrap();
    }
    let relu = identite_machine(d.path(), Some(&prog)).unwrap();
    assert_eq!(relu.cle_publique(), c.cle_publique());
    eprintln!("puce réelle : {} OK", relu.cle_publique().id().unwrap());
}
