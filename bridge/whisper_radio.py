"""Transcription locale des prises radio validees (whisper.cpp, modele small).
Le Pi envoie le mp3 du journal. On rend le texte, rien d'autre.
Jamais un dechiffrement : le fichier est deja le son que l'utilisateur a entendu.
"""
import json
import os
import subprocess

HOME = os.path.expanduser("~/.nearfield-bridge")
CLI = os.path.join(HOME, "whisper.cpp", "build", "bin", "whisper-cli")
MODEL = os.path.join(HOME, "whisper.cpp", "models", "ggml-small.bin")
PROMPT = "Conversation radio en français, sans titre ni sous-titre."


def disponible():
    return os.access(CLI, os.X_OK) and os.path.isfile(MODEL)


def process(folder, params):
    src = os.path.join(folder, (params or {}).get("fichier") or "son.mp3")
    if not os.path.isfile(src):
        return "echec", [], "fichier audio absent"
    base = os.path.join(folder, "whisper")
    try:
        proc = subprocess.run(
            [CLI, "-m", MODEL, "-l", "fr", "-t", "4", "-nt", "-sns", "-np",
             "-f", src, "-of", base, "-oj", "--prompt", PROMPT],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return "echec", [], "whisper trop long"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "whisper en echec").strip()
        return "echec", [], err[-180:]
    try:
        with open(base + ".json", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return "echec", [], "transcription illisible"
    texte = " ".join((seg.get("text") or "").strip() for seg in data.get("transcription") or [])
    texte = " ".join(texte.split())
    with open(os.path.join(folder, "texte.json"), "w", encoding="utf-8") as f:
        json.dump({"texte": texte}, f, ensure_ascii=False)
    return "fait", ["texte.json"], texte[:160]
