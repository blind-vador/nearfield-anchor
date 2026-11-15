/// Toutes les raisons pour lesquelles le protocole refuse quelque chose.
/// Les messages sont en français : ils peuvent être dits à voix haute.
#[derive(Debug, thiserror::Error)]
pub enum Erreur {
    #[error("format invalide : {0}")]
    Format(String),
    #[error("signature invalide")]
    Signature,
    #[error("signataire inconnu ou révoqué : {0}")]
    Signataire(String),
    #[error("objet périmé ou daté dans le futur")]
    Date,
    #[error("objet déjà vu (rejeu)")]
    Rejeu,
    #[error("empreinte de fichier différente : {0}")]
    Empreinte(String),
    #[error("chemin refusé : {0}")]
    Chemin(String),
    #[error("certificat refusé : {0}")]
    Certificat(String),
    #[error("appairage refusé : {0}")]
    Appairage(String),
    #[error("transport : {0}")]
    Transport(String),
    #[error("migration refusée : {0}")]
    Migration(String),
    #[error("message refusé : {0}")]
    Message(String),
    #[error("clé : {0}")]
    Cle(String),
    #[error("entrée/sortie : {0}")]
    Es(#[from] std::io::Error),
}

pub type Resultat<T> = Result<T, Erreur>;

impl From<serde_json::Error> for Erreur {
    fn from(e: serde_json::Error) -> Self {
        Erreur::Format(e.to_string())
    }
}

impl From<snow::Error> for Erreur {
    fn from(e: snow::Error) -> Self {
        Erreur::Transport(e.to_string())
    }
}
