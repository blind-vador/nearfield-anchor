"""Mesure de l'antenne (commande "antenne", 2026-10-19) : ce que la cle radio
recoit vraiment, pour savoir si l'antenne est bien cablee et si tout passe.

Une passe rtl_power par bande du scanner (radio_watch.BANDS) :
  plancher   bruit de fond (20e percentile, meme estimateur que le scanner)
  pic        signal le plus fort de la bande en ce moment
  ecart      (pic - plancher) - seuil de detection du scanner pour cette
             bande (margin_db + reglage "marge_detection_db") : positif = le
             scanner capterait ce signal, negatif = sous le seuil

Verdict antenne : la radio FM emet en continu, fort, 24 h sur 24. Antenne
bien branchee : une dizaine de stations ressortent de 10 a 25 dB au-dessus
du bruit (mesure de reference du 2026-10-19). Peu ou aucune : antenne debranchee, cable ou connecteur abime. Le
bruit de fond compare aux mesures precedentes (HISTORIQUE) dit le reste :
plancher qui s'effondre partout = plus rien en entree ; plancher qui monte
= parasite proche (alimentation, USB, ecran).

La cle appartient au scanner (radio-watch) : la console depose une demande
(request), le scanner mesure entre deux fenetres d'ecoute (take_request,
measure, done) et la console attend le resultat (wait). Scanner arrete : la
console mesure elle-meme.
"""
import json
import math
import os
import statistics
import subprocess
import tempfile
import time

REQUEST_PATH = "/tmp/radio_watch_antenne.json"
DONE_PATH = "/tmp/radio_watch_antenne_fait.json"
HISTORIQUE = "/home/vador/autopilot/mesures_antenne.json"
REQUEST_MAX_AGE_SEC = 600
HISTORIQUE_MAX = 30
FM_BAND = "fm_diffusion"
FM_FORTE_DB = 10.0          # station FM nettement au-dessus du bruit (mesure du 2026-10-19 : 13 stations de 11 a 23 dB)
FM_ECART_HZ = 300_000       # deux pics plus proches : la meme station
FM_DEBUT_HZ, FM_FIN_HZ = 87_500_000, 108_000_000
FM_BON, FM_FAIBLE = 5, 1    # stations fortes : bonne reception / faible / rien
DERIVE_DB = 4.0             # plancher qui baisse ou monte de plus : on le signale
DC_GUARD_BINS = 3


# --- echange console <-> scanner ---------------------------------------------------------

def _write(path, data):
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def request():
    asked = time.time()
    try:
        os.remove(DONE_PATH)
    except OSError:
        pass
    _write(REQUEST_PATH, {"t": asked})
    return asked


def requested():
    req = _read(REQUEST_PATH)
    return bool(req) and time.time() - req.get("t", 0) <= REQUEST_MAX_AGE_SEC


def take_request():
    """Pour le scanner : vrai (une fois) si une mesure est demandee."""
    ok = requested()
    try:
        os.remove(REQUEST_PATH)
    except OSError:
        pass
    return ok


def done(result):
    _write(DONE_PATH, result)


def wait(asked, timeout, tick=None):
    """Attend le resultat du scanner ; tick() appele toutes les 45 s."""
    end, last = time.time() + timeout, time.time()
    while time.time() < end:
        res = _read(DONE_PATH)
        if res and res.get("t", 0) >= asked:
            return res
        if tick and time.time() - last >= 45:
            tick()
            last = time.time()
        time.sleep(1)
    return None


# --- mesure --------------------------------------------------------------------------------

def _passe(low, high, step):
    """(niveaux, [(freq, db)]) d'une passe rtl_power, ou (None, erreur)."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        r = subprocess.run(["rtl_power", "-f", f"{low}:{high}:{step}", "-i", "1", "-1", path],
                           capture_output=True, text=True, errors="replace", timeout=120)
        if r.returncode != 0:
            err = r.stderr
            if "No supported devices" in err:
                return None, "cle_absente"
            if "usb_claim_interface" in err or "Failed to open" in err:
                return None, "cle_occupee"
            return None, "rtl_power"
        rows = []
        with open(path, errors="replace") as f:
            for line in f:
                fields = line.strip().split(",")
                if len(fields) >= 7:
                    rows.append(fields)
        guard = len({(r[2], r[3]) for r in rows}) > 1
        levels, bins = [], []
        for fields in rows:
            lo, hi, st = float(fields[2]), float(fields[3]), float(fields[4])
            center = (lo + hi) / 2
            for i, raw in enumerate(fields[6:]):
                try:
                    db = float(raw)
                except ValueError:
                    continue
                freq = lo + i * st
                if math.isnan(db) or (guard and abs(freq - center) <= st * DC_GUARD_BINS):
                    continue  # pic DC du dongle
                levels.append(db)
                bins.append((freq, db))
        return (levels, bins) if levels else (None, "rien")
    except subprocess.TimeoutExpired:
        return None, "delai"
    finally:
        os.remove(path)


def _percentile(values, pct):
    s = sorted(values)
    return s[min(int(len(s) * pct / 100), len(s) - 1)]


def _stations_fm(bins, floor):
    """Pics FM distincts a plus de FM_FORTE_DB au-dessus du plancher."""
    fortes = sorted((b for b in bins if b[1] - floor >= FM_FORTE_DB), key=lambda b: -b[1])
    gardees = []
    for freq, db in fortes:
        if all(abs(freq - f) >= FM_ECART_HZ for f, _ in gardees):
            gardees.append((freq, db))
    return gardees


def measure(bands, seuil_extra=None):
    """bands : radio_watch.BANDS (type sweep). seuil_extra(nom) : reglage
    "marge_detection_db" de la bande. Rend le resultat (dict, JSON)."""
    started = time.time()
    res = {"t": started, "bandes": [], "erreur": None}
    for band in bands:
        if band.get("type") != "sweep":
            continue
        levels, bins = _passe(band["low_hz"], band["high_hz"], band["step_hz"])
        if levels is None:
            if bins in ("cle_absente", "cle_occupee"):
                res["erreur"] = bins
                break
            res["bandes"].append({"nom": band["name"], "dit": band.get("name_spoken", band["name"]), "erreur": bins})
            continue
        floor = _percentile(levels, 20)
        pic_f, pic_db = max(bins, key=lambda b: b[1])
        seuil = band.get("margin_db", 9.0) + (seuil_extra(band["name"]) if seuil_extra else 0.0)
        entry = {"nom": band["name"], "dit": band.get("name_spoken", band["name"]),
                 "plancher": round(floor, 1), "pic_mhz": round(pic_f / 1e6, 4), "pic": round(pic_db, 1),
                 "seuil": round(seuil, 1), "ecart": round(pic_db - floor - seuil, 1)}
        if band["name"] == FM_BAND:
            # Bande FM pleine de stations : son bruit de fond se lit sur les bords,
            # hors des 87,5-108 MHz ou personne n'emet.
            bords = [db for f, db in bins if f < FM_DEBUT_HZ or f > FM_FIN_HZ]
            fm_floor = statistics.median(bords) if len(bords) >= 5 else floor
            entry["plancher_fm"] = round(fm_floor, 1)
            entry["stations"] = [(round(f / 1e6, 1), round(db - fm_floor, 1))
                                 for f, db in _stations_fm([b for b in bins if FM_DEBUT_HZ <= b[0] <= FM_FIN_HZ], fm_floor)]
        res["bandes"].append(entry)
    res["duree"] = round(time.time() - started)
    if not res["erreur"]:
        res["verdict"] = _verdict(res)
        _archiver(res)
    return res


def _reference():
    """Plancher habituel de chaque bande (mediane des mesures precedentes)."""
    hist = _read(HISTORIQUE) or []
    ref = {}
    for m in hist:
        for b in m.get("bandes", []):
            if "plancher" in b:
                ref.setdefault(b["nom"], []).append(b["plancher"])
    return {k: statistics.median(v) for k, v in ref.items()}, len(hist)


def _verdict(res):
    fm = next((b for b in res["bandes"] if b["nom"] == FM_BAND and "stations" in b), None)
    n_fm = len(fm["stations"]) if fm else None
    ref, n_hist = _reference()
    derives = [b["plancher"] - ref[b["nom"]] for b in res["bandes"] if "plancher" in b and b["nom"] in ref]
    derive = round(statistics.median(derives), 1) if derives else None
    if n_fm is None:
        etat = "inconnu"
    elif n_fm >= FM_BON:
        etat = "bon"
    elif n_fm >= FM_FAIBLE:
        etat = "faible"
    else:
        etat = "rien"
    return {"etat": etat, "stations_fm": n_fm, "derive_plancher": derive, "mesures_avant": n_hist,
            "au_dessus": [b["nom"] for b in res["bandes"] if b.get("ecart", -1) > 0]}


def _archiver(res):
    hist = _read(HISTORIQUE) or []
    hist.append({"t": res["t"], "bandes": [{k: b[k] for k in ("nom", "plancher", "pic", "ecart") if k in b}
                                           for b in res["bandes"]],
                 "stations_fm": res["verdict"]["stations_fm"]})
    try:
        _write(HISTORIQUE, hist[-HISTORIQUE_MAX:])
    except OSError:
        pass


def derniere():
    return _read(DONE_PATH)
