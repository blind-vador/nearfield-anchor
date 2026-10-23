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

Les stations FM ne suffisent pas (2026-10-20) : tres fortes, elles entrent
par le cable USB meme antenne devissee. Apres un rebranchement du 19, la FM
donnait encore 16 stations alors que plus rien ne passait au-dessus (aucune
trame de capteur 433 MHz ni d'avion depuis). Le verdict regarde donc aussi
le trafic reellement decode : la derniere trame de capteur (dernier_capteur).

Ecoute des capteurs en direct (2026-10-22) : la mesure ecoute aussi
ECOUTE_CAPTEURS_SEC sur 433,92 MHz (rtl_433). C'est la vraie preuve : le
22/10, avec le coax N-N defectueux (ame coupee, tresse bonne), la FM donnait
20 stations a +24 dB mais 0 trame ; la meme discone branchee sans le coax :
42 trames de 9 capteurs en 3 minutes. Une passe UHF large (400-470 MHz) et la
courbe FM sont gardees pour les audiographes (lib/audiographe.py).

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

import audiographe

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
CAPTEURS_GLOB = "/media/nas/nd1/server/radio_log/donnees/capteurs_2*.jsonl"
CAPTEURS_SILENCE_H = 6      # plus de trame capteur depuis : la reception au-dessus de la FM est suspecte
ECOUTE_CAPTEURS_SEC = 90    # un LaCrosse emet toutes les ~50 s : 90 s en entendent la plupart
UHF_AUDIO = (400_000_000, 470_000_000, 25_000)


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


def _ecoute_capteurs(sec=ECOUTE_CAPTEURS_SEC):
    """Trames de capteurs decodees en direct sur 433,92 MHz : [{"modele", "id", "snr"}],
    ou None si rtl_433 n'a pas pu tourner."""
    try:
        r = subprocess.run(["rtl_433", "-d", "0", "-f", "433.92M", "-F", "json", "-M", "level",
                            "-T", str(int(sec))],
                           capture_output=True, text=True, errors="replace", timeout=sec + 30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    trames = []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("model"):
            trames.append({"modele": d["model"], "id": d.get("id"), "snr": d.get("snr")})
    if not trames and ("No supported devices" in r.stderr or "usb_claim_interface" in r.stderr):
        return None
    return trames


def measure(bands, seuil_extra=None):
    """bands : radio_watch.BANDS (type sweep). seuil_extra(nom) : reglage
    "marge_detection_db" de la bande. Rend le resultat (dict, JSON)."""
    started = time.time()
    res = {"t": started, "bandes": [], "erreur": None, "courbes": []}
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
            fm_bins = [b for b in bins if FM_DEBUT_HZ <= b[0] <= FM_FIN_HZ]
            if fm_bins:
                res["courbes"].append(dict(audiographe.courbe(fm_bins), nom="fm", graduation=5))
        res["bandes"].append(entry)
    if not res["erreur"]:
        _levels, bins = _passe(*UHF_AUDIO)
        if _levels is not None:
            res["courbes"].append(dict(audiographe.courbe(bins), nom="uhf", graduation=10))
        res["capteurs"] = _ecoute_capteurs()
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
    trames = res.get("capteurs")
    return {"etat": etat, "stations_fm": n_fm, "derive_plancher": derive, "mesures_avant": n_hist,
            "au_dessus": [b["nom"] for b in res["bandes"] if b.get("ecart", -1) > 0],
            "trames": None if trames is None else len(trames),
            "capteurs": None if trames is None else len({(t["modele"], t["id"]) for t in trames})}


def _archiver(res):
    hist = _read(HISTORIQUE) or []
    hist.append({"t": res["t"], "bandes": [{k: b[k] for k in ("nom", "plancher", "pic", "ecart") if k in b}
                                           for b in res["bandes"]],
                 "stations_fm": res["verdict"]["stations_fm"], "trames": res["verdict"]["trames"]})
    try:
        _write(HISTORIQUE, hist[-HISTORIQUE_MAX:])
    except OSError:
        pass


def dernier_capteur():
    """Instant de la derniere trame de capteur sans fil decodee (433/868 MHz :
    une dizaine de stations meteo du voisinage, des centaines par jour), ou None."""
    import glob
    for path in sorted(glob.glob(CAPTEURS_GLOB), reverse=True)[:3]:
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 4096))
                lines = f.read().decode(errors="replace").strip().splitlines()
            for line in reversed(lines):
                try:
                    return float(json.loads(line)["t"])
                except (ValueError, KeyError, TypeError):
                    continue
        except OSError:
            continue
    return None


def derniere():
    return _read(DONE_PATH)
