"""Cles de reception du parc (protocole v2 §6.2) : de quoi chiffrer pour une machine.

Chaque machine (le Pi compris) a une cle de reception X25519 par semaine, gardee chez
elle (dossier v2/reception/, fichiers 0600), et en publie la partie publique sur le
stockage partage, SIGNEE par son identite v2 (objet "cle_reception", valable 7 jours) :

  <partage>/cles_reception/<id v2>.json

Le stockage n'a aucune autorite : une publication n'est crue qu'avec la cle d'identite
connue de la machine (fiche du Pi, ou cle epinglee du Pi cote machine). Les cles des
GARDE_SEMAINES dernieres semaines restent pour ouvrir les anciennes enveloppes ; les plus
vieilles sont effacees (une enveloppe ancienne devient illisible : c'est voulu).

Commun au Pi (lib/pont_v2.py) et aux machines (bridge/nearfield_bridge.py).
Python 3.9 et plus.
"""
import base64
import json
import os
import re
import tempfile
import time

import nearfield_pont as np_

DOSSIER_PARTAGE = "cles_reception"
GARDE_SEMAINES = 5
REPUBLIER_SEC = 24 * 3600   # l'objet vaut 7 jours : republie chaque jour
_NOM = re.compile(r"^\d{4}-\d{2}\.cle$")


def semaine(t=None):
    return time.strftime("%G-%V", time.localtime(t))


def _dossier(dossier_v2):
    d = os.path.join(dossier_v2, "reception")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def cle_courante(dossier_v2):
    """Cle de la semaine (creee au besoin) ; efface celles d'il y a plus de GARDE_SEMAINES."""
    d = _dossier(dossier_v2)
    cle = np_.CleReception.charger_ou_creer(os.path.join(d, semaine() + ".cle"))
    noms = sorted(n for n in os.listdir(d) if _NOM.match(n))
    for vieux in noms[:-GARDE_SEMAINES]:
        try:
            os.remove(os.path.join(d, vieux))
        except OSError:
            pass
    return cle


def trousseau(dossier_v2):
    """Toutes nos cles de reception gardees (pour ouvrir)."""
    d = _dossier(dossier_v2)
    return [np_.CleReception.charger_ou_creer(os.path.join(d, n)) for n in sorted(os.listdir(d)) if _NOM.match(n)]


def _ecrire(chemin, data):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(chemin), prefix=".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, chemin)


def _lire(chemin):
    try:
        with open(chemin) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def publier(dossier_v2, coffre, parc, partage, forcer=False):
    """Publie la cle de la semaine si la publication manque, a change, ou a plus d'un jour.
    Rend True si on a (re)publie."""
    cle = cle_courante(dossier_v2)
    chemin = os.path.join(partage, DOSSIER_PARTAGE, coffre.id + ".json")
    actuelle = _lire(chemin)
    try:
        c = actuelle["charge"]
        a_jour = (c.get("parc") == parc and c.get("cle") == base64.b64encode(cle.public).decode()
                  and time.time() - c.get("emis", 0) < REPUBLIER_SEC)
    except (KeyError, TypeError, AttributeError):
        a_jour = False
    if a_jour and not forcer:
        return False
    _ecrire(chemin, cle.publier(coffre, parc))
    return True


def destinataires(partage, parc, identites):
    """[(id, cle publique)] des machines dont la publication est valide.
    identites : {id v2: cle d'identite connue} (celles qu'on croit, et seulement elles)."""
    trouves = []
    for mid, identite in sorted(identites.items()):
        objet = _lire(os.path.join(partage, DOSSIER_PARTAGE, mid + ".json"))
        if not objet:
            continue
        try:
            trouves.append((mid, np_.verifier_cle_reception(objet, identite, parc)))
        except np_.ErreurPont:
            continue  # perimee, d'un autre parc, ou pas signee par cette identite
    return trouves


def sceller(partage, parc, identites, de, contexte, donnees):
    """Enveloppe de `donnees` pour toutes les machines connues qui ont publie leur cle.
    Leve ErreurPont si personne ne peut la lire."""
    dest = destinataires(partage, parc, identites)
    if not dest:
        raise np_.ErreurPont("aucune cle de reception valide : personne ne pourrait lire")
    return np_.sceller(dest, de, contexte, donnees)


def ouvrir(dossier_v2, moi, de, enveloppe):
    """Ouvre une enveloppe qui nous est destinee, avec nos cles gardees."""
    return np_.ouvrir(enveloppe, moi, de, trousseau(dossier_v2))
