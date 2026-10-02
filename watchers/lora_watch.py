"""Captation LoRa (Meshtastic et APRS LoRa).

La fenetre d'ecoute repere les rafales (puissance au-dessus du bruit), les
garde en I/Q brut sur le disque de stockage avec leur fiche JSON, et confie
chacune tout de suite au pont (lib/lora_messages.py) : la machine compagne
la decode et le Pi annonce le message aussitot ; le Pi ne decode lui-meme
que si aucune machine ne repond.

  meshtastic  869,525 MHz, largeur 250 kHz (preset europeen LongFast)
  lora_aprs   433,775 MHz, largeur 125 kHz (APRS LoRa des radioamateurs)
"""
import json
import os
import subprocess
import time

import numpy as np

import veille_donnees as vd

PROFILES = [
    {"nom": "meshtastic", "freq": 869_525_000, "largeur": 250_000},
    {"nom": "lora_aprs", "freq": 433_775_000, "largeur": 125_000},
]
RATE = 1_000_000
BLOCK = 10_000                # 10 ms d'I/Q
THRESHOLD_DB = 6.0            # au-dessus du bruit median
MIN_BURST_BLOCKS = 5          # 50 ms : plus court qu'un paquet LoRa = parasite
MARGIN_BLOCKS = 10            # 100 ms gardes avant et apres
MAX_BURST_BLOCKS = 600        # 6 s au plus par rafale
STORE_DIR = "/media/nas/nd2/storage/radio/lora"

_next = 0


def _band_power(block_u8, width):
    """Puissance (dB) dans la largeur du canal, autour du centre."""
    iq = block_u8.astype(np.float32) - 127.5
    x = iq[0::2] + 1j * iq[1::2]
    spectrum = np.abs(np.fft.fft(x)) ** 2
    keep = int(len(x) * width / RATE / 2)
    band = np.concatenate([spectrum[1:keep], spectrum[-keep:]])  # sans la raie centrale
    return 10 * np.log10(band.mean() + 1e-9)


def _save(profile, blocks, start_t, peak_db, floor_db):
    folder = os.path.join(STORE_DIR, profile["nom"])
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(start_t))
    base = os.path.join(folder, f"{stamp}_{profile['freq']}")
    with open(base + ".cu8", "wb") as f:
        for b in blocks:
            f.write(b.tobytes())
    info = {"profil": profile["nom"], "freq": profile["freq"], "largeur": profile["largeur"], "taux": RATE,
            "format": "cu8", "t": start_t, "duree_s": round(len(blocks) * BLOCK / RATE, 2),
            "crete_db": round(float(peak_db), 1), "bruit_db": round(float(floor_db), 1), "decode": None}
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(info, f)
    vd.archive("lora", info)
    try:
        import lora_messages
        lora_messages.submit_burst(base + ".cu8", info)
    except Exception as exc:  # le pont ne doit pas arreter l'ecoute
        print(f"[lora_watch] rafale non confiee au pont : {exc!r}")


def scan_window(dwell_sec=30):
    """Ecoute un profil (en alternance) ; rend True si des rafales ont ete gardees."""
    global _next
    profile = PROFILES[_next % len(PROFILES)]
    _next += 1
    proc = subprocess.Popen(["rtl_sdr", "-f", str(profile["freq"]), "-s", str(RATE), "-g", "40", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    history, powers = [], []
    burst, quiet, peak = None, 0, -999.0
    saved = 0
    try:
        end = time.time() + dwell_sec
        while time.time() < end:
            raw = proc.stdout.read(BLOCK * 2)
            if len(raw) < BLOCK * 2:
                break
            block = np.frombuffer(raw, dtype=np.uint8)
            p = _band_power(block, profile["largeur"])
            powers.append(p)
            floor = float(np.median(powers[-500:]))
            history = (history + [block])[-MARGIN_BLOCKS:]
            loud = len(powers) > 50 and p > floor + THRESHOLD_DB
            if burst is None:
                if loud:
                    burst, quiet, peak = list(history), 0, p
                    burst_t = time.time() - MARGIN_BLOCKS * BLOCK / RATE
                continue
            burst.append(block)
            peak = max(peak, p)
            quiet = 0 if loud else quiet + 1
            if quiet >= MARGIN_BLOCKS or len(burst) >= MAX_BURST_BLOCKS:
                if len(burst) - quiet - MARGIN_BLOCKS >= MIN_BURST_BLOCKS:
                    _save(profile, burst, burst_t, peak, floor)
                    saved += 1
                burst = None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if saved:
        print(f"[lora_watch] {profile['nom']} : {saved} rafale(s) gardee(s)")
    return saved > 0
