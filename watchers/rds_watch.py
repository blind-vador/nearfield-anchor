"""Veille RDS des radios FM (2026-11-01) : nom de station, RadioText (titre en
cours, messages), type de programme, annonces trafic et alarmes, en clair dans
la sous-porteuse 57 kHz de chaque station FM.

Pas de liste de stations : chaque passe repere les stations au balayage
(rtl_power sur 85-110 MHz, comme la bande fm_diffusion), puis en ecoute
quelques-unes a tour de role, les moins recemment visitees d'abord. La
demodulation MPX (rtl_fm 171 kHz, sans desaccentuation) passe dans redsea
(compile depuis les sources, voir install.sh), qui sort une ligne JSON par
groupe RDS. Tout est archive ; on dit une station nouvelle, un RadioText
nouveau, une annonce trafic ou une alarme (PTY 30/31)."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

import veille_donnees as vd
from phrases import dire
from sound_paths import RADIO_SCANER_DIR

FM_BAS, FM_HAUT, FM_PAS = 85_000_000, 110_000_000, 100_000
MARGE_DB = 6.0                # comme fm_diffusion (station locale mesuree a +7 dB)
STATIONS_MAX = 25
STATION_SEC = 12              # le nom (PS) arrive en ~1 s, un RadioText complet en 5-10 s
BALAYAGE_SEC = 6 * 3600       # les stations FM bougent peu : un balayage toutes les 6 h
TEXTE_REDIT_SEC = 3 * 3600
SOUND = os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3")
_memory = vd.Memory("/tmp/rds_watch_state.json")

# Types de programme RDS (norme europeenne), tels que redsea les nomme.
PROGRAMMES = {"News": "informations", "Current affairs": "actualité", "Information": "information",
              "Sport": "sport", "Education": "éducation", "Drama": "fiction", "Culture": "culture",
              "Science": "science", "Varied": "divers", "Pop music": "musique pop", "Rock music": "rock",
              "Easy listening": "variétés", "Light classical": "classique léger",
              "Serious classical": "musique classique", "Other music": "musique", "Weather": "météo",
              "Finance": "économie", "Children's programmes": "jeunesse", "Social affairs": "société",
              "Religion": "religion", "Phone-in": "libre antenne", "Travel": "voyage", "Leisure": "loisirs",
              "Jazz music": "jazz", "Country music": "country", "National music": "chanson",
              "Oldies music": "rétro", "Folk music": "musique folk", "Documentary": "documentaire",
              "Alarm test": "essai d'alarme", "Alarm": "ALARME"}


def disponible():
    return shutil.which("redsea") is not None


def _stations():
    """Frequences des stations FM du moment (balayage, garde BALAYAGE_SEC)."""
    connues = _memory.data.get("stations")
    if connues and time.time() - _memory.data.get("balayage", 0) < BALAYAGE_SEC:
        return connues
    fd, chemin = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    bins = []
    try:
        subprocess.run(["rtl_power", "-f", f"{FM_BAS}:{FM_HAUT}:{FM_PAS}", "-i", "1", "-1", chemin],
                       capture_output=True, timeout=90)
        with open(chemin, encoding="utf-8") as f:
            for ligne in f:
                champs = [c.strip() for c in ligne.split(",")]
                if len(champs) < 7:
                    continue
                bas, pas = float(champs[2]), float(champs[4])
                for i, db in enumerate(champs[6:]):
                    try:
                        bins.append((bas + i * pas, float(db)))
                    except ValueError:
                        pass
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[rds_watch] balayage FM impossible : {exc!r}")
    finally:
        try:
            os.remove(chemin)
        except OSError:
            pass
    if not bins:
        return connues or []
    niveaux = sorted(db for _, db in bins)
    plancher = niveaux[len(niveaux) // 5]
    trouvees = []
    for f, db in sorted(bins, key=lambda b: -b[1]):
        if db < plancher + MARGE_DB or len(trouvees) >= STATIONS_MAX:
            break
        f = int(round(f / FM_PAS) * FM_PAS)
        if all(abs(f - t) > 150_000 for t in trouvees):
            trouvees.append(f)
    _memory.data.update(stations=trouvees, balayage=time.time())
    print(f"[rds_watch] {len(trouvees)} station(s) FM reperee(s)")
    return trouvees


def _ecouter(freq, secondes):
    """Groupes RDS (dicts redsea) entendus sur freq pendant secondes."""
    fm = subprocess.Popen(["rtl_fm", "-f", str(freq), "-M", "fm", "-l", "0", "-A", "std", "-p", "0",
                           "-s", "171k", "-F", "9"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    rds = subprocess.Popen(["redsea", "-r", "171k"], stdin=fm.stdout, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL)
    fm.stdout.close()
    groupes = []

    def lire():
        for brut in rds.stdout:
            try:
                groupes.append(json.loads(brut))
            except ValueError:
                pass
    fil = threading.Thread(target=lire, daemon=True)
    fil.start()
    fin = time.time() + secondes
    while time.time() < fin and fm.poll() is None:
        time.sleep(0.2)
    for p in (fm, rds):
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            p.kill()
    fil.join(timeout=2)
    return groupes


def _resume(groupes):
    r = {}
    for g in groupes:
        for cle in ("pi", "ps", "radiotext", "prog_type", "ta", "tp", "long_ps", "callsign"):
            if g.get(cle) not in (None, ""):
                r[cle] = g[cle]
        if g.get("alt_frequencies_a"):
            r["alt"] = g["alt_frequencies_a"]
    return r


def _mhz(freq):
    return f"{freq / 1e6:.1f}".replace(".", ",")


def _annoncer(freq, r):
    """Rend True si quelque chose a ete dit."""
    nom = (r.get("long_ps") or r.get("ps") or "").strip()
    if not nom and not r.get("radiotext"):
        return False
    cle = r.get("pi") or str(freq)
    programme = PROGRAMMES.get(r.get("prog_type"), "")
    texte = " ".join(str(r.get("radiotext") or "").split())
    fiche = {"protocole": "RDS", "frequence_hz": freq, "source": nom or cle, "details": r}
    a_dire = []
    if programme in ("ALARME", "essai d'alarme") and _memory.due(f"alarme|{cle}", 600):
        a_dire.append(dire("rds.alarme", nom=nom or _mhz(freq), programme=programme))
    if r.get("ta") is True and _memory.due(f"trafic|{cle}", 900):
        a_dire.append(dire("rds.trafic", nom=nom or _mhz(freq), frequence=_mhz(freq)))
    nouvelle = cle not in _memory.data.setdefault("connues", {})
    if nouvelle:
        _memory.data["connues"][cle] = {"nom": nom, "freq": freq}
        a_dire.append(dire("rds.station", frequence=_mhz(freq), nom=nom or cle,
                           programme=f", {programme}" if programme else ""))
    if texte and _memory.due(f"texte|{cle}|{texte}", TEXTE_REDIT_SEC):
        a_dire.append(dire("rds.texte", nom=nom or _mhz(freq), texte=texte))
    if not a_dire:
        return False
    vd.announce(" ".join(a_dire), SOUND, f"donnees_rds_{cle}", "rds_watch", fiche=fiche)
    return True


def scan_window(dwell_sec=45):
    """Une passe RDS : quelques stations, les moins recemment visitees d'abord.
    Rend True si au moins une annonce a eu lieu."""
    if not disponible():
        print("[rds_watch] redsea introuvable (voir install.sh)")
        return False
    debut = time.time()
    stations = _stations()
    if not stations:
        return False
    visites = _memory.data.setdefault("visites", {})
    stations = sorted(stations, key=lambda f: visites.get(str(f), 0))
    annonce = False
    for freq in stations:
        reste = dwell_sec - (time.time() - debut)
        if reste < STATION_SEC / 2:
            break
        groupes = _ecouter(freq, min(STATION_SEC, reste))
        visites[str(freq)] = time.time()
        r = _resume(groupes)
        if groupes:
            vd.archive("rds", dict(r, freq_hz=freq, groupes=len(groupes)))
            print(f"[rds_watch] {_mhz(freq)} MHz : {len(groupes)} groupe(s), "
                  f"{r.get('ps', '?')} {('| ' + r['radiotext']) if r.get('radiotext') else ''}")
        try:
            annonce = _annoncer(freq, r) or annonce
        except Exception as exc:   # une annonce ratee n'arrete pas la veille
            print(f"[rds_watch] annonce {_mhz(freq)} : {exc!r}")
    _memory.save()
    return annonce
