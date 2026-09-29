"""Arreter / reprendre les veilles depuis la console.

  "arrete radio", "coupe les trains"     stop + desactivation (reste arretee
                                          apres un redemarrage du Pi)
  "reprends radio", "relance la meteo"   activation + demarrage
  "redemarre radio"                       simple relance du service
  "veilles" (ou "arrete" seul)            liste avec l'etat de chacune

Aiguillage AVANT la reconnaissance normale (main.py) : une action suivie
d'un nom de veille est toujours pour ce module -- "redemarre radio" relance
la radio, alors que "redemarre" seul redemarre le Pi (avec confirmation),
et "reprends" seul decroche toujours le scanner.

sudo -n limite aux commandes systemctl de ces unites (regle sudoers
generee par install.sh).
"""
import subprocess

import matcher

# (unite systemd, nom parle, mots qui la designent)
WATCHERS = [
    ("radio-watch", "radio", ["radio"]),  # pas "scan" : "reprends le scan" = decroche
    ("sncf-watch", "trains", ["train", "trains", "sncf", "gare"]),
    ("meteo-watch", "météo", ["meteo"]),
    ("crue-watch", "crues", ["crue", "crues", "inondation", "inondations"]),
    ("adsb-watch", "avions", ["avion", "avions", "adsb"]),
    ("stas-watch", "bus et tram", ["bus", "tram", "stas"]),
    ("nas-watch", "disques", ["disque", "disques", "nas"]),
    ("log-ship", "copie des journaux", ["journaux", "journal", "logs"]),
]
# "toutes les veilles" : jamais les deux qui protegent les disques.
PROTECTED = {"nas-watch", "log-ship"}
ALL_WORDS = {"toutes", "tout", "tous"}

STOP_WORDS = {"arrete", "arreter", "arretes", "stop", "stoppe", "stopper", "coupe", "couper",
              "desactive", "desactiver", "suspends", "suspend", "suspendre", "pause"}
START_WORDS = {"reprends", "reprend", "reprendre", "relance", "relancer", "reactive", "reactiver",
               "active", "activer", "demarre", "demarrer", "lance", "lancer", "remets", "rallume"}
RESTART_WORDS = {"redemarre", "redemarrer", "redemarrage"}


def _action(words):
    if words & RESTART_WORDS:
        return "restart"
    if words & STOP_WORDS:
        return "stop"
    if words & START_WORDS:
        return "start"
    return None


def parse(line):
    """(action, [unites]) si la ligne s'adresse aux veilles, sinon None.
    action "list" pour la liste seule."""
    words = set(matcher.normalize(line).split())
    action = _action(words)
    units = [unit for unit, _, aliases in WATCHERS if words & set(aliases)]
    if not units and words & ALL_WORDS and words & {"veille", "veilles"}:
        units = [unit for unit, _, _ in WATCHERS if unit not in PROTECTED]
    if action and units:
        return action, units
    if action == "stop" and len(words) == 1:
        return "list", []  # "arrete" seul ; "reprends" seul reste au scanner (decroche)
    return None


UNSTABLE_RESTARTS = 3  # au-dela, la veille "redemarre en boucle" (plante et repart)


def _since(timestamp):
    """ "@1790676390" (systemctl --timestamp=unix) -> "depuis 2 heures 5" """
    import time
    try:
        seconds = time.time() - int(timestamp.lstrip("@"))
    except ValueError:
        # Pas d'horodatage : l'unite n'a pas change d'etat depuis le demarrage.
        return "depuis le démarrage du Pi"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "depuis moins d'une minute"
    if minutes < 60:
        return f"depuis {minutes} minute{'s' if minutes > 1 else ''}"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"depuis {hours} heure{'s' if hours > 1 else ''}" + (f" {minutes}" if minutes else "")
    days = hours // 24
    return f"depuis {days} jour{'s' if days > 1 else ''}"


def _describe(unit):
    """(tourne normalement ?, etat precis a dire)."""
    out = subprocess.run(
        ["systemctl", "show", "--timestamp=unix", "-p",
         "ActiveState,SubState,NRestarts,UnitFileState,ActiveEnterTimestamp,InactiveEnterTimestamp", unit],
        capture_output=True, text=True).stdout
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    active, sub = info.get("ActiveState"), info.get("SubState")
    restarts = int(info.get("NRestarts") or 0)
    at_boot = info.get("UnitFileState") == "enabled"
    if active == "active":
        since = _since(info.get("ActiveEnterTimestamp", ""))
        if restarts >= UNSTABLE_RESTARTS:
            return False, f"active {since}, mais a redémarré {restarts} fois : instable"
        return True, f"active {since}"
    if sub == "auto-restart" or active == "activating":
        return False, f"redémarre en boucle, {restarts} fois"
    if active == "failed":
        return False, f"en panne {_since(info.get('InactiveEnterTimestamp', ''))}"
    return False, (f"arrêtée {_since(info.get('InactiveEnterTimestamp', ''))}"
                   + ("" if at_boot else ", ne repartira pas au démarrage"))


def status_text():
    states = [(name, *_describe(unit)) for unit, name, _ in WATCHERS]
    ok_count = sum(1 for _, ok, _ in states if ok)
    total = len(states)
    summary = (f"Les {total} veilles tournent normalement." if ok_count == total
               else f"{ok_count} veilles sur {total} tournent normalement.")
    details = " ".join(f"{name.capitalize()} : {text}." for name, _, text in states)
    return f"{summary} {details}"


def _systemctl(*args):
    proc = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", *args], capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def handle(action, units):
    if action == "list":
        return status_text()
    names = {unit: name for unit, name, _ in WATCHERS}
    done, failed = [], []
    for unit in units:
        if action == "stop":
            ok = _systemctl("stop", unit) and _systemctl("disable", unit)
        elif action == "start":
            ok = _systemctl("enable", unit) and _systemctl("start", unit)
        else:
            ok = _systemctl("restart", unit)
        (done if ok else failed).append(names[unit])
    verb = {"stop": "arrêtée", "start": "reprise", "restart": "relancée"}[action]
    plural = "s" if len(done) > 1 else ""
    text = f"Veille{plural} {verb}{plural} : {', '.join(done)}." if done else ""
    if action == "stop" and done:
        text += " Elle" + ("s restent" if plural else " reste") + " arrêtée" + plural + " même après un redémarrage."
    if failed:
        text += f" Échec pour : {', '.join(failed)}, la console n'a pas les droits."
    return text.strip()
