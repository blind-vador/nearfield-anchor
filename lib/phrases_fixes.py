"""Phrases fixes du projet, decoupees en phrases (2026-10-18) : la voix
locale (Piper) les garde en cache une par une, si bien qu'une reponse ou une
annonce se reconstitue a partir de phrases deja pretes et que seul le texte
qui change (reponse de Gemini, valeurs, noms) est calcule a la demande.

Une phrase est "fixe" si elle apparait telle quelle, sans variable, dans une
phrase du projet (phrases/fr.ini, personnalite comprise) : un modele a
variables apporte aussi ses phrases sans variable.
"""
import os
import re

import phrases

SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
# Familles de phrases dites par les veilles (annonces, voix des annonces) ;
# le reste est l'interface de la console (voix de la console).
ANNONCES = {"bulletin_capteurs", "radio", "meteo_watch", "meteo", "capteur", "capteurs", "usb", "aprs", "avion", "sonde", "numerique",
            "planeur", "transport", "trains", "satellite", "satellites", "pager", "dmr", "dab", "crue", "balise",
            "donnees", "annonce", "helico", "terrain", "acars", "vdl2", "lora", "atis", "metar", "flash",
            "vigilance", "air", "station", "plantes", "manuel_radio", "notifications"}


# Famille de phrases -> source de voix (commande "voix") : les phrases d'une
# veille sont preparees dans la voix de CETTE veille seulement.
FAMILLE_SOURCE = {
    "trains": "trains", "avion": "avions", "helico": "helicos",
    "radio": "radio", "numerique": "radio", "dmr": "radio", "manuel_radio": "radio", "station": "radio",
    "aprs": "donnees", "capteur": "donnees", "capteurs": "donnees", "pager": "donnees", "lora": "donnees",
    "acars": "donnees", "vdl2": "donnees", "sonde": "donnees", "balise": "donnees", "planeur": "donnees",
    "dab": "donnees", "satellite": "donnees", "satellites": "donnees", "atis": "donnees", "metar": "donnees",
    "donnees": "donnees",
    "terrain": "terrain", "meteo": "alertes", "meteo_watch": "alertes", "crue": "alertes", "vigilance": "alertes",
    "air": "alertes", "bulletin_capteurs": "capteurs", "transport": "bus", "usb": "systeme", "notifications": "systeme", "plantes": "plantes",
}


def source_de(key):
    """Veille qui dit cette phrase, ou None (toutes : on ne sait pas)."""
    if key.startswith("imprimante.evt_"):
        return "imprimante"
    return FAMILLE_SOURCE.get(key.split(".")[0])


def split(text):
    """Texte -> phrases (espaces normalises, ponctuation gardee)."""
    return [s.strip() for s in SENTENCE_RE.split(" ".join((text or "").split())) if s.strip()]


def _famille(key):
    if key.startswith("imprimante.evt_"):
        return True
    return key.split(".")[0] in ANNONCES


_memo = {"signature": None, "tables": None}


def _signature():
    """Change quand une phrase est reecrite (personnalite du jour a minuit)."""
    folder = "/home/vador/autopilot/phrases"  # en dur (axe 4) : phrases deployees par install.sh, hors arbre du depot
    try:
        return tuple(sorted((n, os.path.getmtime(os.path.join(folder, n))) for n in os.listdir(folder)
                            if n.endswith(".ini")))
    except OSError:
        return None


def _tables():
    signature = _signature()
    if _memo["tables"] is None or signature != _memo["signature"]:
        _memo.update(signature=signature, tables=_build())
    return _memo["tables"]


def _build():
    toutes, annonces, par_source = set(), set(), {}
    for key in phrases.base():
        for s in split(phrases.texte(key) or ""):
            if "{" in s:
                continue
            toutes.add(s)
            if _famille(key):
                annonces.add(s)
                par_source.setdefault(s, set()).add(source_de(key))
    return frozenset(toutes), frozenset(annonces), par_source


def toutes():
    return _tables()[0]


def annonces():
    return _tables()[1]


def sources_annonce(sentence):
    """Veilles qui disent cette phrase (None dans l'ensemble : toutes)."""
    return _tables()[2].get(" ".join(sentence.split()), {None})


def est_fixe(sentence):
    return " ".join(sentence.split()) in _tables()[0]
