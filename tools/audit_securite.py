#!/usr/bin/env python3
"""Controle de securite hebdomadaire du Pi (2026-10-23, demande utilisateur :
"que le depot vive avec ses mises a jour"). Lance par securite-audit.timer.

  1. rafraichit la liste des paquets (apt-get update) et compte les mises a
     jour systeme en attente, en relevant celles des services exposes sur le
     reseau (OpenSSL, nginx, haproxy, PHP, SSH, Samba...) ;
  2. interroge la base publique OSV (api.osv.dev) pour les paquets Python
     installes par pip (/usr/local, ~/.local) : ceux de Debian sont corriges
     par les mises a jour systeme sans changer de numero de version, OSV les
     croit donc a tort vulnerables -- ils sont laisses a apt ;
  3. ecrit le bilan (RAPPORT) et, s'il y a quelque chose a faire, une prise
     vocale au journal "donnees" (securite_..., non jouee).

La commande console "securite" lit le dernier bilan ; "mise a jour" applique
les mises a jour systeme ; "securite pip" met a jour les paquets pip signales.
"""
import chemins
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RAPPORT = chemins.base("securite_rapport.json")
EXPOSES = ("openssl", "libssl", "nginx", "haproxy", "php", "openssh", "samba", "sudo", "curl", "libcurl",
           "linux-image", "raspi-firmware", "mariadb", "python3-lxml", "python3-pil", "python3-cryptography",
           "python3-urllib3", "python3-requests")
GRAVITE = {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "MEDIUM": 2, "LOW": 1}
ETIQUETTE = "securite_bilan"


def maj_systeme():
    subprocess.run(["sudo", "-n", "apt-get", "update", "-q"], capture_output=True, timeout=900,
                   env=dict(os.environ, DEBIAN_FRONTEND="noninteractive", LC_ALL="C"))
    out = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=120,
                         env=dict(os.environ, LC_ALL="C")).stdout
    paquets = [l.split("/")[0] for l in out.splitlines() if "/" in l and not l.startswith("Listing")]
    return paquets, [p for p in paquets if p.startswith(EXPOSES)]


def paquets_pip():
    out = subprocess.run([sys.executable, "-m", "pip", "list", "-v", "--format=json"], capture_output=True,
                         text=True, timeout=120).stdout
    try:
        liste = json.loads(out)
    except ValueError:
        return []
    return [(p["name"], p["version"]) for p in liste
            if not str(p.get("location", "")).startswith("/usr/lib/python3/")]


def _post(url, data):
    req = urllib.request.Request(url, json.dumps(data).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _vuln(vid):
    with urllib.request.urlopen(f"https://api.osv.dev/v1/vulns/{vid}", timeout=30) as r:
        return json.load(r)


def _correctif(v, nom):
    for a in v.get("affected", []):
        if a.get("package", {}).get("name", "").lower().replace("_", "-") != nom.lower().replace("_", "-"):
            continue
        for r in a.get("ranges", []):
            fix = [e["fixed"] for e in r.get("events", []) if "fixed" in e]
            if fix:
                return fix[-1]
    return None


def failles_pip(paquets):
    """{paquet: {"version", "gravite", "corrige_en", "alertes", "resume"}}"""
    if not paquets:
        return {}
    rep = _post("https://api.osv.dev/v1/querybatch",
                {"queries": [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in paquets]})
    out = {}
    for (n, v), res in zip(paquets, rep.get("results", [])):
        ids = [x["id"] for x in res.get("vulns", [])]
        ids = [i for i in ids if i.startswith("GHSA")] or ids
        for vid in ids:
            d = _vuln(vid)
            g = ((d.get("database_specific") or {}).get("severity") or "INCONNUE").upper()
            e = out.setdefault(n, {"version": v, "gravite": "LOW", "corrige_en": None, "alertes": 0, "resume": ""})
            e["alertes"] += 1
            if GRAVITE.get(g, 0) >= GRAVITE.get(e["gravite"], 0):
                e["gravite"], e["resume"] = g, (d.get("summary") or "")[:120]
            fix = _correctif(d, n)
            if fix and (e["corrige_en"] is None or _cle(fix) > _cle(e["corrige_en"])):
                e["corrige_en"] = fix
    return out


def _cle(version):
    return [int(x) if x.isdigit() else 0 for x in version.replace("-", ".").split(".")]


def texte(rapport):
    from phrases import dire, nombre
    parts = []
    n, exposes = len(rapport["systeme"]), rapport["systeme_exposes"]
    if n:
        parts.append(dire("securite.systeme", nombre=nombre(n, "mise à jour", "mises à jour"),
                          exposes=", ".join(sorted({p.split("-")[0] for p in exposes})[:6]) or dire("securite.aucun_expose")))
    pip = rapport["pip"]
    if pip:
        graves = sorted(pip.items(), key=lambda kv: -GRAVITE.get(kv[1]["gravite"], 0))
        parts.append(dire("securite.pip", nombre=nombre(len(pip), "paquet"),
                          liste=", ".join(f"{k} {v['version']} ({dire('securite.gravite_' + v['gravite'].lower())})"
                                          for k, v in graves[:5])))
    if not parts:
        return dire("securite.rien")
    return " ".join(parts) + " " + dire("securite.conseil")


def main():
    systeme, exposes = maj_systeme()
    try:
        pip = failles_pip(paquets_pip())
        erreur = None
    except Exception as exc:
        pip, erreur = {}, repr(exc)[:200]
    rapport = {"t": time.time(), "date": time.strftime("%Y-%m-%d %H:%M"), "systeme": systeme,
               "systeme_exposes": exposes, "pip": pip, "erreur_osv": erreur}
    rapport["texte"] = texte(rapport)
    tmp = RAPPORT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rapport, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RAPPORT)
    print(f"[audit_securite] {rapport['texte']}", flush=True)
    if True:  # chaque bilan au journal, avec son son : faille ou rien a signaler (2026-10-23, sons.py)
        try:
            import prise_vocale
            import sons
            from sound_paths import METEO_SCANER_DIR
            son = sons.chemin("securite_faille" if (systeme or pip) else "securite_bilan",
                              os.path.join(METEO_SCANER_DIR, "simple_message.mp3"))
            prise_vocale.record(rapport["texte"], son, ETIQUETTE,
                                meta={"source": "controle de securite", **rapport}, source="systeme")
        except Exception as exc:
            print(f"[audit_securite] prise non enregistree : {exc!r}", flush=True)


if __name__ == "__main__":
    main()
