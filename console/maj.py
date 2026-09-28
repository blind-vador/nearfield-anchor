"""Mise a jour complete du systeme depuis la console, suivie a la voix.

apt-get update, full-upgrade, autoremove --purge, clean -- en arriere-plan
(ca peut durer un quart d'heure sur le Pi), avec seulement quelques
etapes parlees : pas de noms de paquets ni de tailles, le detail complet
part dans un journal (NAS si monte, sinon dossier personnel).

Securites :
- stdin=DEVNULL + DEBIAN_FRONTEND=noninteractive + --force-confold : la
  console tourne sur tty1, apt ne doit JAMAIS poser de question sur ce tty
  (il se figerait en avalant le clavier) ;
- sudo -n : echoue tout de suite si les droits manquent (regle sudoers
  limitee, voir install.sh), au lieu d'attendre un mot de passe.
"""
import os
import re
import subprocess
import sys
import threading
from datetime import datetime

LOG_DIRS = ["/media/nas/nd1/server/logs", os.path.expanduser("~")]
APT_ENV = dict(os.environ, DEBIAN_FRONTEND="noninteractive", LC_ALL="C")
APT_OPTS = ["-y", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
# Paquets dont la mise a jour ne prend effet qu'au redemarrage.
REBOOT_HINTS = ("linux-image", "raspberrypi-kernel", "raspi-firmware", "raspberrypi-bootloader",
                "libc6", "systemd", "rpi-eeprom")
MILESTONES = [(0.25, "Un quart de l'installation est fait."),
              (0.5, "La moitié de l'installation est faite."),
              (0.75, "Trois quarts de l'installation sont faits.")]

_thread = None
_stage = ""


def busy():
    """Etat parle si une mise a jour tourne deja, sinon chaine vide."""
    if _thread is not None and _thread.is_alive():
        return f"Mise à jour en cours : {_stage}"
    return ""


def _log_path():
    for folder in LOG_DIRS:
        if os.path.isdir(folder) and (folder != LOG_DIRS[0] or os.path.ismount("/media/nas/nd1")):
            return os.path.join(folder, f"mise_a_jour_{datetime.now():%Y-%m-%d_%Hh%M}.log")
    return "/tmp/mise_a_jour.log"


def _apt(args, log, on_line=None):
    """Lance sudo apt-get ; rend (code retour, sortie d'erreur utile)."""
    proc = subprocess.Popen(["sudo", "-n", "/usr/bin/apt-get", *args], env=APT_ENV,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace")
    last_error = ""
    for line in proc.stdout:
        log.write(line)
        if line.startswith(("E:", "sudo:")):
            last_error = line
        if on_line:
            on_line(line)
    return proc.wait(), last_error


def _explain(error):
    if "Could not get lock" in error or "Unable to acquire" in error:
        return "Une autre installation est déjà en cours sur le Pi. Réessaie plus tard."
    if "sudo" in error or "password" in error:
        return "La console n'a pas les droits pour faire la mise à jour."
    if "Temporary failure" in error or "Could not resolve" in error:
        return "Pas d'accès à internet."
    return "Le détail est dans le journal de mise à jour."


def _run(say):
    global _stage
    path = _log_path()
    with open(path, "w") as log:
        _stage = "recherche des mises à jour."
        say("Recherche des mises à jour.")
        code, err = _apt(["update"], log)
        if code != 0:
            say("La recherche des mises à jour a échoué. " + _explain(err))
            return

        # Simulation (-s) : nombre de paquets concernes, sans rien toucher.
        sim = subprocess.run(["apt-get", "-s", "full-upgrade"], env=APT_ENV, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True)
        match = re.search(r"(\d+) upgraded, (\d+) newly installed", sim.stdout)
        total = (int(match.group(1)) + int(match.group(2))) if match else 0
        if total == 0:
            say("Tout est déjà à jour.")
        else:
            say(f"{total} paquets à mettre à jour. Téléchargement.")
            _stage = "téléchargement."
            state = {"setup": 0, "installing": False, "next": 0, "reboot": False}

            def progress(line):
                global _stage
                if line.startswith(("Unpacking", "Setting up")) and not state["installing"]:
                    state["installing"] = True
                    _stage = "installation, début."
                    say("Téléchargement fini. Installation en cours.")
                if line.startswith("Setting up"):
                    state["setup"] += 1
                    if any(h in line for h in REBOOT_HINTS):
                        state["reboot"] = True
                    while (state["next"] < len(MILESTONES)
                           and state["setup"] >= MILESTONES[state["next"]][0] * total):
                        _stage = MILESTONES[state["next"]][1].lower()
                        say(MILESTONES[state["next"]][1])
                        state["next"] += 1

            code, err = _apt(["full-upgrade", *APT_OPTS], log, progress)
            if code != 0:
                say("L'installation des mises à jour a échoué. " + _explain(err))
                return
            say("Installation terminée.")

        _stage = "nettoyage."
        say("Nettoyage.")
        _apt(["autoremove", "--purge", *APT_OPTS], log)
        _apt(["clean"], log)

        reboot = os.path.exists("/var/run/reboot-required") or (total and state["reboot"])
        end = "Mise à jour terminée, système propre."
        if reboot:
            end += " Un redémarrage est conseillé : tape redémarre quand tu veux."
        say(end)


def start(say):
    """Lance la mise a jour en arriere-plan ; say(texte) doit bloquer
    jusqu'a la fin de la phrase (les etapes s'enchainent dans l'ordre)."""
    global _thread

    def run():
        try:
            _run(say)
        except Exception as exc:
            print(f"[console] mise a jour : {exc!r}", file=sys.stderr)
            say("La mise à jour s'est arrêtée sur une erreur inattendue.")

    _thread = threading.Thread(target=run, daemon=True)
    _thread.start()
