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

Suivi des appels (2026-10-27) : la porteuse de controle ne porte que de la
signalisation ; la voix part sur une PORTEUSE DE TRAFIC que le controle
attribue a chaque appel. Sans -T, dsd-neo restait sur le controle et n'entendait
jamais la voix. dsd-neo (-fT, sa propre machine a etats TETRA -- PAS -T, qui est le trunking
NXDN/P25/EDACS/DMR et CASSAIT le decodage voix TETRA, retire 2026-10-31) suit
l'attribution, decode la voix du timeslot de trafic, et refuse de lui-meme un
appel chiffre ("locked out"). L'activite voix se lit sur D-TX-GRANTED / D-TX-CEASED. Les appels de POSTE A POSTE en clair sont suivis et gardes
comme les appels de groupe (regle utilisateur, la meme que le DMR : "prive"
veut dire adresse a un poste, pas cache) ; seul le chiffre est ecarte. Tant
qu'un appel est suivi, la session se prolonge (jusqu'a max_seconds) pour ne
pas couper la conversation.

Activite (2026-10-27, releve de 3 min a 429,6125 MHz : reseau sans chiffrement
d'interface, 45 inscriptions, ~25 postes, 74 SDS, 100 etats) : on rend aussi
ce que la signalisation dit en clair -- postes actifs (SSI), inscriptions,
messages d'etat et SDS (expediteur, taille ; pas le contenu, que dsd-neo
n'affiche pas), appels (clair/chiffre/prive), porteuses de trafic attribuees.

Aucune tentative de dechiffrement : jamais --tetra-key (ecoute passive de
bonne foi -- voir la memoire nearfield-tetra et ROADMAP). Le Pi le decode
lui-meme seulement sans machine compagne (2026-11-08) : il chauffe.

Format de sortie de dsd-neo observe (reseau francais, 2026-10-26/27) :
  Sync: +TETRA SB [TETRA BSCH] MCC=208 MNC=29 CC=0x15 TN=4 FN=9 MN=40
  [TETRA SYSINFO] CC=21  carrier=1184  band=4  foff=3  ...  LA=11  ...
  [TETRA MAC-RESOURCE] CC=21  enc=none  ...  addr=SSI(1010)  ...
  [TETRA CMCE D-STATUS] CC=21 src_SSI=30000 status=0xA07F
  [TETRA CMCE D-SDS-DATA] CC=21 src_SSI=30000 CPTI=1 SDTI=3 data_bits=232
  [TETRA MM D-LU-ACCEPT] CC=21 type=0
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
# Le Mac compagnon (construire_dsd_neo.sh), puis le Pi lui-meme (construire_dsd_neo_pi.sh,
# 2026-11-08 : le Pi decode le TETRA seul quand aucune machine compagne n'est la).
_DSD_NEO_POSSIBLES = (os.path.join(HOME, "radio", "bin", "dsd-neo"),
                      os.path.expanduser("~/autopilot/dsd-neo/bin/dsd-neo"))
DSD_NEO = next((c for c in _DSD_NEO_POSSIBLES if os.access(c, os.X_OK)), _DSD_NEO_POSSIBLES[0])
BW_KHZ = 72                      # largeur DSP imposee par le TETRA (dsd-neo -fT)
VOIX_RMS_MIN = 120               # au-dela : la voix de synthese n'est pas du silence
FIN_APPEL_SEC = 3.0              # apres le retour au controle : la fin de phrase arrive
SSI_DIFFUSION = 16777215         # adresse "tous" : pas un poste
POSTES_MAX = 400

_BSCH = re.compile(r"\[TETRA BSCH\][^\n]*?MCC=(\d+)\s+MNC=(\d+)\s+CC=0x([0-9A-Fa-f]+)")
_SYSINFO = re.compile(r"\[TETRA SYSINFO\][^\n]*?carrier=(\d+)\s+band=(\d+)\s+foff=(\d+)")
_LA = re.compile(r"\bLA=(\d+)")
_ENC = re.compile(r"\benc=(\w+)")
_CLIP = re.compile(r"\bCLIP\b|clipping|overload|RF Level.*(?:high|sat)", re.I)
_SSI = re.compile(r"addr=SSI\((\d+)\)")
_STATUS = re.compile(r"\[TETRA CMCE D-STATUS\][^\n]*?src_SSI=(\d+)\s+status=0x([0-9A-Fa-f]+)")
_SDS = re.compile(r"\[TETRA CMCE D-SDS-DATA\][^\n]*?src_SSI=(\d+)[^\n]*?data_bits=(\d+)")
_SETUP = re.compile(r"\[TETRA CMCE D-SETUP\][^\n]*?call=(\d+)[^\n]*?enc=(\d+)\s+com=(\d+)\s+service=(\d+)"
                    r"[^\n]*?priority=(\d+)")
_CONNECT = re.compile(r"\[TETRA CMCE D-CONNECT\][^\n]*?call=(\d+)")
_ALLOC = re.compile(r"\[TETRA CHANNEL-ALLOCATION\][^\n]*?carrier=(\d+)\s+freq=(\d+)")
_SUIVI = re.compile(r"\[TETRA SM\] on traffic channel (\d+) Hz \(carrier (\d+), timeslots 0x[0-9A-Fa-f]+, call (\d+)\)")
# Suivi sur le MEME porteur / autre time-slot (frequent quand le trafic partage
# le porteur du controle, p.ex. 429,6125) : dsd-neo l'annonce sans frequence. A
# compter comme un suivi au meme titre que "on traffic channel".
_SUIVI_MEME = re.compile(r"\[TETRA SM\] follow call (\d+) on this carrier \(timeslots 0x[0-9A-Fa-f]+\)")
_RETOUR = re.compile(r"\[TETRA SM\] back on the control channel")
_CHIFFRE_REFUSE = re.compile(r"\[TETRA CALL\] call=(\d+) .*encrypted; locked out")
_TX_GRANTED = re.compile(r"\[TETRA CMCE D-TX-GRANTED\][^\n]*?call=(\d+)")  # floor accorde = voix
_VOISINES = re.compile(r"D-NWRK-BROADCAST\][^\n]*?neighbours=(\d+)")
# Contenu des messages courts : ligne ajoutee par notre correctif de dsd-neo
# (bridge/dsd-neo-nearfield-sds.patch, 2026-10-27). Sans lui, seule la taille est connue.
_SDS_CONTENU = re.compile(r"\[TETRA SDS PAYLOAD\][^\n]*?src_SSI=(\d+) dst_SSI=(\d+) sdti=(\d+) pid=(-?\d+) "
                          r"bits=(\d+) hex=([0-9A-F]*)(?: text=([^\n]*))?")
SDS_MAX = 60                     # messages rendus par ecoute (la reponse tient dans une ligne)

# Identifiants de protocole SDS (ETSI EN 300 392-2, table 29.21) : ce que porte le message.
PID_NOMS = {1: "OTAK", 2: "texte", 3: "position NMEA", 4: "WAP", 5: "WCMP", 6: "M-DMO", 7: "PIN", 8: "telephone",
            9: "texte immediat", 10: "position LIP", 11: "modem", 12: "concatenation", 13: "DOTAM",
            130: "texte", 131: "position NMEA", 132: "WAP", 133: "WCMP", 134: "M-DMO", 136: "telephone",
            137: "texte immediat", 138: "message gestion", 140: "concatenation", 141: "DOTAM"}

# Decalage de porteuse (ETSI EN 300 392-2, SYSINFO "offset") : 0, +6,25, -6,25, +12,5 kHz.
_FOFF_HZ = {0: 0, 1: 6250, 2: -6250, 3: 12500}


def nom_pid(pid):
    if pid is None or pid < 0:
        return "donnees sans protocole"
    if 192 <= pid <= 255:
        return "donnees constructeur %d" % pid   # usage propre au reseau (flotte, billettique...)
    if 64 <= pid <= 127 or 200 <= pid:
        return "donnees reseau %d" % pid
    return PID_NOMS.get(pid, "protocole %d" % pid)


def _bits(hexa):
    return "".join(format(int(c, 16), "04b") for c in hexa)


def _signe(v, n):
    return v - (1 << n) if v & (1 << (n - 1)) else v


def lip_court(hexa):
    """Rapport de position court LIP (ETSI TS 100 392-18-1, PID 10) : position,
    erreur, vitesse, cap. Rend None si ce n'en est pas un."""
    b = _bits(hexa)
    if len(b) < 2 + 2 + 25 + 24 + 3 + 7 + 4 or b[:2] != "00":
        return None
    i = 4                                       # type de PDU (2) + temps ecoule (2)
    lon = _signe(int(b[i:i + 25], 2), 25) * 360.0 / (1 << 25); i += 25
    lat = _signe(int(b[i:i + 24], 2), 24) * 180.0 / (1 << 24); i += 24
    err = int(b[i:i + 3], 2); i += 3
    v = int(b[i:i + 7], 2); i += 7
    cap = int(b[i:i + 4], 2)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    vitesse = None if v == 127 else (v if v <= 28 else round(16 * 1.038 ** (v - 13)))
    erreurs = [2, 20, 200, 2000, 20000, 200000, None, None]
    return {"lat": round(lat, 5), "lon": round(lon, 5), "erreur_m": erreurs[err], "vitesse_kmh": vitesse,
            "cap_deg": cap * 22.5}


_PROPRE = re.compile(r"[^0-9A-Za-zÀ-ÖØ-öø-ÿ .,;:!?'’\-/()@%+&\"]")


def nettoyer(txt):
    """Texte d'information (STAS, 2026-10-27) precede d'octets d'en-tete de
    l'application, que le decodeur rend en signes isoles ("®  ø  î") : on les
    retire, et on commence au premier mot d'au moins trois lettres."""
    t = re.sub(r"\s+", " ", _PROPRE.sub(" ", txt or "")).strip()
    m = re.search(r"[^\W\d_]{3,}", t)
    t = t[m.start():].strip() if m else t
    # Octet d'en-tete colle au premier mot ("îHORAIRES", "ÞCher") : une lettre
    # accentuee isolee devant une lettre sans accent n'ouvre pas un mot francais.
    if len(t) > 1 and not t[0].isascii() and t[1].isascii() and t[1].isalpha():
        t = t[1:]
    return t


def lisible(txt):
    """Vrai texte humain ? Le reseau local (2026-10-27) envoie sous le protocole
    "texte" des donnees BINAIRES d'application (le decodeur en fait des espaces et
    des signes epars) : on ne les lit pas a voix haute. Au moins 70 % de lettres,
    chiffres et ponctuation courante, et un mot de trois lettres."""
    t = (txt or "").strip()
    if len(t) < 3:
        return False
    utiles = sum(1 for c in t if _lettre(c) or c.isdigit() or c in " .,;:!?'-/()@%+")
    serre = t.replace(" ", "")
    return (sum(1 for c in serre if _lettre(c) or c.isdigit()) >= 0.7 * len(serre)
            and utiles >= 0.9 * len(t) and re.search(r"[A-Za-z]{3,}", t) is not None)


# 2026-11-09 : isalnum() accepte tout le Latin-1, et des octets binaires lus en Latin-1
# ("ÿûÿöÿúÿÿøÿ ÿòÿïÿÿâÿí") passaient pour du texte : quatre "messages" du poste 30000
# lus a voix haute. Une lettre, c'est l'alphabet latin de base et les accents du francais.
_ACCENTS_FR = set("àâäçéèêëîïôöùûüœæÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŒÆ")


def _lettre(c):
    return (c.isascii() and c.isalpha()) or c in _ACCENTS_FR


def lisible_brut(txt):
    """Le texte brut porte-t-il une vraie phrase (au moins 60 % de lettres une
    fois les espaces retires) ? Ecarte les donnees binaires ou seuls quelques
    octets tombent par hasard sur des lettres."""
    serre = re.sub(r"\s+", "", txt or "")
    return len(serre) >= 8 and sum(1 for c in serre if _lettre(c)) >= 0.6 * len(serre)


# Debut des donnees d'application dans le contenu (en caracteres hex) : apres
# l'octet du protocole, l'en-tete SDS-TL (2 octets) et l'octet de codage du texte.
_DEBUT_APPLI = {130: 8, 137: 8, 2: 4, 9: 4}


def messages(texte):
    """Messages courts lisibles dans la sortie de dsd-neo (correctif nearfield) :
    expediteur, destinataire, nature, texte en clair ou position LIP, et le contenu
    brut (hex) des autres, pour apprendre la physionomie du reseau."""
    out = []
    for src, dst, sdti, pid, bits, hexa, txt in _SDS_CONTENU.findall(texte):
        pid = int(pid)
        m = {"src": src, "dst": dst, "sdti": int(sdti), "pid": pid, "nature": nom_pid(pid), "bits": int(bits),
             "hex": hexa[:128]}
        if txt and lisible(nettoyer(txt)) and lisible_brut(txt):
            m["texte"] = nettoyer(txt)
        debut = _DEBUT_APPLI.get(pid)
        if debut and len(hexa) >= debut + 2 and "texte" not in m:
            # Donnees sous un protocole texte : leur premier octet distingue les
            # formats de l'application (physionomie du reseau).
            m["nature"] = "donnees en texte"
            m["type_appli"] = hexa[debut:debut + 2]
        if pid == 10:
            # Le contenu commence par l'octet du protocole (lu par dsd-neo au meme endroit).
            corps = hexa[2:] if hexa[:2] == "%02X" % pid else hexa
            pos = lip_court(corps)
            if pos:
                m["position"] = pos
        out.append(m)
    return out[-SDS_MAX:]


def disponible():
    return os.access(DSD_NEO, os.X_OK)


def frequence_porteuse(band, carrier, foff):
    """Frequence descendante exacte d'une porteuse annoncee par le SYSINFO :
    bande x 100 MHz + numero x 25 kHz + decalage. (1184, bande 4, decalage 3 :
    429,6125 MHz -- le controle qu'on ecoutait a 12,5 kHz pres.)"""
    return int(band) * 100_000_000 + int(carrier) * 25_000 + _FOFF_HZ.get(int(foff), 0)


def parser(texte):
    """Lit la sortie de dsd-neo -fT -T et en tire l'identite reseau et
    l'activite. Pur (testable sans dongle). Rend un dict de metadonnees --
    jamais de contenu dechiffre."""
    reseau = {}
    bsch = _BSCH.findall(texte)
    if bsch:
        mcc, mnc, cc = bsch[-1]
        reseau = {"mcc": int(mcc), "mnc": int(mnc), "cc": int(cc, 16)}
    sysinfo = _SYSINFO.findall(texte)
    if sysinfo:
        carrier, band, foff = sysinfo[-1]
        reseau["carrier"] = int(carrier)
        reseau["freq_hz"] = frequence_porteuse(band, carrier, foff)
    la = _LA.findall(texte)
    if la:
        reseau["la"] = int(la[-1])
    voisines = _VOISINES.findall(texte)
    if voisines:
        reseau["voisines"] = int(voisines[-1])
    enc = sorted(set(_ENC.findall(texte)))          # p.ex. {"none"} ou {"none","aie"}

    postes = {}
    for ssi in _SSI.findall(texte):
        if int(ssi) != SSI_DIFFUSION:
            postes[ssi] = postes.get(ssi, 0) + 1
    if len(postes) > POSTES_MAX:  # la reponse tient dans une ligne de la liaison
        postes = dict(sorted(postes.items(), key=lambda kv: -kv[1])[:POSTES_MAX])
    etats = {}
    for src, code in _STATUS.findall(texte):
        cle = "%s:%s" % (src, code.upper())
        etats[cle] = etats.get(cle, 0) + 1
    sds = {}
    for src, bits in _SDS.findall(texte):
        s = sds.setdefault(src, {"n": 0, "bits": 0})
        s["n"] += 1
        s["bits"] += int(bits)
    appels = {}
    for call, e, com, service, prio in _SETUP.findall(texte):
        appels[call] = {"chiffre": e != "0", "prive": com == "0", "service": int(service), "priorite": int(prio)}
    for call in _CONNECT.findall(texte):
        appels.setdefault(call, {})
    for call in _CHIFFRE_REFUSE.findall(texte):
        appels.setdefault(call, {})["chiffre"] = True
    suivis = [{"freq_hz": int(f), "carrier": int(c), "call": call} for f, c, call in _SUIVI.findall(texte)]
    # Suivis sur le meme porteur (le log ne donne pas de frequence) : on les
    # rattache a la porteuse de controle courante -- celle qu'on ecoute deja.
    for call in _SUIVI_MEME.findall(texte):
        suivis.append({"freq_hz": reseau.get("freq_hz", 0), "carrier": reseau.get("carrier"), "call": call})
    # Sans -T : pas de ligne [TETRA SM], mais un D-TX-GRANTED prouve une voix
    # accordee sur l'appel -> compte comme suivi (registre/stats).
    for call in _TX_GRANTED.findall(texte):
        if not any(s["call"] == call for s in suivis):
            suivis.append({"freq_hz": reseau.get("freq_hz", 0), "carrier": reseau.get("carrier"), "call": call})
    trafic = sorted({int(f) for _, f in _ALLOC.findall(texte) if int(f) > 0} | {s["freq_hz"] for s in suivis if s["freq_hz"]})
    activite = {
        "postes": postes,                            # SSI -> nombre de mentions
        "inscriptions": texte.count("[TETRA MM D-LU-ACCEPT]"),
        "groupes": texte.count("[TETRA MM D-GROUP-IDENTITY]"),
        "etats": etats,                              # "src:code" -> nombre
        "sds": sds,                                  # src -> {n, bits} (taille seulement)
        "appels": appels,                            # call -> {chiffre, prive, service, priorite}
        "suivis": suivis,                            # appels effectivement suivis sur leur porteuse
        "trafic_hz": trafic,                         # porteuses de trafic attribuees
        "messages": messages(texte),                 # contenu des SDS (avec le correctif de dsd-neo)
    }
    return {
        "reseau": reseau,
        "bsch": len(bsch),                           # nombre de synchros de controle
        "sysinfo": len(sysinfo),
        "enc": enc,                                  # modes de chiffrement vus (lus, pas casses)
        "clair": "none" in enc,                      # du trafic de signalisation en clair
        "mac_resource": texte.count("[TETRA MAC-RESOURCE]"),
        "clip": bool(_CLIP.search(texte)),           # dongle sature : baisser le gain
        "activite": activite,
    }


def postes_actifs(activite, mentions_min=2):
    """SSI vus au moins deux fois : un seul passage peut etre un bloc mal lu."""
    return {s: n for s, n in (activite.get("postes") or {}).items() if n >= mentions_min}


def voix_gardable(activite):
    """Doctrine : on garde TOUTE voix en clair. dsd-neo ne synthetise QUE du
    clair -- il laisse tomber les trames chiffrees ("such speech frames are
    dropped") et verrouille les appels chiffres ("locked out") -- donc un wav
    non silencieux EST de la voix en clair. La garde repose sur le RMS (voir
    run), pas sur la reconnaissance d'un suivi : le suivi n'est qu'un indice
    pour le registre. Ce garde-fou ne refuse QUE s'il y a une preuve POSITIVE
    de chiffrement sur un appel suivi (ceinture et bretelles ; en pratique rare,
    puisque la voix chiffree n'arrive pas jusqu'au wav)."""
    appels = activite.get("appels") or {}
    for s in activite.get("suivis") or []:
        if (appels.get(s["call"]) or {}).get("chiffre"):
            return False
    return True


def _journaliser_brut(freq_hz, texte):
    """Garde la sortie brute de dsd-neo (diagnostic : raison de non-suivi, derive
    de format...). Dossier plafonne a 50 fichiers. Jamais bloquant."""
    try:
        import glob
        d = os.path.join(HOME if os.path.isdir(HOME) else os.path.expanduser("~/autopilot"), "tetra_brut")
        os.makedirs(d, exist_ok=True)
        nom = "%d_%s.log" % (int(freq_hz), time.strftime("%Y%m%d_%H%M%S"))
        with open(os.path.join(d, nom), "w", encoding="utf-8", errors="replace") as fh:
            fh.write(texte)
        for vieux in sorted(glob.glob(os.path.join(d, "*.log")))[:-50]:
            os.remove(vieux)
    except Exception:
        pass


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


def decoder(host, port, freq_hz, gain, seconds, ppm=0, max_seconds=None, relais=True):
    """Connecte dsd-neo au rtl_tcp du Pi (host:port), decode `seconds` s sur
    freq_hz au gain donne -- prolonge jusqu'a max_seconds tant qu'un appel est
    suivi -- et rend les metadonnees + un chemin de voix en clair (ou None).
    relais=False : sur le Pi lui-meme, rtl_tcp est local, pas de reduction IQ.
    Ne leve pas : en cas d'echec, {"ok": False, "erreur": ...}."""
    if not disponible():
        return {"ok": False, "erreur": "dsd-neo absent"}
    seconds = max(1.0, float(seconds))
    max_seconds = max(seconds, float(max_seconds or seconds))
    tmp = tempfile.mkdtemp(prefix="tetra_")
    voix = os.path.join(tmp, "voix.wav")
    # Par le relais iq_fanout (2026-11-01) : IQ reduit cote Pi, 4 fois moins de
    # Wi-Fi (sa puce calait sous le flux brut a 1,152 MS/s).
    fo = None
    try:
        if relais:
            import iq_fanout
            fo = iq_fanout.FanOut(host, int(port))
            host, port = "127.0.0.1", fo.start()
    except Exception as exc:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "erreur": "proxy IQ: %r" % exc}
    source = "rtltcp:%s:%d:%d:%s:%d:%d" % (host, int(port), int(freq_hz), str(gain), int(ppm), BW_KHZ)
    cmd = [DSD_NEO, "-fT", "-i", source, "-w", voix]  # PAS -T : il casse le decodage voix TETRA (2026-10-31)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
    except Exception as exc:
        if fo:
            fo.stop()
        return {"ok": False, "erreur": "lancement dsd-neo : %r" % exc}
    sortie = []
    etat = {"suivi": False, "retour": 0.0}

    def lire():
        try:
            for ligne in proc.stdout:
                sortie.append(ligne)
                # Voix active = floor accorde (D-TX-GRANTED), relache au
                # D-TX-CEASED : marqueurs fiables SANS -T. On garde aussi les
                # lignes [TETRA SM] quand elles apparaissent.
                # Un appel CLAIR en cours de montage (D-SETUP enc=0) rafraichit
                # la grace : on tient jusqu'au floor meme quand la sonde de base
                # est courte (porteuse d'exploration) -- sinon on quittait avant
                # la voix ("repere mais pas suivi", 2026-10-31).
                if ("D-TX-GRANTED" in ligne or "[TETRA SM] on traffic channel" in ligne
                        or "[TETRA SM] follow call" in ligne):
                    etat["suivi"] = True
                    etat["retour"] = time.time()
                elif "D-TX-CEASED" in ligne or _RETOUR.search(ligne):
                    etat["suivi"] = False
                    etat["retour"] = time.time()
                elif "D-SETUP" in ligne and "enc=0" in ligne:
                    etat["retour"] = time.time()
        except Exception:
            pass

    fil = threading.Thread(target=lire, daemon=True)
    fil.start()
    debut = time.time()
    while proc.poll() is None:
        ecoule = time.time() - debut
        if ecoule >= max_seconds:
            break
        en_appel = etat["suivi"] or time.time() - etat["retour"] < FIN_APPEL_SEC
        if ecoule >= seconds and not en_appel:
            break
        time.sleep(0.25)
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
    if fo:
        fo.stop()
    texte = "".join(sortie)
    _journaliser_brut(freq_hz, texte)
    res = parser(texte)
    rms = _rms_wav(voix)
    res["voix_wav"] = voix if rms >= VOIX_RMS_MIN and voix_gardable(res["activite"]) else None
    res["voix_rms"] = rms
    res["duree"] = round(time.time() - debut, 1)
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
    act = res.get("activite") or {}
    postes = postes_actifs(act)
    if postes:
        bout += ", %d postes actifs" % len(postes)
    appels = act.get("appels") or {}
    if appels:
        clairs = sum(1 for a in appels.values() if not a.get("chiffre"))
        bout += ", %d appel%s dont %d en clair" % (len(appels), "s" if len(appels) > 1 else "", clairs)
    elif res.get("clair"):
        bout += ", signalisation en clair"
    elif res.get("enc"):
        bout += ", trafic chiffre"
    return bout
