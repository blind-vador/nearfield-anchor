"""Travail compagnon "voix" (2026-10-19) : calculer les sons Piper que le Pi
prepare d'avance (phrases fixes de l'interface et des annonces, phrases du
jour reecrites par Gemini, toutes les phrases d'une voix nouvellement
choisie). Memes modeles que le Pi (copies par lui dans le dossier du pont,
voix_piper/), memes reglages ; ~20 fois plus vite que sur le Pi.

Entree (dossier du travail) : entrees.json [{n, modele, locuteur, texte,
length_scale, noise_scale, noise_w}]. Sortie : sons.tar (<n>.wav).
Le calcul tourne dans un environnement Python a part (piper-venv, piper-tts).
"""
import os
import shutil
import subprocess

HOME = os.path.expanduser("~/.nearfield-bridge")
VENV_PYTHON = os.path.join(HOME, "piper-venv", "bin", "python")
WORKER = os.path.join(HOME, "voix_piper_worker.py")
MODELES_LOCAUX = os.path.join(HOME, "voix_piper")


def available():
    return os.access(VENV_PYTHON, os.X_OK) and os.path.exists(WORKER)


def _modeles(share, dossier, noms):
    """Copie locale des modeles (mise a jour si le Pi en a change)."""
    os.makedirs(MODELES_LOCAUX, exist_ok=True)
    for nom in noms:
        for ext in (".onnx", ".onnx.json"):
            src, dst = os.path.join(share, dossier, nom + ext), os.path.join(MODELES_LOCAUX, nom + ext)
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copyfile(src, dst + ".tmp")
                os.replace(dst + ".tmp", dst)


def process(folder, params):
    share = os.path.dirname(os.path.dirname(folder))  # <pont>/travaux/<id>
    _modeles(share, params.get("dossier_modeles", "voix_piper"), params.get("modeles") or [])
    out = subprocess.run([VENV_PYTHON, WORKER, folder, MODELES_LOCAUX], capture_output=True, text=True,
                         timeout=3600)
    lines = (out.stdout or "").strip().splitlines()
    if out.returncode != 0 or not os.path.exists(os.path.join(folder, "sons.tar")):
        return "echec", [], "calcul des voix impossible : " + ((out.stderr or "").strip().splitlines() or ["?"])[-1][:200]
    return "fait", ["sons.tar"], lines[-1] if lines else "sons calcules"
