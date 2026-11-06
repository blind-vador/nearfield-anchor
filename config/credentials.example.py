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

# Cle API Google Gemini (offre gratuite, aistudio.google.com/apikey) --
# commande "décris" de la console (console/vision.py). Sur l'offre gratuite,
# Google peut utiliser les photos envoyees pour ameliorer ses modeles.
GEMINI_API_KEY = ""

# Autres services d'IA, facultatifs (lib/connecteurs_ia.py). Grok est payant.
FREESOUND_API_KEY = ""   # commande "cherche des sons", facultatif
GROQ_API_KEY = ""
CEREBRAS_API_KEY = ""
MISTRAL_API_KEY = ""
OPENROUTER_API_KEY = ""
GROK_API_KEY = ""
