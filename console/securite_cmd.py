"""Commande "securite" (2026-10-23) : le dernier controle de securite du Pi
(tools/audit_securite.py, chaque semaine) ; "securite pip" met a jour les
paquets Python installes par pip qui ont une faille corrigee, puis refait le
controle. Les mises a jour systeme passent par la commande "mise a jour".
"""
import json
import subprocess
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import matcher  # noqa: E402
from phrases import dire, nombre  # noqa: E402

RAPPORT = "/home/vador/autopilot/securite_rapport.json"
AUDIT = "/home/vador/nearfield_anchor/tools/audit_securite.py"  # script du depot (axe 4)


def _rapport():
    try:
        with open(RAPPORT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _age(t):
    jours = int((time.time() - t) // 86400)
    return dire("securite.aujourdhui") if jours < 1 else dire("securite.il_y_a", duree=nombre(jours, "jour"))


def _maj_pip(rapport):
    cibles = [f"{nom}>={e['corrige_en']}" for nom, e in (rapport.get("pip") or {}).items() if e.get("corrige_en")]
    if not cibles:
        return dire("securite.pip_rien")
    r = subprocess.run(["sudo", "-n", sys.executable, "-m", "pip", "install", "-q", "--upgrade",
                        "--break-system-packages"] + cibles, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        print(f"[securite] pip : {r.stderr[-400:]}", file=sys.stderr)
        return dire("securite.pip_echec")
    subprocess.run([sys.executable, AUDIT], capture_output=True, timeout=1800)
    return dire("securite.pip_fait", nombre=nombre(len(cibles), "paquet")) + " " + (_rapport() or {}).get("texte", "")


def run(line):
    rapport = _rapport()
    if not rapport:
        return dire("securite.pas_de_bilan")
    if "pip" in matcher.normalize(line or "").split():
        return _maj_pip(rapport)
    return dire("securite.bilan_du", quand=_age(rapport["t"])) + " " + rapport.get("texte", "")
