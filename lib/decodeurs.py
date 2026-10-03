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


# Textes transportes avec la voix (2026-10-03) : lignes de dsd-fme.
SRC_RE = re.compile(r"SRC[=:]\s*(\d+)")
TGT_RE = re.compile(r"TGT[=:]\s*(\d+)")
ALIAS_RE = re.compile(r"Alias:\s*\[?([^\]]*?)\]?\s*$")
TEXT_RE = re.compile(r"(?:SMS|ISO7 Text|ISO8 Text|Complete Meta Text|Text):\s*(.+?)\s*$")
GPS_RE = re.compile(r"GPS:\s*\(?\s*(-?\d+\.\d+)[^\d-]+(-?\d+\.\d+)")


def new_stats(freq_hz=None):
    return {"clean": 0, "total": 0, "protocol": None, "freq_hz": freq_hz, "src": None, "tgt": None}


def _readable(value):
    value = " ".join(value.split())
    return value if sum(c.isalnum() for c in value) >= MIN_DECODED_MESSAGE_CHARS else ""


def dsd_line(stats, text):
    """Une ligne de sortie de dsd-fme : compte les synchros propres ou en
    erreur, retient l'emetteur, et fait dire aussitot les textes transportes
    (alias, SMS, textes, GPS) une fois le decodage valide."""
    m = re.search(r"Sync: [+-]([\w-]+)", text)
    if m:
        stats["total"] += 1
        if "ERR" not in text:
            stats["clean"] += 1
            stats["protocol"] = DSD_PROTOCOLS.get(m.group(1), m.group(1))
    for regex, field in ((SRC_RE, "src"), (TGT_RE, "tgt")):
        found = regex.search(text)
        if found:
            stats[field] = found.group(1)
    if not dsd_validated(stats):
        return
    for regex, kind in ((ALIAS_RE, "alias"), (TEXT_RE, "texte")):
        found = regex.search(text)
        if found and _readable(found.group(1)):
            _say(kind, _readable(found.group(1)), stats)
            return
    found = GPS_RE.search(text)
    if found:
        _say("gps", f"{found.group(1)}, {found.group(2)}", stats)


def _say(kind, value, stats):
    try:
        import messages_numeriques
        messages_numeriques.found(kind, value, stats)
    except Exception as exc:  # une annonce ratee n'arrete pas le decodage
        print(f"[decodeurs] texte {kind} : {exc!r}")


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
