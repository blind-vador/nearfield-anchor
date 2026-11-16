//! Fausse puce pour les essais : même protocole que `pf-enclave` / `pf-tpm`, mais la
//! clé est dans la poignée en clair. Ne sert qu'aux tests (`tests/puce.rs`).
use std::io::Read;

use nearfield_pont::cles::{Alg, Coffre, CoffreLogiciel};

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let code = match a.get(1).map(String::as_str) {
        Some("disponible") => 0,
        Some("creer") => {
            let c = CoffreLogiciel::generer(Alg::P256);
            std::fs::write(&a[2], c.en_pem().unwrap()).unwrap();
            println!("{}", c.cle_publique().public);
            0
        }
        Some("publique") => match std::fs::read_to_string(&a[2]) {
            Ok(p) => {
                println!("{}", CoffreLogiciel::depuis_pem(&p).unwrap().cle_publique().public);
                0
            }
            Err(_) => 2,
        },
        Some("signer") => {
            let c = CoffreLogiciel::depuis_pem(&std::fs::read_to_string(&a[2]).unwrap()).unwrap();
            if let Some(i) = a.iter().position(|x| x == "--raison") {
                std::fs::write(format!("{}.raison", a[2]), &a[i + 1]).unwrap();
            }
            let mut m = Vec::new();
            std::io::stdin().read_to_end(&mut m).unwrap();
            let sig = c.signer(&m).unwrap();
            // Comme un TPM : signature DER.
            let s = p256::ecdsa::Signature::from_slice(&sig).unwrap();
            println!("{}", data_encoding::BASE64.encode(s.to_der().as_bytes()));
            0
        }
        _ => 64,
    };
    std::process::exit(code);
}
