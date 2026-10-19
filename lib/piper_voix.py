"""Catalogue officiel des voix Piper et installation automatique (2026-10-18).

Source : depot officiel du projet Piper, rhasspy/piper-voices sur Hugging
Face (version v1.0.0) ; son catalogue voices.json donne pour chaque voix la
langue, la qualite, les locuteurs, et la taille et l'empreinte MD5 de
chaque fichier. Une installation telecharge le modele (.onnx), sa
configuration (.onnx.json) et sa fiche (MODEL_CARD, licence et origine des
enregistrements), verifie taille et empreinte, puis seulement les met en
place (rien d'a moitie installe). Extraits officiels a ecouter avant
d'installer : samples/speaker_<n>.mp3 du meme depot.
"""
import hashlib
import json
import os
import time

import requests

import piper_out

REPO = "rhasspy/piper-voices"
VERSION = "v1.0.0"
BASE_URL = f"https://huggingface.co/{REPO}/resolve/{VERSION}/"
CATALOGUE_URL = BASE_URL + "voices.json"
CATALOGUE_PATH = os.path.join(piper_out.PIPER_DIR, "voices.json")
CATALOGUE_TTL_SEC = 7 * 86400
CARDS_DIR = os.path.join(piper_out.VOICE_DIR, "fiches")
TIMEOUT = 30
HEADERS = {"User-Agent": "nearfield-anchor (Raspberry Pi)"}


def catalogue(fresh=False):
    """{cle: entree du catalogue} ; garde une copie locale (hors ligne)."""
    try:
        age = time.time() - os.path.getmtime(CATALOGUE_PATH)
    except OSError:
        age = None
    if fresh or age is None or age > CATALOGUE_TTL_SEC:
        try:
            resp = requests.get(CATALOGUE_URL, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            tmp = CATALOGUE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, CATALOGUE_PATH)
            return data
        except (requests.RequestException, ValueError, OSError) as exc:
            print(f"[piper_voix] catalogue injoignable : {exc!r}")
    try:
        with open(CATALOGUE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def languages(cat):
    """[(code, nom natif, nom anglais, [cles])], francais d'abord."""
    found = {}
    for key, v in cat.items():
        lang = v["language"]
        found.setdefault(lang["code"], (lang["code"], lang.get("name_native") or lang["code"],
                                        lang.get("country_english") or "", []))[3].append(key)
    return sorted(found.values(), key=lambda l: (not l[0].startswith("fr_"), l[1].lower(), l[2]))


def installed(key):
    return key in piper_out.installed_models()


def size_mb(entry):
    return round(sum(f["size_bytes"] for f in entry["files"].values()) / 1e6)


def sample_url(entry, speaker=0):
    folder = os.path.dirname(next(p for p in entry["files"] if p.endswith(".onnx")))
    return f"{BASE_URL}{folder}/samples/speaker_{speaker}.mp3"


def _md5(path):
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def install(key, cat=None):
    """Telecharge et verifie la voix. Rend (True, None) ou (False, raison)."""
    cat = cat or catalogue()
    entry = cat.get(key)
    if entry is None:
        return False, "voix absente du catalogue"
    os.makedirs(CARDS_DIR, exist_ok=True)
    staged = []
    try:
        for path, info in entry["files"].items():
            name = os.path.basename(path)
            final = os.path.join(CARDS_DIR, f"{key}.txt") if name == "MODEL_CARD" else os.path.join(piper_out.VOICE_DIR, name)
            tmp = final + ".telechargement"
            staged.append((tmp, final))  # nettoye en cas d'echec
            with requests.get(BASE_URL + path, headers=HEADERS, timeout=TIMEOUT, stream=True) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for block in resp.iter_content(1 << 20):
                        f.write(block)
            if os.path.getsize(tmp) != info["size_bytes"] or _md5(tmp) != info["md5_digest"]:
                return False, f"fichier {name} abîmé au téléchargement (taille ou empreinte différente)"
        for tmp, final in staged:
            os.replace(tmp, final)
        staged = []
        return True, None
    except (requests.RequestException, OSError) as exc:
        return False, f"téléchargement impossible : {exc.__class__.__name__}"
    finally:
        for tmp, _ in staged:
            try:
                os.remove(tmp)
            except OSError:
                pass


def remove(key):
    """Retire la voix (modele et configuration) ; la fiche reste."""
    for ext in (".onnx", ".onnx.json"):
        try:
            os.remove(os.path.join(piper_out.VOICE_DIR, key + ext))
        except OSError:
            pass
