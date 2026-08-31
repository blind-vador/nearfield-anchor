# --- Chemins des banques sonores ---
# Un seul endroit a modifier pour adapter l'installation a un autre NAS,
# un autre point de montage, ou une autre organisation de dossiers --
# plutot que des chemins en dur repetes et parfois divergents dans chaque
# watcher (2026-08-31, demande explicite de l'utilisateur en preparant ce
# depot).
#
# A l'installation (voir install.sh), ce fichier est copie a la racine du
# repertoire de deploiement, aux cotes des watchers et des bibliotheques
# -- import direct ("from sound_paths import RADIO_SCANER_DIR") sans
# changer la structure d'imports plate existante.

NAS_BASE = "/media/nas/nd1/server"

RADIO_SCANER_DIR = f"{NAS_BASE}/radio_scaner"        # radio_watch.py, airband_bridge.py
METEO_SCANER_DIR = f"{NAS_BASE}/meteo_scaner"        # meteo_watch.py, crue_watch.py (partage)
TRAIN_SCANER_DIR = f"{NAS_BASE}/train_scaner"        # sncf_watch.py, stas_watch.py (partage)
FLIGHT_SCANER_DIR = f"{NAS_BASE}/flight_scaner"      # adsb_watch.py
RADIO_LOG_DIR = f"{NAS_BASE}/radio_log"              # radio_log.py (journal audio des captures)

# radiono : corrige au passage (2026-08-31) -- pointait auparavant sur
# /media/nas/nd2/server/radiono, un chemin qui n'existe pas (verifie :
# /media/nas/nd2/server/ est introuvable). Le contenu reel vit sous nd1.
RADIONO_DIR = f"{NAS_BASE}/radiono"

# Captures du mode diagnostic spectral generique (tools/spectral_diagnostic.py,
# 2026-08-31) -- identification des chevauchements de porteuses sur
# n'importe quelle bande, activable a la demande.
DIAGNOSTIC_DIR = f"{NAS_BASE}/radio_log/diagnostic"
