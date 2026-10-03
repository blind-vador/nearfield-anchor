"""Appel texte simple a Gemini (cle cles/gemini.txt, voir reglages.py),
avec repli sur les modeles suivants si l'un est epuise ou retire.
Rend (texte, None) ou (None, raison parlee)."""
import requests

import reglages
from phrases import dire

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]
TIMEOUT = 30


def ask(prompt, temperature=0.8, jpeg=None, json_mode=False, timeout=TIMEOUT):
    """jpeg : image (octets JPEG) jointe a la question, pour la description d'images.
    json_mode : Gemini rend du JSON strict (responseMimeType), rendu tel quel."""
    key = reglages.cle("gemini")
    if not key:
        return None, dire("gemini.cle_gemini")
    parts = [{"text": prompt}]
    if jpeg is not None:
        import base64
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}})
    config = {"temperature": temperature}
    if json_mode:
        config["responseMimeType"] = "application/json"
    body = {"contents": [{"parts": parts}], "generationConfig": config}
    reason = dire("gemini.gemini_repond")
    for model in MODELS:
        try:
            resp = requests.post(URL.format(model=model), json=body, timeout=timeout,
                                 headers={"x-goog-api-key": key})
        except requests.RequestException:
            return None, dire("gemini.acces_gemini")
        if resp.status_code in (401, 403):
            return None, dire("gemini.google_refuse_cle_gemini")
        if not resp.ok:
            reason = dire("gemini.quota_gemini_epuise") if resp.status_code == 429 else reason
            continue
        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, ValueError):
            reason = dire("gemini.reponse_gemini_inutilisable")
            continue
        if json_mode:
            return text, None
        text = " ".join(text.replace("*", "").replace("#", "").split())
        if text:
            return text, None
    return None, reason
