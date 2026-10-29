# --- Racines du systeme de fichiers ---
# Un seul endroit a adapter pour installer le scanner sur une autre machine :
# aucun chemin personnel (utilisateur, machine) n'est cable en dur ailleurs.
#
# Historique : les chemins etaient repetes en dur (/home/vador/...) dans ~165
# fichiers -- reflexe des sessions d'agents precedentes. Centralises ici le
# 2026-10-29 a la demande explicite de l'utilisateur : le code doit etre
# viable sur n'importe quelle machine Linux, sans reference a un utilisateur
# ni a une machine en particulier.
#
# Comme sound_paths.py (banques sonores sur le NAS), ce fichier est sur le
# PYTHONPATH a plat -- import direct "import chemins" / "from chemins import
# BASE". Chaque racine lit une variable d'environnement si elle est definie,
# sinon un defaut relatif a l'utilisateur courant.
import os

# Repertoire personnel de l'utilisateur courant, qui qu'il soit.
HOME = os.environ.get("NFA_USER_HOME") or os.path.expanduser("~")

# Etat runtime du scanner (jadis /home/vador/autopilot) : ecrit par le code,
# propre a CETTE machine, hors depot. Surchargeable par NFA_HOME.
BASE = os.environ.get("NFA_HOME") or os.path.join(HOME, "autopilot")

# Depot git du code. Ce fichier vit dans <depot>/config/chemins.py, donc on
# remonte de deux niveaux -- aucune variable requise. Surchargeable par NFA_REPO.
DEPOT = os.environ.get("NFA_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Voix Piper (jadis /home/vador/piper). Surchargeable par NFA_PIPER.
PIPER = os.environ.get("NFA_PIPER") or os.path.join(HOME, "piper")


def base(*parts):
    """Chemin sous l'etat runtime : base('reglages_cache', 'priorites.json')."""
    return os.path.join(BASE, *parts)


def depot(*parts):
    """Chemin sous le depot du code : depot('tools', 'audit_securite.py')."""
    return os.path.join(DEPOT, *parts)


def home(*parts):
    """Chemin sous le repertoire personnel de l'utilisateur courant."""
    return os.path.join(HOME, *parts)
