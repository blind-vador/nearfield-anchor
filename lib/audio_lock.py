"""Arbitre des haut-parleurs, partage entre tous les services (processus
systemd separes) : priorites et comportement de chaque source, reglables
depuis la console (commande "priorites", reglages priorites.json).

Chaque source a un rang (0 = la plus prioritaire) et un comportement face
a une source plus prioritaire :
  attend     ne parle jamais en meme temps : file d'attente, la plus
             prioritaire passe en premier (et non la premiere arrivee) ;
  attenue    joue quand meme, a volume reduit (volume_attenue) tant qu'une
             source plus prioritaire parle ; une source plus prioritaire
             peut aussi demarrer par-dessus elle ;
  abandonne  laisse tomber si une source plus prioritaire parle deja.
Rien n'est jamais coupe en cours de route (choix de l'utilisateur,
2026-10-01).

Registre commun : REGISTRY (JSON), modifie sous un verrou court
(REGISTRY_LOCK) ; les entrees des processus morts sont purgees. Interface
inchangee pour les appelants : lock = acquire(); ... release(lock).
Le volume dynamique est applique par ceux qui jouent le son (scale() sur
chaque morceau de PCM) ; dropped() dit si la source a abandonne.
"""
import audioop
import fcntl
import json
import os
import sys
import threading
import time
import uuid

REGISTRY = "/tmp/audio_arbitre.json"
REGISTRY_LOCK = "/tmp/audio_arbitre.lock"
SETTINGS = "/media/nas/nd1/server/nearfield-anchor-settings/priorites.json"
SETTINGS_CACHE = "/home/vador/autopilot/reglages_cache/priorites.json"

# Delai maximum d'attente : au-dela on joue quand meme (2026-08-31 : un
# detenteur bloque avait fige toute la voix pendant des heures ; 300 s
# couvre le plus long son legitime du projet, 3 min 20).
ACQUIRE_TIMEOUT_SEC = 300
POLL_INTERVAL_SEC = 0.2
GAIN_REFRESH_SEC = 0.2
SETTINGS_REFRESH_SEC = 10
ENTRY_MAX_AGE_SEC = 3600

SOURCES = {
    "alertes": "alertes météo et crues",
    "console": "console",
    "trains": "trains",
    "avions": "avions",
    "radio": "radio en direct",
    "donnees": "données radio : APRS, ACARS, capteurs, pagers, planeurs",
    "bus": "bus et tram",
    "disques": "disques",
}
DEFAULT_ORDER = list(SOURCES)
DEFAULT_MODES = {s: "attend" for s in SOURCES}
DEFAULT_MODES["radio"] = "attenue"   # une capture continue en fond sous une annonce
MODES = {"attend": "attend son tour", "attenue": "joue atténuée", "abandonne": "abandonne si occupé"}
DEFAULT_VOLUME = 0.3

# Script -> source, pour les appelants qui ne la precisent pas.
SCRIPT_SOURCES = {
    "radio_watch": "radio", "sncf_watch": "trains", "meteo_watch": "alertes", "crue_watch": "alertes",
    "adsb_watch": "avions", "stas_watch": "bus", "nas_watch": "disques", "main": "console",
}

_local = threading.local()
_settings_cache = (0.0, None)


def default_source():
    name = os.path.splitext(os.path.basename(sys.argv[0] or ""))[0]
    return SCRIPT_SOURCES.get(name, "console")


def settings(fresh=False):
    """{"ordre": [...], "comportements": {...}, "volume_attenue": 0.3}"""
    global _settings_cache
    if not fresh and _settings_cache[1] is not None and time.time() - _settings_cache[0] < SETTINGS_REFRESH_SEC:
        return _settings_cache[1]
    data = None
    for path in (SETTINGS, SETTINGS_CACHE):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            break
        except (OSError, ValueError):
            continue
    data = data if isinstance(data, dict) else {}
    order = [s for s in data.get("ordre", []) if s in SOURCES]
    order += [s for s in DEFAULT_ORDER if s not in order]
    modes = dict(DEFAULT_MODES)
    modes.update({k: v for k, v in (data.get("comportements") or {}).items() if k in SOURCES and v in MODES})
    volume = data.get("volume_attenue", DEFAULT_VOLUME)
    result = {"ordre": order, "comportements": modes,
              "volume_attenue": volume if isinstance(volume, (int, float)) else DEFAULT_VOLUME}
    _settings_cache = (time.time(), result)
    return result


def save_settings(data):
    """Ecrit sur le NAS et la copie locale. False si le NAS est absent."""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    ok = True
    for path in (SETTINGS, SETTINGS_CACHE):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except OSError:
            if path == SETTINGS:
                ok = False
    settings(fresh=True)
    return ok


# --- registre ------------------------------------------------------------------

def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _Registry:
    def __enter__(self):
        self.lock = open(REGISTRY_LOCK, "a")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            with open(REGISTRY, encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, ValueError):
            entries = []
        now = time.time()
        self.entries = [e for e in entries if isinstance(e, dict) and _alive(e.get("pid", 0))
                        and now - e.get("t", now) < ENTRY_MAX_AGE_SEC]
        return self

    def __exit__(self, *exc):
        try:
            tmp = REGISTRY + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.entries, f)
            os.replace(tmp, REGISTRY)
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()


def _read():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


class Ticket:
    def __init__(self, source):
        self.id = uuid.uuid4().hex
        self.source = source
        self.dropped = False
        self.gain = 1.0
        self._gain_at = 0.0


def _decide(me, entries):
    """ "joue", "abandonne" ou "attend" pour l'entree me."""
    playing = [e for e in entries if e["etat"] == "joue" and e["id"] != me["id"]]
    # Ce qui m'empeche de parler : tout ce qui joue, sauf une source moins
    # prioritaire en mode attenue (elle baissera le volume pour moi).
    blockers = [p for p in playing if not (p["rang"] > me["rang"] and p["mode"] == "attenue")]
    waiting = sorted((e for e in entries if e["etat"] == "attente"), key=lambda e: (e["rang"], e["t"]))
    first_in_line = not waiting or waiting[0]["id"] == me["id"]
    if not blockers and first_in_line:
        return "joue"
    higher_playing = [b for b in blockers if b["rang"] < me["rang"]]
    if me["mode"] == "attenue" and blockers and len(higher_playing) == len(blockers):
        return "joue"          # sous une source plus prioritaire, volume reduit
    if me["mode"] == "abandonne" and higher_playing:
        return "abandonne"
    return "attend"


def acquire(source=None, timeout=ACQUIRE_TIMEOUT_SEC):
    """Attend son tour selon les priorites (au plus timeout secondes, puis
    joue quand meme). Rend un Ticket a repasser a release() dans TOUS les
    cas (abandon : ticket.dropped = True)."""
    source = source if source in SOURCES else default_source()
    conf = settings()
    ticket = Ticket(source)
    me = {"id": ticket.id, "pid": os.getpid(), "source": source, "rang": conf["ordre"].index(source),
          "mode": conf["comportements"][source], "etat": "attente", "t": time.time()}
    deadline = time.time() + timeout
    while True:
        with _Registry() as reg:
            entry = next((e for e in reg.entries if e["id"] == me["id"]), None)
            if entry is None:
                entry = dict(me)
                reg.entries.append(entry)
            verdict = _decide(entry, reg.entries)
            if verdict == "attend" and time.time() >= deadline:
                print(f"[audio_lock] attente de {timeout:.0f}s depassee pour {source} : on joue quand meme.")
                verdict = "joue"
            if verdict == "joue":
                entry["etat"] = "joue"
                entry["t"] = time.time()
            elif verdict == "abandonne":
                reg.entries.remove(entry)
                ticket.dropped = True
        if verdict != "attend":
            break
        time.sleep(POLL_INTERVAL_SEC)
    _local.ticket = ticket
    if ticket.dropped:
        print(f"[audio_lock] {source} abandonne : une source plus prioritaire parle.")
    return ticket


def release(ticket):
    if ticket is None:
        return
    if getattr(_local, "ticket", None) is ticket:
        _local.ticket = None
    if ticket.dropped:
        return
    try:
        with _Registry() as reg:
            reg.entries = [e for e in reg.entries if e["id"] != ticket.id]
    except OSError as exc:
        print(f"[audio_lock] liberation impossible : {exc}")


# --- volume dynamique, pour ceux qui jouent le son -------------------------------

def dropped():
    t = getattr(_local, "ticket", None)
    return bool(t and t.dropped)


def current_gain(ticket=None):
    """1.0 ; volume_attenue si une source plus prioritaire parle par-dessus ;
    0.0 si la source a abandonne. Relu au plus toutes les GAIN_REFRESH_SEC.
    ticket : a passer depuis un fil autre que celui qui a fait acquire()."""
    t = ticket or getattr(_local, "ticket", None)
    if t is None:
        return 1.0
    if t.dropped:
        return 0.0
    now = time.time()
    if now - t._gain_at >= GAIN_REFRESH_SEC:
        t._gain_at = now
        entries = _read()
        me = next((e for e in entries if e.get("id") == t.id), None)
        above = me is not None and any(e.get("etat") == "joue" and e.get("rang", 99) < me["rang"]
                                       for e in entries)
        t.gain = settings()["volume_attenue"] if above else 1.0
    return t.gain


def scale(data, ticket=None):
    """Volume courant applique a un morceau de PCM entier 16 bits."""
    gain = current_gain(ticket)
    if gain == 1.0 or not data:
        return data
    try:
        return audioop.mul(data[: len(data) - len(data) % 2], 2, gain)
    except audioop.error:
        return data


def scale_float(data, ticket=None):
    """Meme chose pour du PCM flottant 32 bits (RTLSDR-Airband)."""
    gain = current_gain(ticket)
    if gain == 1.0 or not data:
        return data
    import numpy as np
    samples = np.frombuffer(data[: len(data) - len(data) % 4], dtype=np.float32) * gain
    return samples.astype(np.float32).tobytes()
