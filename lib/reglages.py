"""Reglages de nearfield anchor, sur le disque du NAS : dossier
nearfield-anchor-settings du partage "server", modifiable depuis le Mac,
l'iPhone, ou la console (commandes ville, gare, aeroport).

  lieu.json      commune de reference : meteo, avions, distances
  trains.json    gare suivie par la veille trains
  avions.json    aeroport proche et grand aeroport (contexte aerien)
  favoris.json   gares et aeroports favoris (en tete des listes de la console)
  console.json   voix de la console (Microsoft ou "espeak") et debits
  cles/*.txt     une cle API par fichier, en texte brut : coller, enregistrer

Le disque du NAS decroche parfois : chaque lecture reussie est recopiee
dans CACHE_DIR (carte SD) et sert de repli. Sans NAS ni copie, les valeurs
par defaut ci-dessous (Saint-Etienne) s'appliquent. Les cles retombent en
dernier recours sur l'ancien credentials.py.

Les cles sont relues a chaque usage (pas besoin de relancer une veille
apres avoir colle une cle) ; les lieux sont lus au demarrage des veilles,
la console relance celles concernees apres un changement.
"""
import chemins
import json
import os
import sys

from phrases import Etiquettes

SETTINGS_DIR = "/media/nas/nd1/server/nearfield-anchor-settings"
KEYS_DIR = os.path.join(SETTINGS_DIR, "cles")
CACHE_DIR = chemins.base("reglages_cache")
CREDENTIALS_DIR = chemins.base("pi_console")

DEFAULTS = {
    "lieu": {"commune": "Saint-Étienne", "code_insee": "42218",
             "departement": "42", "departement_nom": "Loire",
             "lat": 45.4397, "lon": 4.3872},
    "trains": {"gare": "Saint-Étienne Châteaucreux", "uic": "87726000"},
    "avions": {
        "proche": {"nom": "Saint-Étienne Bouthéon", "oaci": "LFMH", "lat": 45.5406, "lon": 4.2964},
        "grand": {"nom": "Lyon Saint-Exupéry", "oaci": "LFLL", "lat": 45.7256, "lon": 5.0811},
    },
    "favoris": {"gares": [], "aeroports": []},
    # debit : reponses ; debit_echo : touches et navigation (plus rapide).
    # conseils : "toujours" ou "premiere_fois" (console/conseils.py).
    # centres d'interet du scanner (lib/interets.py) : {categorie: niveau}
    "interets": {},
    # radio numerique DAB+ (watchers/dab_watch.py) : stations interdites en plus
    # des grandes radios et des commerciales, ex. ["Radio Truc"]
    "dab": {"interdites": []},
    # plantes de l'utilisateur (lib/plantes.py, commande "plantes")
    "plantes": {"plantes": []},
    # plantes reperees par Gemini sur les photos, proposees a l'ajout (touche S)
    "plantes_suggestions": {"suggestions": [], "refusees": []},
    # capteurs sans fil de l'utilisateur, appairés (lib/capteurs_miens.py)
    "capteurs": {"miens": []},
    # reseaux de transport en commun de la commune (lib/transports.py)
    "transports": {"reseaux": []},
    # voix par source (lib/voix_sources.py, commande "voix") : {source: {...}}
    "voix_sources": {},
    "console": {"voix": "fr-FR-DeniseNeural", "debit": "+20%", "debit_echo": "+50%",
                "conseils": "toujours", "conseils_vus": []},
    "imprimante": {
        "materiau": "PLA",
        "buse_mm": 0.4,
        "test_filament": True,
        "couche": "normale",
        "remplissage": "normal",
        "supports": False,
        "adherence": "skirt",
        # type de tete (demontage/remontage de buse a chaud) : "ptfe", "tout_metal", None = demander
        "tete": None,
    },
}

# fichier cles/<nom>.txt -> (ancien nom dans credentials.py, usage dit a voix haute)
KEYS = {
    "sncf": ("SNCF_TOKEN", "trains"),
    "opensky_id": ("OPENSKY_CLIENT_ID", "avions, identifiant"),
    "opensky_secret": ("OPENSKY_CLIENT_SECRET", "avions, secret"),
    "gemini": ("GEMINI_API_KEY", "description photo"),
    "octoprint": ("OCTOPRINT_API_KEY", "imprimante"),
    "airlabs": ("AIRLABS_API_KEY", "tableau des aéroports"),
    "prim": ("PRIM_API_KEY", "perturbations des transports d'Île-de-France"),
}
KEY_LABELS = Etiquettes("reglages.cle", {k: v[1] for k, v in KEYS.items()}, "usage d'une clé API, dit quand elle manque")

README = """Réglages de nearfield anchor (deepspace9)

Dossier cles : un fichier texte par clé API. Ouvrir le fichier, coller la
clé seule, enregistrer. Pas de guillemets, pas de nom devant. Les veilles
relisent les clés à chaque usage : rien à redémarrer.
  sncf.txt            api.sncf.com (veille trains)
  opensky_id.txt      OpenSky, client id (veille avions)
  opensky_secret.txt  OpenSky, client secret (veille avions)
  gemini.txt          Google Gemini (commande décris)
  octoprint.txt       OctoPrint (commande imprimante)
  airlabs.txt         AirLabs, airlabs.co (commande tableau aéroport)
  prim.txt            Île-de-France Mobilités, plateforme PRIM : perturbations
                      des bus, trams, métros et RER quand la ville choisie est
                      en Île-de-France. Compte sur prim.iledefrance-mobilites.fr,
                      menu personnel, Mon jeton d'API, Générer le jeton d'API
                      (affiché une seule fois : le coller ici aussitôt).

lieu.json, trains.json, avions.json : se règlent depuis la console avec
les commandes ville, gare et aéroport. favoris.json : l'étoile dans les
listes de gares et d'aéroports. Modifiables à la main aussi ; si
le fichier devient illisible, le Pi garde la dernière version valide.
La commande réglages de la console dit ce qui est en place.
"""


def _atomic_write(path, text, mode=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _cache(relpath, text, mode=None):
    """Recopie sur la carte SD, seulement si le contenu a change (usure)."""
    path = os.path.join(CACHE_DIR, relpath)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == text:
                    return
        except OSError:
            pass
        _atomic_write(path, text, mode)
    except OSError as exc:
        print(f"[reglages] copie locale impossible ({relpath}) : {exc}")


def _read_text(path):
    # utf-8-sig : un editeur iPhone ou Windows peut ajouter une marque BOM.
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def get(name):
    """Reglages `name` (lieu, trains, avions) : NAS, sinon copie locale,
    completes par les valeurs par defaut."""
    data = None
    try:
        text = _read_text(os.path.join(SETTINGS_DIR, f"{name}.json"))
        data = json.loads(text)
        _cache(f"{name}.json", text)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        print(f"[reglages] {name}.json illisible sur le NAS : {exc}")
    if data is None:
        try:
            data = json.loads(_read_text(os.path.join(CACHE_DIR, f"{name}.json")))
        except (OSError, ValueError):
            data = None
    merged = json.loads(json.dumps(DEFAULTS[name]))
    if isinstance(data, dict):
        merged.update(data)
    return merged


def save(name, data):
    """Ecrit sur le NAS (et la copie locale). False si le NAS est
    inaccessible : rien n'est alors enregistre, pour que le NAS reste la
    seule reference."""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        _atomic_write(os.path.join(SETTINGS_DIR, f"{name}.json"), text)
    except OSError as exc:
        print(f"[reglages] {name}.json non enregistre : {exc}")
        return False
    _cache(f"{name}.json", text)
    return True


def _clean_key(text):
    """Premiere ligne non vide, sans espaces ni guillemets autour."""
    for line in text.splitlines():
        line = line.strip().strip("\"'").strip()
        if line:
            return line
    return ""


def _credentials_value(name):
    attr = KEYS[name][0]
    if CREDENTIALS_DIR not in sys.path:
        sys.path.insert(0, CREDENTIALS_DIR)
    try:
        import credentials
    except ImportError:
        return ""
    return (getattr(credentials, attr, "") or "").strip()


def cle(name):
    """Cle API `name` (voir KEYS) : fichier du NAS, sinon copie locale,
    sinon credentials.py. Chaine vide si absente partout."""
    relpath = os.path.join("cles", f"{name}.txt")
    try:
        value = _clean_key(_read_text(os.path.join(KEYS_DIR, f"{name}.txt")))
        if value:
            _cache(relpath, value + "\n", 0o600)
            return value
    except OSError:
        try:
            value = _clean_key(_read_text(os.path.join(CACHE_DIR, relpath)))
            if value:
                return value
        except OSError:
            pass
    return _credentials_value(name)


def ensure():
    """Cree le dossier, les JSON par defaut et les fichiers de cles vides
    s'ils manquent. Une cle deja presente dans credentials.py est recopiee
    dans son fichier vide. Rend la liste des cles ainsi recopiees, ou None
    si le NAS est inaccessible."""
    migrated = []
    try:
        os.makedirs(KEYS_DIR, exist_ok=True)
        for name, values in DEFAULTS.items():
            path = os.path.join(SETTINGS_DIR, f"{name}.json")
            if not os.path.exists(path):
                _atomic_write(path, json.dumps(values, ensure_ascii=False, indent=2) + "\n")
        readme = os.path.join(SETTINGS_DIR, "LISEZMOI.txt")
        try:
            current_readme = _read_text(readme)
        except FileNotFoundError:
            current_readme = None
        if current_readme != README:  # notre fichier : tenu a jour
            _atomic_write(readme, README)
        for name in KEYS:
            path = os.path.join(KEYS_DIR, f"{name}.txt")
            try:
                current = _clean_key(_read_text(path))
            except FileNotFoundError:
                current = ""
            if current:
                continue
            old = _credentials_value(name)
            _atomic_write(path, old + "\n" if old else "")
            if old:
                migrated.append(name)
    except OSError as exc:
        print(f"[reglages] dossier de reglages non cree : {exc}")
        return None
    return migrated


def keys_status():
    """{nom: presente ?} -- jamais la valeur elle-meme."""
    return {name: bool(cle(name)) for name in KEYS}
