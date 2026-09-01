"""Copie en continu les logs des watchers (journalctl) vers le NAS
(2026-09-01, demande explicite) -- survit a la rotation/troncature du
journal systemd local (plusieurs avertissements "Journal file ...
truncated" constates en session) et reste accessible meme si la carte SD
du Pi tombe en panne, meme philosophie que le depot git.

Extrait aussi les messages decodes (ACARS, Morse/multimon-ng) vers un
fichier dedie (DECODED_LOG_PATH), facile a relire sans le bruit du
reste -- repere via les marqueurs deja existants dans le code
("decode numerique:" dans radio_watch.py, "acars_watch] annonce :" dans
acars_watch.py).

Attend que le NAS soit REELLEMENT monte avant d'ecrire quoi que ce soit :
/media/nas/nd1 n'est PAS dans fstab (voir nas_manager/nas_watch.py), le
montage se fait apres coup au demarrage par un script utilisateur --
ecrire avant risquerait de remplir la carte SD et de perdre
silencieusement les lignes une fois le vrai disque monte par-dessus
(mount shadowing)."""

import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")
from sound_paths import NAS_BASE

NAS_MOUNT_POINT = "/media/nas/nd1"
LOG_DIR = f"{NAS_BASE}/logs"
DECODED_LOG_PATH = f"{LOG_DIR}/decoded_messages.log"

UNITS = [
    "radio-watch", "sncf-watch", "meteo-watch", "crue-watch",
    "adsb-watch", "stas-watch", "nas-watch",
]

MAX_LOG_SIZE_BYTES = 20 * 1024 * 1024  # rotation simple, 1 sauvegarde .1
CHECK_MOUNT_INTERVAL_SEC = 10
JOURNALCTL_RESTART_DELAY_SEC = 5

DECODED_PATTERNS = [
    re.compile(r"decode numerique\s*:\s*(.+)$"),
    re.compile(r"acars_watch\]\s*annonce\s*:\s*(.+)$"),
]


def _wait_for_nas():
    while not os.path.ismount(NAS_MOUNT_POINT):
        print(f"[log_ship] NAS pas encore monte ({NAS_MOUNT_POINT}) -- attente")
        time.sleep(CHECK_MOUNT_INTERVAL_SEC)
    os.makedirs(LOG_DIR, exist_ok=True)


def _rotate_if_needed(path):
    try:
        if os.path.getsize(path) > MAX_LOG_SIZE_BYTES:
            old_path = path + ".1"
            if os.path.exists(old_path):
                os.remove(old_path)
            os.rename(path, old_path)
    except FileNotFoundError:
        pass


def _append_line(path, line):
    _rotate_if_needed(path)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line if line.endswith("\n") else line + "\n")
    except OSError as exc:
        print(f"[log_ship] echec ecriture {path} : {exc}")


def _extract_decoded(unit, line):
    for pattern in DECODED_PATTERNS:
        m = pattern.search(line)
        if m:
            return f"[{unit}] {m.group(1)}"
    return None


def _tail_unit(unit):
    """Tourne indefiniment : relance journalctl -f si jamais il
    s'arrete (ne devrait pas arriver en usage normal, mais pas de raison
    de laisser le suivi de CE service mourir silencieusement)."""
    log_path = f"{LOG_DIR}/{unit}.log"
    while True:
        try:
            proc = subprocess.Popen(
                ["journalctl", "-u", unit, "-f", "-n", "0", "--no-pager", "-o", "short-iso"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace",
            )
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                _append_line(log_path, line)
                decoded = _extract_decoded(unit, line)
                if decoded:
                    _append_line(DECODED_LOG_PATH, decoded)
        except Exception as exc:
            print(f"[log_ship] erreur sur le suivi de {unit} : {exc}")
        print(f"[log_ship] journalctl -u {unit} -f s'est arrete -- redemarrage dans {JOURNALCTL_RESTART_DELAY_SEC}s")
        time.sleep(JOURNALCTL_RESTART_DELAY_SEC)


def main():
    print("[log_ship] demarrage")
    _wait_for_nas()
    for unit in UNITS:
        threading.Thread(target=_tail_unit, args=(unit,), daemon=True).start()
    while True:
        time.sleep(60)
        if not os.path.ismount(NAS_MOUNT_POINT):
            print("[log_ship] NAS demonte -- les ecritures echoueront (silencieusement, en log) "
                  "jusqu'au remontage par nas_watch.py")


if __name__ == "__main__":
    main()
