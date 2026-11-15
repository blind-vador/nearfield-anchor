//! Forme canonique JSON (RFC 8785, « JCS »), restreinte aux entiers.
//!
//! Un même objet n'a qu'une seule suite d'octets possible : c'est elle qui est
//! signée. Les nombres à virgule sont refusés (leur forme canonique est piégeuse et
//! le protocole n'en a pas besoin).

use serde_json::Value;

use crate::{Erreur, Resultat};

/// Octets canoniques d'une valeur JSON.
pub fn canonique(valeur: &Value) -> Resultat<Vec<u8>> {
    let mut sortie = String::new();
    ecrire(valeur, &mut sortie)?;
    Ok(sortie.into_bytes())
}

fn ecrire(valeur: &Value, sortie: &mut String) -> Resultat<()> {
    match valeur {
        Value::Null => sortie.push_str("null"),
        Value::Bool(b) => sortie.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                sortie.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                sortie.push_str(&u.to_string());
            } else {
                return Err(Erreur::Format("nombre à virgule refusé dans un objet signé".into()));
            }
        }
        // serde_json échappe exactement comme JCS : \" \\ \b \f \n \r \t, \u00xx
        // en minuscules pour les autres caractères de contrôle, rien d'autre.
        Value::String(s) => sortie.push_str(&serde_json::to_string(s)?),
        Value::Array(liste) => {
            sortie.push('[');
            for (i, v) in liste.iter().enumerate() {
                if i > 0 {
                    sortie.push(',');
                }
                ecrire(v, sortie)?;
            }
            sortie.push(']');
        }
        Value::Object(champs) => {
            // Tri des clés par unités UTF-16, comme l'exige la RFC 8785.
            let mut cles: Vec<&String> = champs.keys().collect();
            cles.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16()));
            sortie.push('{');
            for (i, cle) in cles.into_iter().enumerate() {
                if i > 0 {
                    sortie.push(',');
                }
                sortie.push_str(&serde_json::to_string(cle)?);
                sortie.push(':');
                ecrire(&champs[cle], sortie)?;
            }
            sortie.push('}');
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn texte(v: Value) -> String {
        String::from_utf8(canonique(&v).unwrap()).unwrap()
    }

    #[test]
    fn trie_les_cles_et_retire_les_espaces() {
        assert_eq!(texte(json!({"b": 1, "a": [true, null, "x"]})), r#"{"a":[true,null,"x"],"b":1}"#);
    }

    #[test]
    fn echappe_comme_la_rfc() {
        assert_eq!(texte(json!("é\n\u{1f}\"/")), "\"é\\n\\u001f\\\"/\"");
    }

    #[test]
    fn tri_utf16_et_non_par_octets() {
        // U+FF61 (UTF-16 0xFF61) vient après U+1F600 (UTF-16 0xD83D…) en UTF-16,
        // alors qu'en UTF-8 l'ordre serait inverse.
        assert_eq!(texte(json!({"\u{ff61}": 1, "\u{1f600}": 2})), "{\"\u{1f600}\":2,\"\u{ff61}\":1}");
    }

    #[test]
    fn refuse_les_nombres_a_virgule() {
        assert!(canonique(&json!({"x": 1.5})).is_err());
    }

    #[test]
    fn negatifs_et_grands_entiers() {
        assert_eq!(texte(json!([-3, 18446744073709551615u64])), "[-3,18446744073709551615]");
    }
}
