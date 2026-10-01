"""Relais radioamateurs appris tout seuls depuis l'APRS : beaucoup de relais
(ou leurs objets APRS) annoncent leur frequence de sortie, leur tonalite et
leur mode ("145.612MHz T123 R17k", "70cm Voice (D-Star) 439.68750MHz
-9.4000MHz"). Ceux a portee (RANGE_KM) sont ajoutes a l'ecoute du scanner,
groupes par bande (6 m, 2 m, 70 cm) : un rtl_power par groupe, rapide.
Analogique ou numerique (DMR, D-Star, C4FM) : le decodage numerique du
scanner s'en charge (dsd-fme -fa).
"""
import json
import os
import re
import time

STORE_PATH = "/media/nas/nd1/server/radio_log/relais.json"
LOCAL_PATH = "/home/vador/autopilot/relais.json"
RANGE_KM = 60
FORGET_SEC = 30 * 86400
GROUPS = [  # (nom, nom dit, bas, haut)
    ("relais_6m", "relais radioamateur 6 mètres", 50_000_000, 52_000_000),
    ("relais_2m", "relais radioamateur 2 mètres", 144_000_000, 146_000_000),
    ("relais_70cm", "relais radioamateur 70 centimètres", 430_000_000, 440_000_000),
]
FREQ = re.compile(r"(\d{2,3}\.\d{2,5})\s?MHz")
REPEATER_HINTS = re.compile(r"\b(T\d{3}|R\d+k|tone|CTCSS|Voice|MMDVM|relais|repeater|RPT)\b", re.IGNORECASE)
MODES = {"D-Star": "dstar", "DMR": "dmr", "C4FM": "c4fm"}


def _path():
    return STORE_PATH if os.path.isdir(os.path.dirname(STORE_PATH)) else LOCAL_PATH


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data):
    path = _path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[relais] liste non ecrite : {exc}")


def learn(name, comment, symbol, km):
    """Paquet APRS d'une station a km kilometres : si c'est un relais qui
    annonce sa frequence, on l'apprend. Rend la frequence apprise ou None."""
    if not comment or km is None or km > RANGE_KM:
        return None
    if re.search(r"LoRa|RX ONLY|iGate", comment, re.IGNORECASE):
        return None  # passerelle de donnees, pas un relais vocal
    m = FREQ.search(comment)
    if not m:
        return None
    looks_like = symbol in ("r", "&", "0") or REPEATER_HINTS.search(comment)
    if not looks_like:
        return None
    freq = int(round(float(m.group(1)) * 1e6))
    if not any(low <= freq <= high for _, _, low, high in GROUPS):
        return None
    mode = next((code for word, code in MODES.items() if word in comment), "fm")
    tone = re.search(r"\bT(\d{3})\b|tone\s*(\d+)|CTCSS\s*(\d+)", comment)
    data = load()
    known = data.get(str(freq))
    data[str(freq)] = {"nom": name.strip(), "mode": mode, "km": round(km, 1),
                       "tonalite": int(next(g for g in tone.groups() if g)) if tone else None,
                       "vu": time.time(), "appris": (known or {}).get("appris", time.time())}
    data = {f: r for f, r in data.items() if time.time() - r.get("vu", 0) < FORGET_SEC}
    _save(data)
    return None if known else freq


def bands():
    """Bandes "fm_channels" du scanner, une par groupe qui contient des relais."""
    data = load()
    result = []
    for name, spoken, low, high in GROUPS:
        channels = sorted(int(f) for f in data if low <= int(f) <= high)
        if channels:
            result.append({"name": name, "name_spoken": spoken, "type": "fm_channels",
                           "channels": channels, "mode": "nfm", "margin_db": 8.0, "span_hz": 12_500})
    return result
