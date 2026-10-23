"""Commande "relais" (2026-10-23) : les relais radioamateurs appris par
l'APRS (lib/relais.py), du plus proche au plus loin, et ce qu'on en entend
vraiment : ecoutes du scanner, captations, confirmations par la tonalite
d'acces (CTCSS). Puis combien de relais lointains sont connus sans etre
balayes. C'est la carte de ce qui est audible d'ici.
"""
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import relais  # noqa: E402
from phrases import dire, nombre  # noqa: E402

MODES = {"fm": "FM", "dmr": "DMR", "dstar": "D-Star", "c4fm": "C4FM"}


def _mhz(f):
    return f"{int(f) / 1e6:.4f}".rstrip("0").rstrip(".").replace(".", ",")


def _depuis(t):
    minutes = max(1, round((time.time() - t) / 60))
    if minutes < 90:
        return nombre(minutes, "minute")
    heures = round(minutes / 60)
    return nombre(heures, "heure") if heures < 48 else nombre(round(heures / 24), "jour")


def texte():
    connus = relais.load()
    if not connus:
        return dire("relais.aucun")
    aud = relais.audibilite()
    parts = []
    for f, r in sorted(connus.items(), key=lambda kv: kv[1].get("km", 999)):
        a = aud.get(f, {})
        tete = dire("relais.fiche", nom=r.get("nom", "?"), mhz=_mhz(f), mode=MODES.get(r.get("mode"), r.get("mode")),
                    km=round(r.get("km", 0)))
        if r.get("tonalite"):
            tete += ", " + dire("relais.tonalite", hz=r["tonalite"])
        if a.get("captures"):
            etat = dire("relais.entendu", fois=nombre(a["captures"], "fois", "fois"),
                        depuis=_depuis(a.get("dernier_entendu", time.time())))
            if a.get("confirmations"):
                etat += ", " + dire("relais.confirme", fois=nombre(a["confirmations"], "fois", "fois"))
        elif a.get("ecoutes"):
            etat = dire("relais.jamais_entendu", fois=nombre(a["ecoutes"], "écoute"))
        else:
            etat = dire("relais.pas_encore_ecoute")
        parts.append(f"{tete} : {etat}")
    texte_ = dire("relais.titre", nombre=nombre(len(connus), "relais", "relais"), km=relais.RANGE_KM) + " " + ". ".join(parts) + "."
    loin = relais.lointains()
    if loin:
        proches = sorted(loin.values(), key=lambda r: r.get("km", 9999))[:3]
        texte_ += " " + dire("relais.lointains", nombre=nombre(len(loin), "relais", "relais"),
                             exemples=", ".join(f"{r.get('lieu') or r.get('nom')} à {round(r.get('km', 0))} kilomètres"
                                                for r in proches))
    return texte_
