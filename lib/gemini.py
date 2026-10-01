"""Appel texte simple a Gemini (cle cles/gemini.txt, voir reglages.py),
avec repli sur les modeles suivants si l'un est epuise ou retire.
Rend (texte, None) ou (None, raison parlee)."""
import requests

import reglages

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]
TIMEOUT = 30


def ask(prompt, temperature=0.8, jpeg=None):
    """jpeg : image (octets JPEG) jointe a la question, pour la description d'images."""
    key = reglages.cle("gemini")
    if not key:
        return None, "pas de clé Gemini"
    parts = [{"text": prompt}]
    if jpeg is not None:
        import base64
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}})
    body = {"contents": [{"parts": parts}], "generationConfig": {"temperature": temperature}}
    reason = "Gemini ne répond pas"
    for model in MODELS:
        try:
            resp = requests.post(URL.format(model=model), json=body, timeout=TIMEOUT,
                                 headers={"x-goog-api-key": key})
        except requests.RequestException:
            return None, "pas d'accès à Gemini"
        if resp.status_code in (401, 403):
            return None, "Google refuse la clé Gemini"
        if not resp.ok:
            reason = "quota Gemini épuisé" if resp.status_code == 429 else reason
            continue
        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, ValueError):
            reason = "réponse de Gemini inutilisable"
            continue
        text = " ".join(text.replace("*", "").replace("#", "").split())
        if text:
            return text, None
    return None, reason
