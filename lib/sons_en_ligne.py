"""Chercher des sons courts en ligne et les poser dans le depot des sonneries
(commande "cherche des sons", 2026-11-07). Openverse n'a besoin d'aucune cle ;
Freesound en direct se branche par cles/freesound.txt. Wikimedia n'est pas propose :
son serveur de fichiers refuse ce client (HTTP 429, attente de 600 s, 2026-11-07),
et ses sons sont donc aussi ecartes d'Openverse.

Chaque son telecharge a sa ligne dans sonneries/credits_sons_telecharges.txt :
titre, auteur, licence et page. Certaines licences demandent de citer l'auteur
si on partage le son.
"""
import html
import os
import re
import shutil
import tempfile
import time
import urllib.parse

import requests

import decodage
import reglages
import sonneries
import tmp_ram

ENTETES = {"User-Agent": "nearfield-anchor/1.0"}
DELAI_SEC = 15
TAILLE_MAX = 15 * 1024 * 1024
DUREE_MAX_SEC = 30
NOM_MAX = 70
RESULTATS_MAX = 30
_EXT_PAR_TYPE = {"oga": "ogg", "mpeg": "mp3"}
FOURNISSEURS = {
    "openverse": "Openverse",
    "freesound": "Freesound",
}
CREDITS = os.path.join(os.path.dirname(sonneries.DEPOT), "credits_sons_telecharges.txt")


def disponibles():
    """Les fournisseurs utilisables : Freesound seulement avec sa cle."""
    return [f for f in FOURNISSEURS if f != "freesound" or reglages.cle("freesound")]


def _ext(*candidats):
    """Extension connue des sonneries (".mp3"...), ou None."""
    for c in candidats:
        nom = os.path.splitext(urllib.parse.urlparse(c or "").path)[1] or "." + (c or "")
        nom = nom.lower().lstrip(".")
        nom = _EXT_PAR_TYPE.get(nom, nom)
        if "." + nom in sonneries.EXTENSIONS:
            return "." + nom
    return None


def _son(titre, auteur, licence, url, page, duree, ext):
    return {"titre": _titre(titre), "auteur": (auteur or "").strip(), "licence": licence or "",
            "url": url, "page": page or url, "duree": duree, "ext": ext}


def _titre(texte):
    """Titre lisible a la voix : sans balises ni extension."""
    texte = html.unescape(re.sub(r"<[^>]+>", "", texte or "")).strip()
    texte = re.sub(r"(\.[A-Za-z0-9]{2,4})+$", "", texte)
    return " ".join(texte.replace("_", " ").split()) or "son"


def _openverse(mot):
    resp = requests.get("https://api.openverse.org/v1/audio/", headers=ENTETES, timeout=DELAI_SEC,
                        params={"q": mot, "length": "shortest", "page_size": 20, "excluded_source": "wikimedia_audio"})
    resp.raise_for_status()
    sons = []
    for r in resp.json().get("results") or []:
        ext = _ext(r.get("filetype"), r.get("url"))
        if not ext or not r.get("url"):
            continue
        code = str(r.get("license") or "").upper()
        licence = f"{'' if code in ('CC0', 'PDM') else 'CC '}{code} {r.get('license_version') or ''}".strip()
        duree = (r.get("duration") or 0) / 1000 or None
        sons.append(_son(r.get("title"), r.get("creator"), licence, r["url"],
                         r.get("foreign_landing_url"), duree, ext))
    return sons


def _licence_freesound(valeur):
    valeur = valeur or ""
    if "zero" in valeur or "publicdomain" in valeur or valeur == "Creative Commons 0":
        return "CC0"
    if "nc" in valeur.lower() and ("/by-nc" in valeur or "NonCommercial" in valeur):
        return "CC BY-NC"
    return "CC BY"


def _freesound(mot):
    resp = requests.get("https://freesound.org/apiv2/search/", headers=ENTETES, timeout=DELAI_SEC, params={
        "query": mot, "token": reglages.cle("freesound"), "page_size": RESULTATS_MAX,
        "filter": f"duration:[0 TO {DUREE_MAX_SEC}]",
        "fields": "name,username,license,duration,previews,url"})
    resp.raise_for_status()
    sons = []
    for r in resp.json().get("results") or []:
        url = (r.get("previews") or {}).get("preview-hq-mp3")
        if url:
            sons.append(_son(r.get("name"), r.get("username"), _licence_freesound(r.get("license")),
                             url, r.get("url"), r.get("duration"), ".mp3"))
    return sons


_CHERCHER = {"openverse": _openverse, "freesound": _freesound}


def chercher(fournisseur, mot):
    """Liste de sons ; une erreur de reseau remonte a l'appelant."""
    return _CHERCHER[fournisseur](mot)[:RESULTATS_MAX]


def nom_de_fichier(son):
    nom = re.sub(r"[^\w ,()'-]", " ", son["titre"])
    nom = " ".join(nom.split())[:NOM_MAX].strip(" .,-'") or "son"
    return nom + son["ext"]


def _telecharger(url, dest):
    with requests.get(url, headers=ENTETES, timeout=DELAI_SEC, stream=True) as resp:
        resp.raise_for_status()
        total = 0
        with open(dest, "wb") as f:
            for bloc in resp.iter_content(65536):
                total += len(bloc)
                if total > TAILLE_MAX:
                    raise OSError("son trop gros")
                f.write(bloc)


class Apercus:
    """Sons telecharges pour l'ecoute, en memoire. Le depot les reprend sans
    les redemander au fournisseur."""

    def __init__(self):
        self.dossier = tempfile.mkdtemp(prefix="sons_ligne_", dir=tmp_ram.dossier())
        self._n = 0

    def fichier(self, son):
        """Chemin du son, telecharge au besoin ; None si impossible."""
        if son.get("fichier") and os.path.exists(son["fichier"]):
            return son["fichier"]
        self._n += 1
        chemin = os.path.join(self.dossier, f"{self._n}_{time.time_ns()}{son['ext']}")
        try:
            _telecharger(son["url"], chemin)
        except (requests.RequestException, OSError) as exc:
            print(f"[sons_en_ligne] telechargement : {exc.__class__.__name__} {getattr(getattr(exc, 'response', None), 'status_code', '')}")
            try:
                os.remove(chemin)
            except OSError:
                pass
            return None
        son["fichier"] = chemin
        return chemin

    def fermer(self):
        shutil.rmtree(self.dossier, ignore_errors=True)


def _chemin_libre(dossier, nom):
    base, ext = os.path.splitext(nom)
    chemin = os.path.join(dossier, nom)
    n = 2
    while os.path.exists(chemin):
        chemin = os.path.join(dossier, f"{base} ({n}){ext}")
        n += 1
    return chemin


def deposer(son, apercus, dossier=None):
    """Pose le son dans le depot (celui des sonneries, ou dossier : celui des decors
    de la gare), s'il se lit vraiment. Rend son chemin ou None."""
    dossier = dossier or sonneries.DEPOT
    source = apercus.fichier(son)
    if not source or not decodage.pcm(source, 8000, timeout=20):   # page d'erreur, fichier abime
        return None
    try:
        os.makedirs(dossier, exist_ok=True)
        dest = _chemin_libre(dossier, nom_de_fichier(son))
        shutil.copyfile(source, dest)
    except OSError as exc:
        print(f"[sons_en_ligne] depot : {exc}")
        return None
    try:
        with open(CREDITS, "a", encoding="utf-8") as f:
            f.write(" ; ".join((os.path.basename(dest), son["titre"], son["auteur"], son["licence"], son["page"])) + "\n")
    except OSError as exc:
        print(f"[sons_en_ligne] credits : {exc}")
    return dest
