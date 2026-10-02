"""Regles communes des decodeurs numeriques, partagees par le scanner
(watchers/radio_watch.py) et l'ecoute manuelle (lib/manual_tuner.py) :
tous les decodeurs fonctionnent pareil partout (demande utilisateur
2026-10-02 : le DMR manquait en mode manuel).

- dsd-fme (voix numerique : DMR, dPMR, NXDN, M17, D-Star, YSF, P25...) : un
  decodage n'est accepte qu'apres DSD_MIN_CLEAN_SYNC synchros sans erreur,
  au moins DSD_MIN_CLEAN_RATIO des synchros vues (sinon du bruit passe) ;
- multimon-ng (donnees : bipeurs POCSAG/FLEX, APRS AFSK1200, DTMF, morse,
  selectifs...) : un message n'est garde que s'il a assez de vrais caracteres.
"""
import re

DSD_PROTOCOLS = {"DMR": "DMR", "P25p1": "P25", "P25p2": "P25", "YSF": "YSF", "DSTAR": "D-Star",
                 "M17": "M17", "NXDN48": "NXDN", "NXDN96": "NXDN", "NXDN": "NXDN", "dPMR": "dPMR",
                 "X2-TDMA": "X2-TDMA"}
DSD_MIN_CLEAN_SYNC = 2
DSD_MIN_CLEAN_RATIO = 0.25
DSD_STEREO_DATAGRAM = 640   # 16 bits, deux slots TDMA
DSD_MONO_DATAGRAM = 320
DSD_VOICE_RATE = 8000
# Modes que l'auto-detection (-fa) ne tente pas, essayes selon la bande.
DSD_EXTRA_MODES = [
    (446_000_000, 446_200_000, "-fm", "dPMR"),
    (144_000_000, 146_000_000, "-fz", "M17"),
    (430_000_000, 440_000_000, "-fz", "M17"),
    (148_000_000, 174_000_000, "-fi", "NXDN"),
    (440_000_000, 470_000_000, "-fi", "NXDN"),
]
DSD_MONO_MODES = {"-fm", "-fz", "-fi", "-fn"}
MULTIMON_ARGS = [
    "-a", "AFSK1200",
    "-a", "POCSAG512", "-a", "POCSAG1200", "-a", "POCSAG2400",
    "-a", "FLEX",
    "-a", "FSK9600",
    "-a", "MORSE_CW",
    "-a", "DTMF",
    "-a", "EEA", "-a", "EIA", "-a", "CCIR",
    "-a", "ZVEI1", "-a", "ZVEI2", "-a", "ZVEI3", "-a", "DZVEI", "-a", "PZVEI",
    "-t", "raw", "-",
]
MULTIMON_RATE = 22050
MIN_DECODED_MESSAGE_CHARS = 4


def new_stats():
    return {"clean": 0, "total": 0, "protocol": None}


def dsd_line(stats, text):
    """Compte une ligne de sortie de dsd-fme (synchros propres ou en erreur)."""
    m = re.search(r"Sync: [+-]([\w-]+)", text)
    if m:
        stats["total"] += 1
        if "ERR" not in text:
            stats["clean"] += 1
            stats["protocol"] = DSD_PROTOCOLS.get(m.group(1), m.group(1))


def dsd_validated(stats):
    return stats["clean"] >= DSD_MIN_CLEAN_SYNC and stats["clean"] >= DSD_MIN_CLEAN_RATIO * stats["total"]


def extra_mode(freq_hz):
    for low, high, flag, name in DSD_EXTRA_MODES:
        if low <= freq_hz <= high:
            return flag, name
    return None


def meaningful(msg, min_chars=MIN_DECODED_MESSAGE_CHARS):
    """Faux positifs de multimon-ng : fragments morse non resolus ("<___.>")
    retires, puis assez de caracteres alphanumeriques reels."""
    cleaned = re.sub(r"<[^>]*>", "", msg)
    return sum(1 for c in cleaned if c.isalnum()) >= min_chars
