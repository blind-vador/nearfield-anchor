"""Captation LoRa (Meshtastic et APRS LoRa).

Detection par DECHIRP (2026-10-03), comme un vrai recepteur LoRa : chaque
symbole recu est multiplie par une rampe descendante ; le preambule LoRa (8
rampes montantes identiques) devient alors un pic net, au meme endroit,
plusieurs symboles d'affilee -- meme 15 a 20 dB SOUS le bruit (c'est le
principe du LoRa). Une porteuse, un capteur 433,92 MHz voisin ou du bruit
s'etalent et ne donnent jamais plusieurs symboles alignes. L'ancienne
detection par energie (+6 dB) gardait les capteurs voisins (0 trame decodee
sur toutes ses rafales) et ratait le vrai LoRa, presque toujours sous le
bruit. Mesures : synthese gr-lora_sdr detectee jusqu'a ~-21 dB (SF12) ;
13 fausses rafales enregistrees et 20 s de bruit : 0 ou 1 symbole aligne.

Un preambule detecte -> on garde la seconde d'avant et la suite de la trame
(I/Q brut, disque de stockage, avec sa fiche JSON), et on la confie aussitot
au pont (lib/lora_messages.py) : la machine compagne la decode et le Pi
annonce le message ; le Pi ne decode lui-meme que si aucune machine ne repond.

  meshtastic  869,525 MHz, largeur 250 kHz, SF11 (preset europeen LongFast)
  lora_aprs   433,775 MHz, largeur 125 kHz, SF12 (APRS LoRa des radioamateurs)
"""
import json
import os
import subprocess
import time

import numpy as np

import veille_donnees as vd
from lecture_flux import lire

PROFILES = [
    {"nom": "meshtastic", "freq": 869_525_000, "largeur": 250_000, "sf": 11, "apres_s": 2.5},
    {"nom": "lora_aprs", "freq": 433_775_000, "largeur": 125_000, "sf": 12, "apres_s": 6.0},
]
RATE = 1_000_000
MIN_PAPR = 25.0               # pic / moyenne du spectre dechirpe (bruit pur : 10 a 17)
MIN_RUN = 4                   # symboles alignes d'affilee (preambule : 7 ou 8)
BEFORE_S = 1.0                # garde avant la detection (debut du preambule)
STORE_DIR = "/media/nas/nd2/storage/radio/lora"

_next = 0


def _downchirp(profile):
    n = int(2 ** profile["sf"] * RATE / profile["largeur"])
    bw, T = profile["largeur"], 2 ** profile["sf"] / profile["largeur"]
    t = np.arange(n) / RATE
    return np.exp(-2j * np.pi * (-bw / 2 * t + bw / (2 * T) * t ** 2)).astype(np.complex64)


def _iq(raw):
    x = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
    z = (x[0::2] + 1j * x[1::2]).astype(np.complex64)
    return z - z.mean()


def _peak(raw, ref):
    """(case du pic, pic / moyenne) du symbole dechirpe."""
    spec = np.abs(np.fft.fft(_iq(raw) * ref)) ** 2
    i = int(np.argmax(spec))
    return i, float(spec[i] / (spec.mean() + 1e-12))


def _save(profile, raw, start_t, papr):
    folder = os.path.join(STORE_DIR, profile["nom"])
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(start_t))
    base = os.path.join(folder, f"{stamp}_{profile['freq']}")
    with open(base + ".cu8", "wb") as f:
        f.write(raw)
    info = {"profil": profile["nom"], "freq": profile["freq"], "largeur": profile["largeur"], "taux": RATE,
            "format": "cu8", "t": start_t, "duree_s": round(len(raw) / 2 / RATE, 2), "sf": profile["sf"],
            "detection": "dechirp", "pic": round(papr, 1), "decode": None}
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(info, f)
    vd.archive("lora", info)
    try:
        import lora_messages
        lora_messages.submit_burst(base + ".cu8", info)
    except Exception as exc:  # le pont ne doit pas arreter l'ecoute
        print(f"[lora_watch] trame non confiee au pont : {exc!r}")


def scan_window(dwell_sec=30):
    """Ecoute un profil (en alternance). Rend toujours False : la piqure LoRa
    ne vient que d'une trame vraiment decodee (lib/lora_messages.py)."""
    global _next
    profile = PROFILES[_next % len(PROFILES)]
    _next += 1
    ref = _downchirp(profile)
    sym_bytes = 2 * len(ref)
    keep = int(BEFORE_S * RATE * 2 / sym_bytes) + 1
    proc = subprocess.Popen(["rtl_sdr", "-f", str(profile["freq"]), "-s", str(RATE), "-g", "40", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    history, run, prev, saved, best = [], 0, None, 0, 0.0
    try:
        lire(proc.stdout, RATE * 2 // 4)  # le premier quart de seconde (reglage du tuner) est jete
        end = time.time() + dwell_sec
        while time.time() < end:
            raw = lire(proc.stdout, sym_bytes)
            if len(raw) < sym_bytes:
                if not raw:
                    print(f"[lora_watch] {profile['nom']} : la cle n'envoie plus rien, fenetre abandonnee")
                break
            history = (history + [raw])[-keep:]
            i, papr = _peak(raw, ref)
            best = max(best, papr)
            aligned = prev is not None and min((i - prev) % len(ref), (prev - i) % len(ref)) <= 1
            run = run + 1 if papr >= MIN_PAPR and aligned else (1 if papr >= MIN_PAPR else 0)
            prev = i if papr >= MIN_PAPR else None
            if run >= MIN_RUN:
                start_t = time.time() - len(history) * len(ref) / RATE
                tail = lire(proc.stdout, int(profile["apres_s"] * RATE) * 2)
                _save(profile, b"".join(history) + tail, start_t, papr)
                saved += 1
                history, run, prev = [], 0, None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"[lora_watch] {profile['nom']} : {saved} trame(s) LoRa reperee(s), pic max {best:.0f}")
    return False
