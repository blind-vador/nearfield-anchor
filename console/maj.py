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
from phrases import dire, nombre

LOG_DIRS = ["/media/nas/nd1/server/logs", os.path.expanduser("~")]
APT_ENV = dict(os.environ, DEBIAN_FRONTEND="noninteractive", LC_ALL="C")
APT_OPTS = ["-y", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
# Paquets dont la mise a jour ne prend effet qu'au redemarrage.
REBOOT_HINTS = ("linux-image", "raspberrypi-kernel", "raspi-firmware", "raspberrypi-bootloader",
                "libc6", "systemd", "rpi-eeprom")
MILESTONES = [(0.25, "maj.quart"), (0.5, "maj.moitie"), (0.75, "maj.trois_quarts")]  # cles des phrases

_thread = None
_stage = ""


def busy():
    """Etat parle si une mise a jour tourne deja, sinon chaine vide."""
    if _thread is not None and _thread.is_alive():
        return dire("maj.mise_jour_cours", v1=_stage)
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
        return dire("maj.autre_installation_deja_cours")
    if "sudo" in error or "password" in error:
        return dire("maj.console_droits_faire_mise")
    if "Temporary failure" in error or "Could not resolve" in error:
        return dire("maj.acces_internet")
    return dire("maj.detail_journal_mise_jour")


def _newer_kernel_installed():
    """Raspberry Pi OS ne pose pas toujours /var/run/reboot-required apres
    un nouveau noyau (constate le 2026-09-29 : 6.12 installe, 6.6 toujours
    actif, aucun drapeau). On compare donc directement le noyau actif a
    ceux presents dans /lib/modules, de la meme variante (v7l, v8...)."""
    running = os.uname().release  # ex. 6.6.51+rpt-rpi-v7l
    variant = running.rsplit("-", 1)[-1]

    def version(release):
        return tuple(int(p) for p in re.findall(r"\d+", release.split("+")[0]))

    try:
        installed = [d for d in os.listdir("/lib/modules") if d.endswith("-" + variant)]
    except OSError:
        return False
    return any(version(d) > version(running) for d in installed)


def _run(say):
    global _stage
    path = _log_path()
    with open(path, "w") as log:
        _stage = dire("maj.recherche_mises_jour")
        say(dire("maj.recherche_mises_jour_2"))
        code, err = _apt(["update"], log)
        if code != 0:
            say((dire("maj.recherche_mises_jour_echoue") + " ") + _explain(err))
            return

        # Simulation (-s) : nombre de paquets concernes, sans rien toucher.
        sim = subprocess.run(["apt-get", "-s", "full-upgrade"], env=APT_ENV, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True)
        match = re.search(r"(\d+) upgraded, (\d+) newly installed", sim.stdout)
        total = (int(match.group(1)) + int(match.group(2))) if match else 0
        if total == 0:
            say(dire("maj.tout_deja_jour"))
        else:
            say(dire("maj.paquets", paquets=nombre(total, "paquet")))
            _stage = "téléchargement."
            state = {"setup": 0, "installing": False, "next": 0, "reboot": False}

            def progress(line):
                global _stage
                if line.startswith(("Unpacking", "Setting up")) and not state["installing"]:
                    state["installing"] = True
                    _stage = dire("maj.installation_debut")
                    say(dire("maj.telechargement_fini_installation_cours"))
                if line.startswith("Setting up"):
                    state["setup"] += 1
                    if any(h in line for h in REBOOT_HINTS):
                        state["reboot"] = True
                    while (state["next"] < len(MILESTONES)
                           and state["setup"] >= MILESTONES[state["next"]][0] * total):
                        milestone = dire(MILESTONES[state["next"]][1])
                        _stage = milestone.lower()
                        say(milestone)
                        state["next"] += 1

            code, err = _apt(["full-upgrade", *APT_OPTS], log, progress)
            if code != 0:
                say((dire("maj.installation_mises_jour_echoue") + " ") + _explain(err))
                return
            say(dire("maj.installation_terminee"))

        _stage = "nettoyage."
        say(dire("maj.nettoyage"))
        _apt(["autoremove", "--purge", *APT_OPTS], log)
        _apt(["clean"], log)

        reboot = (os.path.exists("/var/run/reboot-required") or (total and state["reboot"])
                  or _newer_kernel_installed())
        end = dire("maj.mise_jour_terminee_systeme")
        if reboot:
            end += (" " + dire("maj.redemarrage_conseille_tape_redemarre"))
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
            say(dire("maj.mise_jour_arretee_erreur"))

    _thread = threading.Thread(target=run, daemon=True)
    _thread.start()
