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


def ask(prompt, temperature=0.8, jpeg=None, json_mode=False, timeout=TIMEOUT, mime="image/jpeg", audio=None, audio_mime="audio/ogg"):
    """jpeg : image (octets ; type mime, JPEG par defaut) jointe a la question.
    audio : son (octets, type audio_mime) joint, pour transcrire une voix captee
    (ATIS...). json_mode : Gemini rend du JSON strict (responseMimeType)."""
    texte_seul = jpeg is None and audio is None
    if texte_seul:
        texte = connecteurs_ia.demander(prompt, temperature, json_mode, timeout)
        if texte:
            return texte, None
    texte, raison = _demander_gemini(prompt, temperature, jpeg, json_mode, timeout, mime, audio, audio_mime)
    if texte:
        connecteurs_ia.compter("gemini")
        return texte, None
    if texte_seul:
        texte = connecteurs_ia.demander(prompt, temperature, json_mode, timeout, dernier=True)
        if texte:
            return texte, None
    return None, raison


def _demander_gemini(prompt, temperature, jpeg, json_mode, timeout, mime, audio, audio_mime):
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
            # La reponse peut venir en plusieurs morceaux : n'en lire qu'un la coupait net.
            text = "".join(p.get("text", "") for p in resp.json()["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, ValueError):
            reason = dire("gemini.reponse_gemini_inutilisable")
            continue
        if json_mode:
            return text, None
        text = " ".join(text.replace("*", "").replace("#", "").split())
        if text:
            return text, None
    return None, reason
