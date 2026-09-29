"""Parasites connus, derniere capture et verrouillage du scanner radio.

Partage entre radio_watch.py / airband_bridge.py (qui ecrivent la derniere
capture et consultent les parasites connus) et la console (commandes
"parasite", "accroche", "decroche").

Empreinte sonore : repartition de l'energie du signal radio (sans
annonces ni carillons) sur SIGNATURE_BANDS bandes log-espacees de la bande
vocale 300-3000 Hz, en log, centree et normalisee -- deux sons se
ressemblent si le produit scalaire de leurs empreintes (similarite
cosinus) depasse MATCH_SIMILARITY. Volontairement simple : c'est la
"forme" du bruit (souffle, sifflement, bourdonnement...) qui est comparee,
pas son volume. Premiere version, seuils a calibrer sur l'usage reel.

Fichiers :
- parasites connus : sur le NAS (survit a une panne de carte SD), sinon
  en local ;
- derniere capture et demande de verrouillage : /tmp (volatils, remis a
  zero au redemarrage, voulu).
"""
import json
import os
import tempfile
import time

NAS_ROOT = "/media/nas/nd1"
SIGNATURES_NAS = f"{NAS_ROOT}/server/radio_log/parasites_connus.json"
SIGNATURES_LOCAL = "/home/vador/autopilot/pi_console/parasites_connus.json"
LAST_CAPTURE_PATH = "/tmp/radio_watch_derniere_capture.json"
CONTROL_PATH = "/tmp/radio_watch_controle.json"
PLAGES_PATH = "/tmp/radio_watch_plages.json"

SIGNATURE_BANDS = 24
SIGNATURE_LOW_HZ = 300.0
SIGNATURE_HIGH_HZ = 3000.0
MIN_AUDIO_SEC = 0.5
MAX_AUDIO_SEC = 10.0
FREQ_TOLERANCE_HZ = 12_500   # un parasite "revient" s'il retombe a +/- 12,5 kHz
MATCH_SIMILARITY = 0.92
PRECHECK_SEC = 1.5           # audio ecoute (sans rien annoncer) avant de decider


def _signatures_path():
    return SIGNATURES_NAS if os.path.ismount(NAS_ROOT) else SIGNATURES_LOCAL


def _atomic_write_json(path, data):
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# --- Empreinte sonore ---

def fingerprint(pcm_bytes, sample_rate, sample_format):
    """Liste de SIGNATURE_BANDS flottants, ou None si l'audio est trop court."""
    import numpy as np
    dtype = np.float32 if sample_format == "f32le" else np.int16
    samples = np.frombuffer(pcm_bytes[: len(pcm_bytes) - len(pcm_bytes) % np.dtype(dtype).itemsize], dtype=dtype)
    samples = samples.astype(np.float64)[: int(sample_rate * MAX_AUDIO_SEC)]
    frame = 1024
    if len(samples) < max(frame, sample_rate * MIN_AUDIO_SEC):
        return None
    hop = frame // 2
    window = np.hanning(frame)
    frames = [samples[i:i + frame] * window for i in range(0, len(samples) - frame + 1, hop)]
    power = np.mean(np.abs(np.fft.rfft(np.array(frames), axis=1)) ** 2, axis=0)
    freqs = np.fft.rfftfreq(frame, 1.0 / sample_rate)
    edges = np.geomspace(SIGNATURE_LOW_HZ, SIGNATURE_HIGH_HZ, SIGNATURE_BANDS + 1)
    bands = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (freqs >= lo) & (freqs < hi)
        bands.append(np.log10(np.sum(power[mask]) + 1e-12) if mask.any() else -12.0)
    vec = np.array(bands) - np.mean(bands)
    norm = np.linalg.norm(vec)
    if norm < 1e-9:
        return None
    return [round(float(v), 5) for v in vec / norm]


def similarity(a, b):
    return sum(x * y for x, y in zip(a, b))


# --- Parasites connus ---

_cache = {"path": None, "mtime": None, "entries": []}


def load_signatures():
    """Relit le fichier seulement s'il a change (la console l'enrichit
    pendant que le scanner tourne)."""
    path = _signatures_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if _cache["path"] != path or _cache["mtime"] != mtime:
        _cache.update(path=path, mtime=mtime, entries=_read_json(path, []))
    return _cache["entries"]


def has_signatures_near(freq_hz):
    return any(abs(e["freq_hz"] - freq_hz) <= FREQ_TOLERANCE_HZ for e in load_signatures())


def matches_known_noise(freq_hz, pcm_bytes, sample_rate, sample_format):
    """(True, similarite) si l'audio ressemble a un parasite connu proche
    en frequence ; sinon (False, meilleure similarite ou None)."""
    fp = fingerprint(pcm_bytes, sample_rate, sample_format)
    if fp is None:
        return False, None
    best = None
    for entry in load_signatures():
        if abs(entry["freq_hz"] - freq_hz) > FREQ_TOLERANCE_HZ:
            continue
        sim = similarity(fp, entry["fingerprint"])
        best = sim if best is None else max(best, sim)
        if sim >= MATCH_SIMILARITY:
            return True, sim
    return False, best


def add_signature(capture):
    path = _signatures_path()
    entries = _read_json(path, [])
    entries.append({
        "freq_hz": capture["freq_hz"],
        "label": capture["label"],
        "fingerprint": capture["fingerprint"],
        "ajoute_le": time.strftime("%Y-%m-%d %H:%M"),
    })
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_json(path, entries)
    return len(entries)


# --- Derniere capture (ecrite par le scanner, lue par la console) ---

def record_capture(source, freq_hz, mode, label, target, log_path, fp):
    """source : "rtl_fm" (canaux FM et balayages) ou "airband" (fenetres
    RTLSDR-Airband) ; target : nom de la bande ou de la fenetre, pour
    pouvoir s'y verrouiller."""
    try:
        _atomic_write_json(LAST_CAPTURE_PATH, {
            "source": source, "freq_hz": freq_hz, "mode": mode, "label": label,
            "target": target, "log_path": log_path, "fingerprint": fp,
            "time": time.time(), "marked_noise": False,
        })
    except OSError as exc:
        print(f"[noise_signatures] derniere capture non memorisee : {exc}")


def last_capture():
    return _read_json(LAST_CAPTURE_PATH, None)


def mark_last_capture_noise():
    capture = last_capture()
    if capture:
        capture["marked_noise"] = True
        _atomic_write_json(LAST_CAPTURE_PATH, capture)


# --- Verrouillage (demande par la console, applique par le scanner) ---

def request_lock(capture):
    _atomic_write_json(CONTROL_PATH, {"lock": True, "capture": capture, "time": time.time()})


def request_unlock():
    _atomic_write_json(CONTROL_PATH, {"lock": False, "time": time.time()})


def lock_request():
    """La capture a laquelle se verrouiller, ou None si balayage normal."""
    control = _read_json(CONTROL_PATH, {})
    return control.get("capture") if control.get("lock") else None


# --- Mode manuel (dialogue de la console, ecoute en direct) ---

def request_manual(target):
    _atomic_write_json(CONTROL_PATH, {"lock": False, "manual": target, "time": time.time()})


def manual_request():
    """La frequence a ecouter en direct, ou None hors mode manuel."""
    return _read_json(CONTROL_PATH, {}).get("manual")


def write_plages(plages):
    _atomic_write_json(PLAGES_PATH, plages)


def read_plages():
    return _read_json(PLAGES_PATH, [])
