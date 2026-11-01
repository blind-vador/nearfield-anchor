"""Guetteur SSTV (images en television a balayage lent), 2026-11-01.

Branche comme multimon-ng sur tout flux FM/AM/BLU que le scanner ouvre (pas de
mur : l'ISS en envoie sur 145,800 MHz, les radioamateurs en 2 m / 70 cm, mais on
ne decide pas d'avance ou). Lit du S16LE mono sur l'entree standard :

  - repere l'en-tete VIS (1900 Hz 300 ms, coupure 1200 Hz 10 ms, 1900 Hz
    300 ms) et ecrit "SSTV: debut" -- le scanner garde alors le canal ouvert
    le temps de l'image, meme sans voix ;
  - decode avec le paquet Python "sstv" (pip, 18 modes) et ecrit
    "SSTV: image <chemin> <mode> <complete|partielle>" pour chaque image.

Usage : python3 sstv_veille.py <taux> <dossier_images>"""
import os
import re
import sys
import time

import numpy as np

TRAME_SEC = 0.01
IMAGE_MAX_SEC = 300          # PD 290 : la plus longue (~4 min 50)
ESSAI_SEC = 10               # essai de decodage pendant la reception
_ENTETE = re.compile(r"9{18,}[2.]{1,4}9{18,}")


def _trames(x, taux):
    n = int(taux * TRAME_SEC)
    k = len(x) // n
    if not k:
        return ""
    t = np.arange(n) / taux
    blocs = x[:k * n].reshape(k, n)
    total = (blocs ** 2).sum(axis=1) * n / 2 + 1e-9
    def puissance(f):
        return np.abs(blocs @ np.exp(-2j * np.pi * f * t)) ** 2
    p19, p12 = puissance(1900) / total, puissance(1200) / total
    return "".join("9" if a > 0.4 else ("2" if b > 0.4 else ".") for a, b in zip(p19, p12))


def _decoder(samples, taux):
    try:
        import sstv
        # en int16 : des flottants sont lus comme deja normalises (-1..1)
        return sstv.decode(np.clip(samples, -32768, 32767).astype(np.int16), taux)
    except Exception as exc:
        print(f"SSTV: erreur {exc!r}", file=sys.stderr, flush=True)
        return []


def _sauver(images, dossier):
    for img in images:
        mode = str(img.info.get("sstv_mode", "")).replace("Mode.", "")
        complete = "complete" if img.info.get("sstv_complete") else "partielle"
        chemin = os.path.join(dossier, f"sstv_{time.strftime('%Y-%m-%d_%H%M%S')}_{mode}.png")
        img.save(chemin)
        print(f"SSTV: image {chemin} {mode} {complete}", flush=True)


def main():
    taux = int(sys.argv[1]) if len(sys.argv) > 1 else 22050
    dossier = sys.argv[2] if len(sys.argv) > 2 else "/tmp"
    os.makedirs(dossier, exist_ok=True)
    entree = sys.stdin.buffer
    morceau = taux // 5 * 2                      # 0,2 s
    guet = np.zeros(0, dtype=np.float32)          # les 2 dernieres secondes, pour l'en-tete
    image, essai = None, 0.0                      # audio depuis l'en-tete, sinon None
    while True:
        data = entree.read(morceau)
        if not data:
            break
        x = np.frombuffer(data[:len(data) // 2 * 2], dtype=np.int16).astype(np.float32)
        if image is None:
            guet = np.concatenate([guet, x])[-2 * taux:]
            if _ENTETE.search(_trames(guet, taux)):
                print("SSTV: debut", flush=True)
                image, essai = [guet], time.time()
            continue
        image.append(x)
        duree = sum(len(b) for b in image) / taux
        if time.time() - essai > ESSAI_SEC or duree > IMAGE_MAX_SEC:
            essai = time.time()
            trouvees = _decoder(np.concatenate(image), taux)
            if any(i.info.get("sstv_complete") for i in trouvees) or duree > IMAGE_MAX_SEC:
                _sauver(trouvees, dossier)
                image, guet = None, np.zeros(0, dtype=np.float32)
    if image is not None:                         # flux coupe en cours d'image : ce qu'on a
        _sauver(_decoder(np.concatenate(image), taux), dossier)


if __name__ == "__main__":
    main()
