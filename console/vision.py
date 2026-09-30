"""Description vocale de la webcam de l'imprimante par un modele d'IA.

Photo prise via le service camera d'OctoPi (mjpg-streamer, qui occupe deja
la webcam pour OctoPrint : ouvrir /dev/video0 directement echouerait),
envoyee a Gemini (offre gratuite, cles/gemini.txt des reglages),
description lue a voix haute, puis tout est oublie.

Confidentialite :
- la photo et la description ne touchent jamais la carte SD : octets en
  memoire, liberes apres la lecture ; la description n'est pas gardee
  pour "repete" (main.py ne la memorise pas, voir Command.ephemeral) ;
- MAIS la photo part chez Google, qui peut s'en servir pour ameliorer ses
  modeles sur l'offre gratuite : effacer notre copie n'efface pas la leur.

Une photo trop sombre n'est pas envoyee (quota gratuit epargne, et une
description "l'image est noire" n'apprend rien).
"""
import base64
import io
import json
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, "/home/vador/autopilot/pi_console")
import reglages  # noqa: E402

SNAPSHOT_URL = "http://127.0.0.1:8080/?action=snapshot"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Ordre d'essai : si un modele a epuise son quota gratuit (429) ou est
# indisponible, on passe au suivant.
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]
# Luminosite moyenne 0-255. Mesure du 2026-09-29 : a 17 (bureau eteint,
# penombre), Gemini decrivait encore correctement l'imprimante (pas
# d'impression, tubes visibles) : on ne refuse que le noir quasi total.
MIN_BRIGHTNESS = 10
REQUEST_TIMEOUT_SEC = 45

PROMPT = (
    "Tu décris l'image d'une webcam placée dans le caisson d'une imprimante 3D, "
    "pour une personne aveugle qui l'écoute via une synthèse vocale. Réponds en français, "
    "en trois phrases maximum, sans liste ni mise en forme. Le plus important d'abord : "
    "l'état de l'imprimante et d'une éventuelle impression (en cours, terminée, ratée, "
    "filament emmêlé, pièce décollée du plateau), puis tout ce qui semble anormal ou dangereux. "
    "Lis tout texte visible. Ne parle des couleurs que si c'est utile. "
    "Si tu n'es pas sûr de quelque chose, dis-le plutôt que d'inventer."
)

_thread = None


def busy():
    if _thread is not None and _thread.is_alive():
        return "Une description est déjà en cours."
    return ""


def _snapshot():
    return urllib.request.urlopen(SNAPSHOT_URL, timeout=5).read()


def _brightness(jpeg):
    from PIL import Image, ImageStat
    with Image.open(io.BytesIO(jpeg)) as im:
        return ImageStat.Stat(im.convert("L")).mean[0]


def _ask_gemini(jpeg, question):
    key = reglages.cle("gemini")
    if not key:
        return None, "Clé Gemini absente : colle-la dans le fichier gemini point txt du dossier cles des réglages."
    prompt = PROMPT + (f" La personne demande en plus : {question}" if question else "")
    body = json.dumps({"contents": [{"parts": [
        {"text": prompt},
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
    ]}]}).encode()
    last_problem = "Le service d'intelligence artificielle ne répond pas."
    for model in GEMINI_MODELS:
        req = urllib.request.Request(GEMINI_URL.format(model=model), data=body,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            out = json.load(urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC))
            text = out["candidates"][0]["content"]["parts"][0]["text"].strip()
            return " ".join(text.replace("*", "").split()), None
        except urllib.error.HTTPError as exc:
            print(f"[console] vision {model} : HTTP {exc.code}", file=sys.stderr)
            if exc.code in (401, 403):
                return None, "Google refuse la clé Gemini. Vérifie-la."
            if exc.code == 429:
                last_problem = "Quota gratuit de Gemini épuisé pour le moment. Réessaie plus tard."
            continue  # 429, 5xx, modele retire : modele suivant
        except (urllib.error.URLError, TimeoutError, OSError):
            return None, "Pas d'accès à internet, ou Google ne répond pas."
        except (KeyError, IndexError, ValueError):
            # Reponse sans texte (ex. bloquee par les filtres de securite).
            last_problem = "Gemini n'a pas renvoyé de description."
            continue
    return None, last_problem


def _run(question, say):
    say("Photo.")
    try:
        jpeg = _snapshot()
    except OSError:
        say("La caméra ne répond pas. Vérifie que la webcam est allumée.")
        return
    try:
        if _brightness(jpeg) < MIN_BRIGHTNESS:
            say("Photo trop sombre, le caisson n'est pas éclairé. Rien n'a été envoyé.")
            return
        say("Analyse en cours.")
        description, problem = _ask_gemini(jpeg, question)
    finally:
        del jpeg  # la photo n'a jamais ete ecrite sur disque ; on lache la derniere reference
    say(description or problem)
    description = None


def start(question, say):
    """Lance prise de vue + analyse + lecture en arriere-plan ; say(texte)
    doit bloquer jusqu'a la fin de la phrase."""
    global _thread

    def run():
        try:
            _run(question, say)
        except Exception as exc:
            print(f"[console] vision : {exc!r}", file=sys.stderr)
            say("La description a échoué sur une erreur inattendue.")

    _thread = threading.Thread(target=run, daemon=True)
    _thread.start()
