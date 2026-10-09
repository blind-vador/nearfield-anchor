"""Journal des rejets (2026-10-09) : les candidats ecartes en silence par le
scanner ("bruit, pas de signal reel" -- RNNoise ; ou "parasite connu"). Chaque
rejet garde un COURT extrait audio (RADIO_LOG_DIR/rejets/, purge 48 h comme le
reste du journal) et une entree d'index.

La console ("journal des rejets", console/rejets_cmd.py) rejoue chaque extrait
et laisse l'utilisateur trancher A L'OREILLE :
- n (noter)   : c'etait en fait un VRAI signal, ecarte a tort  -> verdict "signal"
- r (rejeter) : c'etait bien du bruit/parasite                 -> verdict "bruit"

Ces verdicts (faux rejets vs bruit confirme) sont la base pour recalibrer les
seuils et l'apprentissage, et pour la double verification par le Mac mini via
le bridge (modele neuronal plus costaud) -- cumul metadonnees + IA + humain.
"""
import fcntl
import json
import os
import time

from sound_paths import RADIO_LOG_DIR

REJETS_DIR = os.path.join(RADIO_LOG_DIR, "rejets")
INDEX_PATH = os.path.join(REJETS_DIR, "index.json")
LOCK_PATH = "/tmp/rejets.lock"
MAX_ENTRIES = 500  # borne dure : on ne garde que les plus recents (+ purge 48 h)


def _read():
    try:
        with open(INDEX_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _with_lock(fn):
    """Lecture-modification-ecriture atomique sous verrou (le scanner ajoute,
    la console etiquette -- chacun de son cote)."""
    os.makedirs(REJETS_DIR, exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read()
        result = fn(data)
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, INDEX_PATH)
        return result


def record(log_path, freq_hz, label, reason, fingerprint=None):
    """Ajoute un rejet a l'index. log_path = extrait audio deja ecrit (par
    RadioLogSession.finalize(subdir="rejets")). reason : "bruit" ou "parasite"."""
    if not log_path:
        return

    def add(data):
        data.append({
            "fichier": os.path.basename(log_path),
            "freq_hz": freq_hz,
            "label": label,
            "raison": reason,
            "le": time.time(),
            "verdict": None,           # None = pas encore trie
            "empreinte": fingerprint,  # pour recalibrage / double verif IA
        })
        del data[:-MAX_ENTRIES]
    try:
        _with_lock(add)
    except Exception as exc:
        print(f"[rejets] enregistrement impossible : {exc}")


def entries():
    """Rejets du plus recent au plus ancien, seulement ceux dont l'extrait
    existe encore (purge 48 h)."""
    out = []
    for e in _read():
        if os.path.isfile(os.path.join(REJETS_DIR, e.get("fichier", ""))):
            out.append(e)
    out.sort(key=lambda e: e.get("le", 0), reverse=True)
    return out


def path_of(fichier):
    return os.path.join(REJETS_DIR, fichier)


def set_verdict(fichier, verdict):
    """verdict : "signal" (vrai signal ecarte a tort), "bruit" (rejet correct),
    ou None (a trier). Retourne True si trouve."""
    found = [False]

    def apply(data):
        for e in data:
            if e.get("fichier") == fichier:
                e["verdict"] = verdict
                found[0] = True
    try:
        _with_lock(apply)
    except Exception as exc:
        print(f"[rejets] verdict non enregistre : {exc}")
    return found[0]


def purge(cutoff):
    """Retire de l'index les entrees plus vieilles que cutoff (epoch) ou dont
    l'extrait a disparu. Les mp3 eux-memes sont purges par radio_log."""
    def clean(data):
        kept = [e for e in data if e.get("le", 0) >= cutoff
                and os.path.isfile(os.path.join(REJETS_DIR, e.get("fichier", "")))]
        data[:] = kept
    try:
        _with_lock(clean)
    except Exception as exc:
        print(f"[rejets] purge impossible : {exc}")
