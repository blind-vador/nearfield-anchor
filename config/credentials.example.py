# --- Identifiants API ---
# Modele pour config/credentials.py (le vrai fichier, JAMAIS commite --
# voir .gitignore). Copie ce fichier en credentials.py et remplace les
# valeurs par les tiennes.
#
# A l'installation (voir install.sh), credentials.py est copie a la
# racine du repertoire de deploiement, aux cotes des watchers -- import
# direct ("from credentials import SNCF_TOKEN") sans changer la
# structure d'imports plate existante.

# API SNCF (moteur Navitia) -- compte gratuit sur numerique.sncf.com,
# 150 000 requetes/mois. Utilise par watchers/sncf_watch.py.
SNCF_TOKEN = ""

# API OpenSky Network -- compte gratuit, identifiants OAuth2 (client
# credentials) recuperes sur le tableau de bord opensky-network.org.
# Utilise par watchers/adsb_watch.py.
OPENSKY_CLIENT_ID = ""
OPENSKY_CLIENT_SECRET = ""

# Cle API OctoPrint -- a creer dans OctoPrint (Parametres > Application
# Keys). Utilisee par la commande "imprimante" de la console (console/commands.py).
OCTOPRINT_API_KEY = ""
