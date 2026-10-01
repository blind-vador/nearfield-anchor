"""Traitement d'un passage Meteor-M (machine compagne) : enregistrement I/Q
8 bits a 1,024 MHz centre sur 137,5 MHz -> image des nuages.

  1. extraction de 137,1 et 137,9 MHz (decalage, filtre passe-bas calcule
     aux seuls echantillons gardes, decimation par 6 -> 170,7 kHz) ;
  2. meteor_demod (72 puis 80 kbit/s) et meteor_decode (variantes) : la plus
     grande image gagne.
Compatible Python 3.9 (Python d'Apple).
"""
import os
import subprocess

import numpy as np

DECIM = 6
TAPS = 101
CHUNK = 1 << 20
TRIES = [(72000, []), (72000, ["-d"]), (80000, ["-i"]), (80000, ["-i", "-d"])]
MIN_LINES = 80


def _lowpass(cutoff_hz, rate):
    n = np.arange(TAPS) - (TAPS - 1) / 2
    h = np.sinc(2 * cutoff_hz / rate * n) * np.hamming(TAPS)
    return (h / h.sum()).astype(np.float32)


def extract(path, center, rate, freqs):
    """Un fichier I/Q 16 bits par frequence ; rend [(puissance, chemin)], la plus forte d'abord."""
    h = _lowpass(85_000, rate)
    outputs = []
    for freq in freqs:
        out_path = "%s.%d.raw" % (path, freq // 1000)
        offset = freq - center
        power, total, n0, phase = 0.0, 0, 0, 0
        tail = np.zeros(TAPS - 1, np.complex64)
        with open(path, "rb") as src, open(out_path, "wb") as dst:
            while True:
                raw = src.read(CHUNK * 2)
                if len(raw) < 2:
                    break
                iq = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.uint8).astype(np.float32) - 127.5
                x = (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)
                t = np.arange(n0, n0 + len(x))
                x *= np.exp(-2j * np.pi * offset * t / rate).astype(np.complex64)
                n0 += len(x)
                xx = np.concatenate([tail, x])
                tail = x[-(TAPS - 1):]
                start = (-phase) % DECIM
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


def _lines(image):
    """Hauteur d'une image BMP (en-tete lu sans bibliotheque). BMP plutot que
    PNG : meteor_decode n'ecrit du PNG que si libpng etait la a la compilation."""
    try:
        with open(image, "rb") as f:
            head = f.read(26)
        return abs(int.from_bytes(head[22:26], "little", signed=True)) if head[:2] == b"BM" else 0
    except OSError:
        return 0


def decode(raw_path, work, out_rate, bin_dir):
    best = (None, 0)
    for symrate in sorted({s for s, _ in TRIES}):
        symbols = os.path.join(work, "sym%d.s" % symrate)
        subprocess.run([os.path.join(bin_dir, "meteor_demod"), "-B", "-q", "-s", str(out_rate), "--bps", "16",
                        "-r", str(symrate), "-o", symbols, raw_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
        if not os.path.exists(symbols) or os.path.getsize(symbols) < 10000:
            continue
        for rate, flags in TRIES:
            if rate != symrate:
                continue
            image = os.path.join(work, "image_%d_%s.bmp" % (symrate, "".join(flags) or "base"))
            subprocess.run([os.path.join(bin_dir, "meteor_decode"), "-B", "-q"] + flags + ["-o", image, symbols],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
            lines = _lines(image)
            if lines > best[1]:
                best = (image, lines)
    return best


def process(job_dir, params, bin_dir):
    """Rend (etat, sorties, message)."""
    path = os.path.join(job_dir, params["fichier"])
    rate = int(params["taux"])
    work = os.path.join(job_dir, "travail")
    os.makedirs(work, exist_ok=True)
    try:
        candidates = extract(path, int(params["centre"]), rate, [int(f) for f in params["frequences"]])
        best = (None, 0)
        for _, raw in candidates:
            best = decode(raw, work, rate // DECIM, bin_dir)
            if best[1] >= MIN_LINES:
                break
        if best[0] is None or best[1] < MIN_LINES:
            return "echec", [], "signal trop faible, aucune image décodable"
        os.replace(best[0], os.path.join(job_dir, "image.bmp"))
        return "fait", ["image.bmp"], "%d lignes" % best[1]
    finally:
        for name in os.listdir(work):
            os.remove(os.path.join(work, name))
        os.rmdir(work)
        for f in [path] + ["%s.%d.raw" % (path, int(fr) // 1000) for fr in params["frequences"]]:
            try:
                os.remove(f)
            except OSError:
                pass
