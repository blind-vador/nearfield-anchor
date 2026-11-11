"""Examen des prises radio par la passe d'analyse du Mac (2026-11-11).

Travaux deposes par le Pi (lib/examen.py) :
  - "examen" (file rapide) : un son du journal demande avec la touche E, copie
    dans le dossier du travail (son.<ext>) ;
  - "examen_lot" (file lourde) : la passe de la nuit, lue directement dans
    radio_log (params.mode == "lot").

Pourquoi ici et plus dans launchd : macOS refuse l'acces aux volumes reseau aux
taches launchd d'arriere-plan ("Operation not permitted"), la passe tournait a
vide depuis le 11 octobre. L'agent du pont a cet acces, et ses sous-processus
en heritent. Le script d'analyse (mac/analyse.py, venv ~/pcw-analyse) ne fait
qu'ecrire dans le dossier du travail ; le Pi range les propositions lui-meme.
"""
import os
import subprocess

VENV_PYTHON = os.path.expanduser("~/pcw-analyse/venv/bin/python")
SCRIPT = os.path.expanduser("~/pcw-analyse/analyse.py")
DUREE_CLIP_SEC = 600
DUREE_LOT_SEC = 3 * 3600


def disponible():
    return os.access(VENV_PYTHON, os.X_OK) and os.path.isfile(SCRIPT)


def _radio_log(folder):
    """<partage>/travaux/<id> -> <server>/radio_log (frere du dossier du pont)."""
    partage = os.path.dirname(os.path.dirname(os.path.abspath(folder)))
    return os.path.join(os.path.dirname(partage), "radio_log")


def _sorties(folder):
    """Fichiers rendus, chemins relatifs au dossier du travail (signes par le pont)."""
    out = ["propositions.json"]
    if os.path.isfile(os.path.join(folder, "transcription.json")):
        out.append("transcription.json")
    audio = os.path.join(folder, "propositions_audio")
    if os.path.isdir(audio):
        out += [os.path.join("propositions_audio", n) for n in sorted(os.listdir(audio))]
    return out


def process(folder, params):
    params = params or {}
    if not disponible():
        return "echec", [], "passe d'analyse absente (~/pcw-analyse)"
    if params.get("mode") == "lot":
        cmd = [VENV_PYTHON, "-W", "ignore", SCRIPT, "--lot", "--base", _radio_log(folder), "--sortie", folder,
               "--hours", str(params.get("heures", 24)), "--max", str(params.get("max", 300))]
        duree = DUREE_LOT_SEC
    else:
        src = os.path.join(folder, params.get("fichier") or "son.mp3")
        if not os.path.isfile(src):
            return "echec", [], "son a examiner absent"
        cmd = [VENV_PYTHON, "-W", "ignore", SCRIPT, "--clip", src, "--sortie", folder,
               "--nom", params.get("origine") or os.path.basename(src),
               "--origine", "soir" if params.get("soir") else "demande"]
        try:  # Whisper d'abord : la parole comprise decide du nettoyage (plan phase 4)
            import whisper_radio
            if whisper_radio.disponible():
                cmd += ["--whisper-cli", whisper_radio.CLI, "--whisper-modele", whisper_radio.MODEL,
                        "--whisper-prompt", whisper_radio.PROMPT]
        except Exception:
            pass
        duree = DUREE_CLIP_SEC
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=duree)
    except subprocess.TimeoutExpired:
        return "echec", [], "examen trop long"
    if proc.returncode != 0 or not os.path.isfile(os.path.join(folder, "propositions.json")):
        err = (proc.stderr or proc.stdout or "examen en echec").strip()
        return "echec", [], err[-180:]
    lignes = [l for l in (proc.stdout or "").splitlines() if l.startswith("[analyse]")]
    return "fait", _sorties(folder), (lignes[-1][10:] if lignes else "examen fait")[:160]
