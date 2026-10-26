"""Decodeur TETRA pour la machine compagne (dsd-neo), 2026-10-26.

Le Pi sert son dongle par rtl_tcp pendant une fenetre du scanner ; cette
machine s'y connecte, decode le TETRA (dsd-neo -fT, pipeline I/Q a 72 kHz) et
renvoie au Pi :
  - l'IDENTITE RESEAU, metadonnees DIFFUSEES EN CLAIR sur le canal de controle
    (MCC/MNC du reseau, Colour Code, zone LA, numero de porteuse) ;
  - la presence d'un trafic et s'il est chiffre ou non -- lu tel que dsd-neo
    l'annonce ("enc=none" / "enc=..."), JAMAIS dechiffre ;
  - une eventuelle voix EN CLAIR, deja synthetisee par dsd-neo (-w), renvoyee
    telle quelle.

Aucune tentative de dechiffrement : dsd-neo n'en fait pas, et on ne lui demande
rien de plus que ce qui est audible (ecoute passive de bonne foi -- voir la
memoire nearfield-tetra et ROADMAP). Le Pi ne decode jamais le TETRA lui-meme
(chaleur) : tout passe par la machine compagne la plus libre.

Format de sortie de dsd-neo observe (429,600 MHz, reseau francais, 2026-10-26) :
  Sync: +TETRA SB [TETRA BSCH] MCC=208 MNC=29 CC=0x15 TN=4 FN=9 MN=40
  [TETRA SYSINFO] CC=21  carrier=1184  band=4  ...  LA=11  ...
  [TETRA MAC-RESOURCE] CC=21  enc=none  ...  addr=SSI(1010)  ...
"""
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import wave

HOME = os.path.expanduser("~/.nearfield-bridge")
DSD_NEO = os.path.join(HOME, "radio", "bin", "dsd-neo")
BW_KHZ = 72                      # largeur DSP imposee par le TETRA (dsd-neo -fT)
VOIX_RMS_MIN = 120               # au-dela : la voix de synthese n'est pas du silence

_BSCH = re.compile(r"\[TETRA BSCH\][^\n]*?MCC=(\d+)\s+MNC=(\d+)\s+CC=0x([0-9A-Fa-f]+)")
_SYSINFO = re.compile(r"\[TETRA SYSINFO\][^\n]*?carrier=(\d+)")
_LA = re.compile(r"\bLA=(\d+)")
_ENC = re.compile(r"\benc=(\w+)")
_CLIP = re.compile(r"\bCLIP\b|clipping|overload|RF Level.*(?:high|sat)", re.I)


def disponible():
    return os.access(DSD_NEO, os.X_OK)


def parser(texte):
    """Lit la sortie de dsd-neo -fT et en tire l'identite reseau et l'activite.
    Pur (testable sans dongle). Rend un dict de metadonnees -- jamais de
    contenu dechiffre."""
    reseau = {}
    bsch = _BSCH.findall(texte)
    if bsch:
        mcc, mnc, cc = bsch[-1]
        reseau = {"mcc": int(mcc), "mnc": int(mnc), "cc": int(cc, 16)}
    sysinfo = _SYSINFO.findall(texte)
    if sysinfo:
        reseau["carrier"] = int(sysinfo[-1])
    la = _LA.findall(texte)
    if la:
        reseau["la"] = int(la[-1])
    enc = sorted(set(_ENC.findall(texte)))          # p.ex. {"none"} ou {"none","aie"}
    return {
        "reseau": reseau,
        "bsch": len(bsch),                           # nombre de synchros de controle
        "sysinfo": len(sysinfo),
        "enc": enc,                                  # modes de chiffrement vus (lus, pas casses)
        "clair": "none" in enc,                      # du trafic de signalisation en clair
        "mac_resource": texte.count("[TETRA MAC-RESOURCE]"),
        "clip": bool(_CLIP.search(texte)),           # dongle sature : baisser le gain
    }


def _rms_wav(path):
    """RMS moyen du wav (0 si absent/illisible/silence)."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 2048:
            return 0
        import audioop
        with wave.open(path) as w:
            if w.getsampwidth() != 2:
                return 0
            data = w.readframes(w.getnframes())
        return audioop.rms(data, 2) if data else 0
    except Exception:
        return 0


def decoder(host, port, freq_hz, gain, seconds, ppm=0):
    """Connecte dsd-neo au rtl_tcp du Pi (host:port), decode `seconds` s sur
    freq_hz au gain donne, et rend les metadonnees + un chemin de voix en clair
    (ou None). Ne leve pas : en cas d'echec, {"ok": False, "erreur": ...}."""
    if not disponible():
        return {"ok": False, "erreur": "dsd-neo absent"}
    tmp = tempfile.mkdtemp(prefix="tetra_")
    voix = os.path.join(tmp, "voix.wav")
    source = "rtltcp:%s:%d:%d:%s:%d:%d" % (host, int(port), int(freq_hz), str(gain), int(ppm), BW_KHZ)
    cmd = [DSD_NEO, "-fT", "-i", source, "-w", voix]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
    except Exception as exc:
        return {"ok": False, "erreur": "lancement dsd-neo : %r" % exc}
    sortie = []

    def lire():
        try:
            for ligne in proc.stdout:
                sortie.append(ligne)
        except Exception:
            pass

    fil = threading.Thread(target=lire, daemon=True)
    fil.start()
    time.sleep(max(1.0, float(seconds)))
    # Arret propre du groupe (dsd-neo + librtlsdr), puis coup de grace.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            break
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            continue
    fil.join(timeout=2)
    texte = "".join(sortie)
    res = parser(texte)
    rms = _rms_wav(voix)
    res["voix_wav"] = voix if rms >= VOIX_RMS_MIN else None
    res["voix_rms"] = rms
    res["ok"] = True
    res["_tmp"] = tmp
    return res


def resume_fr(res):
    """Phrase courte (journal/TTS) a partir d'un resultat decoder()."""
    r = res.get("reseau") or {}
    if not r.get("mcc"):
        return "TETRA : porteuse presente, pas de synchro reseau"
    bout = "TETRA reseau %s-%s" % (r["mcc"], r["mnc"])
    if r.get("la") is not None:
        bout += ", zone %s" % r["la"]
    if res.get("clair"):
        bout += ", trafic en clair"
    elif res.get("enc"):
        bout += ", trafic chiffre"
    return bout
