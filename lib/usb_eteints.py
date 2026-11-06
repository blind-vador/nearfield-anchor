"""Appareils USB eteints volontairement (commande "usb camera off",
2026-10-20) : la veille USB (watchers/usb_watch.py) ne les annonce plus
comme debranches et ne les rappelle plus ; "usb" les dit a l'arret,
volontairement. La marque part toute seule quand l'appareil revient, ou
par "usb camera on".
"""
import chemins
import json
import os
import subprocess
import time

FICHIER = chemins.base("usb_eteints.json")

# mots tapes -> role de la veille USB (usb_watch.NOMS)
ROLES = {"camera": "camera", "webcam": "camera", "imprimante": "imprimante", "printer": "imprimante",
         "radio": "radio", "dongle": "radio", "cle": "radio", "son": "son", "audio": "son", "clavier": "clavier"}
OFF = {"off", "eteint", "eteinte", "eteins", "eteindre", "coupe", "coupee", "arret", "arrete", "arretee", "debranche",
       "debranchee"}
ON = {"on", "allume", "allumee", "rallume", "rallumee", "branche", "branchee", "rebranche", "rebranchee", "remis",
      "remise"}


def lire():
    try:
        with open(FICHIER, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def marquer(role, eteint):
    data = lire()
    if eteint:
        data[role] = time.time()
    else:
        data.pop(role, None)
    tmp = FICHIER + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, FICHIER)
    if role == "camera":
        _webcamd(eteint)


def _webcamd(eteint):
    """Camera eteinte : webcamd arrete et desactive, sinon il la cherche toutes les
    deux minutes, redemarrage compris. Rallumee : il repart."""
    try:
        subprocess.run(["sudo", "-n", "systemctl", "disable" if eteint else "enable", "--now", "webcamd"],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[usb_eteints] webcamd : {exc!r}")


def appliquer():
    """Au demarrage de la veille USB : une camera deja marquee eteinte."""
    if "camera" in lire():
        _webcamd(True)
