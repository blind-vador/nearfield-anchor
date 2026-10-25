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

# Comptage des synchros commun au direct et aux juges, du Pi comme des
# machines compagnes (bridge/juges_radio.py, 2026-10-25).
from juges_radio import PROTOCOLES as DSD_PROTOCOLS, compter_synchro  # noqa: F401

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
    # Bandes professionnelles (2026-10-24 : bribes de voix numerique non
    # decodees vers 460 MHz) : NXDN etroit, dPMR, puis NXDN large, essayes
    # lun apres lautre quand la voix FM est ecartee (extra_modes).
    (148_000_000, 174_000_000, "-fi", "NXDN"),
    (148_000_000, 174_000_000, "-fm", "dPMR"),
    (148_000_000, 174_000_000, "-fn", "NXDN"),
    (440_000_000, 470_000_000, "-fi", "NXDN"),
    (440_000_000, 470_000_000, "-fm", "dPMR"),
    (440_000_000, 470_000_000, "-fn", "NXDN"),
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
# Texte transporte par la voix numerique (SMS/alias DMR...) : deja protege du
# bruit par dsd_validated (la voix doit s'etre verrouillee avant), donc on peut
# etre permissif -- on LIT meme un texte court en clair (2026-10-09, meme
# principe que le correctif pagers : on ne jette pas un message en clair).
MIN_TRANSPORTED_TEXT_CHARS = 2


# Textes transportes avec la voix (2026-10-03) : lignes de dsd-fme.
SRC_RE = re.compile(r"SRC[=:]\s*(\d+)")
TGT_RE = re.compile(r"TGT[=:]\s*(\d+)")
ALIAS_RE = re.compile(r"Alias:\s*\[?([^\]]*?)\]?\s*$")
TEXT_RE = re.compile(r"(?:SMS|ISO7 Text|ISO8 Text|Complete Meta Text|Text):\s*(.+?)\s*$")
GPS_RE = re.compile(r"GPS:\s*\(?\s*(-?\d+\.\d+)[^\d-]+(-?\d+\.\d+)")
# Paquets de donnees IP du DMR (2026-10-14) : l'enregistrement automatique
# Motorola (ARS, port 4005) qu'un poste envoie au serveur de dispatch quand il
# s'allume ou revient a portee. dsd-fme en affiche la charge utile comme
# "UTF8 Text: _-- -402__" : ce n'est PAS un texte, c'est le numero du poste.
# Il va au registre DMR (lib/dmr_registre.ars_event), jamais en annonce.
DATA_SLOT_RE = re.compile(r"Slot ([12]) Data Header")
DATA_SRC_RE = re.compile(r"^SRC\(24\):\s*0*(\d+);")
DATA_DST_RE = re.compile(r"^DST\(24\):\s*0*(\d+);\s*IP:\s*([\d.]+)")
ARS_RE = re.compile(r"^UDP Protocol;.*\bARS;")
UNREACHABLE_RE = re.compile(r"^ICMP Protocol;.*Unreachable", re.I)


def new_stats(freq_hz=None):
    return {"clean": 0, "total": 0, "protocol": None, "freq_hz": freq_hz, "src": None, "tgt": None}


def _readable(value):
    value = " ".join(value.split())
    return value if sum(c.isalnum() for c in value) >= MIN_TRANSPORTED_TEXT_CHARS else ""


def dsd_line(stats, text):
    """Une ligne de sortie de dsd-fme : compte les synchros propres ou en
    erreur, retient l'emetteur, et fait dire aussitot les textes transportes
    (alias, SMS, textes, GPS) une fois le decodage valide."""
    try:  # code couleur, intervalles, groupes, emetteurs, appel prive, chiffrement, relais
        import dmr_registre
        dmr_registre.line(stats, text)
    except Exception as exc:
        print(f"[decodeurs] dmr : {exc!r}")
    compter_synchro(stats, text)
    for regex, field in ((SRC_RE, "src"), (TGT_RE, "tgt")):
        found = regex.search(text)
        if found:
            stats[field] = found.group(1)
    if _data_packet_line(stats, text):
        return
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


def _data_packet_line(stats, text):
    """Suit un paquet de donnees IP du DMR ligne par ligne. Un refus du reseau
    (ICMP "Unreachable") arrive AVANT le paquet qu'il renvoie ("Attached
    Message") : le serveur de dispatch n'a pas ete joint. Rend True quand la
    ligne est la charge utile d'un enregistrement ARS (rien a dire)."""
    p = stats.setdefault("paquet", {})
    m = DATA_SLOT_RE.search(text)
    if m:
        stats["paquet"] = {"slot": m.group(1)}
        return False
    m = DATA_SRC_RE.search(text)
    if m:
        p["src"] = m.group(1)
        return False
    m = DATA_DST_RE.search(text)
    if m:
        p["dst"], p["ip"] = m.group(1), m.group(2)
        return False
    if UNREACHABLE_RE.search(text):
        p["refus"] = True
        return False
    if ARS_RE.search(text):
        p["ars"] = True
        return False
    if p.get("ars") and text.startswith("UTF8 Text:"):
        try:
            import dmr_registre
            dmr_registre.ars_event(stats, p)
        except Exception as exc:
            print(f"[decodeurs] ars : {exc!r}")
        stats["paquet"] = {"slot": p.get("slot")}
        return True
    return False


def _say(kind, value, stats):
    try:
        import messages_numeriques
        messages_numeriques.found(kind, value, stats)
    except Exception as exc:  # une annonce ratee n'arrete pas le decodage
        print(f"[decodeurs] texte {kind} : {exc!r}")


def dsd_validated(stats):
    return stats["clean"] >= DSD_MIN_CLEAN_SYNC and stats["clean"] >= DSD_MIN_CLEAN_RATIO * stats["total"]


def extra_modes(freq_hz):
    """[(option, nom)] de tous les modes a essayer sur cette frequence, sans doublon."""
    out = []
    for low, high, flag, name in DSD_EXTRA_MODES:
        if low <= freq_hz <= high and flag not in [f for f, _ in out]:
            out.append((flag, name))
    return out


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
