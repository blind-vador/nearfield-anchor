"""Appel texte simple a Gemini (cle cles/gemini.txt, voir reglages.py),
avec repli sur les modeles suivants si l'un est epuise ou retire. Une question
en texte passe d'abord par les autres services dont la cle est remplie
(lib/connecteurs_ia.py) ; sans autre cle, rien ne change.
Rend (texte, None) ou (None, raison parlee)."""
import requests

import connecteurs_ia
import reglages
from phrases import dire

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]
TIMEOUT = 30
REPOS_MODELE_SEC = 1800


def _attente_quota(resp):
    """Google dit quand le quota revient ("31380s") ; le quota gratuit est par modele."""
    try:
        for detail in resp.json()["error"]["details"]:
            if str(detail.get("retryDelay", "")).endswith("s"):
                return min(float(detail["retryDelay"][:-1]), connecteurs_ia.REPOS_MAX_SEC)
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return connecteurs_ia.REPOS_QUOTA_SEC


def contexte_scanneur():
    """Ce qu'une IA doit savoir avant de parler d'une reception (2026-11-07) :
    scanneur agnostique, radioamateurs aveugles, veille citoyenne, en clair
    seulement. Sans lui, une IA a pris du TETRA recu en clair pour un piratage
    ou un faux decodage."""
    import avions_info
    return dire("ia.contexte_scanneur", langue=avions_info._langue())


def ask(prompt, temperature=0.8, jpeg=None, json_mode=False, timeout=TIMEOUT, mime="image/jpeg", audio=None,
        audio_mime="audio/ogg", scanneur=False, recherche=False):
    """jpeg : image (octets ; type mime, JPEG par defaut) jointe a la question.
    audio : son (octets, type audio_mime) joint, pour transcrire une voix captee
    (ATIS...). json_mode : Gemini rend du JSON strict (responseMimeType).
    scanneur : la question porte sur ce que le scanner a capte ; le contexte
    (contexte_scanneur) passe devant.
    recherche (2026-11-17) : Gemini cherche sur le web avant de repondre (outil google_search),
    pour une reponse a jour ; les autres services, qui ne cherchent pas, sont sautes. Le texte
    est rendu tel quel (le JSON strict de Gemini ne se combine pas avec la recherche)."""
    if scanneur:
        prompt = contexte_scanneur() + "\n\n" + prompt
    texte_seul = jpeg is None and audio is None and not recherche
    if texte_seul:
        texte = connecteurs_ia.demander(prompt, temperature, json_mode, timeout)
        if texte:
            return texte, None
    texte, raison = _demander_gemini(prompt, temperature, jpeg, json_mode, timeout, mime, audio, audio_mime,
                                     recherche)
    if texte:
        connecteurs_ia.compter("gemini")
        return texte, None
    if texte_seul:
        texte = connecteurs_ia.demander(prompt, temperature, json_mode, timeout, dernier=True)
        if texte:
            return texte, None
    return None, raison


def _demander_gemini(prompt, temperature, jpeg, json_mode, timeout, mime, audio, audio_mime, recherche=False):
    import base64
    key = reglages.cle("gemini")
    if not key:
        return None, dire("gemini.cle_gemini")
    parts = [{"text": prompt}]
    if jpeg is not None:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(jpeg).decode()}})
    if audio is not None:
        parts.append({"inline_data": {"mime_type": audio_mime, "data": base64.b64encode(audio).decode()}})
    config = {"temperature": temperature}
    if json_mode and not recherche:
        config["responseMimeType"] = "application/json"
    body = {"contents": [{"parts": parts}], "generationConfig": config}
    if recherche:
        body["tools"] = [{"google_search": {}}]
    reason = dire("gemini.gemini_repond")
    for model in MODELS:
        repos = f"gemini:{model}"
        if connecteurs_ia._en_repos(repos):   # epuise ou muet : ni attente ni requete pour rien
            reason = dire("gemini.quota_gemini_epuise")
            continue
        try:
            resp = requests.post(URL.format(model=model), json=body, timeout=timeout,
                                 headers={"x-goog-api-key": key})
        except requests.RequestException as exc:
            # Un modele muet n'empeche pas les suivants (2026-11-07 : le dernier repondait,
            # et on disait "pas d'acces a Gemini").
            connecteurs_ia._reposer(repos, REPOS_MODELE_SEC, type(exc).__name__)
            reason = dire("gemini.acces_gemini")
            continue
        if resp.status_code in (401, 403):
            return None, dire("gemini.google_refuse_cle_gemini")
        if resp.status_code == 429:
            connecteurs_ia._reposer(repos, _attente_quota(resp), "quota")
            reason = dire("gemini.quota_gemini_epuise")
            continue
        if not resp.ok:
            continue
        try:
            # La reponse peut venir en plusieurs morceaux : n'en lire qu'un la coupait net.
            text = "".join(p.get("text", "") for p in resp.json()["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, ValueError):
            reason = dire("gemini.reponse_gemini_inutilisable")
            continue
        if json_mode or recherche:
            if text.strip():
                return text, None
            continue
        text = " ".join(text.replace("*", "").replace("#", "").split())
        if text:
            return text, None
    return None, reason
