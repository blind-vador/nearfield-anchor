"""Guet appris par spot (2026-10-31) : combien de temps CE point (frequence)
met, d'habitude, a confirmer un signal apres qu'on s'y pose. Se compose avec le
plancher du tempo dans tempo.sonde() -- dwell = max(plancher_litterature,
guet_appris) -- pour que le mode dechaine ne survole pas trop vite un spot qui,
chez nous, met plus longtemps a accrocher.

Le scanner reste agnostique : on ne vise rien, on apprend de l'usage pour mieux
affiner la PRISE. Deterministe par frequence (seau de BUCKET_HZ). Survit aux
redemarrages (dossier runtime).

Qui apprend, qui en profite : la mesure n'a lieu qu'au moment ou un signal se
CONFIRME -- donc surtout en entre_deux / lent (sonde large) ; dechaine, lui, en
PROFITE (sa sonde courte est rallongee sur les spots connus comme lents). Un
spot jamais vu en mode large reste survole vite en dechaine (assume).

Estimation bornee a GUET_MAX_SEC : monte vite vers le plus lent vu, redescend
lentement (un spot qui n'a plus besoin d'autant se detend)."""
import json
import os
import time

import chemins

PATH = chemins.base("guet.json")
BUCKET_HZ = 12_500
GUET_MAX_SEC = 6.0
DECAY = 0.97          # a chaque mise a jour, la valeur apprise se detend un peu
MIN_DELAY_SEC = 0.3   # sous ce delai : bruit de mesure, on ignore


def _bucket(freq_hz):
    return str(int(round(freq_hz / BUCKET_HZ) * BUCKET_HZ))


def _load():
    try:
        with open(PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def confirmation(freq_hz):
    """Temps de confirmation appris pour cette frequence (s), 0 si inconnu."""
    if not freq_hz:
        return 0.0
    e = _load().get(_bucket(freq_hz))
    try:
        return float(e.get("sec", 0.0)) if e else 0.0
    except (TypeError, ValueError):
        return 0.0


def noter(freq_hz, delay_sec):
    """Enregistre le temps (start -> confirmation) observe sur cette frequence."""
    if not freq_hz or delay_sec is None or delay_sec < MIN_DELAY_SEC:
        return
    delay = min(float(delay_sec), GUET_MAX_SEC)
    data = _load()
    k = _bucket(freq_hz)
    old = 0.0
    try:
        old = float((data.get(k) or {}).get("sec", 0.0))
    except (TypeError, ValueError):
        old = 0.0
    sec = min(max(delay, old * DECAY), GUET_MAX_SEC)  # monte vers le plus lent, redescend doucement
    data[k] = {"sec": round(sec, 2), "t": time.time(), "n": int((data.get(k) or {}).get("n", 0)) + 1}
    tmp = PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, PATH)
    except OSError as exc:
        print(f"[guet] non enregistre : {exc!r}")
