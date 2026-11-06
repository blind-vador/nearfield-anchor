"""Autres services d'IA pour les questions en texte (2026-11-07), pour soulager
le quota gratuit de Gemini. Chacun se branche par son fichier cles/<nom>.txt :
vide ou absent, il n'existe pas, et tout marche comme avant avec Gemini seul.

Ordre : les services gratuits, puis Gemini (lib/gemini.py), puis Grok, qui est
payant et ne sert donc qu'en dernier. Images et sons restent chez Gemini.
Un service qui refuse (quota, cle, panne) se repose, pour toutes les veilles ;
le suivant prend la question. Ni la cle ni la question n'entrent dans les traces.
"""
import fcntl
import json
import os
import re
import time

import requests

import reglages
import tmp_ram

CONNECTEURS = (
    {"nom": "groq", "url": "https://api.groq.com/openai/v1/chat/completions",
     "modeles": ("openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b")},
    {"nom": "cerebras", "url": "https://api.cerebras.ai/v1/chat/completions",
     "modeles": ("gpt-oss-120b", "qwen-3.8-27b")},
    {"nom": "mistral", "url": "https://api.mistral.ai/v1/chat/completions",
     "modeles": ("mistral-small-latest", "mistral-medium-latest")},
    {"nom": "openrouter", "url": "https://openrouter.ai/api/v1/chat/completions",
     "modeles": ("openrouter/free",)},
    {"nom": "grok", "url": "https://api.x.ai/v1/chat/completions",
     "modeles": ("grok-4.3", "grok-4.5"), "dernier": True},
)
REPOS_QUOTA_SEC = 600
REPOS_CLE_SEC = 6 * 3600
REPOS_PANNE_SEC = 120
REPOS_MAX_SEC = 24 * 3600
CONNEXION_SEC = 5
ETAT = os.path.join(tmp_ram.DOSSIER, "connecteurs_ia.json")
CONSIGNE_JSON = "Réponds uniquement par un objet JSON valide, sans texte autour."


def _modifier(fonction):
    """Etat partage entre veilles, en memoire. Une panne ici ne casse jamais une question."""
    try:
        os.makedirs(os.path.dirname(ETAT), exist_ok=True)
        with open(ETAT + ".verrou", "a") as verrou:
            fcntl.flock(verrou, fcntl.LOCK_EX)
            try:
                with open(ETAT, encoding="utf-8") as f:
                    etat = json.load(f)
            except (OSError, ValueError):
                etat = {}
            fonction(etat)
            tmp = ETAT + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(etat, f)
            os.replace(tmp, ETAT)
    except OSError as exc:
        print(f"[connecteurs_ia] etat non note : {exc}")


def _en_repos(nom):
    try:
        with open(ETAT, encoding="utf-8") as f:
            return time.time() < json.load(f).get("repos", {}).get(nom, 0)
    except (OSError, ValueError, AttributeError):
        return False


def _reposer(nom, secondes, pourquoi):
    print(f"[connecteurs_ia] {nom} : {pourquoi}, repos {int(secondes)} s")
    _modifier(lambda e: e.setdefault("repos", {}).__setitem__(nom, time.time() + secondes))


def compter(nom):
    """Reponses du jour par service."""
    def ajouter(etat):
        jour = time.strftime("%Y-%m-%d")
        if etat.get("jour") != jour:
            etat["jour"], etat["appels"] = jour, {}
        appels = etat.setdefault("appels", {})
        appels[nom] = appels.get(nom, 0) + 1
    _modifier(ajouter)


def actifs():
    """Noms des services dont la cle est remplie."""
    return [c["nom"] for c in CONNECTEURS if reglages.cle(c["nom"])]


def _attente_quota(resp):
    try:
        secondes = float(resp.headers.get("Retry-After", ""))
    except ValueError:
        secondes = REPOS_QUOTA_SEC
    return min(max(secondes, 1.0), REPOS_MAX_SEC)


def _texte_de(resp):
    contenu = resp.json()["choices"][0]["message"]["content"]
    if isinstance(contenu, list):
        contenu = "".join(p.get("text", "") for p in contenu if isinstance(p, dict))
    return re.sub(r"<think>.*?</think>", "", contenu or "", flags=re.S).strip()


def _nettoyer(texte, json_mode):
    """Comme Gemini : texte sans markdown, ou JSON strict. None si inutilisable."""
    if not json_mode:
        return " ".join(texte.replace("*", "").replace("#", "").split()) or None
    debut = min((i for i in (texte.find("{"), texte.find("[")) if i >= 0), default=-1)
    fin = max(texte.rfind("}"), texte.rfind("]"))
    if debut < 0 or fin < debut:
        return None
    morceau = texte[debut:fin + 1]
    try:
        json.loads(morceau)
    except ValueError:
        return None
    return morceau


def _essayer(connecteur, cle, prompt, temperature, json_mode, fin):
    nom = connecteur["nom"]
    entetes = {"Authorization": f"Bearer {cle}"}
    messages = [{"role": "user", "content": prompt}]
    if json_mode:
        messages.insert(0, {"role": "system", "content": CONSIGNE_JSON})
    for modele in connecteur["modeles"]:
        corps = {"model": modele, "messages": messages, "temperature": temperature}
        if json_mode:
            corps["response_format"] = {"type": "json_object"}
        for _ in range(2):
            restant = fin - time.time()
            if restant < 1:
                return None
            try:
                resp = requests.post(connecteur["url"], json=corps, headers=entetes,
                                     timeout=(CONNEXION_SEC, restant))
            except requests.RequestException as exc:
                _reposer(nom, REPOS_PANNE_SEC, type(exc).__name__)
                return None
            # Tous les modeles ne prennent pas le format JSON : la consigne et le controle restent.
            if resp.status_code == 400 and "response_format" in corps:
                del corps["response_format"]
                continue
            break
        if resp.status_code == 429:
            _reposer(nom, _attente_quota(resp), "quota")
            return None
        if resp.status_code in (401, 402, 403):
            _reposer(nom, REPOS_CLE_SEC, f"refus HTTP {resp.status_code}")
            return None
        if resp.status_code >= 500:
            _reposer(nom, REPOS_PANNE_SEC, f"HTTP {resp.status_code}")
            return None
        if not resp.ok:   # modele retire ou refuse : le suivant
            print(f"[connecteurs_ia] {nom} {modele} : HTTP {resp.status_code}")
            continue
        try:
            texte = _nettoyer(_texte_de(resp), json_mode)
        except (KeyError, IndexError, TypeError, ValueError):
            texte = None
        if texte:
            return texte
        print(f"[connecteurs_ia] {nom} {modele} : reponse inutilisable")
    return None


def demander(prompt, temperature=0.8, json_mode=False, timeout=30, dernier=False):
    """Texte du premier service qui repond, ou None (Gemini prend alors la suite).
    dernier : les services payants, quand Gemini lui-meme n'a pas repondu."""
    fin = time.time() + timeout
    for connecteur in CONNECTEURS:
        if bool(connecteur.get("dernier")) != dernier:
            continue
        cle = reglages.cle(connecteur["nom"])
        if not cle or _en_repos(connecteur["nom"]):
            continue
        texte = _essayer(connecteur, cle, prompt, temperature, json_mode, fin)
        if texte:
            compter(connecteur["nom"])
            return texte
    return None
