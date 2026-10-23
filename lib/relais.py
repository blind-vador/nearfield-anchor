"""Relais radioamateurs appris tout seuls depuis l'APRS : beaucoup de relais
(ou leurs objets APRS) annoncent leur frequence de sortie, leur tonalite et
leur mode ("145.612MHz T123 R17k", "70cm Voice (D-Star) 439.68750MHz
-9.4000MHz"). Ceux a portee (RANGE_KM) sont ajoutes a l'ecoute du scanner,
groupes par bande (6 m, 2 m, 70 cm) : un rtl_power par groupe, rapide.
Analogique ou numerique (DMR, D-Star, C4FM) : le decodage numerique du
scanner s'en charge (dsd-fme -fa).

2026-10-23 (demande utilisateur : "ces frequences et codes d'acces nous
apprennent sans doute ce qui est audible") :
  - formats en plus : frequence sans "MHz" ("145.2125"), liaison entre
    bandes ("145.250<=>431.250" : les deux sont apprises), decalage d'entree
    ("-0.600", "-060" en dizaines de kHz comme le veut l'APRS), lieu ;
  - les relais hors de portee sont gardes a part (LOINTAINS_PATH), pour
    information : connus, mais pas balayes ;
  - audibilite (AUDIBILITE_PATH, carte SD) : combien de fois chaque relais a
    ete ecoute par le scanner, entendu (captation enregistree), et CONFIRME
    (sa tonalite CTCSS retrouvee dans le son, lib/ctcss.py) ; la commande
    console "relais" le dit.
"""
import json
import os
import re
import threading
import time

STORE_PATH = "/media/nas/nd1/server/radio_log/relais.json"
LOCAL_PATH = "/home/vador/autopilot/relais.json"
LOINTAINS_PATH = "/home/vador/autopilot/relais_lointains.json"
AUDIBILITE_PATH = "/home/vador/autopilot/relais_audibilite.json"
RANGE_KM = 60
FORGET_SEC = 30 * 86400
ECRIRE_ECOUTES_SEC = 600      # compteurs d'ecoute gardes en memoire, ecrits au plus toutes les 10 min
GROUPS = [  # (nom, nom dit, bas, haut)
    ("relais_6m", "relais radioamateur 6 mètres", 50_000_000, 52_000_000),
    ("relais_2m", "relais radioamateur 2 mètres", 144_000_000, 146_000_000),
    ("relais_70cm", "relais radioamateur 70 centimètres", 430_000_000, 440_000_000),
]
FREQ = re.compile(r"(\d{2,3}\.\d{2,5})\s?MHz")
FREQ_NUE = re.compile(r"(?<![\d.])((?:5[0-3]|14[4-6]|4[34]\d)\.\d{2,5})(?![\d])")
DECALAGE = re.compile(r"(?<![\d.])([+-]\d{1,2}\.\d{1,4})\s?(?:MHz)?(?![\d])|(?<![\w.])([+-]\d{3})(?![\d.])")
REPEATER_HINTS = re.compile(r"\b(T\d{3}|R\d+k|tone|CTCSS|Voice|MMDVM|relais|repeater|RPT)\b|<=>", re.IGNORECASE)
MODES = {"D-Star": "dstar", "DMR": "dmr", "C4FM": "c4fm"}
_jetons = re.compile(FREQ.pattern + "|" + FREQ_NUE.pattern + r"|<=>|\bT\d{3}\b|\bR\d+k\b|[+-]\d{1,2}\.\d{1,4}\s?(MHz)?"
                     r"|(?<![\w.])[+-]\d{3}(?![\d.])|\b(tone|CTCSS)\s*\d+(\s*Hz)?", re.IGNORECASE)


def _path():
    return STORE_PATH if os.path.isdir(os.path.dirname(STORE_PATH)) else LOCAL_PATH


def _lire(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _ecrire(path, data):
    try:
        tmp = path + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[relais] {os.path.basename(path)} non ecrit : {exc}")


def load():
    return _lire(_path())


def _save(data):
    _ecrire(_path(), data)


def _frequences(comment):
    """Frequences annoncees, dans les bandes des relais : d'abord celles en
    "MHz", sinon les nombres nus (relais qui ne mettent pas l'unite)."""
    trouvees = [float(m) for m in FREQ.findall(comment)] or [float(m) for m in FREQ_NUE.findall(comment)]
    out = []
    for mhz in trouvees:
        hz = int(round(mhz * 1e6))
        if any(low <= hz <= high for _, _, low, high in GROUPS) and hz not in out:
            out.append(hz)
    return out


def _decalage_hz(comment):
    m = DECALAGE.search(comment)
    if not m:
        return None
    if m.group(1):
        return int(round(float(m.group(1)) * 1e6))
    return int(m.group(2)) * 10_000  # APRS : "-060" = -600 kHz


def _lieu(comment):
    reste = " ".join(_jetons.sub(" ", comment).split()).strip(" ,;-/")
    return reste[:60] or None


def learn(name, comment, symbol, km, lat=None, lon=None):
    """Paquet APRS d'une station a km kilometres : si c'est un relais qui
    annonce sa frequence, on l'apprend (a portee) ou on le note (lointain).
    Rend la frequence nouvellement apprise a portee, ou None."""
    if not comment or km is None:
        return None
    if re.search(r"LoRa|RX ONLY|iGate", comment, re.IGNORECASE):
        return None  # passerelle de donnees, pas un relais vocal
    freqs = _frequences(comment)
    if not freqs:
        return None
    looks_like = symbol in ("r", "&", "0") or REPEATER_HINTS.search(comment)
    if not looks_like:
        return None
    mode = next((code for word, code in MODES.items() if word in comment), "fm")
    tone = re.search(r"\bT(\d{3})\b|tone\s*(\d+)|CTCSS\s*(\d+)", comment, re.IGNORECASE)
    tonalite = int(next(g for g in tone.groups() if g)) if tone else None
    fiche = {"nom": name.strip(), "mode": mode, "km": round(km, 1), "tonalite": tonalite,
             "decalage_hz": _decalage_hz(comment), "lieu": _lieu(comment),
             "lien": [f for f in freqs] if len(freqs) > 1 else None, "lat": lat, "lon": lon,
             "commentaire": comment[:120], "vu": time.time()}
    if km > RANGE_KM:  # connu, pour information, mais trop loin pour etre balaye
        lointains = _lire(LOINTAINS_PATH)
        for f in freqs:
            lointains[str(f)] = dict(fiche, appris=(lointains.get(str(f)) or {}).get("appris", time.time()))
        _ecrire(LOINTAINS_PATH, {f: r for f, r in lointains.items() if time.time() - r.get("vu", 0) < FORGET_SEC})
        return None
    data = load()
    nouveau = None
    for f in freqs:
        known = data.get(str(f))
        data[str(f)] = dict(fiche, appris=(known or {}).get("appris", time.time()))
        if not known and nouveau is None:
            nouveau = f
    data = {f: r for f, r in data.items() if time.time() - r.get("vu", 0) < FORGET_SEC}
    _save(data)
    return nouveau


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


def relais_proche(freq_hz, tolerance_hz=6_250):
    """(frequence, fiche) du relais connu a cette frequence, ou (None, None)."""
    for f, r in load().items():
        if abs(int(f) - int(freq_hz)) <= tolerance_hz:
            return int(f), r
    return None, None


# --- audibilite ------------------------------------------------------------------------

_ecoutes = {"n": {}, "ecrit": time.time()}
_verrou = threading.Lock()


def _maj_audibilite(maj):
    with _verrou:
        data = _lire(AUDIBILITE_PATH)
        maj(data)
        _ecrire(AUDIBILITE_PATH, data)


def note_ecoutes(channels):
    """Le scanner vient d'ecouter ces frequences de relais (en memoire, ecrit
    de temps en temps : pas d'ecriture a chaque tour)."""
    for f in channels:
        _ecoutes["n"][str(int(f))] = _ecoutes["n"].get(str(int(f)), 0) + 1
    if time.time() - _ecoutes["ecrit"] >= ECRIRE_ECOUTES_SEC:
        n, _ecoutes["n"], _ecoutes["ecrit"] = _ecoutes["n"], {}, time.time()

        def ajoute(data):
            for f, k in n.items():
                e = data.setdefault(f, {})
                e["ecoutes"] = e.get("ecoutes", 0) + k
                e["derniere_ecoute"] = time.time()
        _maj_audibilite(ajoute)


def note_capture(freq_hz, ctcss_hz=None):
    """Une captation enregistree sur la frequence d'un relais connu ; rend
    "confirme" si sa tonalite a ete retrouvee, "autre_tonalite", "entendu"."""
    f, fiche = relais_proche(freq_hz)
    if f is None:
        return None
    attendu = (fiche or {}).get("tonalite")
    if ctcss_hz and attendu and abs(float(ctcss_hz) - float(attendu)) <= 1.5:
        verdict = "confirme"
    elif ctcss_hz and attendu:
        verdict = "autre_tonalite"
    else:
        verdict = "entendu"

    premier = []

    def ajoute(data):
        e = data.setdefault(str(f), {})
        if not e.get("captures"):
            premier.append(True)
        e["captures"] = e.get("captures", 0) + 1
        e["dernier_entendu"] = time.time()
        if verdict == "confirme":
            e["confirmations"] = e.get("confirmations", 0) + 1
        if ctcss_hz:
            tons = e.setdefault("tonalites_entendues", {})
            tons[str(ctcss_hz)] = tons.get(str(ctcss_hz), 0) + 1
    _maj_audibilite(ajoute)
    if premier:  # relais entendu pour la premiere fois : decouverte (2026-10-23, sons.py)
        try:
            import sons
            from sound_player import play_sound_effect
            chemin = sons.chemin("nouvel_emetteur")
            if chemin:
                play_sound_effect(chemin, label="relais")
        except Exception as exc:
            print(f"[relais] son : {exc!r}")
    return verdict


def audibilite():
    return _lire(AUDIBILITE_PATH)


def lointains():
    return _lire(LOINTAINS_PATH)
