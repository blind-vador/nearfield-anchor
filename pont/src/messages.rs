//! Liste fermée des messages du canal (spec §8).
//!
//! Un type inconnu, un champ en trop ou une taille hors limite : le message est
//! refusé et la session fermée par l'appelant. Aucun message ne transporte de
//! commande à exécuter.

use serde::{Deserialize, Serialize};

use crate::objet::{nom_sur, ObjetSigne};
use crate::{Erreur, Resultat};

pub const MAX_TEXTE: usize = 4000;
pub const MAX_COURT: usize = 128;
/// Données d'un morceau : 60 Kio encodés en base64.
pub const MAX_MORCEAU_B64: usize = 81_920;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Etat {
    Libre,
    Occupe,
    Endormi,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Message {
    Battement { id: String, etat: Etat, capacites: Vec<String>, puissance_libre: u8 },
    CertificatDemande { id: String, transport: String },
    Certificat { id: String, objet: ObjetSigne },
    Revocations { id: String, objet: ObjetSigne },
    TravailAnnonce { id: String, travail: String },
    TravailPris { id: String, travail: String, jusqu_a: i64 },
    Progression {
        id: String,
        travail: String,
        etape: String,
        pourcentage: u8,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        phrase: Option<String>,
    },
    Resultat { id: String, objet: ObjetSigne },
    Dire {
        id: String,
        texte: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        voix: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        debit: Option<i32>,
    },
    Morceau { id: String, flux: String, n: u32, donnees: String, fin: bool },
    Oubli { id: String, objet: ObjetSigne },
}

fn court(nom: &str, v: &str) -> Resultat<()> {
    if v.is_empty() || v.len() > MAX_COURT || v.chars().any(char::is_control) {
        return Err(Erreur::Message(format!("champ « {nom} » invalide")));
    }
    Ok(())
}

fn texte(nom: &str, v: &str) -> Resultat<()> {
    if v.len() > MAX_TEXTE {
        return Err(Erreur::Message(format!("champ « {nom} » trop long")));
    }
    Ok(())
}

impl Message {
    pub fn id(&self) -> &str {
        match self {
            Message::Battement { id, .. }
            | Message::CertificatDemande { id, .. }
            | Message::Certificat { id, .. }
            | Message::Revocations { id, .. }
            | Message::TravailAnnonce { id, .. }
            | Message::TravailPris { id, .. }
            | Message::Progression { id, .. }
            | Message::Resultat { id, .. }
            | Message::Dire { id, .. }
            | Message::Morceau { id, .. }
            | Message::Oubli { id, .. } => id,
        }
    }

    /// Limites de taille et noms sûrs, en plus de la forme imposée par serde.
    pub fn valider(&self) -> Resultat<()> {
        court("id", self.id())?;
        match self {
            Message::Battement { capacites, puissance_libre, .. } => {
                if capacites.len() > 64 || *puissance_libre > 100 {
                    return Err(Erreur::Message("battement hors limites".into()));
                }
                capacites.iter().try_for_each(|c| court("capacite", c))
            }
            Message::CertificatDemande { transport, .. } => {
                if crate::de_b64(transport)?.len() != 32 {
                    return Err(Erreur::Message("clé de transport".into()));
                }
                Ok(())
            }
            Message::TravailAnnonce { travail, .. } | Message::TravailPris { travail, .. } => nom_sur(travail),
            Message::Progression { travail, etape, pourcentage, phrase, .. } => {
                nom_sur(travail)?;
                court("etape", etape)?;
                if *pourcentage > 100 {
                    return Err(Erreur::Message("pourcentage".into()));
                }
                phrase.as_deref().map_or(Ok(()), |p| texte("phrase", p))
            }
            Message::Dire { texte: t, voix, debit, .. } => {
                texte("texte", t)?;
                if let Some(v) = voix {
                    court("voix", v)?;
                }
                if debit.map_or(false, |d| !(-100..=100).contains(&d)) {
                    return Err(Erreur::Message("débit".into()));
                }
                Ok(())
            }
            Message::Morceau { flux, donnees, .. } => {
                court("flux", flux)?;
                if donnees.len() > MAX_MORCEAU_B64 {
                    return Err(Erreur::Message("morceau trop gros".into()));
                }
                Ok(())
            }
            Message::Certificat { .. } | Message::Revocations { .. } | Message::Resultat { .. } | Message::Oubli { .. } => {
                Ok(()) // l'objet signé est vérifié par le destinataire, avec la bonne clé
            }
        }
    }

    pub fn encoder(&self) -> Resultat<Vec<u8>> {
        self.valider()?;
        Ok(serde_json::to_vec(self)?)
    }

    pub fn decoder(octets: &[u8]) -> Resultat<Self> {
        let m: Message = serde_json::from_slice(octets).map_err(|e| Erreur::Message(e.to_string()))?;
        m.valider()?;
        Ok(m)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn aller_retour() {
        let m = Message::Progression {
            id: "m1".into(),
            travail: "20261115-whisper-ab12".into(),
            etape: "transcription".into(),
            pourcentage: 40,
            phrase: Some("Le Mac transcrit, 40 pour cent.".into()),
        };
        let o = m.encoder().unwrap();
        assert!(String::from_utf8_lossy(&o).contains("\"type\":\"progression\""));
        assert_eq!(Message::decoder(&o).unwrap(), m);
    }

    #[test]
    fn refuse_type_inconnu_et_champ_en_trop() {
        assert!(Message::decoder(br#"{"type":"executer","id":"x","commande":"rm -rf /"}"#).is_err());
        assert!(Message::decoder(br#"{"type":"travail_annonce","id":"x","travail":"t","shell":"ls"}"#).is_err());
    }

    #[test]
    fn refuse_chemins_et_limites() {
        assert!(Message::decoder(br#"{"type":"travail_annonce","id":"x","travail":"../../etc"}"#).is_err());
        assert!(Message::decoder(br#"{"type":"progression","id":"x","travail":"t","etape":"e","pourcentage":101}"#).is_err());
        let long = format!(r#"{{"type":"dire","id":"x","texte":"{}"}}"#, "a".repeat(MAX_TEXTE + 1));
        assert!(Message::decoder(long.as_bytes()).is_err());
    }
}
