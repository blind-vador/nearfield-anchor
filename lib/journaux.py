"""Journaux separes (demande utilisateur 2026-10-02 : "quand 40 voitures
passent, on evite d'avoir plein d'enregistrements a parcourir pour trouver
la radio") : chaque enregistrement du journal (server/radio_log/) est range
dans un journal d'apres l'etiquette au debut de son nom, et chaque journal a
ses propres archives (extraits gardes, fleche droite).

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
La veille terrain (sites militants, Mastodon, Telegram) va dans "donnees"
(2026-10-19 : elle s'entassait dans le journal radio).
"""
import os

KEEP_DIR = "/media/nas/nd2/storage/radio"
ORDER = ["radio", "messages", "donnees", "meteo", "capteurs", "satellites", "plantes", "transports"]
NAMES = {"radio": "radio", "messages": "messages", "donnees": "données", "meteo": "météo", "capteurs": "capteurs",
         "satellites": "satellites", "plantes": "plantes", "transports": "transports"}
PREFIXES = {
    "plantes": ("plante_",),
    "transports": ("transport_",),
    "satellites": ("passage_", "satellite", "meteor"),
    "capteurs": ("voiture", "capteur_", "capteurs_", "sonde_"),
    # anciens noms (avant le 2026-10-03) : ACARS, vdl2, pager, meshtastic -> messages
    "messages": ("message_", "ACARS", "vdl2", "pager", "meshtastic"),
    "donnees": ("APRS", "OGN_", "balise_", "lora", "terrain_", "trafic_", "securite_"),  # trafic_ : resume horaire (2026-10-23)
    "meteo": ("meteo_",),   # "meteor" (satellites) a deja ete pris plus haut
}
# Mots qui designent un journal dans une commande ("journal capteurs",
# "archives satellites"...), sans accents (matcher.normalize).
WORDS = {
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


def category_of(filename):
    name = os.path.basename(filename)
    for category, prefixes in PREFIXES.items():
        if name.startswith(prefixes):
            return category
    return "radio"


def from_words(normalized_text):
    """Journal designe dans une commande deja normalisee ; radio par defaut."""
    words = set(normalized_text.split())
    for category in ("satellites", "meteo", "capteurs", "messages", "donnees", "plantes", "transports", "radio"):
        if words & set(WORDS[category]):
            return category
    return "radio"


def archive_dir(category):
    """Extraits gardes : la radio garde son dossier d'origine, les autres
    journaux ont chacun le leur."""
    return KEEP_DIR if category == "radio" else os.path.join(KEEP_DIR, "archives", category)
