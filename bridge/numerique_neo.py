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
IDLE_FIN_SEC = 10.0              # tolerance de silence AVANT de fermer, apres la derniere ACTIVITE
                                 # (trame voix numerique OU audio FM actif). Calee sur le local
                                 # DSD_FME_IDLE_SEC=10 : une conversation DMR/FM ne doit pas etre
                                 # coupee entre deux interlocuteurs (coupure seche signalee 2026-10-31).

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
            "-fi", "-fn", "-fm", "-fz", "-fp"}

# Decodeurs NUMERIQUES lances EN PARALLELE (modele arbitre local) sur UN meme
# flux via le proxy iq_fanout : chacun epingle a un mode, on garde celui qui
# CONFIRME le mieux. -fa seul FLOTTE (DMR vu comme dPMR...) ; les modes epingles
# a cote tranchent. -fa couvre deja P25/NXDN/D-STAR/YSF ; on ajoute DMR (-fs),
# dPMR (-fm), M17 (-fz) qu'il verrouille mal ou pas. AGNOSTIQUE : on DECOUVRE
# lequel accroche, on ne devine pas le mode.
MODES_PARALLELES = ["-fa", "-fs", "-fm", "-fz"]
MODE_ANALOGIQUE = "-fA"           # moniteur FM analogique (en parallele, pour le -6)


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
    # Nombre de lignes de synchro NUMERIQUE : signal AGNOSTIQUE de "porteuse
    # numerique presente", quel que soit le mode (DMR/P25/NXDN/dPMR/...). Mesure
    # (live 2026-10-31) : vraie FM analogique ~11 lignes sur 12 s (faux verrous
    # epars), une porteuse numerique 54-376. Le mode n'est pas devine : dsd-neo
    # DECOUVRE ce qui est module ; on ne fait que compter l'activite de synchro.
    sync_hits = sum(1 for l in texte.splitlines() if _SYNC.search(l))
    return {
        "protocole": proto,
        "meta": meta,
        "chiffre": chiffre,
        "clair": not chiffre,
        "voix_vue": bool(_VOICE.search(texte)),
        "sync": bool(_SYNC.search(texte)),
        "sync_hits": sync_hits,
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


def _audio_actif(path):
    """RMS de la toute fin du wav -6 (audio FM brut, 48k mono 16 bits) : le canal
    est-il encore actif ? L'audio analogique n'a pas de "trame voix" dans stdout,
    donc on surveille son niveau pour prolonger la fenetre comme une voix
    numerique. Lecture tolerante (on saute l'entete, on lit la queue), jamais
    bloquante."""
    try:
        sz = os.path.getsize(path)
        if sz < 44 + 4000:
            return False
        import audioop
        with open(path, "rb") as f:
            f.seek(max(44, sz - 48000))   # ~0,5 s a 48 kHz mono 16 bits
            data = f.read()
        if len(data) % 2:
            data = data[:-1]
        return bool(data) and audioop.rms(data, 2) >= AUDIO_RMS_MIN
    except Exception:
        return False


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


def _confirme(r, rms):
    """Un decodeur a-t-il CONFIRME un mode numerique (pas un faux verrou) ?
    Preuve positive : Color Code (DMR), NAC (P25), trame voix vue, ou voix de
    synthese reellement produite."""
    meta = r.get("meta") or {}
    return bool(r.get("sync") and (meta.get("color_code") is not None or meta.get("nac") is not None
                                   or r.get("voix_vue") or rms >= VOIX_RMS_MIN))


def decoder(host, port, freq_hz, gain, seconds, ppm=0, max_seconds=None, mode="-fa", plein=True,
            modes_extra=None):
    """Decode la fenetre freq_hz servie par le rtl_tcp du Pi, en lançant
    PLUSIEURS dsd-neo EN PARALLELE sur le meme flux (via le proxy iq_fanout, car
    rtl_tcp ne sert qu'un client). Modele arbitre local : chaque mode epingle
    (MODES_PARALLELES, + `mode` impose, + `modes_extra` de la bande) CONFIRME ou
    non ; on garde le gagnant -- AGNOSTIQUE, on DECOUVRE la modulation, on ne la
    devine pas. `plein` (defaut) ajoute un moniteur analogique -fA (-6) en
    parallele pour la FM. Prolonge jusqu'a max_seconds tant qu'une activite
    (voix numerique OU audio FM) arrive. Rend le resultat du gagnant + l'audio
    FM, ou {"ok": False, "erreur": ...}. Ne leve pas."""
    if not disponible():
        return {"ok": False, "erreur": "dsd-neo absent"}
    seconds = max(1.0, float(seconds))
    max_seconds = max(seconds, float(max_seconds or seconds))
    # Jeu de modes numeriques a essayer en parallele (dedup, liste blanche).
    modes = []
    for m in ([mode] if mode else []) + MODES_PARALLELES + list(modes_extra or []):
        if m in MODES_OK and m not in modes:
            modes.append(m)
    if not modes:
        modes = ["-fa"]
    tmp = tempfile.mkdtemp(prefix="numerique_")
    audio = os.path.join(tmp, "audio.wav")
    # Proxy fan-out : UNE connexion a l'amont, rediffusee aux N dsd-neo locaux.
    try:
        import iq_fanout
        fo = iq_fanout.FanOut(host, int(port))
        pport = fo.start()
    except Exception as exc:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "erreur": "proxy IQ: %r" % exc}
    src = "rtltcp:127.0.0.1:%d:%d:%s:%d:%d" % (pport, int(freq_hz), str(gain), int(ppm), BW_KHZ)
    etat = {"activite": 0.0}
    decs = []

    def lanceur(m, wavpath, analog):
        flag = ["-6", wavpath] if analog else ["-w", wavpath]
        cmd = [DSD_NEO, m, "-i", src] + flag
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=True)
        d = {"mode": m, "proc": p, "sortie": [], "wav": wavpath, "analog": analog}

        def lire():
            try:
                for ligne in p.stdout:
                    d["sortie"].append(ligne)
                    if not analog and _VOICE.search(ligne):
                        etat["activite"] = time.time()
            except Exception:
                pass

        threading.Thread(target=lire, daemon=True).start()
        decs.append(d)
        return d

    # Le PREMIER lance devient "commandant" du proxy (il regle freq/taux/gain de
    # l'amont) : on le lance seul d'abord, puis les autres + l'analogique.
    try:
        lanceur(modes[0], os.path.join(tmp, "v0.wav"), False)
        time.sleep(1.0)
        for i, m in enumerate(modes[1:], 1):
            lanceur(m, os.path.join(tmp, "v%d.wav" % i), False)
        if plein:
            lanceur(MODE_ANALOGIQUE, audio, True)
    except Exception as exc:
        for d in decs:
            try:
                os.killpg(d["proc"].pid, signal.SIGKILL)
            except Exception:
                pass
        fo.stop()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "erreur": "lancement dsd-neo : %r" % exc}

    debut = time.time()
    dernier_check_audio = 0.0
    while any(d["proc"].poll() is None for d in decs):
        now = time.time()
        ecoule = now - debut
        if ecoule >= max_seconds:
            break
        # Activite analogique : le -6 actif prolonge la fenetre comme une voix
        # numerique (meme tolerance IDLE_FIN_SEC) -- sinon une conversation FM
        # serait coupee a `seconds`. Verifie ~1x/s (I/O).
        if plein and now - dernier_check_audio >= 1.0:
            dernier_check_audio = now
            if _audio_actif(audio):
                etat["activite"] = now
        en_act = etat["activite"] and now - etat["activite"] < IDLE_FIN_SEC
        if ecoule >= seconds and not en_act:
            break
        time.sleep(0.25)
    for d in decs:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            if d["proc"].poll() is not None:
                break
            try:
                os.killpg(d["proc"].pid, sig)
            except ProcessLookupError:
                break
            try:
                d["proc"].wait(timeout=2)
            except subprocess.TimeoutExpired:
                continue
    time.sleep(0.3)   # laisser les fils stdout vider
    fo.stop()
    duree = round(time.time() - debut, 1)

    # Arbitre : parse chaque decodeur numerique, garde le gagnant (confirme, le
    # plus de synchros ; a egalite, celui qui a de la voix).
    cand = []
    for d in decs:
        if d["analog"]:
            continue
        texte = "".join(d["sortie"])
        r = parser(texte)
        rms = _rms_wav(d["wav"])
        cand.append({"mode": d["mode"], "r": r, "rms": rms, "wav": d["wav"],
                     "conf": _confirme(r, rms), "hits": r.get("sync_hits", 0), "texte": texte})
    confirmes = sorted([c for c in cand if c["conf"]],
                       key=lambda c: (c["rms"] >= VOIX_RMS_MIN, c["hits"]), reverse=True)
    gagnant = confirmes[0] if confirmes else (max(cand, key=lambda c: c["hits"]) if cand else None)
    _journaliser_brut(freq_hz, (gagnant or {}).get("texte", ""))

    res = dict((gagnant or {}).get("r") or parser(""))
    rms_g = (gagnant or {}).get("rms", 0)
    res["numerique_confirme"] = bool(gagnant and gagnant["conf"])
    res["voix_rms"] = rms_g
    if gagnant:
        res["mode_gagnant"] = gagnant["mode"]
    # dsd-neo ne synthetise que du clair : wav non silencieux = voix en clair.
    res["voix_wav"] = (gagnant["wav"] if (gagnant and res["numerique_confirme"]
                       and rms_g >= VOIX_RMS_MIN and not res.get("chiffre")) else None)
    # Porteuse NUMERIQUE active (agnostique, au SIGNAL) : un gagnant confirme, OU
    # un decodeur avec synchro soutenue (>= 15 lignes ET >= 1,3/s). Sert a NE
    # JAMAIS jouer la FM d'une porteuse numerique. Seuils live : FM ~0,9/s, num >= 4,5/s.
    max_hits = max((c["hits"] for c in cand), default=0)
    res["sync_hits"] = max_hits
    res["digital_actif"] = bool(res["numerique_confirme"] or any(
        c["hits"] >= 15 and c["hits"] / max(duree, 1.0) >= 1.3 for c in cand))
    # Audio ANALOGIQUE (-fA / -6) : seulement si AUCUNE porteuse numerique active
    # (sinon ce serait le bruit du numerique). Gate grossier : le Pi raffine (RNNoise).
    res["audio_wav"] = None
    res["audio_rms"] = 0
    if plein and not res["digital_actif"] and not res.get("chiffre"):
        rms_a = _rms_wav(audio)
        res["audio_rms"] = rms_a
        if rms_a >= AUDIO_RMS_MIN:
            res["audio_wav"] = audio
    res["duree"] = duree
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
