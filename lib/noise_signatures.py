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
BACKGROUND_NAS = f"{NAS_ROOT}/server/radio_log/bruit_de_fond.json"
BACKGROUND_LOCAL = "/home/vador/autopilot/pi_console/bruit_de_fond.json"
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
# Canaux a silencieux (RTLSDR-Airband) : l'audio arrive par morceaux ; on
# prolonge la pre-ecoute jusqu'a avoir assez de son pour une empreinte, sans
# depasser PRECHECK_MAX_SEC (etude du 2026-09-30 : 117 pre-ecoutes sur 128 du
# PMR446 n'avaient pas les 0,5 s necessaires et laissaient tout passer).
PRECHECK_MAX_SEC = 3.0
DUPLICATE_SIMILARITY = 0.98  # une empreinte a ce point identique n'apporte rien
BACKGROUND_PER_BAND = 12     # echantillons de bruit de fond gardes par bande


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
    return any(e.get("actif", True) and abs(e["freq_hz"] - freq_hz) <= FREQ_TOLERANCE_HZ
               for e in load_signatures())


def matches_known_noise(freq_hz, pcm_bytes, sample_rate, sample_format):
    """(True, similarite) si l'audio ressemble a un parasite connu proche
    en frequence ; sinon (False, meilleure similarite ou None).
    Les filtres desactives (actif=False, commande console "filtres") sont
    ignores : le parasite est garde mais ne bloque plus rien."""
    fp = fingerprint(pcm_bytes, sample_rate, sample_format)
    if fp is None:
        return False, None
    best = None
    for entry in load_signatures():
        if not entry.get("actif", True):
            continue
        if abs(entry["freq_hz"] - freq_hz) > FREQ_TOLERANCE_HZ:
            continue
        sim = similarity(fp, entry["fingerprint"])
        best = sim if best is None else max(best, sim)
        if sim >= MATCH_SIMILARITY:
            return True, sim
    return False, best


def add_signature(capture):
    """Ajoute l'empreinte aux parasites connus, sauf quasi-doublon (meme
    frequence, similarite >= DUPLICATE_SIMILARITY : 7 paires de ce genre
    trouvees parmi les 172 premieres). Rend le nombre total."""
    path = _signatures_path()
    entries = _read_json(path, [])
    for e in entries:
        if (abs(e["freq_hz"] - capture["freq_hz"]) <= FREQ_TOLERANCE_HZ
                and similarity(e["fingerprint"], capture["fingerprint"]) >= DUPLICATE_SIMILARITY):
            return len(entries)
    entries.append({
        "freq_hz": capture["freq_hz"],
        "label": capture["label"],
        "fingerprint": capture["fingerprint"],
        "ajoute_le": time.strftime("%Y-%m-%d %H:%M"),
    })
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_json(path, entries)
    return len(entries)


# --- Gestion des filtres par la console (commande "filtres", 2026-10-09) ---
# Les parasites sont marques un par un ("parasite"), et sur une bande chargee
# (PMR446...) des dizaines d'empreintes s'accumulent et finissent par bloquer
# toute la bande. La commande "filtres" les regroupe par canal (~12,5 kHz) et
# permet de les ACTIVER/DESACTIVER sans les supprimer (reversible) ou de les
# purger. Un filtre desactive est ignore par matches_known_noise/has_signatures.

def _bucket(freq_hz):
    """Numero de canal ~12,5 kHz : regroupe les empreintes d'une meme frequence."""
    return round(freq_hz / FREQ_TOLERANCE_HZ)


def _in_group(entry, label, bucket):
    return entry.get("label", "") == label and _bucket(entry["freq_hz"]) == bucket


def filtres():
    """Parasites regroupes par (bande, canal) pour la console : liste triee de
    {cle, freq_hz, label, nb, actif}. actif = au moins une empreinte active."""
    groups = {}
    for e in load_signatures():
        key = (e.get("label", ""), _bucket(e["freq_hz"]))
        g = groups.setdefault(key, {"freqs": [], "nb": 0, "actifs": 0})
        g["freqs"].append(e["freq_hz"])
        g["nb"] += 1
        g["actifs"] += 1 if e.get("actif", True) else 0
    out = [{"cle": [label, bucket], "freq_hz": sum(g["freqs"]) / len(g["freqs"]),
            "label": label, "nb": g["nb"], "actif": g["actifs"] > 0}
           for (label, bucket), g in groups.items()]
    return sorted(out, key=lambda x: x["freq_hz"])


def _rewrite_signatures(entries):
    path = _signatures_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_json(path, entries)


def set_group_active(cle, actif):
    """Active ou desactive toutes les empreintes du groupe (cle = [label, bucket]).
    Retourne True si le fichier a change."""
    label, bucket = cle[0], cle[1]
    entries = _read_json(_signatures_path(), [])
    changed = False
    for e in entries:
        if _in_group(e, label, bucket) and e.get("actif", True) != actif:
            e["actif"] = actif
            changed = True
    if changed:
        _rewrite_signatures(entries)
    return changed


def delete_group(cle):
    """Supprime toutes les empreintes du groupe. Retourne les entrees retirees
    (pour restore_entries)."""
    label, bucket = cle[0], cle[1]
    entries = _read_json(_signatures_path(), [])
    removed = [e for e in entries if _in_group(e, label, bucket)]
    if removed:
        _rewrite_signatures([e for e in entries if not _in_group(e, label, bucket)])
    return removed


def restore_entries(removed):
    """Remet des empreintes supprimees (Ctrl+Z de la commande "filtres")."""
    if not removed:
        return False
    entries = _read_json(_signatures_path(), [])
    entries.extend(removed)
    _rewrite_signatures(entries)
    return True


# --- Bruit de fond (idee de l'utilisateur, 2026-09-30) ---
# Chaque candidat que RNNoise ecarte comme bruit EST un echantillon du fond de
# sa bande : on garde les derniers par bande, et chaque capture recoit sa
# ressemblance au fond. Indicateur seulement : rien n'est bloque sur cette
# base (priorite n 1 : ne jamais bloquer a tort).

def _background_path():
    return BACKGROUND_NAS if os.path.ismount(NAS_ROOT) else BACKGROUND_LOCAL


def add_background(target, freq_hz, fp):
    if not target or not fp:
        return
    path = _background_path()
    data = _read_json(path, {})
    samples = data.get(target, [])
    samples.append({"freq_hz": freq_hz, "fingerprint": fp, "le": time.strftime("%Y-%m-%d %H:%M")})
    data[target] = samples[-BACKGROUND_PER_BAND:]
    try:
        _atomic_write_json(path, data)
    except OSError as exc:
        print(f"[noise_signatures] echantillon de fond non range : {exc}")


def background_similarity(target, fp):
    """Ressemblance maximale au bruit de fond connu de la bande, ou None."""
    if not target or not fp:
        return None
    samples = _read_json(_background_path(), {}).get(target, [])
    if not samples:
        return None
    return round(max(similarity(fp, s["fingerprint"]) for s in samples), 3)


# --- Derniere capture (ecrite par le scanner, lue par la console) ---

def record_capture(source, freq_hz, mode, label, target, log_path, fp, metrics=None):
    """source : "rtl_fm" (canaux FM et balayages) ou "airband" (fenetres
    RTLSDR-Airband) ; target : nom de la bande ou de la fenetre, pour
    pouvoir s'y verrouiller. metrics : ce que le systeme a mesure pendant la
    capture (probabilite de voix, niveau, trous du silencieux...), confronte
    plus tard aux notes de l'utilisateur (dialogue de notation du journal)."""
    metrics = dict(metrics or {})
    metrics["ressemblance_fond"] = background_similarity(target, fp)
    try:
        _atomic_write_json(LAST_CAPTURE_PATH, {
            "source": source, "freq_hz": freq_hz, "mode": mode, "label": label,
            "target": target, "log_path": log_path, "fingerprint": fp,
            "time": time.time(), "marked_noise": False, "metrics": metrics,
        })
    except OSError as exc:
        print(f"[noise_signatures] derniere capture non memorisee : {exc}")
    if log_path:  # meme sans empreinte : les mesures servent a la notation
        try:
            _remember_capture(log_path, {"freq_hz": freq_hz, "label": label, "fingerprint": fp,
                                         "source": source, "mode": mode, "target": target,
                                         "metrics": metrics})
        except OSError as exc:
            print(f"[noise_signatures] empreinte non rangee pres de l'enregistrement : {exc}")


# --- Empreintes rangees a cote des enregistrements du journal ---
# Pour pouvoir rejeter APRES COUP n'importe quel enregistrement (dialogue
# "journal" de la console), pas seulement la derniere capture : le mp3 du
# journal contient les annonces et carillons, inutilisables comme empreinte.

INDEX_NAME = ".empreintes.json"


def _index_path(log_path):
    return os.path.join(os.path.dirname(log_path), INDEX_NAME)


def _remember_capture(log_path, entry):
    index_path = _index_path(log_path)
    index = _read_json(index_path, {})
    folder = os.path.dirname(log_path)
    # Menage : les enregistrements purges (48 h) n'ont plus besoin d'empreinte.
    index = {name: e for name, e in index.items() if os.path.exists(os.path.join(folder, name))}
    index[os.path.basename(log_path)] = entry
    _atomic_write_json(index_path, index)


def capture_info(log_path):
    """Frequence, libelle et empreinte d'un enregistrement du journal, ou None."""
    return _read_json(_index_path(log_path), {}).get(os.path.basename(log_path))


def forget_capture(log_path):
    index_path = _index_path(log_path)
    index = _read_json(index_path, {})
    if index.pop(os.path.basename(log_path), None) is not None:
        _atomic_write_json(index_path, index)


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
