#!/usr/bin/env python3
"""Reparation a la volee des voix Microsoft (edge-tts), 2026-10-22.

Le 22/10, Microsoft a change le jeton d'acces de ses voix (Sec-MS-GEC) :
edge-tts 7.2.8 recevait un refus 403 et tout passait en voix de secours
depuis des jours ; la 7.3.1, sortie le 13/10, corrigeait deja.

Pas de test regulier (decision utilisateur : ce serait des requetes
gaspillees, le Pi parle de toute facon souvent). C'est la parole elle-meme
qui detecte la panne : un refus 403 pendant une synthese
(voix_sources.erreur_microsoft) note l'erreur dans le journal et lance ce
script par systemd (edge-maj.service, hors des services qu'il redemarre),
au plus une fois toutes les voix_sources.REPARATION_ECART_SEC. Alors :

  - regarde la derniere version d'edge-tts sur PyPI ;
  - plus recente : l'essaie a part (dossier temporaire, une seule synthese)
    et ne l'installe QUE si elle parle vraiment ; puis redemarre les
    services qui parlent et l'annonce a la voix ;
  - sinon : le note et l'annonce, une fois par jour au plus.

Le retour de Microsoft (premiere synthese reussie apres un refus) est note
par voix_sources.noter_microsoft. Journal : voix_sources.JOURNAL_MS, copie
sur le NAS.
"""
import chemins
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voix_sources  # noqa: E402

ETAT = chemins.base("voix_microsoft_etat.json")
VOIX_TEST = "fr-FR-DeniseNeural"
PYPI = "https://pypi.org/pypi/edge-tts/json"
SERVICES = r"watch|pi-console|voix-preparation"
RAPPEL_ECHEC_SEC = 24 * 3600   # panne sans correctif : rappel une fois par jour

ESSAI = r'''
import asyncio, sys, edge_tts
async def m():
    await edge_tts.Communicate("Essai.", sys.argv[1]).save(sys.argv[2])
asyncio.run(m())
'''


def _essai(pythonpath):
    """(ok, erreur) d'une synthese avec l'edge-tts de ce dossier."""
    env = dict(os.environ, PYTHONPATH=pythonpath)
    fd, sortie = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        r = subprocess.run([sys.executable, "-c", ESSAI, VOIX_TEST, sortie], capture_output=True, text=True,
                           timeout=45, env=env, cwd="/tmp")
        ok = r.returncode == 0 and os.path.getsize(sortie) > 1000
        return ok, "" if ok else ((r.stderr or "").strip().splitlines() or ["?"])[-1][:300]
    except subprocess.TimeoutExpired:
        return False, "delai depasse"
    finally:
        os.remove(sortie)


def _version_installee():
    try:
        return importlib.metadata.version("edge-tts")
    except importlib.metadata.PackageNotFoundError:
        return "0"


def _cle(version):
    return tuple(int(x) if x.isdigit() else 0 for x in version.split("."))


def _lire_etat():
    try:
        with open(ETAT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _ecrire_etat(etat):
    tmp = ETAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False)
    os.replace(tmp, ETAT)


def _annoncer(texte):
    try:
        import speak
        speak.speak_cloud(texte, source="systeme")
    except Exception as exc:
        print(f"[edge_maj] annonce impossible : {exc!r}", flush=True)


def _redemarrer_services():
    sortie = subprocess.run(["systemctl", "list-units", "--type=service", "--state=running", "--no-legend",
                             "--plain"], capture_output=True, text=True).stdout
    unites = [l.split()[0] for l in sortie.splitlines() if l.split() and re.search(SERVICES, l.split()[0])]
    for unite in unites:  # une par une : la regle sudo d'install.sh vise "systemctl restart <nom>"
        subprocess.run(["sudo", "-n", "systemctl", "restart", unite[:-len(".service")]], timeout=120)
    return unites


def _mettre_a_jour(installee):
    """Essaie la derniere version a part ; l'installe si elle parle. Rend
    (evenement, details)."""
    try:
        with urllib.request.urlopen(PYPI, timeout=20) as r:
            derniere = json.load(r)["info"]["version"]
    except Exception as exc:
        return "pypi_injoignable", {"erreur": repr(exc)[:200]}
    if _cle(derniere) <= _cle(installee):
        return "aucun_correctif", {"installee": installee, "derniere": derniere}
    essai = tempfile.mkdtemp(prefix="edge_essai_")
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", essai, "--no-deps",
                            f"edge-tts=={derniere}"], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return "essai_impossible", {"derniere": derniere, "erreur": (r.stderr or "")[-300:]}
        ok, erreur = _essai(essai)
        if not ok:
            return "correctif_inefficace", {"installee": installee, "derniere": derniere, "erreur": erreur}
        r = subprocess.run(["sudo", "-n", sys.executable, "-m", "pip", "install", "-q", "--upgrade",
                            "--break-system-packages", "--no-deps", f"edge-tts=={derniere}"],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return "installation_ratee", {"derniere": derniere, "erreur": (r.stderr or "")[-300:]}
        return "mis_a_jour", {"avant": installee, "apres": derniere}
    finally:
        shutil.rmtree(essai, ignore_errors=True)


def main():
    if not voix_sources._reparation().get("ouverte"):
        # lance sans refus en cours (install.sh redemarre toutes les unites) : rien a faire
        print("[edge_maj] aucun refus Microsoft en cours : rien a faire", flush=True)
        return
    installee = _version_installee()
    evenement, details = _mettre_a_jour(installee)
    voix_sources.journal_microsoft(evenement, **details)
    if evenement == "mis_a_jour":
        _ecrire_etat({"t": time.time(), "evenement": evenement, "version": details["apres"]})
        unites = _redemarrer_services()  # ce script tourne a part (systemd) : il finit sa phrase
        voix_sources.journal_microsoft("services_redemarres", services=unites)
        _annoncer(f"Microsoft avait changé l'accès à ses voix. edge-tts est passé de la version "
                  f"{details['avant']} à la version {details['apres']} : les voix Microsoft sont rétablies.")
        return
    etat = _lire_etat()
    if time.time() - etat.get("annonce", 0) >= RAPPEL_ECHEC_SEC:
        _annoncer("Les voix Microsoft sont refusées et aucune mise à jour d'edge-tts ne corrige encore le "
                  "problème : les voix de secours prennent le relais.")
        etat["annonce"] = time.time()
    etat.update(t=time.time(), evenement=evenement, version=installee)
    _ecrire_etat(etat)


if __name__ == "__main__":
    main()
