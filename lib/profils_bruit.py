"""Empreintes du bruit de fond, par bande et par frequence (2026-10-02) :
apprises a chaque ecoute que RNNoise ecarte comme bruit (le "neutre" de la
frequence hors emission), utilisees par le nettoyage doux
(audio_filters.GentleCleaner). Sur la carte SD du Pi.
"""
import json
import os
import time

import numpy as np

import audio_filters

STORE_PATH = "/home/vador/autopilot/profils_bruit.json"
PER_BAND = 12
NEAR_HZ = 200_000


def _load():
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def learn(target, freq_hz, pcm_s16, sample_rate):
    spectrum = audio_filters.noise_spectrum(pcm_s16, sample_rate)
    if spectrum is None or not target:
        return False
    data = _load()
    entries = [e for e in data.get(target, []) if abs(e["freq_hz"] - freq_hz) > 5_000 or e["taux"] != sample_rate]
    entries.append({"freq_hz": int(freq_hz), "taux": sample_rate, "t": time.time(),
                    "amplitude": [round(float(v), 2) for v in spectrum]})
    data[target] = entries[-PER_BAND:]
    tmp = STORE_PATH + ".tmp"
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STORE_PATH)
    return True


def get(target, freq_hz, sample_rate):
    """Empreinte la plus proche en frequence (meme taux), sinon la mediane
    de la bande ; None si rien d'appris."""
    entries = [e for e in _load().get(target, []) if e["taux"] == sample_rate]
    if not entries:
        return None
    near = [e for e in entries if abs(e["freq_hz"] - freq_hz) <= NEAR_HZ]
    if near:
        return np.array(min(near, key=lambda e: abs(e["freq_hz"] - freq_hz))["amplitude"])
    return np.median(np.array([e["amplitude"] for e in entries]), axis=0)
