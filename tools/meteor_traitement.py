"""Traitement d'un passage Meteor-M enregistre par satellites_watch, au
ralenti (nice 19), hors du scanner :

  1. enregistrement I/Q 8 bits a 1,024 MHz centre sur 137,5 MHz : on extrait
     137,1 et 137,9 MHz (decalage, filtre passe-bas, decimation par 6 ->
     170,7 kHz), et on garde la frequence la plus forte ;
  2. meteor_demod (72 puis 80 kbit/s) et meteor_decode (variantes) : la plus
     grande image gagne ;
  3. image reduite, decrite par Gemini (analyse d'image : usage autorise de
     la cle), puis annoncee ; l'image reste sur le NAS, le brut est efface.

Usage : meteor_traitement.py fichier.u8 "Meteor M2 3"
"""
import os
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, "/home/vador/autopilot/pi_console")

RATE = 1_024_000
CENTER = 137_500_000
FREQS = [137_100_000, 137_900_000]
DECIM = 6
OUT_RATE = RATE // DECIM
TAPS = 101
CHUNK = 1 << 20
BIN = "/usr/local/bin" if os.path.exists("/usr/local/bin/meteor_demod") else "/home/vador/autopilot/bin"
TRIES = [(72000, []), (72000, ["-d"]), (80000, ["-i"]), (80000, ["-i", "-d"])]
MIN_LINES = 80
PROMPT = """Voici une image prise il y a quelques minutes par le satellite météo russe {sat}, en orbite
basse, lors de son passage au-dessus de la France et de l'Europe de l'Ouest. La personne qui l'écoute est
aveugle et habite à Saint-Étienne, dans le centre-est de la France. En 3 ou 4 phrases simples, à lire à voix
haute, décris la couverture nuageuse, surtout au-dessus du centre-est de la France si tu peux le repérer :
ciel dégagé, nuages épars, couche étendue, gros amas pouvant être orageux. Si l'image est partielle,
bruitée ou difficile à situer, dis-le franchement. Pas de markdown, pas de liste."""


def _lowpass(cutoff_hz):
    n = np.arange(TAPS) - (TAPS - 1) / 2
    h = np.sinc(2 * cutoff_hz / RATE * n) * np.hamming(TAPS)
    return (h / h.sum()).astype(np.float32)


def extract(path):
    """Ecrit un fichier I/Q 16 bits par frequence ; rend [(puissance, chemin)]."""
    h = _lowpass(85_000)
    outputs = []
    for freq in FREQS:
        out_path = f"{path}.{freq // 1000}.raw"
        offset = freq - CENTER
        power, total, n0, tail, phase = 0.0, 0, 0, np.zeros(TAPS - 1, np.complex64), 0
        with open(path, "rb") as src, open(out_path, "wb") as dst:
            while True:
                raw = src.read(CHUNK * 2)
                if len(raw) < 2:
                    break
                iq = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.uint8).astype(np.float32) - 127.5
                x = (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)
                t = np.arange(n0, n0 + len(x))
                x *= np.exp(-2j * np.pi * offset * t / RATE).astype(np.complex64)
                n0 += len(x)
                # Filtre calcule seulement aux echantillons gardes (1 sur DECIM) :
                # six fois moins de calcul qu'un filtrage complet puis decimation.
                xx = np.concatenate([tail, x])
                tail = x[-(TAPS - 1):]
                start = (-phase) % DECIM  # decimation alignee sur le flux global
                windows = np.lib.stride_tricks.sliding_window_view(xx, TAPS)[start::DECIM]
                y = windows @ h[::-1]
                phase = (phase + len(x)) % DECIM
                power += float(np.sum(np.abs(y) ** 2))
                total += len(y)
                pcm = np.empty(len(y) * 2, dtype=np.int16)
                pcm[0::2] = np.clip(y.real * 200, -32767, 32767)
                pcm[1::2] = np.clip(y.imag * 200, -32767, 32767)
                dst.write(pcm.tobytes())
        outputs.append((power / max(total, 1), out_path))
    return sorted(outputs, reverse=True)


def decode(raw_path, work):
    """Essaie les variantes ; rend (image, lignes) la meilleure, ou (None, 0)."""
    best = (None, 0)
    for symrate in sorted({s for s, _ in TRIES}):
        symbols = os.path.join(work, f"sym{symrate}.s")
        subprocess.run([f"{BIN}/meteor_demod", "-B", "-q", "-s", str(OUT_RATE), "--bps", "16",
                        "-r", str(symrate), "-o", symbols, raw_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
        if not os.path.exists(symbols) or os.path.getsize(symbols) < 10000:
            continue
        for rate, flags in TRIES:
            if rate != symrate:
                continue
            image = os.path.join(work, f"image_{symrate}_{''.join(flags) or 'base'}.png")
            subprocess.run([f"{BIN}/meteor_decode", "-B", "-q", *flags, "-o", image, symbols],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
            if os.path.exists(image):
                try:
                    from PIL import Image
                    with Image.open(image) as im:
                        lines = im.size[1]
                except Exception:
                    lines = 0
                if lines > best[1]:
                    best = (image, lines)
    return best


def describe(image, sat):
    import io
    from PIL import Image
    import gemini
    with Image.open(image) as im:
        im = im.convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    text, problem = gemini.ask(PROMPT.format(sat=sat), temperature=0.4, jpeg=buf.getvalue())
    return text if text else f"Je n'ai pas pu faire décrire l'image : {problem}."


def main(path, sat):
    import veille_donnees as vd
    work = path + ".travail"
    os.makedirs(work, exist_ok=True)
    try:
        candidates = extract(path)
        image, lines = None, 0
        for _, raw in candidates[:1] + candidates[1:]:
            image, lines = decode(raw, work)
            if lines >= MIN_LINES:
                break
        if image is None or lines < MIN_LINES:
            vd.announce(f"Passage {sat} : signal trop faible, aucune image décodable cette fois.",
                        "/media/nas/nd1/server/radio_scaner/decoding_numeric.mp3", "meteor", "meteor_traitement")
            return
        final = path.rsplit(".", 1)[0] + ".png"
        os.replace(image, final)
        text = describe(final, sat)
        vd.archive("meteor", {"satellite": sat, "image": final, "lignes": lines, "description": text})
        vd.announce(f"Image du satellite météo {sat} reçue. {text}",
                    "/media/nas/nd1/server/radio_scaner/decoding_numeric.mp3", "meteor", "meteor_traitement")
    finally:
        for name in os.listdir(work):
            os.remove(os.path.join(work, name))
        os.rmdir(work)
        for f in [path] + [f"{path}.{f // 1000}.raw" for f in FREQS]:
            try:
                os.remove(f)
            except OSError:
                pass


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
