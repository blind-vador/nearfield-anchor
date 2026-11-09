"""Journaux separes (demande utilisateur 2026-10-02 : "quand 40 voitures
passent, on evite d'avoir plein d'enregistrements a parcourir pour trouver
la radio") : chaque enregistrement du journal (server/radio_log/) est range
dans un journal d'apres l'etiquette au debut de son nom.

Archives (extraits gardes, fleche droite ; 2026-11-06) : un seul dossier par jour,
tous journaux confondus, on archive assez peu pour ne pas compliquer.
  storage/radio/lundi_6_octobre/                 mois en cours
  storage/radio/septembre/lundi_11_septembre/    autre mois de l'annee
  storage/radio/2025/septembre/lundi_11_septembre/  autre annee
Dans le dossier : <heure>.mp3, suivi du decodeur pour un rendu annexe et du
journal s'il n'est pas radio ("20h57m44s messages.mp3"). Un titre choisi a la main
garde son nom. Les dossiers suivent le calendrier au prochain rangement.

  radio       voix captees par le scanner (bandes, canaux, aviation,
              numerique) : tout ce qui n'est pas dans les autres
  messages    messages texte transportes par radio et decodes (2026-10-03) :
              avions (ACARS, VDL2), bipeurs, messages et bulletins APRS,
              Meshtastic ; chacun est une prise vocale (lib/prise_vocale.py)
              avec sa fiche <nom>.json
  donnees     autres donnees decodees : stations APRS, planeurs, balises
  capteurs    capteurs sans fil, ballons-sondes, voitures ; plus un releve
              par capteur et par jour, meme ceux ecartes du bulletin meteo
              (console/journal_capteurs.py)
  satellites  passages et images de satellites
  plantes     calendrier de vie et conseils pour les plantes de l'utilisateur
              (lib/plantes_calendrier.py, 2026-10-06)
  transports  perturbations des reseaux de bus, tram, metro de la commune
              (watchers/stas_watch.py, 2026-10-07)
  meteo       bulletin horaire des capteurs meteo du voisinage
              (lib/bulletin_capteurs.py, 2026-10-19)
  alarmes     toutes les alarmes jouees (lib/alarmes.py, 2026-10-24) : une
              sonnerie et le message, avec la fin (compris ou sans reponse)
La veille terrain (sites militants, Mastodon, Telegram) va dans "donnees"
(2026-10-19 : elle s'entassait dans le journal radio).
"""
import datetime
import os
import re

KEEP_DIR = "/media/nas/nd2/storage/radio"
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
_MOIS_IDX = {m: i + 1 for i, m in enumerate(MOIS)}
_MOIS_IDX.update({"fevrier": 2, "aout": 8, "decembre": 12})
_NOM_JOUR = "(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"
RE_DOSSIER_JOUR = re.compile("^" + _NOM_JOUR + r"_(\d{1,2})_([a-zéû]+)$")
RE_ANCIEN_NOM = re.compile("^" + _NOM_JOUR + r" (\d{1,2}) ([a-zéû]+) (\d{4}) (\d{2}h\d{2}m\d{2}s)(\..+)$")
RE_ANNEE = re.compile(r"^\d{4}$")
ANNEES_MAX = 15   # un jour sans annee : on remonte jusqu'a son nom de jour exact
EXT_GARDEES = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".txt")
ORDER = ["alarmes", "radio", "messages", "donnees", "meteo", "capteurs", "satellites", "plantes", "transports"]
NAMES = {"alarmes": "alarmes", "radio": "radio", "messages": "messages", "donnees": "données", "meteo": "météo", "capteurs": "capteurs",
         "satellites": "satellites", "plantes": "plantes", "transports": "transports"}
PREFIXES = {
    "alarmes": ("alarme_",),
    "plantes": ("plante_",),
    "transports": ("transport_",),
    "satellites": ("passage_", "satellite", "meteor"),
    "capteurs": ("voiture", "capteur_", "capteurs_", "sonde_"),
    # anciens noms (avant le 2026-10-03) : ACARS, vdl2, pager, meshtastic -> messages
    "messages": ("message_", "ACARS", "vdl2", "pager", "meshtastic"),
    # donnees_ (2026-11-08) : ce qui est detecte, deduit ou simplement lu, sans etre un vrai message
    "donnees": ("donnees_", "APRS", "OGN_", "balise_", "lora", "terrain_", "trafic_", "securite_"),  # trafic_ : resume horaire (2026-10-23)
    "meteo": ("meteo_",),   # "meteor" (satellites) a deja ete pris plus haut
}
# Mots qui designent un journal dans une commande ("journal capteurs",
# "archives satellites"...), sans accents (matcher.normalize).
WORDS = {
    "alarmes": ("alarme", "alarmes", "urgence", "urgences", "sonnerie", "sonneries"),
    "satellites": ("satellite", "satellites", "meteor", "spatial"),
    "capteurs": ("capteur", "capteurs", "voiture", "voitures", "sonde", "sondes", "thermometre", "thermometres"),
    "messages": ("message", "messages", "acars", "vdl2", "avion", "avions", "bipeur", "bipeurs", "pager", "pagers", "dab",
                 "meshtastic"),
    "donnees": ("donnee", "donnees", "data", "aprs", "lora", "planeur", "planeurs", "balise", "balises", "terrain"),
    "radio": ("radio", "voix"),
    "plantes": ("plante", "plantes", "jardin", "jardinage"),
    "transports": ("transport", "transports", "bus", "tram", "metro", "perturbation", "perturbations"),
    "meteo": ("meteo", "bulletin", "bulletins", "temperature", "temperatures", "releve", "releves"),
}


# Messages ou donnees (demande utilisateur 2026-11-08) : un vrai message est ecrit par
# quelqu'un pour etre lu (bipeur avec texte, SMS, APRS, ACARS, SDS, FT8, ASN). Le reste
# -- detection d'un protocole, porteuse ou poste nouveau, identite, etat, signalisation,
# alias, resume -- est une donnee lue ou deduite : journal "donnees".
TYPES_DONNEES = ("FMS", "FMSFSK", "DTMF", "ZVEI1", "ZVEI2", "ZVEI3", "DZVEI", "PZVEI", "EEA", "EIA", "CCIR",
                 "FLEETSYNC", "MPT1327", "MDC1200", "X10", "CLIPFSK", "UFSK1200",
                 "AFSK1200", "AFSK2400", "AFSK2400_2", "AFSK2400_3", "HAPN4800", "FSK9600")
_TEXTE_PAGER = re.compile(r"(Alpha|Numeric|Skyper|ALN|NUM|TON)\b[:|]?\s*\S")


def etiquette_decodage(texte):
    """Etiquette de journal d'un texte decode par multimon-ng ou les decodeurs FFSK :
    "message_donnees" s'il porte un vrai message, "donnees_decodees" sinon."""
    texte = (texte or "").strip()
    nom = re.split(r"[:,|\s]", texte, 1)[0].upper()
    if nom in TYPES_DONNEES:
        return "donnees_decodees"
    if nom.startswith(("POCSAG", "FLEX")):
        return "message_donnees" if _TEXTE_PAGER.search(texte) else "donnees_decodees"
    return "message_donnees"


def category_of(filename):
    name = os.path.basename(filename)
    for category, prefixes in PREFIXES.items():
        if name.startswith(prefixes):
            return category
    return "radio"


def from_words(normalized_text):
    """Journal designe dans une commande deja normalisee ; radio par defaut."""
    words = set(normalized_text.split())
    for category in ("alarmes", "satellites", "meteo", "capteurs", "messages", "donnees", "plantes", "transports", "radio"):
        if words & set(WORDS[category]):
            return category
    return "radio"


def date_de(jour, mois, annee):
    numero = _MOIS_IDX.get(mois)
    try:
        return datetime.date(int(annee), numero, int(jour)) if numero else None
    except ValueError:
        return None


def nom_jour(date):
    return f"{JOURS[date.weekday()]}_{date.day}_{MOIS[date.month - 1]}"


def nom_heure(stamp):
    return f"{stamp.hour:02d}h{stamp.minute:02d}m{stamp.second:02d}s"


def nom_garde(stamp, category, detail=""):
    """Nom sans extension dans le dossier du jour."""
    parties = [nom_heure(stamp), detail.replace("/", "-")]
    if category != "radio":
        parties.append(category)
    return " ".join(p for p in parties if p)


def journal_du_nom(nom):
    """Journal d'un extrait range : le dernier mot de son nom, radio sinon."""
    mots = os.path.splitext(nom)[0].split()
    return mots[-1] if len(mots) >= 2 and mots[-1] in NAMES else "radio"


def dossier_du_jour(date, ref=None):
    """Ou range ce jour, vu d'aujourd'hui."""
    ref = ref or datetime.date.today()
    jour = nom_jour(date)
    if date.year != ref.year:
        return os.path.join(KEEP_DIR, str(date.year), MOIS[date.month - 1], jour)
    if date.month != ref.month:
        return os.path.join(KEEP_DIR, MOIS[date.month - 1], jour)
    return os.path.join(KEEP_DIR, jour)


def date_du_dossier(nom, annee=None, ref=None):
    """Date d'un dossier de jour. Hors d'un dossier d'annee : la plus recente
    qui tombe bien ce jour de la semaine, sans depasser aujourd'hui."""
    m = RE_DOSSIER_JOUR.match(nom)
    if not m:
        return None
    if annee is not None:
        return date_de(m[2], m[3], annee)
    ref = ref or datetime.date.today()
    for an in range(ref.year, ref.year - ANNEES_MAX, -1):
        date = date_de(m[2], m[3], an)
        if date and date <= ref and JOURS[date.weekday()] == m[1]:
            return date
    return date_de(m[2], m[3], ref.year)


def _sous_dossiers(chemin):
    try:
        return [e for e in os.scandir(chemin) if e.is_dir()]
    except OSError:
        return []


def jours_ranges(ref=None):
    """[(date, chemin)] de tous les dossiers de jour, quel que soit leur niveau.
    Seuls les noms de jour, de mois et d'annee comptent : lora, satellites... restent a part."""
    jours = []

    def mois_de(chemin, annee):
        for jour in _sous_dossiers(chemin):
            date = date_du_dossier(jour.name, annee, ref)
            if date:
                jours.append((date, jour.path))

    for e in _sous_dossiers(KEEP_DIR):
        date = date_du_dossier(e.name, None, ref)
        if date:
            jours.append((date, e.path))
        elif e.name in MOIS:
            mois_de(e.path, None)
        elif RE_ANNEE.match(e.name):
            for m in _sous_dossiers(e.path):
                if m.name in MOIS:
                    mois_de(m.path, int(e.name))
    return jours


def _libre(chemin):
    """Ne remplace jamais un fichier deja range."""
    if not os.path.exists(chemin):
        return chemin
    base, ext = os.path.splitext(chemin)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def _ranger_vrac(entree, category, ref):
    """Fichier pose directement a la racine, ou ancien archives/<journal>/."""
    m = RE_ANCIEN_NOM.match(entree.name)
    date = date_de(m[2], m[3], m[4]) if m else None
    if date:
        nom = m[5] if category == "radio" else f"{m[5]} {category}"
        nom += m[6]
    else:  # titre choisi a la main : il reste, la date du fichier donne le jour
        date = datetime.date.fromtimestamp(entree.stat().st_mtime)
        nom = entree.name
    dossier = dossier_du_jour(date, ref)
    os.makedirs(dossier, exist_ok=True)
    os.rename(entree.path, _libre(os.path.join(dossier, nom)))


def _deplacer_jour(source, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        os.rename(source, dest)
        return
    for e in os.scandir(source):
        os.rename(e.path, _libre(os.path.join(dest, e.name)))
    os.rmdir(source)


def _oter_si_vide(chemin):
    try:
        os.rmdir(chemin)
    except OSError:
        pass


def ranger_archives(ref=None):
    """Remet chaque jour a sa place selon le calendrier et range ce qui traine en
    vrac. Sans effet si tout est deja en place, ne remplace ni n'efface aucun
    fichier. Rend le nombre d'elements deplaces."""
    ref = ref or datetime.date.today()
    deplaces = 0
    anciens = os.path.join(KEEP_DIR, "archives")
    lieux = [(KEEP_DIR, "radio")] + [(os.path.join(anciens, c), c) for c in ORDER]
    for dossier, category in lieux:
        try:
            entrees = list(os.scandir(dossier))
        except OSError:
            continue
        for entree in entrees:
            try:
                if (entree.is_file() and not entree.name.startswith(".")
                        and entree.name.lower().endswith(EXT_GARDEES)):
                    _ranger_vrac(entree, category, ref)
                    deplaces += 1
            except OSError as exc:
                print(f"[journaux] {entree.name} non range : {exc}")
    for date, chemin in jours_ranges(ref):
        dest = dossier_du_jour(date, ref)
        if os.path.normpath(chemin) == os.path.normpath(dest):
            continue
        try:
            _deplacer_jour(chemin, dest)
            deplaces += 1
        except OSError as exc:
            print(f"[journaux] {chemin} non range : {exc}")
    for e in _sous_dossiers(KEEP_DIR):   # mois et annees restes vides
        if RE_ANNEE.match(e.name):
            for m in _sous_dossiers(e.path):
                if m.name in MOIS:
                    _oter_si_vide(m.path)
        if RE_ANNEE.match(e.name) or e.name in MOIS:
            _oter_si_vide(e.path)
    for c in ORDER:
        _oter_si_vide(os.path.join(anciens, c))
    _oter_si_vide(anciens)
    if deplaces:
        print(f"[journaux] archives : {deplaces} elements ranges")
    return deplaces
