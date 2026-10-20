"""Commande "usb" : etat des peripheriques USB et dernieres reparations
automatiques, lus dans l'etat publie par watchers/usb_watch.py
(2026-10-11)."""
import datetime
import json
import time

from phrases import Etiquettes, dire, nombre

STATE_FILE = "/tmp/usb_watch_etat.json"
STALE_SEC = 120  # la veille ecrit toutes les 15 s

NOMS = Etiquettes("usb.nom", {"clavier": "Le clavier", "son": "La carte son", "radio": "Le dongle radio",
                              "camera": "La caméra", "imprimante": "L'imprimante"},
                  "nom d'un appareil USB, en début de phrase")
ETATS = Etiquettes("usb.etat", {"ok": "fonctionne", "absent": "absent", "debranche": "débranché ou muet",
                                "veille": "en veille automatique, corrigé",
                                "sans_pilote": "vu mais pas pris en charge, réparation en cours",
                                "pilote_tnt": "pris par le pilote TNT, réparation en cours",
                                "flux_mort": "image figée, réparation en cours",
                                "deconnectee": "branchée mais déconnectée d'OctoPrint",
                                "eteint": "à l'arrêt, volontairement"},
                   "état d'un appareil USB, après son nom")


def run(line):
    """ "usb camera off" / "usb imprimante on" : appareil eteint volontairement
    ou rallume (lib/usb_eteints.py) ; sinon l'etat de tous les appareils."""
    import matcher
    import usb_eteints
    mots = set(matcher.normalize(line).split())
    roles = [usb_eteints.ROLES[m] for m in mots if m in usb_eteints.ROLES]
    eteint = bool(mots & usb_eteints.OFF)
    if roles and (eteint or mots & usb_eteints.ON):
        role = roles[0]
        usb_eteints.marquer(role, eteint)
        return dire("usb.marque_eteint" if eteint else "usb.marque_allume", appareil=NOMS[role][:1].lower() + NOMS[role][1:])
    return usb_status()


def usb_status():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return dire("usb.veille_arretee")
    if time.time() - state.get("maj", 0) > STALE_SEC:
        return dire("usb.veille_arretee")
    devices = state.get("appareils", {})
    ok = [NOMS[r][:1].lower() + NOMS[r][1:] for r in NOMS if devices.get(r) == "ok"]  # noms "en debut de phrase"
    other = [dire("usb.etat_appareil", appareil=NOMS[r], etat=ETATS[devices[r]])
             for r in NOMS if devices.get(r) not in (None, "ok", "absent") and devices[r] in ETATS]
    parts = []
    if ok:
        parts.append(dire("usb.fonctionnent", liste=", ".join(ok)))
    parts += other
    stuck = state.get("ports_bloques", [])
    if stuck:
        parts.append(dire("usb.ports_bloques", appareils=nombre(len(stuck), "appareil")))
    today = datetime.date.today()
    repairs = [r for r in state.get("reparations", [])
               if datetime.date.fromtimestamp(r["t"]) == today and r.get("resolu")]
    if repairs:
        last = datetime.datetime.fromtimestamp(repairs[-1]["t"])
        parts.append(dire("usb.reparations_jour", nombre=len(repairs), heure=last.hour,
                          minutes=f"{last.minute:02d}"))
    return " ".join(parts) or dire("usb.rien")
