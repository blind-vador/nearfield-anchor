//! Liste fermée des messages du canal (spec §8).
//!
//! Un type inconnu, un champ en trop ou une taille hors limite : le message est
//! refusé et la session fermée par l'appelant. Aucun message ne transporte de
//! commande à exécuter.
//!
//! Les champs « données » (métadonnées d'un décodage, mesure de puissance, état
//! de session) sont des objets descriptifs bornés (§8.2) : taille, profondeur et
//! longueur des textes limitées ; le destinataire les range ou les dit, il ne les
//! interprète jamais comme une instruction.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::objet::{nom_sur, ObjetSigne};
use crate::{Erreur, Resultat};

pub const MAX_TEXTE: usize = 4000;
pub const MAX_COURT: usize = 128;
/// Données d'un morceau : 60 Kio encodés en base64.
pub const MAX_MORCEAU_B64: usize = 81_920;
/// Données descriptives bornées (§8.2) : taille JSON et profondeur.
pub const MAX_DONNEES: usize = 24_000;
pub const PROFONDEUR_DONNEES: usize = 6;
/// Durée d'écoute demandée à une machine (TETRA, numérique).
pub const MAX_SECONDES: u32 = 3600;
pub const MAX_LISTE: usize = 256;

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
    Battement {
        id: String,
        etat: Etat,
        capacites: Vec<String>,
        puissance_libre: u8,
        /// Voix de synthèse que la machine sait rendre (relais « dire »).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        voix: Option<Vec<String>>,
        /// Données : état de la session (écran verrouillé, inactivité).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        session: Option<Value>,
        /// Données : mesure de puissance (attribution des travaux).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        puissance: Option<Value>,
        /// Données : de quoi réveiller la machine (adresse matérielle, adresses IP).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        reseau: Option<Value>,
    },
    /// Nouvelle clé de transport ; `preuve` : objet `renouvellement` signé par la clé
    /// d'identité de la machine (obligatoire pour renouveler sans la v1, §4.1).
    CertificatDemande {
        id: String,
        transport: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        preuve: Option<ObjetSigne>,
    },
    Certificat { id: String, objet: ObjetSigne },
    Revocations { id: String, objet: ObjetSigne },
    TravailAnnonce { id: String, travail: String },
    TravailPris { id: String, travail: String, jusqu_a: i64 },
    /// Le travail est terminé ; son résultat signé est sur le stockage partagé.
    TravailFini { id: String, travail: String },
    /// Du neuf sur ces tickets (identifiants seulement, le contenu est signé à part).
    Tickets { id: String, ids: Vec<String> },
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
    /// Réponse finale à un `dire` (le son est arrivé en `morceau`, flux = id du `dire`).
    DireFin {
        id: String,
        reponse_a: String,
        ok: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        taux: Option<u32>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        erreur: Option<String>,
    },
    Morceau {
        id: String,
        flux: String,
        n: u32,
        donnees: String,
        fin: bool,
        /// Quelle piste du flux (voix, audio, banc_xxx…) : un nom simple.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        piste: Option<String>,
        /// Échantillons par seconde du PCM 16 bits mono.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        taux: Option<u32>,
    },
    Oubli { id: String, objet: ObjetSigne },
    /// Décodage TETRA en direct. La machine se connecte au flux radio de
    /// l'ordonnateur (son adresse, jamais une adresse reçue) sur `port`.
    TetraDemande {
        id: String,
        port: u16,
        freq_hz: u64,
        gain: f64,
        secondes: u32,
        max_secondes: u32,
        ppm: f64,
    },
    TetraResultat {
        id: String,
        reponse_a: String,
        ok: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        erreur: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        reseau: Option<Value>,
        #[serde(default)]
        bsch: u32,
        #[serde(default)]
        sysinfo: u32,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        enc: Option<Value>,
        #[serde(default)]
        clair: bool,
        #[serde(default)]
        mac_resource: u32,
        #[serde(default)]
        clip: bool,
        #[serde(default)]
        voix: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        resume: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        activite: Option<Value>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        duree: Option<f64>,
    },
    /// Décodage numérique en direct (DMR, P25…), même règle d'adresse que TETRA.
    NumeriqueDemande {
        id: String,
        port: u16,
        freq_hz: u64,
        gain: f64,
        secondes: u32,
        max_secondes: u32,
        ppm: f64,
        /// Juge imposé, par exemple « -fa » ou « -fs ».
        mode: String,
        #[serde(default)]
        banc: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        modes: Option<Vec<String>>,
    },
    NumeriqueResultat {
        id: String,
        reponse_a: String,
        ok: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        erreur: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        protocole: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        meta: Option<Value>,
        #[serde(default)]
        chiffre: bool,
        #[serde(default)]
        clair: bool,
        #[serde(default)]
        voix: bool,
        #[serde(default)]
        audio: bool,
        #[serde(default)]
        flux: bool,
        #[serde(default)]
        numerique_confirme: bool,
        #[serde(default)]
        digital_actif: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        sync_hits: Option<i64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        voix_rms: Option<f64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        audio_rms: Option<f64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        resume: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        duree: Option<f64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        banc: Option<Vec<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        banc_preuve: Option<Value>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        preuve: Option<Value>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        rendus: Option<Vec<Rendu>>,
    },
}

/// Un juge numérique qui a rendu quelque chose (voix en clair envoyée à part).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rendu {
    pub mode: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub protocole: Option<String>,
    #[serde(default)]
    pub confirme: bool,
    #[serde(default)]
    pub hits: i64,
    #[serde(default)]
    pub rms: f64,
    #[serde(default)]
    pub chiffre: bool,
    #[serde(default)]
    pub voix: bool,
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

fn texte_opt(nom: &str, v: &Option<String>) -> Resultat<()> {
    v.as_deref().map_or(Ok(()), |t| texte(nom, t))
}

/// Nom simple : lettres minuscules, chiffres, « _ » et « - » (pistes, modes, voix d'un juge).
fn simple(nom: &str, v: &str) -> Resultat<()> {
    court(nom, v)?;
    if !v.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-') {
        return Err(Erreur::Message(format!("champ « {nom} » invalide")));
    }
    Ok(())
}

fn liste<F: Fn(&str) -> Resultat<()>>(nom: &str, v: &[String], max: usize, f: F) -> Resultat<()> {
    if v.len() > max {
        return Err(Erreur::Message(format!("liste « {nom} » trop longue")));
    }
    v.iter().try_for_each(|x| f(x))
}

fn fini(nom: &str, v: f64, min: f64, max: f64) -> Resultat<()> {
    if !v.is_finite() || v < min || v > max {
        return Err(Erreur::Message(format!("champ « {nom} » hors limites")));
    }
    Ok(())
}

/// Données descriptives bornées (§8.2).
pub(crate) fn donnees(nom: &str, v: &Option<Value>) -> Resultat<()> {
    fn parcourir(v: &Value, prof: usize) -> bool {
        if prof > PROFONDEUR_DONNEES {
            return false;
        }
        match v {
            Value::String(t) => t.len() <= MAX_TEXTE,
            Value::Array(a) => a.len() <= MAX_LISTE && a.iter().all(|x| parcourir(x, prof + 1)),
            Value::Object(o) => {
                o.len() <= MAX_LISTE
                    && o.iter().all(|(k, x)| k.len() <= MAX_COURT && parcourir(x, prof + 1))
            }
            _ => true,
        }
    }
    let Some(v) = v else { return Ok(()) };
    let taille = serde_json::to_vec(v).map(|o| o.len()).unwrap_or(usize::MAX);
    if taille > MAX_DONNEES || !parcourir(v, 0) {
        return Err(Erreur::Message(format!("données « {nom} » hors limites")));
    }
    Ok(())
}

fn ecoute(port: u16, freq_hz: u64, gain: f64, secondes: u32, max_secondes: u32, ppm: f64) -> Resultat<()> {
    if port == 0 || freq_hz == 0 || freq_hz > 10_000_000_000 {
        return Err(Erreur::Message("port ou fréquence".into()));
    }
    if secondes == 0 || secondes > MAX_SECONDES || max_secondes < secondes || max_secondes > MAX_SECONDES {
        return Err(Erreur::Message("durée d'écoute".into()));
    }
    fini("gain", gain, 0.0, 100.0)?;
    fini("ppm", ppm, -1000.0, 1000.0)
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
            | Message::Oubli { id, .. }
            | Message::TravailFini { id, .. }
            | Message::Tickets { id, .. }
            | Message::DireFin { id, .. }
            | Message::TetraDemande { id, .. }
            | Message::TetraResultat { id, .. }
            | Message::NumeriqueDemande { id, .. }
            | Message::NumeriqueResultat { id, .. } => id,
        }
    }

    /// Limites de taille et noms sûrs, en plus de la forme imposée par serde.
    pub fn valider(&self) -> Resultat<()> {
        court("id", self.id())?;
        match self {
            Message::Battement { capacites, puissance_libre, voix, session, puissance, reseau, .. } => {
                if capacites.len() > 64 || *puissance_libre > 100 {
                    return Err(Erreur::Message("battement hors limites".into()));
                }
                capacites.iter().try_for_each(|c| court("capacite", c))?;
                if let Some(v) = voix {
                    liste("voix", v, 64, |x| court("voix", x))?;
                }
                donnees("session", session)?;
                donnees("puissance", puissance)?;
                donnees("reseau", reseau)
            }
            Message::TravailFini { travail, .. } => nom_sur(travail),
            Message::Tickets { ids, .. } => liste("ids", ids, MAX_LISTE, nom_sur),
            Message::DireFin { reponse_a, taux, erreur, .. } => {
                court("reponse_a", reponse_a)?;
                if taux.map_or(false, |t| t == 0 || t > 192_000) {
                    return Err(Erreur::Message("taux".into()));
                }
                texte_opt("erreur", erreur)
            }
            Message::TetraDemande { port, freq_hz, gain, secondes, max_secondes, ppm, .. } => {
                ecoute(*port, *freq_hz, *gain, *secondes, *max_secondes, *ppm)
            }
            Message::NumeriqueDemande { port, freq_hz, gain, secondes, max_secondes, ppm, mode, modes, .. } => {
                ecoute(*port, *freq_hz, *gain, *secondes, *max_secondes, *ppm)?;
                simple("mode", mode)?;
                if let Some(m) = modes {
                    liste("modes", m, 64, |x| simple("modes", x))?;
                }
                Ok(())
            }
            Message::TetraResultat { reponse_a, erreur, reseau, enc, resume, activite, duree, .. } => {
                court("reponse_a", reponse_a)?;
                texte_opt("erreur", erreur)?;
                texte_opt("resume", resume)?;
                donnees("reseau", reseau)?;
                donnees("enc", enc)?;
                donnees("activite", activite)?;
                duree.map_or(Ok(()), |d| fini("duree", d, 0.0, 86_400.0))
            }
            Message::NumeriqueResultat {
                reponse_a, erreur, protocole, meta, voix_rms, audio_rms, resume, duree, banc, banc_preuve,
                preuve, rendus, ..
            } => {
                court("reponse_a", reponse_a)?;
                texte_opt("erreur", erreur)?;
                texte_opt("resume", resume)?;
                if let Some(p) = protocole {
                    court("protocole", p)?;
                }
                donnees("meta", meta)?;
                donnees("banc_preuve", banc_preuve)?;
                donnees("preuve", preuve)?;
                for r in [voix_rms, audio_rms] {
                    r.map_or(Ok(()), |x| fini("rms", x, 0.0, 1e9))?;
                }
                duree.map_or(Ok(()), |d| fini("duree", d, 0.0, 86_400.0))?;
                if let Some(b) = banc {
                    liste("banc", b, 64, |x| simple("banc", x))?;
                }
                if let Some(rs) = rendus {
                    if rs.len() > 64 {
                        return Err(Erreur::Message("rendus".into()));
                    }
                    for r in rs {
                        simple("mode", &r.mode)?;
                        if let Some(p) = &r.protocole {
                            court("protocole", p)?;
                        }
                        fini("rms", r.rms, 0.0, 1e9)?;
                    }
                }
                Ok(())
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
            Message::Morceau { flux, donnees, piste, taux, .. } => {
                court("flux", flux)?;
                if donnees.len() > MAX_MORCEAU_B64 {
                    return Err(Erreur::Message("morceau trop gros".into()));
                }
                if let Some(p) = piste {
                    simple("piste", p)?;
                }
                if taux.map_or(false, |t| t == 0 || t > 192_000) {
                    return Err(Erreur::Message("taux".into()));
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
        let octets = serde_json::to_vec(self)?;
        if octets.len() > crate::transport::MAX_CLAIR {
            return Err(Erreur::Message("message trop gros pour une trame".into()));
        }
        Ok(octets)
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
    fn nouveaux_types_aller_retour() {
        let exemples = [
            r#"{"type":"battement","id":"b","etat":"libre","capacites":["whisper"],"puissance_libre":80,"voix":["Thomas"],"session":{"verrouille":false,"inactif_s":12},"puissance":{"puissance":120.5,"disponible":80.0}}"#,
            r#"{"type":"travail_fini","id":"f","travail":"20261115-whisper-ab12"}"#,
            r#"{"type":"tickets","id":"t","ids":["t-1","t-2"]}"#,
            r#"{"type":"dire_fin","id":"d","reponse_a":"r1","ok":true,"taux":22050}"#,
            r#"{"type":"morceau","id":"m","flux":"r1","n":0,"donnees":"AAAA","fin":false,"piste":"numerique_banc_dmr","taux":8000}"#,
            r#"{"type":"tetra_demande","id":"r2","port":1234,"freq_hz":392512500,"gain":32,"secondes":12,"max_secondes":60,"ppm":0}"#,
            r#"{"type":"tetra_resultat","id":"x","reponse_a":"r2","ok":true,"reseau":{"mcc":208,"mnc":1},"bsch":3,"enc":["TEA1"],"clair":false,"resume":"Réseau vu.","duree":12.3}"#,
            r#"{"type":"numerique_demande","id":"r3","port":1234,"freq_hz":467238000,"gain":32.5,"secondes":8,"max_secondes":8,"ppm":0,"mode":"-fa","banc":true,"modes":["-fs","-fi"]}"#,
            r#"{"type":"numerique_resultat","id":"y","reponse_a":"r3","ok":true,"protocole":"DMR","meta":{"cc":1},"sync_hits":12,"voix_rms":340.5,"banc":["dmr"],"rendus":[{"mode":"-fs","protocole":"DMR","confirme":true,"hits":4,"rms":12.0,"chiffre":false,"voix":true}]}"#,
        ];
        for e in exemples {
            let m = Message::decoder(e.as_bytes()).unwrap_or_else(|x| panic!("{e} : {x}"));
            assert_eq!(Message::decoder(&m.encoder().unwrap()).unwrap(), m);
        }
    }

    #[test]
    fn nouveaux_types_refuses() {
        let refuses = [
            // une adresse à contacter n'existe pas en v2 : champ en trop
            r#"{"type":"tetra_demande","id":"r","host":"10.0.0.9","port":1234,"freq_hz":1,"gain":32,"secondes":12,"max_secondes":12,"ppm":0}"#,
            r#"{"type":"tetra_demande","id":"r","port":1234,"freq_hz":1,"gain":32,"secondes":12,"max_secondes":5,"ppm":0}"#,
            r#"{"type":"tetra_demande","id":"r","port":0,"freq_hz":1,"gain":32,"secondes":12,"max_secondes":12,"ppm":0}"#,
            r#"{"type":"numerique_demande","id":"r","port":1,"freq_hz":1,"gain":32,"secondes":8,"max_secondes":8,"ppm":0,"mode":"-fa; rm -rf /"}"#,
            r#"{"type":"tickets","id":"t","ids":["../secret"]}"#,
            r#"{"type":"morceau","id":"m","flux":"r","n":0,"donnees":"","fin":true,"piste":"../x"}"#,
            r#"{"type":"numerique_resultat","id":"y","reponse_a":"r","ok":true,"rendus":[{"mode":"-fs","inconnu":1}]}"#,
            r#"{"type":"dire_fin","id":"d","reponse_a":"r","ok":true,"commande":"say"}"#,
        ];
        for e in refuses {
            assert!(Message::decoder(e.as_bytes()).is_err(), "aurait dû être refusé : {e}");
        }
        let profond = format!("{}1{}", "[".repeat(PROFONDEUR_DONNEES + 2), "]".repeat(PROFONDEUR_DONNEES + 2));
        let m = format!(r#"{{"type":"tetra_resultat","id":"x","reponse_a":"r","ok":true,"activite":{profond}}}"#);
        assert!(Message::decoder(m.as_bytes()).is_err());
        let gros = format!(r#"{{"type":"tetra_resultat","id":"x","reponse_a":"r","ok":true,"activite":{{"a":"{}"}}}}"#,
                           "x".repeat(MAX_TEXTE + 1));
        assert!(Message::decoder(gros.as_bytes()).is_err());
    }

    #[test]
    fn refuse_chemins_et_limites() {
        assert!(Message::decoder(br#"{"type":"travail_annonce","id":"x","travail":"../../etc"}"#).is_err());
        assert!(Message::decoder(br#"{"type":"progression","id":"x","travail":"t","etape":"e","pourcentage":101}"#).is_err());
        let long = format!(r#"{{"type":"dire","id":"x","texte":"{}"}}"#, "a".repeat(MAX_TEXTE + 1));
        assert!(Message::decoder(long.as_bytes()).is_err());
    }
}
