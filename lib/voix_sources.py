"""Voix par source (2026-10-19) : chaque veille (gare, avions, imprimante...),
la console et les touches du clavier ont leur propre voix, reglee par la
commande "voix" :

  voix          voix principale : une voix Microsoft, une voix Piper, espeak,
                ou "aleatoire" (une voix differente a chaque fois)
  debit         debit de la voix principale ("+20%")
  hauteur       hauteur (voix Microsoft seulement, "+10Hz")
  secours       voix Piper quand Microsoft ne repond pas, ou "aleatoire"
  secours_debit debit de la voix de secours

Etat de Microsoft partage par tous les services (MS_ETAT, tenu par la sonde
de la console et par chaque echec ou succes) : on ne fait plus attendre une
annonce 8 s sur un service qui ne repond pas.

choisir(source) rend {"moteur": "edge"|"piper"|"espeak", "voix", "debit",
"hauteur", "aleatoire"} ; fichier_cache() nomme un echantillon en cache pour
ce choix (le debit et la hauteur entrent dans le nom s'ils ne sont pas
neutres).
"""
import json
import os
import random
import sys
import time

from phrases import Etiquettes

ALEATOIRE = "aleatoire"
ALEATOIRE_PIPER = "aleatoire_piper"   # une voix locale (Piper) differente a chaque fois
ESPEAK = "espeak"
MS_ETAT = "/tmp/microsoft_etat.json"
MS_VALIDITE_SEC = 20 * 60
CACHE_SEC = 10
NEUTRE_DEBIT, NEUTRE_HAUTEUR = "+0%", "+0Hz"

SOURCES = Etiquettes("voix.source", {
    "console": "console : réponses et dialogues",
    "touches": "console : lettres et touches du clavier",
    "trains": "veille gare",
    "avions": "veille avions",
    "helicos": "veille hélicoptères",
    "radio": "veille radio : scanner, réceptions",
    "donnees": "données radio : APRS, capteurs, pagers, LoRa",
    "terrain": "veille terrain",
    "alertes": "alertes météo et crues",
    "capteurs": "bulletin horaire des capteurs météo",
    "bus": "bus et tram",
    "disques": "veille disques",
    "imprimante": "veille imprimante",
    "plantes": "plantes",
    "systeme": "système : USB, machine compagne",
}, "source de voix, dans la commande voix")
ANNONCES = [s for s in SOURCES if s not in ("console", "touches")]

# Processus -> source ; etiquette d'annonce (prise_vocale) -> source.
SCRIPTS = {"radio_watch": "radio", "sncf_watch": "trains", "meteo_watch": "alertes", "crue_watch": "alertes",
           "adsb_watch": "avions", "stas_watch": "bus", "nas_watch": "disques", "main": "console",
           "imprimante_watch": "imprimante", "veille_terrain": "terrain", "usb_watch": "systeme",
           "compagnon_watch": "systeme"}
TAGS = {"meteo_capteurs": "capteurs", "plantes": "plantes", "veille_terrain": "terrain", "terrain": "terrain", "helicos": "helicos",
        "compagnon": "systeme", "usb": "systeme"}

_cache = {"t": 0.0, "data": None}


def source_pour(tag=None):
    """Source d'une annonce : son etiquette si on la connait, sinon le service."""
    if tag in TAGS:
        return TAGS[tag]
    script = os.path.splitext(os.path.basename(sys.argv[0] or ""))[0]
    return SCRIPTS.get(script, "donnees" if tag else "console")


def _tout():
    if _cache["data"] is None or time.time() - _cache["t"] > CACHE_SEC:
        import reglages
        try:
            data = reglages.get("voix_sources") or {}
        except Exception:
            data = {}
        _cache.update(t=time.time(), data=data)
    return _cache["data"]


def _defaut(source):
    base = {"voix": ALEATOIRE, "debit": NEUTRE_DEBIT, "hauteur": NEUTRE_HAUTEUR,
            "secours": ALEATOIRE, "secours_debit": NEUTRE_DEBIT}
    if source in ("console", "touches"):
        import reglages
        console = reglages.get("console")
        base.update(voix=console.get("voix") or ESPEAK, secours=console.get("voix_secours") or "piper:fr_FR-siwis-medium",
                    debit=console.get("debit_echo" if source == "touches" else "debit", NEUTRE_DEBIT))
        base["secours_debit"] = base["debit"]
    return base


def reglage(source):
    r = _defaut(source)
    r.update(_tout().get(source) or {})
    return r


def enregistrer(source, valeurs):
    import reglages
    data = dict(reglages.get("voix_sources") or {})
    data[source] = valeurs
    ok = reglages.save("voix_sources", data)
    _cache["data"] = None
    return ok


# --- Microsoft -------------------------------------------------------------------------

def microsoft_ok():
    try:
        with open(MS_ETAT, encoding="utf-8") as f:
            etat = json.load(f)
        return bool(etat.get("ok")) and time.time() - etat.get("t", 0) < MS_VALIDITE_SEC
    except (OSError, ValueError):
        return False


def noter_microsoft(ok):
    try:
        tmp = MS_ETAT + f".{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ok": bool(ok), "t": time.time()}, f)
        os.replace(tmp, MS_ETAT)
    except OSError:
        pass


# --- choix ------------------------------------------------------------------------------

def _microsoft_voix():
    import speak
    return speak.EDGE_TTS_VOICES


def secours(source):
    import piper_out
    r = reglage(source)
    voix = piper_out.canonical(r["secours"])
    alea = voix == ALEATOIRE or not piper_out.resolve(voix)
    if alea:
        ids = piper_out.announce_voice_ids()
        if not ids:
            return {"moteur": ESPEAK, "voix": ESPEAK, "debit": r["secours_debit"], "hauteur": NEUTRE_HAUTEUR,
                    "aleatoire": False}
        voix = random.choice(ids)
    return {"moteur": "piper", "voix": voix, "debit": r["secours_debit"], "hauteur": NEUTRE_HAUTEUR,
            "aleatoire": alea}


def _pool():
    import piper_out
    return piper_out.announce_voice_ids()


def choisir(source, ms_ok=None):
    """Voix a utiliser maintenant pour cette source. Si Microsoft est absent
    alors qu'il est demande, le choix rendu est la voix de secours, avec la
    voix demandee dans "demande" : ce qu'elle a deja en cache passe avant."""
    import piper_out
    r = reglage(source)
    ms_ok = microsoft_ok() if ms_ok is None else ms_ok
    voix = r["voix"]
    if voix == ALEATOIRE_PIPER:
        pool = _pool()
        if pool:
            return {"moteur": "piper", "voix": random.choice(pool), "debit": r["debit"], "hauteur": NEUTRE_HAUTEUR,
                    "aleatoire": True}
        voix = ESPEAK
    if voix == ESPEAK:
        return {"moteur": ESPEAK, "voix": ESPEAK, "debit": r["debit"], "hauteur": NEUTRE_HAUTEUR, "aleatoire": False}
    if voix.startswith("piper:"):
        voix = piper_out.canonical(voix)
        if piper_out.resolve(voix):
            return {"moteur": "piper", "voix": voix, "debit": r["debit"], "hauteur": NEUTRE_HAUTEUR,
                    "aleatoire": False}
        return secours(source)
    alea = voix == ALEATOIRE
    if not ms_ok:
        repli = secours(source)
        repli["demande"] = {"moteur": "edge", "voix": random.choice(_microsoft_voix()) if alea else voix,
                            "debit": r["debit"], "hauteur": r["hauteur"], "aleatoire": alea}
        return repli
    return {"moteur": "edge", "voix": random.choice(_microsoft_voix()) if alea else voix,
            "debit": r["debit"], "hauteur": r["hauteur"], "aleatoire": alea}


def suffixe(choix):
    """"" ou "@+20%" / "@+20%-10Hz" : le debit et la hauteur font partie du nom."""
    debit = choix.get("debit") or NEUTRE_DEBIT
    hauteur = choix.get("hauteur") or NEUTRE_HAUTEUR if choix.get("moteur") == "edge" else NEUTRE_HAUTEUR
    if debit == NEUTRE_DEBIT and hauteur == NEUTRE_HAUTEUR:
        return ""
    return "@" + debit + ("" if hauteur == NEUTRE_HAUTEUR else hauteur)


def fichier_cache(dossier, choix):
    """Nom de l'echantillon de ce choix dans le dossier d'une phrase repetitive."""
    if choix["moteur"] == "edge":
        return os.path.join(dossier, f"{choix['voix']}{suffixe(choix)}.mp3")
    return os.path.join(dossier, f"piper-{choix['voix'].split(':', 1)[1]}{suffixe(choix)}.wav")


def voix_piper_source(source):
    """{(voix piper, debit)} que cette source peut utiliser maintenant
    (preparation a l'avance) : sa voix locale choisie, toutes celles du tirage
    si aleatoire, ou sa voix de secours tant que Microsoft ne repond pas."""
    import piper_out
    r = reglage(source)
    voix = r["voix"]
    if voix == ALEATOIRE_PIPER:
        return {(v, r["debit"]) for v in _pool()}
    if voix.startswith("piper:"):
        voix = piper_out.canonical(voix)
        return {(voix, r["debit"])} if piper_out.resolve(voix) else set()
    if voix == ESPEAK or microsoft_ok():
        return set()
    sec = piper_out.canonical(r["secours"])
    if sec == ALEATOIRE or not piper_out.resolve(sec):
        return {(v, r["secours_debit"]) for v in _pool()}
    return {(sec, r["secours_debit"])}


def voix_piper_en_service():
    found = set()
    for source in ANNONCES:
        found |= voix_piper_source(source)
    return found


def locale(voix):
    """Voix principale qui ne passe pas par Microsoft (pas d'etape secours)."""
    return voix == ALEATOIRE_PIPER or voix == ESPEAK or voix.startswith("piper:")


def resume(source):
    """Phrase courte : "voix aleatoire, secours Siwis"."""
    r = reglage(source)
    return nom_voix(r["voix"]) if locale(r["voix"]) else f"{nom_voix(r['voix'])}, secours {nom_voix(r['secours'])}"


def nom_voix(voix):
    import piper_out
    if voix == ALEATOIRE:
        return "aléatoire"
    if voix == ALEATOIRE_PIPER:
        return "aléatoire, voix locales"
    if voix == ESPEAK:
        return "espeak"
    if voix.startswith("piper:"):
        return piper_out.label(piper_out.canonical(voix)).split(",")[0]
    parts = voix.split("-")
    return parts[2].replace("MultilingualNeural", "").replace("Neural", "") if len(parts) > 2 else voix
