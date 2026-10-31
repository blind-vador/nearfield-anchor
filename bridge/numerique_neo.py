"""Decodeur numerique (DMR, P25, NXDN, dPMR... -- tout sauf TETRA) pour la
machine compagne, via dsd-neo (-fa, auto-detection). Palier offload-2, 2026-10-31.

Meme principe que bridge/tetra_neo.py : le Pi sert son dongle par rtl_tcp
pendant une fenetre ; quand il chauffe (lib/offload.py), il DELEGUE le decodage
ici au lieu de faire tourner dsd-fme localement. Cette machine s'y connecte,
decode en -fa et renvoie au Pi :
  - le protocole reconnu et ses metadonnees EN CLAIR (DMR : color code,
    talkgroup, source ; P25 : NAC, talkgroup) -- lues telles que dsd-neo les
    affiche, JAMAIS dechiffrees ;
  - s'il y a du chiffrement (lu "enc"/"Encrypted", jamais casse) ;
  - une eventuelle voix EN CLAIR deja synthetisee par dsd-neo (-w), renvoyee
    telle quelle. dsd-neo ne synthetise QUE du clair (il laisse tomber les
    trames chiffrees) -- un wav non silencieux EST donc de la voix en clair.

Le Pi ne decode jamais TETRA lui-meme (pi/4-DQPSK) : ca c'est tetra_neo. Ici
au contraire le Pi SAIT decoder en local (dsd-fme) ; on ne l'epaule que pour le
soulager thermiquement. Doctrine : ecoute passive, on garde tout le clair, le
"prive" (adresse a un poste) est garde comme le groupe, seul le chiffre est
ecarte.
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
BW_KHZ = 48                      # baseband DSP 48 kHz : 10 echantillons/symbole a 4800 sym/s
                                 # (DMR, P25p1, NXDN...). A 24 kHz dsd-neo avertit "atypical
                                 # SPS, decoders assume ~48k" et la synchro part en erreur FEC.
VOIX_RMS_MIN = 120               # au-dela : la voix de synthese n'est pas du silence
AUDIO_RMS_MIN = 120              # idem pour l'audio FM brut (-6). Garde-fou grossier seulement :
                                 # le Pi raffine (RNNoise/CTCSS/chaine_audio). dsd-neo squelche
                                 # deja l'audio en -8 (squelch auto), donc un canal ferme = silencieux.
IDLE_FIN_SEC = 2.0               # apres la derniere trame voix : laisser finir la phrase

# Sortie de dsd-neo (-fa / -fs) calee sur la vraie sortie DMR du reseau local
# (test live 2026-10-31 via le rtl_tcp du Pi, relais 467,2408 MHz) :
#   Sync: +DMR   slot1  [slot2] | Color Code=01 | IDLE
#   | Color Code=01 | DATA
#   Slot 1 Data Header - Indiv - Unconfirmed Delivery - Source: 16448250 Target: 120
#   | Color Code=01 | CSBK / R12U
# (NB : au mauvais centre, +/-3 kHz, tout partait en "CACH/Burst FEC ERR" -- le
# Pi doit deleguer la frequence RECENTREE, pas le pic nominal.) Regex tolerantes.
_SYNC = re.compile(r"\bSync:\s*([+\-]?\w[\w /.\-]*)")
_DMR = re.compile(r"\bDMR\b", re.I)
_P25 = re.compile(r"\bP25\b|\bPhase\s*[12]\b", re.I)
_NXDN = re.compile(r"\bNXDN\b", re.I)
_DPMR = re.compile(r"\bdPMR\b", re.I)
_CC = re.compile(r"\bColor ?Code=(\d+)")
_NAC = re.compile(r"\bNAC[=:\s]+0?x?([0-9A-Fa-f]+)")
_TG = re.compile(r"\b(?:Target|TGT?|Talkgroup|Group)[=:\s]+(\d+)")
_SRC = re.compile(r"\b(?:Source|Src|RID|src_id)[=:\s]+(\d+)")
# Chiffrement DMR/P25 (lu, jamais casse) : en-tete de confidentialite, algo, cle.
_ENC = re.compile(r"\bEncrypt(?:ed|ion)\b|\bPrivacy\b|\bPI Header\b|\benc=(?!none|0\b)\w+"
                  r"|\bRC4\b|\bAES\b|\bDES\b|\bKEY ?ID\b|\bALG\b", re.I)
# Voix reellement decodee : superframe voix DMR (VC1/VC2), trame voix P25, etc.
_VOICE = re.compile(r"\bVC[12]?\b|\bVOICE\b|\bVoice\b|synthesiz", re.I)

# Modes dsd-neo que le Pi peut imposer (il sait ce qu'il delegue) : evite le
# flottement de -fa vers NXDN/dPMR sur un relais DMR connu. Liste blanche.
MODES_OK = {"-fa", "-fs", "-ft", "-fr", "-f1", "-f2", "-fd", "-fx", "-fy",
            "-fi", "-fn", "-fm", "-fp"}


def disponible():
    return os.access(DSD_NEO, os.X_OK)


def _protocole(texte):
    for rx, nom in ((_P25, "P25"), (_DMR, "DMR"), (_NXDN, "NXDN"), (_DPMR, "dPMR")):
        if rx.search(texte):
            return nom
    s = _SYNC.search(texte)
    return (s.group(1).strip() if s else None)


def parser(texte):
    """Lit la sortie de dsd-neo -fa et en tire le protocole + les metadonnees en
    clair. Pur (testable sans dongle). Jamais de contenu dechiffre."""
    proto = _protocole(texte)
    meta = {}
    cc = _CC.search(texte)
    if cc:
        meta["color_code"] = int(cc.group(1))
    nac = _NAC.search(texte)
    if nac:
        meta["nac"] = int(nac.group(1), 16)
    tgs = sorted({int(t) for t in _TG.findall(texte)})
    if tgs:
        meta["talkgroups"] = tgs[:30]
    srcs = sorted({int(s) for s in _SRC.findall(texte)})
    if srcs:
        meta["sources"] = srcs[:30]
    chiffre = bool(_ENC.search(texte))
    return {
        "protocole": proto,
        "meta": meta,
        "chiffre": chiffre,
        "clair": not chiffre,
        "voix_vue": bool(_VOICE.search(texte)),
        "sync": bool(_SYNC.search(texte)),
    }


def _rms_wav(path):
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


def _journaliser_brut(freq_hz, texte):
    """Garde la sortie brute dsd-neo (diagnostic / calage du parseur). Dossier
    plafonne a 50 fichiers. Jamais bloquant."""
    try:
        import glob
        d = os.path.join(HOME, "numerique_brut")
        os.makedirs(d, exist_ok=True)
        nom = "%d_%s.log" % (int(freq_hz), time.strftime("%Y%m%d_%H%M%S"))
        with open(os.path.join(d, nom), "w", encoding="utf-8", errors="replace") as fh:
            fh.write(texte)
        for vieux in sorted(glob.glob(os.path.join(d, "*.log")))[:-50]:
            os.remove(vieux)
    except Exception:
        pass


def decoder(host, port, freq_hz, gain, seconds, ppm=0, max_seconds=None, mode="-fa", plein=True):
    """Connecte dsd-neo au rtl_tcp du Pi (host:port), decode `seconds` s sur
    freq_hz -- prolonge jusqu'a max_seconds tant que de la voix arrive -- et
    rend les metadonnees + un chemin de voix en clair (ou None). `mode` = le
    flag dsd-neo (-fa auto par defaut ; le Pi passe p.ex. -fs pour un relais
    DMR connu, ce qui evite le flottement vers NXDN/dPMR).

    `plein` (defaut) = DEMODULATION COMPLETE de la fenetre sur UN seul process
    (doctrine 2026-10-31 : le Mac fait TOUTE la demod quand il est sollicite,
    numerique ET FM analogique, sans fragmenter en fenetres dediees). On ajoute
    `-8` (Source Audio Monitor, squelch auto, enregistre aussi la NFM) et
    `-6 audio.wav` (audio brut 48k) : le `-w` sort la voix NUMERIQUE en clair,
    le `-6` sort l'audio ANALOGIQUE du canal, EN MEME TEMPS. On distingue au
    retour : synchro numerique vue -> on rend `voix_wav` (et on tait l'audio
    brut, qui ne serait que du numerique demodule en bruit) ; sinon -> on rend
    `audio_wav` (FM), que le Pi raffinera (RNNoise/CTCSS/chaine_audio).
    Ne leve pas : {"ok": False, "erreur": ...} en cas d'echec."""
    if not disponible():
        return {"ok": False, "erreur": "dsd-neo absent"}
    if mode not in MODES_OK:
        mode = "-fa"
    seconds = max(1.0, float(seconds))
    max_seconds = max(seconds, float(max_seconds or seconds))
    tmp = tempfile.mkdtemp(prefix="numerique_")
    voix = os.path.join(tmp, "voix.wav")
    audio = os.path.join(tmp, "audio.wav")
    source = "rtltcp:%s:%d:%d:%s:%d:%d" % (host, int(port), int(freq_hz), str(gain), int(ppm), BW_KHZ)
    cmd = [DSD_NEO, mode, "-i", source, "-w", voix]
    if plein:
        cmd += ["-8", "-6", audio]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
    except Exception as exc:
        return {"ok": False, "erreur": "lancement dsd-neo : %r" % exc}
    sortie = []
    etat = {"derniere_voix": 0.0}

    def lire():
        try:
            for ligne in proc.stdout:
                sortie.append(ligne)
                if _VOICE.search(ligne):
                    etat["derniere_voix"] = time.time()
        except Exception:
            pass

    fil = threading.Thread(target=lire, daemon=True)
    fil.start()
    debut = time.time()
    while proc.poll() is None:
        ecoule = time.time() - debut
        if ecoule >= max_seconds:
            break
        en_voix = etat["derniere_voix"] and time.time() - etat["derniere_voix"] < IDLE_FIN_SEC
        if ecoule >= seconds and not en_voix:
            break
        time.sleep(0.25)
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
    _journaliser_brut(freq_hz, texte)
    res = parser(texte)
    rms = _rms_wav(voix)
    # Synchro numerique CONFIRMEE (pas un faux verrou -fa sur du bruit analogique :
    # sur de la FM, -fa signale par intermittence un "sync" NXDN/P25 fantome, sans
    # Color Code / NAC ni voix). On exige une vraie preuve : CC, NAC, trame voix
    # vue, ou voix de synthese reellement produite. Sinon la fenetre est ANALOGIQUE.
    meta = res.get("meta") or {}
    res["numerique_confirme"] = bool(
        res["sync"] and (meta.get("color_code") is not None or meta.get("nac") is not None
                         or res.get("voix_vue") or rms >= VOIX_RMS_MIN))
    # dsd-neo ne synthetise que du clair : un wav non silencieux = voix en clair.
    # Garde-fou : on refuse seulement sur preuve POSITIVE de chiffrement.
    res["voix_wav"] = voix if (res["numerique_confirme"] and rms >= VOIX_RMS_MIN
                               and not res["chiffre"]) else None
    res["voix_rms"] = rms
    # Audio ANALOGIQUE (-6) : seulement si la fenetre N'EST PAS du numerique
    # confirme (sinon le brut n'est que du numerique demodule en bruit). Gate
    # grossier : le Pi raffine vraiment (RNNoise/CTCSS). Jamais sur du chiffre.
    res["audio_wav"] = None
    res["audio_rms"] = 0
    if plein and not res["numerique_confirme"] and not res["chiffre"]:
        rms_a = _rms_wav(audio)
        res["audio_rms"] = rms_a
        if rms_a >= AUDIO_RMS_MIN:
            res["audio_wav"] = audio
    res["duree"] = round(time.time() - debut, 1)
    res["ok"] = True
    res["_tmp"] = tmp
    return res


def resume_fr(res):
    """Phrase courte (journal/TTS) a partir d'un resultat decoder()."""
    p = res.get("protocole") or "numerique"
    if not res.get("numerique_confirme"):
        if res.get("audio_wav"):
            return "voix analogique (FM) en clair"
        return "%s : porteuse presente, pas de synchro" % p
    bout = p
    meta = res.get("meta") or {}
    if meta.get("color_code") is not None:
        bout += ", CC %d" % meta["color_code"]
    if meta.get("nac") is not None:
        bout += ", NAC %X" % meta["nac"]
    if meta.get("talkgroups"):
        tg = meta["talkgroups"]
        bout += ", groupe%s %s" % ("s" if len(tg) > 1 else "", ", ".join(str(t) for t in tg[:3]))
    if res.get("chiffre"):
        bout += ", trafic chiffre"
    elif res.get("voix_wav"):
        bout += ", voix en clair"
    return bout
