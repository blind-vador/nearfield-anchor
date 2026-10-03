"""Radio numerique DAB+ (2026-10-03) : textes diffuses avec le son (DLS :
titres, infos, messages de la station) et diaporamas (images), dits au fil
de l'eau comme des messages du journal "messages" (lib/prise_vocale.py) ;
les images sont decrites par Gemini (texte alternatif pour une personne
aveugle).

welle-cli (paquet welle.io) en mode serveur web local : il decode
l'ensemble (multiplex) d'un canal et expose mux.json (stations, textes) et
/slide/<sid> (image courante). Les ensembles ne sont pas configures a la
main : la fenetre les DECOUVRE, quelques canaux de la bande III par visite
(PROBES_PER_VISIT), jusqu'a les avoir tous essayes (redecouverte tous les
RESCAN_SEC) ; ensuite elle ecoute les ensembles trouves a tour de role.
"""
import hashlib
import json
import os
import subprocess
import time

import requests

import veille_donnees as vd
from phrases import dire

CHANNELS = [f"{n}{l}" for n in range(5, 14) for l in "ABCD"] + ["13E", "13F"]
STATE_PATH = "/home/vador/autopilot/dab_ensembles.json"
PORT = 7979
URL = f"http://127.0.0.1:{PORT}"
PROBE_SEC = 9              # le temps qu'un ensemble se fasse reconnaitre
PROBES_PER_VISIT = 4
RESCAN_SEC = 30 * 86400
# Stations INTERDITES (2026-10-03, demande utilisateur) : on ne cherche que ce
# qui est peu connu ou inconnu (radios locales, associatives...). Sont
# ignorees, sans aucun appel a Gemini : Radio France, les grandes radios
# nationales et les reseaux commerciaux (liste ci-dessous), plus celles du
# reglage "dab" (cle "interdites", NAS). Comparaison sans accents ni casse,
# par mot ou groupe de mots entier ("rtl" interdit RTL et RTL2, pas RTLB).
# Reseau "ici" de Radio France : "ici" suivi d'un lieu (ICI_PLACES).
DEFAULT_BANNED = [
    # Radio France
    "france inter", "france culture", "france musique", "franceinfo", "france info", "fip", "mouv",
    "france bleu",
    # grandes radios nationales, publiques ou non
    "rfi", "radio classique", "europe 1", "europe1", "rtl", "rmc", "bfm", "sud radio", "radio nova", "tsf jazz",
    "jazz radio", "skyrock", "fun radio", "virgin", "rfm", "nrj", "nostalgie", "cherie", "rire et chansons",
    "rire & chansons", "oui fm", "m radio", "latina", "africa radio", "radio fg", "fg", "voltage", "radio ado", "ado", "swigg",
    "chante france", "radio orient", "beur fm", "radio monaco", "melody",
    # reseaux commerciaux regionaux
    "radio scoop", "tonic radio", "alouette", "hit west", "contact fm", "radio espace", "forum", "vibration",
    "nostalgie", "jordanne", "totem", "radio star",
]


def _state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"essayes": {}, "trouves": {}, "suivant": 0}


def _save_state(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        print(f"[dab_watch] etat non ecrit : {exc}")


def _start(channel):
    return subprocess.Popen(["welle-cli", "-c", channel, "-w", str(PORT), "-C", CAROUSEL, "-P"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _mux():
    try:
        return requests.get(f"{URL}/mux.json", timeout=2).json()
    except (requests.RequestException, ValueError):
        return None


def _label(obj):
    if isinstance(obj, dict):
        return (obj.get("label") or "").strip()
    return (obj or "").strip() if isinstance(obj, str) else ""


def _ensemble_label(mux):
    return _label(((mux or {}).get("ensemble") or {}).get("label"))


def _probe(channel):
    """Nom de l'ensemble capte sur ce canal, ou None."""
    proc = _start(channel)
    try:
        deadline = time.time() + PROBE_SEC
        while time.time() < deadline:
            time.sleep(1.5)
            label = _ensemble_label(_mux())
            if label:
                return label
        return None
    finally:
        _stop(proc)


def _discover(state, budget_sec):
    now = time.time()
    todo = [c for c in CHANNELS if now - state["essayes"].get(c, 0) > RESCAN_SEC]
    done = 0
    end = time.time() + budget_sec
    for channel in todo[:PROBES_PER_VISIT]:
        if time.time() + PROBE_SEC > end:
            break
        label = _probe(channel)
        state["essayes"][channel] = time.time()
        if label:
            state["trouves"][channel] = {"ensemble": label, "t": time.time()}
            print(f"[dab_watch] ensemble {label} sur le canal {channel}")
        else:
            state["trouves"].pop(channel, None)
        done += 1
    _save_state(state)
    return done


def _say_text(channel, ensemble, service, text):
    key = f"dls|{service}|{text[:100]}"
    if not _memory.due(key, TEXT_REPEAT_SEC):
        return False
    fiche = {"protocole": "DAB+", "source": f"{service}, {ensemble}", "canal": channel, "texte_original": text}
    vd.announce(dire("dab.texte", station=service, texte=text), SOUND, "message_dab", "dab_watch", fiche=fiche)
    return True


def _say_slide(channel, ensemble, service, sid):
    try:
        image = requests.get(f"{URL}/slide/{sid}", timeout=3).content
    except requests.RequestException:
        return False
    if len(image) < 500:
        return False
    digest = hashlib.sha1(image).hexdigest()[:16]
    if not _memory.due(f"slide|{service}|{digest}", 86400):
        return False
    now = time.time()
    _slides[:] = [t for t in _slides if now - t < 3600]
    if len(_slides) >= SLIDES_PER_HOUR:
        return False
    _slides.append(now)
    folder = os.path.join(vd.ARCHIVE_DIR, "dab_images")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{time.strftime('%Y-%m-%d_%H%M%S')}_{digest}.jpg")
    with open(path, "wb") as f:
        f.write(image)
    fiche = {"protocole": "DAB+", "source": f"{service}, {ensemble}", "canal": channel, "image": path}

    def text():  # dans la file d'annonces : Gemini sans bloquer l'ecoute
        try:
            import gemini
            described, problem = gemini.ask(dire("dab.consigne_image", station=service), temperature=0.3, jpeg=image)
        except Exception as exc:
            described, problem = None, repr(exc)
        if problem or not described:
            print(f"[dab_watch] image : {problem}")
            return None  # pas de description : rien a dire (souvent de la publicite)
        return dire("dab.image", station=service, description=" ".join(described.split()))

    vd.announce(text, SOUND, "message_dab_image", "dab_watch", fiche=fiche)
    return True


def _listen(channel, ensemble, dwell_sec):
    proc = _start(channel)
    said = 0
    try:
        deadline = time.time() + dwell_sec
        while time.time() < deadline:
            time.sleep(3)
            mux = _mux()
            if not mux:
                continue
            for svc in mux.get("services") or []:
                name = _label(svc.get("label")) or str(svc.get("sid"))
                _note_station(channel, name)
                if not _kept(name):
                    continue  # station interdite (grande radio, commerciale) : rien n'est dit, aucun appel a Gemini
                dls = _label(svc.get("dls"))
                if dls and _say_text(channel, ensemble, name, dls):
                    said += 1
                mot = svc.get("mot") or {}
                if mot.get("time") or mot.get("lastchange"):
                    if _say_slide(channel, ensemble, name, svc.get("sid")):
                        said += 1
    finally:
        _stop(proc)
        _memory.save()
    return said


def _plain(text):
    import unicodedata
    return " ".join(unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().split()) + " "


def banned_stations():
    try:
        import reglages
        extra = (reglages.get("dab") or {}).get("interdites") or []
    except Exception:
        extra = []
    return DEFAULT_BANNED + [_plain(x).strip() for x in extra]


ICI_PLACES = ("saint", "st ", "loire", "lyon", "rhone", "isere", "auvergne", "drome", "ardeche", "haute",
              "pays", "paris", "provence", "alsace", "nord", "sud")


def _matches(plain, part):
    import re
    part = _plain(part).strip()
    return re.search(rf"(^| ){re.escape(part)}(?![a-z])", plain) is not None


def _kept(name):
    """Station a ecouter : tout ce qui n'est pas interdit."""
    plain = _plain(name)
    if plain.startswith("ici ") and plain[4:].startswith(ICI_PLACES):  # reseau ici (Radio France)
        return False
    return not any(_matches(plain, part) for part in banned_stations())


def _note_station(channel, name):
    """Stations vues par ensemble (pour choisir lesquelles garder)."""
    stations = _memory.data.setdefault("stations", {}).setdefault(channel, [])
    if name not in stations:
        stations.append(name)


def scan_window(dwell_sec=45):
    """Decouverte des ensembles tant qu'il reste des canaux a essayer, sinon
    ecoute d'un ensemble trouve. Rend True si quelque chose a ete dit."""
    if not _which():
        print("[dab_watch] welle-cli introuvable")
        return False
    state = _state()
    if _discover(state, dwell_sec):
        return False
    found = sorted(state["trouves"].items())
    if not found:
        return False
    channel, info = found[state.get("suivant", 0) % len(found)]
    state["suivant"] = state.get("suivant", 0) + 1
    _save_state(state)
    said = _listen(channel, info["ensemble"], dwell_sec)
    if said:
        print(f"[dab_watch] {info['ensemble']} ({channel}) : {said} message(s)")
    return said > 0


def _which():
    import shutil
    return shutil.which("welle-cli")
