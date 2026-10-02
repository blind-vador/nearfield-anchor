"""Journaux separes (demande utilisateur 2026-10-02 : "quand 40 voitures
passent, on evite d'avoir plein d'enregistrements a parcourir pour trouver
la radio") : chaque enregistrement du journal (server/radio_log/) est range
dans un journal d'apres l'etiquette au debut de son nom, et chaque journal a
ses propres archives (extraits gardes, fleche droite).

  radio       voix captees par le scanner (bandes, canaux, aviation,
              numerique) : tout ce qui n'est pas dans les autres
  donnees     annonces de donnees decodees (APRS, ACARS, bipeurs, planeurs,
              balises, LoRa et Meshtastic)
  capteurs    capteurs sans fil, ballons-sondes, voitures ; plus un releve
              par capteur et par jour, meme ceux ecartes du bulletin meteo
              (console/journal_capteurs.py)
  satellites  passages et images de satellites
"""
import os

KEEP_DIR = "/media/nas/nd2/storage/radio"
ORDER = ["radio", "donnees", "capteurs", "satellites"]
NAMES = {"radio": "radio", "donnees": "données", "capteurs": "capteurs", "satellites": "satellites"}
PREFIXES = {
    "satellites": ("passage_", "satellite", "meteor"),
    "capteurs": ("voiture", "capteur_", "sonde_"),
    "donnees": ("APRS", "ACARS", "OGN_", "balise_", "pager", "meshtastic", "lora"),
}
# Mots qui designent un journal dans une commande ("journal capteurs",
# "archives satellites"...), sans accents (matcher.normalize).
WORDS = {
    "satellites": ("satellite", "satellites", "meteor", "spatial"),
    "capteurs": ("capteur", "capteurs", "voiture", "voitures", "sonde", "sondes", "thermometre", "thermometres"),
    "donnees": ("donnee", "donnees", "data", "aprs", "acars", "bipeur", "bipeurs", "lora", "meshtastic"),
    "radio": ("radio", "voix"),
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
    for category in ("satellites", "capteurs", "donnees", "radio"):
        if words & set(WORDS[category]):
            return category
    return "radio"


def archive_dir(category):
    """Extraits gardes : la radio garde son dossier d'origine, les autres
    journaux ont chacun le leur."""
    return KEEP_DIR if category == "radio" else os.path.join(KEEP_DIR, "archives", category)
