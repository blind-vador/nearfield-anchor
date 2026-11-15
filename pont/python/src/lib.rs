//! Liaison Python du crate `nearfield-pont` : `import nearfield_pont`.
//!
//! Les objets du protocole circulent côté Python sous forme de `dict` (le JSON de la
//! spec) ; toute la cryptographie et toutes les vérifications restent en Rust.
//! Aucune clé privée ne traverse la frontière : un `Coffre` signe, il ne se lit pas.
//! Les appels qui attendent le réseau relâchent le verrou global de Python.

use std::net::{TcpListener, TcpStream, ToSocketAddrs};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use serde::de::DeserializeOwned;
use serde::Serialize;
use serde_json::Value;

use pont::appairage::{self, Presentation};
use pont::certificat::{self, Certificat, Demande, ListeRevocations, Role};
use pont::cles::{Alg, CleIdentite, Coffre as _, CoffreLogiciel, Niveau};
use pont::enveloppe;
use pont::messages::Message;
use pont::migration::{self, Migration};
use pont::objet::{self, ObjetSigne};
use pont::transport::{self, Contexte, Emetteur, Recepteur};
use pont::{jcs, maintenant, Erreur};

// ---------------------------------------------------------------- exceptions

create_exception!(nearfield_pont, ErreurPont, PyException, "Refus ou erreur du protocole (message en français, à dire).");
create_exception!(nearfield_pont, ErreurFormat, ErreurPont);
create_exception!(nearfield_pont, SignatureInvalide, ErreurPont);
create_exception!(nearfield_pont, SignataireInconnu, ErreurPont);
create_exception!(nearfield_pont, DateInvalide, ErreurPont);
create_exception!(nearfield_pont, Rejeu, ErreurPont);
create_exception!(nearfield_pont, EmpreinteDifferente, ErreurPont);
create_exception!(nearfield_pont, CheminRefuse, ErreurPont);
create_exception!(nearfield_pont, CertificatRefuse, ErreurPont);
create_exception!(nearfield_pont, AppairageRefuse, ErreurPont);
create_exception!(nearfield_pont, ErreurTransport, ErreurPont);
create_exception!(nearfield_pont, MessageRefuse, ErreurPont);
create_exception!(nearfield_pont, ErreurCle, ErreurPont);
create_exception!(nearfield_pont, ErreurEntreeSortie, ErreurPont);
create_exception!(nearfield_pont, MigrationRefusee, ErreurPont);
create_exception!(nearfield_pont, DechiffrementImpossible, ErreurPont);

fn err(e: Erreur) -> PyErr {
    let texte = e.to_string();
    match e {
        Erreur::Format(_) => ErreurFormat::new_err(texte),
        Erreur::Signature => SignatureInvalide::new_err(texte),
        Erreur::Signataire(_) => SignataireInconnu::new_err(texte),
        Erreur::Date => DateInvalide::new_err(texte),
        Erreur::Rejeu => Rejeu::new_err(texte),
        Erreur::Empreinte(_) => EmpreinteDifferente::new_err(texte),
        Erreur::Chemin(_) => CheminRefuse::new_err(texte),
        Erreur::Certificat(_) => CertificatRefuse::new_err(texte),
        Erreur::Appairage(_) => AppairageRefuse::new_err(texte),
        Erreur::Transport(_) => ErreurTransport::new_err(texte),
        Erreur::Message(_) => MessageRefuse::new_err(texte),
        Erreur::Cle(_) => ErreurCle::new_err(texte),
        Erreur::Es(_) => ErreurEntreeSortie::new_err(texte),
        Erreur::Migration(_) => MigrationRefusee::new_err(texte),
        Erreur::Dechiffrement => DechiffrementImpossible::new_err(texte),
    }
}

fn es(e: std::io::Error) -> PyErr {
    err(Erreur::Es(e))
}

// ---------------------------------------------------------------- dict <-> JSON

/// Objet Python (dict, list, str, int…) vers valeur JSON, via le module `json`.
fn valeur(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    let texte: String = obj.py().import("json")?.call_method1("dumps", (obj,))?.extract()?;
    serde_json::from_str(&texte).map_err(|e| ErreurFormat::new_err(e.to_string()))
}

/// Objet Python vers type du protocole (refuse les champs inconnus).
fn depuis<T: DeserializeOwned>(obj: &Bound<'_, PyAny>, quoi: &str) -> PyResult<T> {
    serde_json::from_value(valeur(obj)?).map_err(|e| ErreurFormat::new_err(format!("{quoi} : {e}")))
}

/// Type du protocole vers objet Python.
fn vers_py<T: Serialize>(py: Python<'_>, v: &T) -> PyResult<PyObject> {
    let texte = serde_json::to_string(v).map_err(|e| ErreurFormat::new_err(e.to_string()))?;
    Ok(py.import("json")?.call_method1("loads", (texte,))?.unbind())
}

fn alg(nom: &str) -> PyResult<Alg> {
    match nom {
        "ed25519" => Ok(Alg::Ed25519),
        "p256" => Ok(Alg::P256),
        _ => Err(ErreurCle::new_err(format!("algorithme inconnu : {nom}"))),
    }
}

fn niveau(nom: &str) -> PyResult<Niveau> {
    serde_json::from_value(Value::String(nom.into())).map_err(|_| ErreurFormat::new_err(format!("niveau inconnu : {nom}")))
}

fn roles(noms: &[String]) -> PyResult<Vec<Role>> {
    noms.iter()
        .map(|r| serde_json::from_value(Value::String(r.clone())).map_err(|_| ErreurFormat::new_err(format!("rôle inconnu : {r}"))))
        .collect()
}

// ---------------------------------------------------------------- clés

/// Coffre logiciel : clé d'identité dans un fichier PKCS#8 (droits 0600).
/// La clé privée ne peut pas être relue depuis Python.
#[pyclass(frozen, module = "nearfield_pont")]
struct Coffre {
    inner: CoffreLogiciel,
}

#[pymethods]
impl Coffre {
    /// Clé neuve en mémoire seulement (tests).
    #[staticmethod]
    #[pyo3(signature = (alg_nom = "p256"))]
    fn generer(alg_nom: &str) -> PyResult<Self> {
        Ok(Coffre { inner: CoffreLogiciel::generer(alg(alg_nom)?) })
    }

    /// Relit la clé du fichier (y compris la `cle.pem` Ed25519 de la v1), ou la crée.
    #[staticmethod]
    #[pyo3(signature = (chemin, alg_nom = "p256"))]
    fn charger_ou_creer(chemin: std::path::PathBuf, alg_nom: &str) -> PyResult<Self> {
        Ok(Coffre { inner: CoffreLogiciel::charger_ou_creer(&chemin, alg(alg_nom)?).map_err(err)? })
    }

    /// Ouvre une clé existante sans jamais en créer (par exemple la `cle.pem` v1).
    #[staticmethod]
    fn ouvrir(chemin: std::path::PathBuf) -> PyResult<Self> {
        let pem = std::fs::read_to_string(&chemin).map_err(es)?;
        Ok(Coffre { inner: CoffreLogiciel::depuis_pem(&pem).map_err(err)? })
    }

    /// Clé publique d'identité : `{"alg": ..., "pub": ...}`.
    fn cle_publique(&self, py: Python<'_>) -> PyResult<PyObject> {
        vers_py(py, &self.inner.cle_publique())
    }

    /// Identifiant de la machine (16 caractères).
    #[getter]
    fn id(&self) -> PyResult<String> {
        self.inner.cle_publique().id().map_err(err)
    }

    #[getter]
    fn niveau(&self) -> &'static str {
        match self.inner.niveau() {
            Niveau::Materiel => "materiel",
            Niveau::Logiciel => "logiciel",
        }
    }

    /// Signature brute de 64 octets.
    fn signer<'py>(&self, py: Python<'py>, message: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
        Ok(PyBytes::new(py, &self.inner.signer(message).map_err(err)?))
    }

    /// Signe une charge (`type`, `parc`, `id` obligatoires) ; rend l'objet signé.
    fn signer_objet(&self, py: Python<'_>, charge: &Bound<'_, PyAny>) -> PyResult<PyObject> {
        let o = ObjetSigne::signer(&self.inner, valeur(charge)?).map_err(err)?;
        vers_py(py, &o)
    }

    fn __repr__(&self) -> String {
        format!("<Coffre {} {}>", self.id().unwrap_or_default(), self.niveau())
    }
}

/// Forme canonique (RFC 8785) d'une valeur JSON.
#[pyfunction]
fn canonique<'py>(py: Python<'py>, v: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    Ok(PyBytes::new(py, &jcs::canonique(&valeur(v)?).map_err(err)?))
}

/// Identifiant d'une clé publique d'identité.
#[pyfunction]
fn identifiant(cle: &Bound<'_, PyAny>) -> PyResult<String> {
    depuis::<CleIdentite>(cle, "clé")?.id().map_err(err)
}

/// Vérifie une signature brute.
#[pyfunction]
fn verifier_signature_brute(cle: &Bound<'_, PyAny>, message: &[u8], signature: &[u8]) -> PyResult<()> {
    depuis::<CleIdentite>(cle, "clé")?.verifier(message, signature).map_err(err)
}

// ---------------------------------------------------------------- objets signés

/// Registre anti-rejeu des `(signataire, nonce)` vus depuis 7 jours.
#[pyclass(module = "nearfield_pont")]
#[derive(Default)]
struct RegistreNonces {
    inner: objet::RegistreNonces,
}

#[pymethods]
impl RegistreNonces {
    #[new]
    fn nouveau() -> Self {
        Self::default()
    }

    #[staticmethod]
    fn charger(chemin: std::path::PathBuf) -> PyResult<Self> {
        Ok(RegistreNonces { inner: objet::RegistreNonces::charger(&chemin).map_err(err)? })
    }

    /// Écriture atomique.
    fn enregistrer(&self, chemin: std::path::PathBuf) -> PyResult<()> {
        self.inner.enregistrer(&chemin).map_err(err)
    }
}

/// Vérifie seulement la signature d'un objet avec la clé attendue.
#[pyfunction]
fn verifier_signature(objet: &Bound<'_, PyAny>, cle: &Bound<'_, PyAny>) -> PyResult<()> {
    let o: ObjetSigne = depuis(objet, "objet signé")?;
    o.verifier_signature(&depuis(cle, "clé")?).map_err(err)
}

/// Vérification complète : signature, type, parc, version, date, et rejeu si un
/// registre est donné. Rend la charge.
#[pyfunction]
#[pyo3(signature = (objet, cle, type_attendu, parc, registre = None))]
fn verifier_objet(
    py: Python<'_>,
    objet: &Bound<'_, PyAny>,
    cle: &Bound<'_, PyAny>,
    type_attendu: &str,
    parc: &str,
    registre: Option<PyRefMut<'_, RegistreNonces>>,
) -> PyResult<PyObject> {
    let o: ObjetSigne = depuis(objet, "objet signé")?;
    let cle: CleIdentite = depuis(cle, "clé")?;
    let mut registre = registre;
    o.verifier(&cle, type_attendu, parc, registre.as_mut().map(|r| &mut r.inner)).map_err(err)?;
    vers_py(py, &o.charge)
}

/// Vérifie les fichiers joints d'un objet (noms sûrs, empreintes SHA-256).
#[pyfunction]
fn verifier_fichiers(objet: &Bound<'_, PyAny>, dossier: std::path::PathBuf) -> PyResult<()> {
    depuis::<ObjetSigne>(objet, "objet signé")?.verifier_fichiers(&dossier).map_err(err)
}

#[pyfunction]
fn empreinte_fichier(py: Python<'_>, chemin: std::path::PathBuf) -> PyResult<String> {
    py.allow_threads(|| objet::empreinte_fichier(&chemin)).map_err(err)
}

/// Lève `CheminRefuse` si le chemin relatif n'est pas sûr (1 à 4 noms simples).
#[pyfunction]
fn chemin_sur(chemin: &str) -> PyResult<()> {
    objet::chemin_sur(chemin).map_err(err)
}

/// Lève `CheminRefuse` si le nom n'est pas un nom de fichier simple.
#[pyfunction]
fn nom_sur(nom: &str) -> PyResult<()> {
    objet::nom_sur(nom).map_err(err)
}

// ---------------------------------------------------------------- appairage

/// Côté machine qui rejoint le parc.
#[pyclass(module = "nearfield_pont")]
struct Rejoignant {
    etat: Option<appairage::Rejoignant>,
    #[pyo3(get)]
    engagement: String,
}

#[pymethods]
impl Rejoignant {
    #[new]
    fn nouveau(identite: &Bound<'_, PyAny>, transport: &[u8]) -> PyResult<Self> {
        let (etat, engagement) = appairage::Rejoignant::nouveau(depuis(identite, "clé")?, transport).map_err(err)?;
        Ok(Rejoignant { etat: Some(etat), engagement })
    }

    /// Reçoit la présentation de l'ordonnateur ; rend `(ma_presentation, code)`.
    fn recevoir(&mut self, py: Python<'_>, presentation: &Bound<'_, PyAny>) -> PyResult<(PyObject, String)> {
        let etat = self.etat.take().ok_or_else(|| AppairageRefuse::new_err("appairage déjà utilisé"))?;
        let b: Presentation = depuis(presentation, "présentation")?;
        let (moi, code) = etat.recevoir(&b).map_err(err)?;
        Ok((vers_py(py, &moi)?, code))
    }
}

/// Côté ordonnateur qui accueille une machine.
#[pyclass(module = "nearfield_pont")]
struct Accueil {
    etat: Option<appairage::Accueil>,
    presentation: Presentation,
}

#[pymethods]
impl Accueil {
    #[new]
    fn nouveau(engagement: &str, identite: &Bound<'_, PyAny>, transport: &[u8]) -> PyResult<Self> {
        let (etat, presentation) =
            appairage::Accueil::nouveau(engagement, depuis(identite, "clé")?, transport).map_err(err)?;
        Ok(Accueil { etat: Some(etat), presentation })
    }

    /// Présentation à envoyer à la machine qui rejoint.
    #[getter]
    fn presentation(&self, py: Python<'_>) -> PyResult<PyObject> {
        vers_py(py, &self.presentation)
    }

    /// Reçoit la présentation de la machine : elle doit tenir son engagement. Rend le code.
    fn verifier(&mut self, presentation: &Bound<'_, PyAny>) -> PyResult<String> {
        let etat = self.etat.take().ok_or_else(|| AppairageRefuse::new_err("appairage déjà utilisé"))?;
        etat.verifier(&depuis(presentation, "présentation")?).map_err(err)
    }
}

/// « 482107 » → « 48, 21, 07 ».
#[pyfunction]
fn code_parle(code: &str) -> String {
    appairage::code_parle(code)
}

// ---------------------------------------------------------------- certificats

/// Clé X25519 de transport, en mémoire seulement ; la partie privée reste en Rust.
#[pyclass(frozen, module = "nearfield_pont")]
struct CleTransport {
    inner: Arc<transport::CleTransport>,
}

#[pymethods]
impl CleTransport {
    #[new]
    fn nouvelle() -> PyResult<Self> {
        Ok(CleTransport { inner: Arc::new(transport::CleTransport::generer().map_err(err)?) })
    }

    #[getter]
    fn public<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.inner.public)
    }
}

/// L'ordonnateur émet un certificat de 12 h ; rend l'objet signé.
#[pyfunction]
#[pyo3(signature = (ordonnateur, parc, nom, identite, niveau_nom, transport, roles_noms, capacites, serie, tolerance_logiciel = false))]
#[allow(clippy::too_many_arguments)]
fn emettre_certificat(
    py: Python<'_>,
    ordonnateur: &Coffre,
    parc: &str,
    nom: &str,
    identite: &Bound<'_, PyAny>,
    niveau_nom: &str,
    transport: &[u8],
    roles_noms: Vec<String>,
    capacites: Vec<String>,
    serie: u64,
    tolerance_logiciel: bool,
) -> PyResult<PyObject> {
    let roles = roles(&roles_noms)?;
    let capacites: Vec<&str> = capacites.iter().map(String::as_str).collect();
    let d = Demande {
        nom,
        identite: depuis(identite, "clé")?,
        niveau: niveau(niveau_nom)?,
        transport,
        roles: &roles,
        capacites: &capacites,
        serie,
    };
    let o = certificat::emettre(&ordonnateur.inner, parc, d, tolerance_logiciel).map_err(err)?;
    vers_py(py, &o)
}

/// Liste de révocation : son numéro ne fait que croître.
#[pyclass(module = "nearfield_pont")]
#[derive(Default)]
struct Revocations {
    inner: ListeRevocations,
}

#[pymethods]
impl Revocations {
    #[new]
    fn nouvelle() -> Self {
        Self::default()
    }

    #[getter]
    fn numero(&self) -> u64 {
        self.inner.numero
    }

    #[getter]
    fn sujets(&self) -> Vec<String> {
        self.inner.sujets.clone()
    }

    fn est_revoque(&self, sujet: &str) -> bool {
        self.inner.est_revoque(sujet)
    }

    /// Ordonnateur : publie la liste suivante (et l'adopte) ; rend l'objet signé.
    fn publier(&mut self, py: Python<'_>, ordonnateur: &Coffre, parc: &str, sujets: Vec<String>) -> PyResult<PyObject> {
        let (suivante, o) = self.inner.publier(&ordonnateur.inner, parc, sujets).map_err(err)?;
        self.inner = suivante;
        vers_py(py, &o)
    }

    /// Machine : adopte une liste reçue, refusée si elle est plus ancienne.
    fn recevoir(&mut self, objet: &Bound<'_, PyAny>, ordonnateur: &Bound<'_, PyAny>, parc: &str) -> PyResult<()> {
        let o: ObjetSigne = depuis(objet, "objet signé")?;
        self.inner.recevoir(&o, &depuis(ordonnateur, "clé")?, parc).map_err(err)
    }
}

/// Vérifie un certificat ; rend son contenu.
#[pyfunction]
#[pyo3(signature = (objet, ordonnateur, parc, revocations, tolerance_logiciel = false, instant = None))]
fn verifier_certificat(
    py: Python<'_>,
    objet: &Bound<'_, PyAny>,
    ordonnateur: &Bound<'_, PyAny>,
    parc: &str,
    revocations: &Revocations,
    tolerance_logiciel: bool,
    instant: Option<i64>,
) -> PyResult<PyObject> {
    let o: ObjetSigne = depuis(objet, "objet signé")?;
    let c = certificat::verifier(
        &o,
        &depuis(ordonnateur, "clé")?,
        parc,
        &revocations.inner,
        tolerance_logiciel,
        instant.unwrap_or_else(maintenant),
    )
    .map_err(err)?;
    vers_py(py, &c)
}

// ---------------------------------------------------------------- messages

/// Valide un message de la liste fermée et l'encode.
#[pyfunction]
fn encoder_message<'py>(py: Python<'py>, message: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let m: Message = serde_json::from_value(valeur(message)?).map_err(|e| MessageRefuse::new_err(e.to_string()))?;
    Ok(PyBytes::new(py, &m.encoder().map_err(err)?))
}

/// Décode et valide un message reçu.
#[pyfunction]
fn decoder_message(py: Python<'_>, octets: &[u8]) -> PyResult<PyObject> {
    vers_py(py, &Message::decoder(octets).map_err(err)?)
}

// ---------------------------------------------------------------- migration v1 → v2

/// Prépare l'objet `migration` : `ancien` ouvert sur la `cle.pem` v1, `nouveau` la
/// clé v2. `transport` (32 octets) pour une machine, `None` pour l'ordonnateur.
#[pyfunction]
#[pyo3(signature = (ancien, nouveau, parc, nom, transport = None))]
fn preparer_migration(
    py: Python<'_>,
    ancien: &Coffre,
    nouveau: &Coffre,
    parc: &str,
    nom: &str,
    transport: Option<&[u8]>,
) -> PyResult<PyObject> {
    vers_py(py, &migration::preparer(&ancien.inner, &nouveau.inner, parc, nom, transport).map_err(err)?)
}

/// Ancienne clé annoncée, NON vérifiée : sert seulement à retrouver la fiche v1.
#[pyfunction]
fn ancienne_annoncee(py: Python<'_>, objet: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    vers_py(py, &migration::ancienne_annoncee(&depuis(objet, "objet signé")?).map_err(err)?)
}

/// Vérifie une migration contre la clé v1 connue de la machine ; rend
/// `{ancienne, identite, niveau, nom, transport?}`.
#[pyfunction]
#[pyo3(signature = (objet, parc, ancienne_connue, registre = None))]
fn verifier_migration(
    py: Python<'_>,
    objet: &Bound<'_, PyAny>,
    parc: &str,
    ancienne_connue: &Bound<'_, PyAny>,
    registre: Option<PyRefMut<'_, RegistreNonces>>,
) -> PyResult<PyObject> {
    let o: ObjetSigne = depuis(objet, "objet signé")?;
    let mut registre = registre;
    let m = migration::verifier(&o, parc, &depuis(ancienne_connue, "clé")?, registre.as_mut().map(|r| &mut r.inner))
        .map_err(err)?;
    vers_py(py, &m)
}

/// Migrations faites : une ancienne clé ne migre qu'une fois.
#[pyclass(module = "nearfield_pont")]
#[derive(Default)]
struct RegistreMigrations {
    inner: migration::RegistreMigrations,
}

#[pymethods]
impl RegistreMigrations {
    #[new]
    fn nouveau() -> Self {
        Self::default()
    }

    #[staticmethod]
    fn charger(chemin: std::path::PathBuf) -> PyResult<Self> {
        Ok(RegistreMigrations { inner: migration::RegistreMigrations::charger(&chemin).map_err(err)? })
    }

    fn enregistrer(&self, chemin: std::path::PathBuf) -> PyResult<()> {
        self.inner.enregistrer(&chemin).map_err(err)
    }

    /// Consigne une migration vérifiée (le dict rendu par `verifier_migration`).
    fn consigner(&mut self, migration: &Bound<'_, PyAny>) -> PyResult<()> {
        let m: Migration = depuis(migration, "migration")?;
        self.inner.consigner(&m).map_err(err)
    }

    /// Identifiant v2 vers lequel cette clé v1 a migré, ou `None`.
    fn migree_vers(&self, ancienne: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
        Ok(self.inner.migree_vers(&depuis(ancienne, "clé")?).map(str::to_string))
    }
}

// ---------------------------------------------------------------- transport

/// Paramètres de vérification du pair, copiés pour le fil réseau.
struct Confiance {
    parc: String,
    ordonnateur: CleIdentite,
    revocations: ListeRevocations,
    tolerance: bool,
    roles_acceptes: Option<Vec<Role>>,
}

impl Confiance {
    fn nouvelle(
        parc: &str,
        ordonnateur: &Bound<'_, PyAny>,
        revocations: &Revocations,
        tolerance: bool,
        roles_acceptes: Option<Vec<String>>,
    ) -> PyResult<Self> {
        Ok(Confiance {
            parc: parc.to_string(),
            ordonnateur: depuis(ordonnateur, "clé")?,
            revocations: revocations.inner.clone(),
            tolerance,
            roles_acceptes: roles_acceptes.as_deref().map(roles).transpose()?,
        })
    }

    fn verifier(&self, o: &ObjetSigne) -> pont::Resultat<Certificat> {
        let c = certificat::verifier(o, &self.ordonnateur, &self.parc, &self.revocations, self.tolerance, maintenant())?;
        if let Some(acceptes) = &self.roles_acceptes {
            if !c.roles.iter().any(|r| acceptes.contains(r)) {
                return Err(Erreur::Certificat(format!("rôle non attendu pour {}", c.nom)));
            }
        }
        Ok(c)
    }
}

fn poignee(
    flux: TcpStream,
    initiateur: bool,
    cle: &transport::CleTransport,
    certificat: &ObjetSigne,
    confiance: &Confiance,
    delai: Duration,
) -> pont::Resultat<Session> {
    // Délai borné pendant la poignée de main : un pair muet ne bloque personne.
    flux.set_read_timeout(Some(delai))?;
    flux.set_write_timeout(Some(delai))?;
    flux.set_nodelay(true)?;
    let reglage = flux.try_clone()?;
    let verifier = |o: &ObjetSigne| confiance.verifier(o);
    let ctx = Contexte { parc: &confiance.parc, cle, mon_certificat: certificat, verifier_pair: &verifier };
    let session = if initiateur { transport::initier(flux, &ctx)? } else { transport::repondre(flux, &ctx)? };
    // Session établie : plus de délai de lecture (le pair peut se taire longtemps
    // entre deux messages ; 2026-11-15, la session tombait au bout de 10 s sans
    // trafic). Le délai d'écriture reste : un envoi ne bloque jamais sans fin.
    reglage.set_read_timeout(None)?;
    let pair = session.pair().clone();
    let (emetteur, recepteur) = session.separer()?;
    Ok(Session { emetteur: Mutex::new(emetteur), recepteur: Mutex::new(recepteur), pair })
}

fn delai(secondes: f64) -> PyResult<Duration> {
    Duration::try_from_secs_f64(secondes)
        .ok()
        .filter(|d| !d.is_zero())
        .ok_or_else(|| ErreurTransport::new_err("délai invalide"))
}

/// Se connecte à l'ordonnateur (ou à un pair) et fait la poignée de main Noise.
/// Par défaut, seul un pair certifié « ordonnateur » est accepté.
#[pyfunction]
#[pyo3(signature = (adresse, parc, cle_transport, certificat, ordonnateur, revocations, tolerance_logiciel = false, roles_acceptes = Some(vec!["ordonnateur".to_string()]), delai_sec = 10.0))]
#[allow(clippy::too_many_arguments)]
fn connecter(
    py: Python<'_>,
    adresse: &str,
    parc: &str,
    cle_transport: &CleTransport,
    certificat: &Bound<'_, PyAny>,
    ordonnateur: &Bound<'_, PyAny>,
    revocations: &Revocations,
    tolerance_logiciel: bool,
    roles_acceptes: Option<Vec<String>>,
    delai_sec: f64,
) -> PyResult<Session> {
    let confiance = Confiance::nouvelle(parc, ordonnateur, revocations, tolerance_logiciel, roles_acceptes)?;
    let mon_certificat: ObjetSigne = depuis(certificat, "certificat")?;
    let cle = cle_transport.inner.clone();
    let delai = delai(delai_sec)?;
    let adresse = adresse.to_string();
    py.allow_threads(move || {
        let mut derniere = None;
        for a in adresse.to_socket_addrs()? {
            match TcpStream::connect_timeout(&a, delai) {
                Ok(flux) => return poignee(flux, true, &cle, &mon_certificat, &confiance, delai),
                Err(e) => derniere = Some(e),
            }
        }
        Err(derniere.unwrap_or_else(|| std::io::Error::other("adresse introuvable")).into())
    })
    .map_err(err)
}

/// Écoute les connexions entrantes (côté ordonnateur).
#[pyclass(frozen, module = "nearfield_pont")]
struct Ecouteur {
    ecoute: TcpListener,
}

#[pymethods]
impl Ecouteur {
    /// Par exemple `Ecouteur("0.0.0.0:47812")` ; port 0 pour un port libre.
    #[new]
    fn nouveau(adresse: &str) -> PyResult<Self> {
        Ok(Ecouteur { ecoute: TcpListener::bind(adresse).map_err(es)? })
    }

    #[getter]
    fn port(&self) -> PyResult<u16> {
        Ok(self.ecoute.local_addr().map_err(es)?.port())
    }

    /// Attend une connexion. Rend une `Entrante`, pas encore authentifiée : faire sa
    /// poignée de main (`repondre`) dans un autre fil pour ne bloquer personne.
    fn accepter(&self, py: Python<'_>) -> PyResult<Entrante> {
        let (flux, adresse) = py.allow_threads(|| self.ecoute.accept()).map_err(es)?;
        Ok(Entrante { flux: Mutex::new(Some(flux)), adresse: adresse.to_string() })
    }
}

/// Connexion acceptée mais pas encore authentifiée.
#[pyclass(frozen, module = "nearfield_pont")]
struct Entrante {
    flux: Mutex<Option<TcpStream>>,
    #[pyo3(get)]
    adresse: String,
}

#[pymethods]
impl Entrante {
    /// Poignée de main côté répondeur. Par défaut tout rôle certifié est accepté.
    #[pyo3(signature = (parc, cle_transport, certificat, ordonnateur, revocations, tolerance_logiciel = false, roles_acceptes = None, delai_sec = 10.0))]
    #[allow(clippy::too_many_arguments)]
    fn repondre(
        &self,
        py: Python<'_>,
        parc: &str,
        cle_transport: &CleTransport,
        certificat: &Bound<'_, PyAny>,
        ordonnateur: &Bound<'_, PyAny>,
        revocations: &Revocations,
        tolerance_logiciel: bool,
        roles_acceptes: Option<Vec<String>>,
        delai_sec: f64,
    ) -> PyResult<Session> {
        let confiance = Confiance::nouvelle(parc, ordonnateur, revocations, tolerance_logiciel, roles_acceptes)?;
        let mon_certificat: ObjetSigne = depuis(certificat, "certificat")?;
        let cle = cle_transport.inner.clone();
        let delai = delai(delai_sec)?;
        let flux = self
            .flux
            .lock()
            .map_err(|_| ErreurTransport::new_err("verrou"))?
            .take()
            .ok_or_else(|| ErreurTransport::new_err("connexion déjà utilisée"))?;
        py.allow_threads(move || poignee(flux, false, &cle, &mon_certificat, &confiance, delai)).map_err(err)
    }
}

/// Session chiffrée et authentifiée. `envoyer` et `recevoir` peuvent être appelés
/// depuis deux fils différents ; `fermer` débloque un `recevoir` en attente.
#[pyclass(frozen, module = "nearfield_pont")]
struct Session {
    emetteur: Mutex<Emetteur>,
    recepteur: Mutex<Recepteur>,
    pair: Certificat,
}

#[pymethods]
impl Session {
    /// Certificat vérifié de l'autre machine.
    #[getter]
    fn pair(&self, py: Python<'_>) -> PyResult<PyObject> {
        vers_py(py, &self.pair)
    }

    fn envoyer(&self, py: Python<'_>, message: &Bound<'_, PyAny>) -> PyResult<()> {
        let m: Message = serde_json::from_value(valeur(message)?).map_err(|e| MessageRefuse::new_err(e.to_string()))?;
        py.allow_threads(|| {
            let mut e = self.emetteur.lock().map_err(|_| Erreur::Transport("verrou".into()))?;
            e.envoyer(&m)
        })
        .map_err(err)
    }

    /// Message suivant (dict), ou `None` quand l'autre a fermé la connexion.
    fn recevoir(&self, py: Python<'_>) -> PyResult<Option<PyObject>> {
        let m = py
            .allow_threads(|| {
                let mut r = self.recepteur.lock().map_err(|_| Erreur::Transport("verrou".into()))?;
                r.recevoir()
            })
            .map_err(err)?;
        m.map(|m| vers_py(py, &m)).transpose()
    }

    fn fermer(&self, py: Python<'_>) {
        py.allow_threads(|| {
            if let Ok(e) = self.emetteur.lock() {
                e.fermer();
            }
        })
    }
}

// ---------------------------------------------------------------- enveloppe chiffrée

/// Clé X25519 de réception (fichier 0600) : on la publie, on ouvre avec, on ne la lit pas.
#[pyclass(frozen, module = "nearfield_pont")]
struct CleReception {
    inner: Arc<enveloppe::CleReception>,
}

#[pymethods]
impl CleReception {
    #[staticmethod]
    fn generer() -> Self {
        CleReception { inner: Arc::new(enveloppe::CleReception::generer()) }
    }

    #[staticmethod]
    fn charger_ou_creer(chemin: std::path::PathBuf) -> PyResult<Self> {
        Ok(CleReception { inner: Arc::new(enveloppe::CleReception::charger_ou_creer(&chemin).map_err(err)?) })
    }

    #[getter]
    fn public<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.inner.public)
    }

    #[getter]
    fn empreinte(&self) -> String {
        self.inner.empreinte()
    }

    /// Objet `cle_reception` signé par le coffre d'identité (vaut 7 jours).
    fn publier(&self, py: Python<'_>, coffre: &Coffre, parc: &str) -> PyResult<PyObject> {
        vers_py(py, &self.inner.publier(&coffre.inner, parc).map_err(err)?)
    }
}

fn destinataires(liste: Vec<(String, Vec<u8>)>) -> PyResult<Vec<(String, [u8; 32])>> {
    liste
        .into_iter()
        .map(|(id, cle)| {
            let cle: [u8; 32] = cle.as_slice().try_into().map_err(|_| ErreurCle::new_err("clé de réception : 32 octets"))?;
            Ok((id, cle))
        })
        .collect()
}

fn trousseau(cles: &[PyRef<'_, CleReception>]) -> Vec<Arc<enveloppe::CleReception>> {
    cles.iter().map(|c| c.inner.clone()).collect()
}

/// Vérifie une publication de clé de réception ; rend la clé publique (32 octets).
#[pyfunction]
fn verifier_cle_reception<'py>(py: Python<'py>, objet: &Bound<'py, PyAny>, identite: &Bound<'py, PyAny>, parc: &str) -> PyResult<Bound<'py, PyBytes>> {
    let o: ObjetSigne = depuis(objet, "objet signé")?;
    let cle = enveloppe::verifier_cle_reception(&o, &depuis(identite, "clé")?, parc).map_err(err)?;
    Ok(PyBytes::new(py, &cle))
}

/// Scelle des octets pour [(id, clé de réception)] ; rend l'enveloppe (dict).
#[pyfunction]
fn sceller(py: Python<'_>, destinataires_: Vec<(String, Vec<u8>)>, de: &str, contexte: &str, donnees: &[u8]) -> PyResult<PyObject> {
    let d = destinataires(destinataires_)?;
    let e = py.allow_threads(|| enveloppe::sceller(&d, de, contexte, donnees)).map_err(err)?;
    vers_py(py, &e)
}

/// Ouvre une enveloppe d'octets. `de` : signataire (vérifié) de l'objet qui la portait.
#[pyfunction]
fn ouvrir<'py>(py: Python<'py>, env: &Bound<'py, PyAny>, moi: &str, de: &str, cles: Vec<PyRef<'py, CleReception>>) -> PyResult<Bound<'py, PyBytes>> {
    let e: enveloppe::Enveloppe = depuis(env, "enveloppe")?;
    let t = trousseau(&cles);
    let clair = py
        .allow_threads(|| enveloppe::ouvrir(&e, moi, de, &t.iter().map(|c| c.as_ref()).collect::<Vec<_>>()))
        .map_err(err)?;
    Ok(PyBytes::new(py, &clair))
}

/// Scelle un fichier vers `cible` (suite de blocs) ; rend l'entête (dict).
#[pyfunction]
fn sceller_fichier(
    py: Python<'_>,
    destinataires_: Vec<(String, Vec<u8>)>,
    de: &str,
    contexte: &str,
    source: std::path::PathBuf,
    cible: std::path::PathBuf,
) -> PyResult<PyObject> {
    let d = destinataires(destinataires_)?;
    let e = py.allow_threads(|| enveloppe::sceller_fichier(&d, de, contexte, &source, &cible)).map_err(err)?;
    vers_py(py, &e)
}

/// Ouvre un fichier scellé vers `cible`, créé seulement si tout est authentifié.
#[pyfunction]
fn ouvrir_fichier<'py>(
    py: Python<'py>,
    entete: &Bound<'py, PyAny>,
    moi: &str,
    de: &str,
    cles: Vec<PyRef<'py, CleReception>>,
    source: std::path::PathBuf,
    cible: std::path::PathBuf,
) -> PyResult<()> {
    let e: enveloppe::Enveloppe = depuis(entete, "enveloppe")?;
    let t = trousseau(&cles);
    py.allow_threads(|| {
        enveloppe::ouvrir_fichier(&e, moi, de, &t.iter().map(|c| c.as_ref()).collect::<Vec<_>>(), &source, &cible)
    })
    .map_err(err)
}

// ---------------------------------------------------------------- module

#[pymodule]
fn nearfield_pont(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("VERSION", pont::VERSION)?;
    m.add("PORT", transport::PORT)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    for (nom, t) in [
        ("ErreurPont", py.get_type::<ErreurPont>()),
        ("ErreurFormat", py.get_type::<ErreurFormat>()),
        ("SignatureInvalide", py.get_type::<SignatureInvalide>()),
        ("SignataireInconnu", py.get_type::<SignataireInconnu>()),
        ("DateInvalide", py.get_type::<DateInvalide>()),
        ("Rejeu", py.get_type::<Rejeu>()),
        ("EmpreinteDifferente", py.get_type::<EmpreinteDifferente>()),
        ("CheminRefuse", py.get_type::<CheminRefuse>()),
        ("CertificatRefuse", py.get_type::<CertificatRefuse>()),
        ("AppairageRefuse", py.get_type::<AppairageRefuse>()),
        ("ErreurTransport", py.get_type::<ErreurTransport>()),
        ("MessageRefuse", py.get_type::<MessageRefuse>()),
        ("ErreurCle", py.get_type::<ErreurCle>()),
        ("ErreurEntreeSortie", py.get_type::<ErreurEntreeSortie>()),
        ("MigrationRefusee", py.get_type::<MigrationRefusee>()),
        ("DechiffrementImpossible", py.get_type::<DechiffrementImpossible>()),
    ] {
        m.add(nom, t)?;
    }
    m.add_class::<Coffre>()?;
    m.add_class::<RegistreNonces>()?;
    m.add_class::<Rejoignant>()?;
    m.add_class::<Accueil>()?;
    m.add_class::<CleTransport>()?;
    m.add_class::<Revocations>()?;
    m.add_class::<Ecouteur>()?;
    m.add_class::<Entrante>()?;
    m.add_class::<Session>()?;
    m.add_class::<RegistreMigrations>()?;
    m.add_class::<CleReception>()?;
    m.add_function(wrap_pyfunction!(canonique, m)?)?;
    m.add_function(wrap_pyfunction!(identifiant, m)?)?;
    m.add_function(wrap_pyfunction!(verifier_signature_brute, m)?)?;
    m.add_function(wrap_pyfunction!(verifier_signature, m)?)?;
    m.add_function(wrap_pyfunction!(verifier_objet, m)?)?;
    m.add_function(wrap_pyfunction!(verifier_fichiers, m)?)?;
    m.add_function(wrap_pyfunction!(empreinte_fichier, m)?)?;
    m.add_function(wrap_pyfunction!(nom_sur, m)?)?;
    m.add_function(wrap_pyfunction!(chemin_sur, m)?)?;
    m.add_function(wrap_pyfunction!(code_parle, m)?)?;
    m.add_function(wrap_pyfunction!(emettre_certificat, m)?)?;
    m.add_function(wrap_pyfunction!(verifier_certificat, m)?)?;
    m.add_function(wrap_pyfunction!(encoder_message, m)?)?;
    m.add_function(wrap_pyfunction!(decoder_message, m)?)?;
    m.add_function(wrap_pyfunction!(connecter, m)?)?;
    m.add_function(wrap_pyfunction!(preparer_migration, m)?)?;
    m.add_function(wrap_pyfunction!(ancienne_annoncee, m)?)?;
    m.add_function(wrap_pyfunction!(verifier_migration, m)?)?;
    m.add_function(wrap_pyfunction!(verifier_cle_reception, m)?)?;
    m.add_function(wrap_pyfunction!(sceller, m)?)?;
    m.add_function(wrap_pyfunction!(ouvrir, m)?)?;
    m.add_function(wrap_pyfunction!(sceller_fichier, m)?)?;
    m.add_function(wrap_pyfunction!(ouvrir_fichier, m)?)?;
    Ok(())
}
