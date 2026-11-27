"""Identifiant Navitia d'une gare (API SNCF), retrouve a partir de son code UIC et de son nom.

Le 2026-11-24, l'API SNCF a abandonne les identifiants "stop_area:SNCF:<UIC>" pour Saint-Etienne
Chateaucreux (et d'autres gares) au profit de "stop_area:SNC:FR::LMU:<uuid>:" : chaque requete rendait
404 ("unknown_object"), la veille de la gare, le tableau des trains et le flash se taisaient sans rien dire.
On essaie l'ancienne forme, puis on cherche la gare par son nom (places), et on garde la reponse.
Un 404 plus tard efface la reponse gardee : on cherche de nouveau.
"""
import json
import os
import time
import unicodedata

import requests

import chemins
import reglages

API = "https://api.sncf.com/v1/coverage/sncf"
CACHE = chemins.base("reglages_cache/gares_sncf.json")
TIMEOUT = 15
NOUVEL_ESSAI_SEC = 600   # gare introuvable : pas plus d'une recherche toutes les 10 minutes

_memoire = {}
_echecs = {}


def _norme(texte):
    t = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in t).split())


def _lire_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ecrire_cache(data):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE)
    except OSError:
        pass


def _existe(ident, cle):
    r = requests.get("%s/stop_areas/%s" % (API, ident), auth=(cle, ""), timeout=TIMEOUT)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


def _chercher(nom, cle):
    r = requests.get("%s/places" % API, params={"q": nom, "type[]": "stop_area", "count": 10},
                     auth=(cle, ""), timeout=TIMEOUT)
    r.raise_for_status()
    voulu = _norme(nom)
    gares = [p for p in r.json().get("places") or [] if p.get("embedded_type") == "stop_area"]
    for p in gares:  # meme nom d'abord, puis un nom qui le contient
        if _norme((p.get("stop_area") or {}).get("name")) == voulu:
            return p["id"]
    for p in gares:
        if voulu and voulu in _norme(p.get("name")):
            return p["id"]
    return None


def identifiant(uic, nom, cle=None):
    """"stop_area:..." de la gare, ou None (cle absente, reseau, gare introuvable)."""
    uic = str(uic)
    if uic in _memoire:
        return _memoire[uic]
    gardes = _lire_cache()
    if uic in gardes and gardes[uic].get("nom") == nom:
        _memoire[uic] = gardes[uic]["id"]
        return _memoire[uic]
    if time.time() - _echecs.get(uic, 0) < NOUVEL_ESSAI_SEC:
        return None
    cle = cle or reglages.cle("sncf")
    if not cle:
        return None
    try:
        ident = "stop_area:SNCF:%s" % uic
        if not _existe(ident, cle):
            ident = _chercher(nom, cle)
    except (requests.RequestException, ValueError) as exc:
        print("[gare_sncf] recherche de %s impossible : %s" % (nom, exc))
        return None
    if not ident:
        _echecs[uic] = time.time()
        print("[gare_sncf] gare %s (UIC %s) introuvable dans l'API SNCF" % (nom, uic))
        return None
    _memoire[uic] = ident
    gardes[uic] = {"nom": nom, "id": ident, "t": int(time.time())}
    _ecrire_cache(gardes)
    print("[gare_sncf] %s : %s" % (nom, ident))
    return ident


def oublier(uic):
    """A appeler sur un 404 : l'identifiant garde ne vaut plus."""
    uic = str(uic)
    _memoire.pop(uic, None)
    gardes = _lire_cache()
    if gardes.pop(uic, None) is not None:
        _ecrire_cache(gardes)


def url(uic, nom, genre, cle=None):
    """URL des departs ("departures") ou arrivees ("arrivals") ; repli sur l'ancienne forme sans reponse."""
    ident = identifiant(uic, nom, cle) or "stop_area:SNCF:%s" % uic
    return "%s/stop_areas/%s/%s" % (API, ident, genre)


def get(uic, nom, genre, params, cle=None):
    """requests.get des departs ou arrivees ; un 404 efface l'identifiant garde et cherche une fois de plus."""
    cle = cle or reglages.cle("sncf")
    resp = requests.get(url(uic, nom, genre, cle), params=params, auth=(cle, ""), timeout=TIMEOUT)
    if resp.status_code == 404:
        oublier(uic)
        resp = requests.get(url(uic, nom, genre, cle), params=params, auth=(cle, ""), timeout=TIMEOUT)
    return resp
