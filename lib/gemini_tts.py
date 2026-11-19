"""Voix neurales de Gemini (synthese vocale par l'API, cle cles/gemini.txt, voir reglages.py).

Une voix de plus dans le catalogue des logiciels installes (voix_sources.catalogue) : "gemini:<Nom>".
Le texte part chez Google, le son revient en WAV (24 kHz, mono) et passe par la file des priorites
comme les autres voix (speak.parler). Quota epuise, modele retire, reseau muet : le modele se repose
(connecteurs_ia, comme lib/gemini.py) et la phrase est dite par la voix reglee pour la source.

    fichier(texte, voix, debit) -> chemin d'un WAV temporaire (a effacer), ou None
"""
import base64
import contextlib
import io
import os
import struct
import tempfile

PREFIXE = "gemini:"
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELES = ["gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview"]
DELAI_S = 30
REPOS_MODELE_S = 1800
REPOS_RETIRE_S = 86400
MAX_TEXTE = 3000

# Les 30 voix toutes faites de Gemini, avec leur caractere (documentation de Google).
VOIX = {
    "Zephyr": "lumineuse", "Puck": "enjouée", "Charon": "informative", "Kore": "ferme", "Fenrir": "vive",
    "Leda": "jeune", "Orus": "ferme", "Aoede": "légère", "Callirrhoe": "détendue", "Autonoe": "lumineuse",
    "Enceladus": "soufflée", "Iapetus": "claire", "Umbriel": "détendue", "Algieba": "douce", "Despina": "douce",
    "Erinome": "claire", "Algenib": "rocailleuse", "Rasalgethi": "informative", "Laomedeia": "enjouée",
    "Achernar": "tendre", "Alnilam": "ferme", "Schedar": "égale", "Gacrux": "mûre", "Pulcherrima": "directe",
    "Achird": "amicale", "Zubenelgenubi": "décontractée", "Vindemiatrix": "délicate", "Sadachbia": "animée",
    "Sadaltager": "savante", "Sulafat": "chaleureuse",
}


def _cle():
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            import reglages
            return reglages.cle("gemini")
    except Exception:
        return ""


def _repos(model):
    return "gemini_tts:" + model


def disponible():
    """Une cle, et au moins un modele qui ne se repose pas."""
    if not _cle():
        return False
    import connecteurs_ia
    return any(not connecteurs_ia._en_repos(_repos(m)) for m in MODELES)


def catalogue():
    if not disponible():
        return []
    return [{"id": PREFIXE + nom, "nom": "%s (Gemini, %s)" % (nom, car), "fournisseur": "gemini"}
            for nom, car in VOIX.items()]


def style(debit):
    """Le debit devient une consigne de diction (Gemini n'a pas de reglage de vitesse)."""
    try:
        n = int(str(debit or "+0%").strip("%"))
    except ValueError:
        return None
    if n >= 50:
        return "rapide, sans hâte excessive"
    if n >= 15:
        return "un peu plus rapide que la normale"
    if n <= -15:
        return "posée, plus lente que la normale"
    return None


def _wav(pcm, taux=24000):
    entete = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, taux, taux * 2, 2, 16) + b"data" + struct.pack("<I", len(pcm))
    return entete + pcm


def _audio(reponse):
    p = reponse["candidates"][0]["content"]["parts"][0]["inlineData"]
    brut = base64.b64decode(p["data"])
    if brut[:4] == b"RIFF":
        return brut
    taux = 24000
    for morceau in str(p.get("mimeType", "")).split(";"):
        if morceau.strip().startswith("rate="):
            taux = int(morceau.split("=", 1)[1])
    return _wav(brut, taux)                       # PCM 16 bits brut (anciens modeles)


def _corps(texte, nom, debit, ancienne):
    part = {"text": texte}
    s = style(debit)
    if s:
        part["speech_metadata"] = {"style": s}
    vc = {"prebuiltVoiceConfig": {"voiceName": nom}} if ancienne else {"voice": nom}
    return {"contents": [{"role": "user", "parts": [part]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": vc}}}


def fichier(texte, voix, debit=None, poster=None):
    """WAV temporaire de la phrase dite par cette voix Gemini, ou None (la source reprend sa voix)."""
    nom = (voix or "")[len(PREFIXE):] if (voix or "").startswith(PREFIXE) else ""
    texte = " ".join(str(texte or "").split())
    if nom not in VOIX or not texte or len(texte) > MAX_TEXTE:
        return None
    cle = _cle()
    if not cle:
        return None
    import connecteurs_ia
    if poster is None:
        import requests
        poster = requests.post
    for model in MODELES:
        if connecteurs_ia._en_repos(_repos(model)):
            continue
        for ancienne in (False, True):
            try:
                r = poster(URL.format(model=model), json=_corps(texte, nom, debit, ancienne), timeout=DELAI_S,
                           headers={"x-goog-api-key": cle})
            except Exception as exc:          # reseau muet : le modele suivant, puis la voix de la source
                connecteurs_ia._reposer(_repos(model), 120, type(exc).__name__)
                break
            if r.status_code == 200:
                try:
                    son = _audio(r.json())
                except (KeyError, IndexError, ValueError, TypeError):
                    connecteurs_ia._reposer(_repos(model), REPOS_MODELE_S, "reponse sans son")
                    break
                fd, chemin = tempfile.mkstemp(suffix=".wav", prefix="gemini_tts_")
                with os.fdopen(fd, "wb") as f:
                    f.write(son)
                return chemin
            if r.status_code == 400 and not ancienne:
                continue                      # forme de la voix d'avant : on la retente
            if r.status_code == 429:
                connecteurs_ia._reposer(_repos(model), connecteurs_ia._attente_quota(r) or REPOS_MODELE_S, "quota")
            elif r.status_code == 404:
                connecteurs_ia._reposer(_repos(model), REPOS_RETIRE_S, "modele absent")
            elif r.status_code in (401, 403):
                return None                   # cle refusee : rien a tenter avec les autres modeles
            else:
                connecteurs_ia._reposer(_repos(model), REPOS_MODELE_S, "HTTP %d" % r.status_code)
            break
    return None
