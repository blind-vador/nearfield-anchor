"""Registre des commandes de la console vocale.

Pour ajouter une commande : ecrire un handler qui retourne la phrase a
dire, puis l'ajouter a COMMANDS avec des mots-cles (correspondance
certaine) et des phrases d'exemple (repli flou, voir matcher.py).
"""
import csv
import datetime
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

import requests

sys.path.insert(0, "/home/vador/autopilot/pi_console")
try:
    import credentials
except ImportError:
    credentials = None

OCTOPRINT_URL = "http://127.0.0.1:5000"
FM_RANGE = "88M:108M:125k"
FM_SCAN_TIMEOUT_SEC = 40


@dataclass
class Command:
    name: str
    description: str
    handler: object
    keywords: list = field(default_factory=list)
    examples: list = field(default_factory=list)
    confirm: str = ""  # si renseigne : question posee, "oui" exige avant d'agir


def _fr_number(value, decimals=1):
    return f"{value:.{decimals}f}".replace(".", ",")


def pi_temp():
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                             text=True, timeout=5).stdout
        value = float(out.split("=")[1].split("'")[0])
    except Exception:
        return "Impossible de lire la température du Pi."
    return f"Le Pi est à {_fr_number(value)} degrés."


def heure():
    now = datetime.datetime.now()
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre"]
    minutes = f" {now.minute}" if now.minute else ""
    return (f"Il est {now.hour} heures{minutes}, "
            f"{jours[now.weekday()]} {now.day} {mois[now.month - 1]}.")


def printer_status():
    api_key = getattr(credentials, "OCTOPRINT_API_KEY", "") if credentials else ""
    if not api_key:
        return ("Clé API OctoPrint non configurée. Ajoute OCTOPRINT_API_KEY "
                "dans le fichier credentials point py.")
    headers = {"X-Api-Key": api_key}
    try:
        printer = requests.get(f"{OCTOPRINT_URL}/api/printer", headers=headers, timeout=5)
        if printer.status_code == 409:
            return "L'imprimante n'est pas connectée à OctoPrint."
        if printer.status_code in (401, 403):
            return "OctoPrint refuse la clé API. Vérifie-la."
        printer.raise_for_status()
        job = requests.get(f"{OCTOPRINT_URL}/api/job", headers=headers, timeout=5).json()
    except requests.RequestException:
        return "OctoPrint ne répond pas."

    data = printer.json()
    temps = data.get("temperature", {})
    parts = [f"Imprimante : {data.get('state', {}).get('text', 'état inconnu')}."]
    progress = (job.get("progress") or {})
    if progress.get("completion") is not None and job.get("state", "").startswith("Printing"):
        parts.append(f"Impression à {round(progress['completion'])} pour cent.")
        left = progress.get("printTimeLeft")
        if left:
            h, m = divmod(int(left) // 60, 60)
            parts.append(f"Il reste {h} heures {m} minutes." if h else f"Il reste {m} minutes.")
    for key, label in (("tool0", "Buse"), ("bed", "Plateau")):
        t = temps.get(key)
        if t and t.get("actual") is not None:
            parts.append(f"{label} {round(t['actual'])} degrés, cible {round(t.get('target') or 0)}.")
    return " ".join(parts)


def radio_scan():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        proc = subprocess.run(["rtl_power", "-f", FM_RANGE, "-i", "2", "-1", path],
                              capture_output=True, text=True, timeout=FM_SCAN_TIMEOUT_SEC)
        if "usb_claim_interface" in proc.stderr or "Failed to open" in proc.stderr:
            return "L'antenne est occupée par la veille radio. Réessaie plus tard."
        if "No supported devices" in proc.stderr:
            return "Aucune antenne radio détectée."
        best_freq, best_db = None, None
        with open(path, newline="") as f:
            for row in csv.reader(f):
                low, step = float(row[2]), float(row[4])
                for i, db in enumerate(row[6:]):
                    db = float(db)
                    if best_db is None or db > best_db:
                        best_freq, best_db = low + i * step, db
    except subprocess.TimeoutExpired:
        return "Le scan radio a pris trop de temps."
    except (OSError, ValueError, IndexError):
        return "Le scan radio a échoué."
    finally:
        os.unlink(path)
    if best_freq is None:
        return "Le scan radio n'a rien renvoyé."
    return f"Signal FM le plus fort : {_fr_number(best_freq / 1e6)} mégahertz."


def help_text():
    lines = [f"{c.name} : {c.description}." for c in COMMANDS]
    return "Commandes disponibles. " + " ".join(lines)


def _power(action, done_text):
    def handler(announce):
        # Annonce AVANT d'agir : apres, le systeme coupe la voix en plein mot.
        announce(done_text)
        # sudo -n : echoue immediatement plutot que d'attendre un mot de
        # passe que personne ne peut taper sur cette console.
        proc = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", action],
                              capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            print(f"[console] systemctl {action} : {proc.stderr.strip()}", file=sys.stderr)
            return "Échec : la console n'a pas les droits pour le faire."
        return ""
    return handler


# "touches" et "répète" sont interceptes par main.py (ils agissent sur la
# boucle clavier elle-meme) ; ils figurent ici pour l'aide et la
# reconnaissance.
#
# redémarrer / éteindre : en tete de liste pour que toute ligne qui les
# mentionne passe par la confirmation, et SANS phrases d'exemple : jamais
# declenches par le repli flou, uniquement par mot-cle explicite.
COMMANDS = [
    Command("redémarrer", "redémarre le Pi, après confirmation",
            _power("reboot", "Redémarrage du Pi. À tout de suite."),
            keywords=["redemarre", "redemarrer", "redemarrage", "reboot"],
            confirm="Confirmer le redémarrage du Pi ? Tape oui puis entrée. Toute autre réponse annule."),
    Command("éteindre", "éteint le Pi, après confirmation",
            _power("poweroff", "Extinction du Pi. Attends trente secondes avant de couper le courant."),
            keywords=["eteins", "eteindre", "extinction", "shutdown", "arrete le pi"],
            confirm="Confirmer l'extinction du Pi ? Il faudra couper et remettre le courant pour le rallumer. "
                    "Tape oui puis entrée. Toute autre réponse annule."),
    Command("aide", "liste des commandes", help_text,
            keywords=["aide", "help", "commandes"],
            examples=["aide", "qu'est-ce que tu sais faire", "liste des commandes"]),
    Command("imprimante", "état de l'impression et températures", printer_status,
            keywords=["imprimante", "impression", "octoprint", "buse", "plateau"],
            examples=["elle en est où l'impression", "état de l'imprimante",
                      "ça imprime", "où en est le print"]),
    Command("température", "température du Pi", pi_temp,
            keywords=["temperature du pi", "temperature pi", "chaleur", "temp"],
            examples=["quelle température fait le pi", "le pi chauffe",
                      "température processeur"]),
    Command("heure", "heure et date", heure,
            keywords=["heure", "date", "quel jour"],
            examples=["il est quelle heure", "on est quel jour"]),
    Command("radio", "scan de la bande FM", radio_scan,
            keywords=["radio", "scan", "fm"],
            examples=["scanne la bande fm", "cherche une station"]),
    Command("simulation", "simule les annonces : train, avion, météo, crue, bus, radio, disque, ou tout", None,
            keywords=["simulation", "simulations", "simule", "simuler"],
            examples=["simulation"]),
    Command("touches", "repérage des touches du clavier", None,
            keywords=["touches", "touche", "clavier", "reperage"],
            examples=["test du clavier", "trouver les touches"]),
    Command("répète", "répète la dernière réponse", None,
            keywords=["repete", "repeter", "encore"],
            examples=["tu peux répéter", "redis"]),
]
