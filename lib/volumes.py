"""Volumes par veille et par sorte de son (2026-10-22, commande console
"volume") : un volume general (la carte son), puis pour chaque veille un
curseur "volume de la veille" et, quand elle en a plusieurs, un curseur par
canal (gare : annonces, trains, ambiance du hall, ambiance meteo...).

Volume d'un son = veille x canal (en pourcentage, 0 a MAX_PCT). Applique au
vol, morceau par morceau, la ou le son est joue : audio_lock.scale (annonces,
effets, ecoutes radio), gare_ambiance (lits d'ambiance), console/voix
(reponses et touches). Un changement s'entend donc tout de suite, meme au
milieu d'un son.

Fichier local (carte SD, pas le NAS : relu tres souvent, et le son ne doit
pas dependre des disques) ; relu au plus toutes les RELIRE_SEC.
"""
import json
import os
import subprocess
import time

CHEMIN = "/home/vador/autopilot/volumes.json"
RELIRE_SEC = 1.0
MAX_PCT = 150
VEILLE = "veille"          # curseur general d'une veille
MIXER = "Speaker"          # commande de volume de la carte son USB

# Veilles (sources de son de lib/audio_lock.py) et leurs canaux. Le premier
# canal sert par defaut a tout son qui ne dit pas le sien.
VEILLES = [
    ("trains", "gare", [("annonces", "annonces de la gare"),
                        ("bruitages", "trains qui arrivent, partent ou passent"),
                        ("ambiance", "ambiance du hall"),
                        ("meteo", "ambiance météo : pluie, orage, vent, neige")]),
    ("radio", "scanner radio", [("annonces", "annonces du scanner"),
                                ("ecoute", "son des écoutes radio")]),
    ("console", "console", [("reponses", "réponses de la console"),
                            ("touches", "écho des touches"),
                            ("ecoute", "écoute radio réglée à la main")]),
    ("alertes", "alertes météo et crues", [("annonces", "annonces")]),
    ("avions", "avions", [("annonces", "annonces")]),
    ("helicos", "hélicoptères", [("annonces", "annonces")]),
    ("donnees", "données radio", [("annonces", "annonces")]),
    ("bus", "bus et tram", [("annonces", "annonces")]),
    ("imprimante", "imprimante 3D", [("annonces", "annonces")]),
    ("disques", "disques", [("annonces", "annonces")]),
]
_canaux = {s: [c for c, _ in canaux] for s, _, canaux in VEILLES}
_cache = {"t": 0.0, "mtime": None, "data": {}}


def _lire():
    now = time.time()
    if now - _cache["t"] >= RELIRE_SEC:
        _cache["t"] = now
        try:
            mtime = os.path.getmtime(CHEMIN)
        except OSError:
            mtime = None
        if mtime != _cache["mtime"]:
            try:
                with open(CHEMIN, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            _cache.update(mtime=mtime, data=data if isinstance(data, dict) else {})
    return _cache["data"]


def pct(source, canal=VEILLE):
    try:
        return int(((_lire().get(source) or {}).get(canal, 100)))
    except (TypeError, ValueError):
        return 100


def regler(source, canal, valeur):
    """Enregistre un curseur (borne entre 0 et MAX_PCT) ; rend la valeur gardee."""
    valeur = max(0, min(MAX_PCT, int(valeur)))
    data = dict(_lire())
    data[source] = dict(data.get(source) or {}, **{canal: valeur})
    tmp = CHEMIN + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CHEMIN)
    _cache.update(t=0.0, mtime=None)
    return valeur


def facteur(source, canal=None):
    """Multiplicateur d'un son de cette veille (1.0 = tel quel)."""
    canaux = _canaux.get(source)
    if not canaux:
        return pct(source) / 100
    if canal not in canaux:
        canal = canaux[0]
    return pct(source) * pct(source, canal) / 10000


# --- volume general : la carte son ----------------------------------------------------

def general():
    """Volume de la carte son en pourcentage (echelle a l'oreille, amixer -M), ou None."""
    try:
        out = subprocess.run(["amixer", "-M", "sget", MIXER], capture_output=True, text=True, timeout=5).stdout
        import re
        m = re.search(r"\[(\d+)%\]", out)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def regler_general(valeur):
    valeur = max(0, min(100, int(valeur)))
    try:
        subprocess.run(["amixer", "-q", "-M", "sset", MIXER, f"{valeur}%"], timeout=5)
        subprocess.run(["sudo", "-n", "alsactl", "store"], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # garde au redemarrage
    except (OSError, subprocess.TimeoutExpired):
        pass
    return general()
