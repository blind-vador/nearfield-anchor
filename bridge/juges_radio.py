"""Juges du scanner radio, communs au Pi et aux machines compagnes (Python 3.9
et plus, bibliotheque standard seule).

Un "juge" est un mode de dsd-fme ; on lui fait passer un son brut du dongle
(dsd-fme -6, 48 kHz) et on lit ce qu'il en tire. Le Pi le fait lui-meme
(watchers/juges_watch.py) ou le confie a une machine compagne plus libre
(travail "radio_juges" du pont, 2026-10-25) : meme code, meme version de
dsd-fme (bridge/construire_dsd.sh), donc memes verdicts des deux cotes
(verifie a l'octet pres le 2026-10-25).
"""
import os
import re
import subprocess
import tempfile
import time
import wave

PROTOCOLES = {"DMR": "DMR", "P25p1": "P25", "P25p2": "P25", "YSF": "YSF", "DSTAR": "D-Star",
              "M17": "M17", "NXDN48": "NXDN", "NXDN96": "NXDN", "NXDN": "NXDN", "dPMR": "dPMR",
              "X2-TDMA": "X2-TDMA"}
SYNC_RE = re.compile(r"Sync: [+-]([\w-]+)")
ENC_RE = re.compile(r"\b(ENC|Encrypt\w*|ALG ID|Key ID|KID)\b", re.I)
# Trame de VOIX DMR en clair : superframe VC1..VC6 ou en-tete voix VLC, apres
# le dernier "|" de la ligne Sync. On EXCLUT la voix chiffree ("VC1*", le "*")
# et tout le reste (IDLE au repos, TLC terminateur, CSBK/DATA/PI signalisation)
# : eux ne doivent pas declencher d'ouverture (2026-10-27, stats["voix"]).
DMR_VOIX_RE = re.compile(r"\|\s*V(?:C[1-6]|LC)\b(?!\*)")
SEUIL_MIN = 2                       # en dessous, pas meme une proposition (bruit pur)
RATIO_MIN = 0.25
VOIX_MIN_SEC = 1.0
TIMEOUT_SEC = 600


def _propre(stats, protocole):
    stats["clean"] += 1
    stats["protocol"] = protocole
    vus = stats.setdefault("vus", {})
    vus[protocole] = vus.get(protocole, 0) + 1


def compter_synchro(stats, text):
    """Compte une synchro de dsd-fme, propre ou en erreur (direct, juges du Pi,
    juges des machines compagnes).

    Sanity check du 2026-10-25 : en dPMR, la ligne "Sync:" ne porte jamais
    ERR, le verdict est sur la ligne qui suit ("TG=(CRC ERR) Src=...") ; en
    M17, une ligne "Sync: +M17 STR" seule ne prouve rien. Sur du bruit, les
    deux comptaient 100 % de synchros propres : 41 faux "dPMR confirme" en
    3 jours. Desormais une synchro dPMR n'est propre que si la ligne suivante
    a un groupe ou un emetteur lu sans erreur (ni chiffre incertain "*"), et
    une synchro M17 que si son en-tete (LSF) est lu sans erreur."""
    m = SYNC_RE.search(text)
    if m:
        protocole = PROTOCOLES.get(m.group(1), m.group(1))
        stats["total"] += 1
        stats["preuve"] = None
        if protocole == "dPMR":
            stats["preuve"] = protocole   # verdict a la ligne suivante
        elif protocole == "M17":
            if "LSF" in text and "ERR" not in text:
                _propre(stats, protocole)
        elif "ERR" not in text:
            _propre(stats, protocole)
        if protocole == "DMR" and DMR_VOIX_RE.search(text):
            stats["voix"] = stats.get("voix", 0) + 1   # vraie voix en clair, pas IDLE/TLC
        return
    attente = stats.get("preuve")
    if attente and text.strip():
        stats["preuve"] = None
        if ("TG=" in text or "Src=" in text) and "ERR" not in text and "*" not in text:
            _propre(stats, attente)


def duree_wav(chemin):
    """Secondes de voix decodee (dsd-fme -w). Un fichier dont l'en-tete n'a pas
    ete fini (dsd-fme arrete) est estime d'apres sa taille."""
    try:
        with wave.open(chemin) as w:
            frames, rate, size = w.getnframes(), w.getframerate(), w.getnchannels() * w.getsampwidth()
        if frames:
            return frames / float(rate)
        return max(0, os.path.getsize(chemin) - 44) / float(rate * size)
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        return 0.0


def juger(juge, wav, dsd="dsd-fme", voix=None, priorite=("nice", "-n", "19"), timeout=TIMEOUT_SEC):
    """Fait passer un son devant un juge. Rend (verdict, chemin de la voix
    decodee) ; la voix n'est gardee que si le juge propose quelque chose."""
    voix = voix or tempfile.mktemp(prefix="juge_", suffix=".wav")
    try:
        r = subprocess.run(list(priorite) + [dsd, juge, "-i", wav, "-s", "48000", "-o", "null", "-w", voix],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=timeout)
        texte = r.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        texte = ""
    stats = {"clean": 0, "total": 0, "protocol": None}
    for ligne in texte.splitlines():
        compter_synchro(stats, ligne)
    total, propres, vus = stats["total"], stats["clean"], stats.get("vus", {})
    protocole = max(vus, key=vus.get) if vus else None
    voix_sec = duree_wav(voix) if os.path.exists(voix) and os.path.getsize(voix) > 44 else 0.0
    verdict = {"total": total, "propres": propres, "protocole": protocole, "voix_s": round(voix_sec, 1),
               "chiffre": bool(ENC_RE.search(texte)), "t": time.time()}
    # Une "proposition" (a confirmer) : assez de synchros propres et de la voix.
    verdict["propose"] = (not verdict["chiffre"] and propres >= SEUIL_MIN
                          and propres >= RATIO_MIN * max(total, 1) and voix_sec >= VOIX_MIN_SEC)
    if not verdict["propose"]:
        if os.path.exists(voix):
            os.remove(voix)
        voix = None
    return verdict, voix
