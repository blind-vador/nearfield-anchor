"""Arbitre des haut-parleurs, partage entre tous les services (processus
systemd separes) : priorites et comportement de chaque source, reglables
depuis la console (commande "priorites", reglages priorites.json).

Chaque source a un rang (0 = la plus prioritaire). Regle (choix de
l'utilisateur, 2026-10-17, remplace celle du 2026-10-16 qui coupait le son :
une annonce interrompue continuait a volume zero, on en perdait le contenu
et le temps reel) : rien n'est jamais coupe ni perdu, la plus prioritaire
domine et les autres passent dessous, attenuees.
  - une source plus prioritaire demarre sans attendre ; celles deja en cours
    continuent a volume reduit ("volume_attenue") puis remontent a la fin ;
  - "attend" : file d'attente (la plus prioritaire d'abord, puis l'ordre
    d'arrivee) : ne demarre que quand aucune source aussi ou plus prioritaire
    ne joue. Une moins prioritaire en cours ne la retient pas : elle passe
    dessous, attenuee ;
  - "attenue" : demarre tout de suite, attenuee tant qu'une plus prioritaire
    parle (chevauchement, comme avant le 2026-10-16) ; n'attend que la fin
    d'un son de la meme source (jamais deux fois la meme voix superposee) ;
  - "abandonne" : laisse tomber si une plus prioritaire joue deja.

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

from phrases import Etiquettes

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

SOURCES = Etiquettes("source", {
    "alertes": "alertes météo et crues",
    "console": "console",
    "trains": "trains",
    "avions": "avions",
    "radio": "radio en direct",
    "donnees": "données radio : APRS, ACARS, capteurs, pagers, planeurs",
    "bus": "bus et tram",
    "disques": "disques",
    "imprimante": "imprimante 3D",
    "helicos": "hélicoptères au-dessus de la ville",
}, "nom d'une source de son, réglage des priorités")
DEFAULT_ORDER = list(SOURCES)
DEFAULT_MODES = {s: "attend" for s in SOURCES}
DEFAULT_MODES["radio"] = "attenue"   # une capture continue en fond sous une annonce
MODES = Etiquettes("priorite", {"attend": "attend son tour", "attenue": "joue atténuée",
                                "abandonne": "abandonne si occupé"}, "comportement d'une source quand une autre parle")
DEFAULT_VOLUME = 0.3

# Script -> source, pour les appelants qui ne la precisent pas.
SCRIPT_SOURCES = {
    "radio_watch": "radio", "sncf_watch": "trains", "meteo_watch": "alertes", "crue_watch": "alertes",
    "adsb_watch": "avions", "stas_watch": "bus", "nas_watch": "disques", "main": "console",
    "imprimante_watch": "imprimante", "veille_systeme": "disques",
}

# Journal ouvert dans la console (console/journal.py, archives.py) : les
# captations radio sont alors enregistrees EN SILENCE (ni carillon, ni
# annonce, ni son en direct -- on ne saurait plus si on entend le direct ou
# le journal), puis une phrase courte previent (speak.journal_notice).
JOURNAL_FLAG = "/tmp/journal_ouvert.json"
SILENT_WHEN_JOURNAL = {"radio", "donnees"}

# Mode calibrage de l'imprimante (2026-10-16) : pendant un test guide a la
# voix, seule la console parle (les consignes ne doivent jamais etre
# couvertes). Les alertes attendent la fin du test ; les autres sources
# abandonnent ; un son deja commence passe a volume zero (rien n'est coupe).
# Le drapeau porte le pid du test : s'il meurt, le mode s'arrete tout seul.
CALIBRAGE_FLAG = "/tmp/calibrage_en_cours.json"
CALIBRAGE_MAX_SEC = 1800          # garde-fou : jamais plus de 30 min de silence
ATTEND_CALIBRAGE = {"alertes"}  # donnees : prises vocales, deja au journal (lib/prise_vocale.py)

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


def journal_open():
    try:
        with open(JOURNAL_FLAG, encoding="utf-8") as f:
            return _alive(int(json.load(f)["pid"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def set_journal_open(is_open):
    try:
        if is_open:
            with open(JOURNAL_FLAG, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid()}, f)
        else:
            os.remove(JOURNAL_FLAG)
    except OSError:
        pass


def calibrage_actif():
    try:
        with open(CALIBRAGE_FLAG, encoding="utf-8") as f:
            data = json.load(f)
        return _alive(int(data["pid"])) and time.time() - float(data["t"]) < CALIBRAGE_MAX_SEC
    except (OSError, ValueError, KeyError, TypeError):
        return False


def set_calibrage(actif):
    try:
        if actif:
            with open(CALIBRAGE_FLAG, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "t": time.time()}, f)
        else:
            os.remove(CALIBRAGE_FLAG)
    except OSError:
        pass


def prendre_sans_attendre(source):
    """Se declare en train de jouer, sans attendre : pour la console (rang 0),
    dont chaque phrase coupe le son des sources moins prioritaires."""
    ticket = Ticket(source)
    conf = settings()
    entry = {"id": ticket.id, "pid": os.getpid(), "source": source, "rang": conf["ordre"].index(source),
             "mode": conf["comportements"][source], "etat": "joue", "t": time.time()}
    with _Registry() as reg:
        reg.entries.append(entry)
    return ticket


class Ticket:
    def __init__(self, source):
        self.id = uuid.uuid4().hex
        self.source = source
        self.dropped = False
        self.silent = False
        self.gain = 1.0
        self._gain_at = 0.0
        self._applied = None  # volume du dernier morceau joue (fondu, _ramp)


def _decide(me, entries):
    """ "joue", "abandonne" ou "attend" pour l'entree me."""
    playing = [e for e in entries if e["etat"] == "joue" and e["id"] != me["id"]]
    if me["mode"] == "attenue":
        # Chevauchement attenue : seule la meme source passe avant (file).
        blockers = [p for p in playing if p["rang"] == me["rang"]]
        same_rank = [e for e in entries if e["etat"] == "attente" and e["rang"] == me["rang"]]
        first_in_line = min(same_rank, key=lambda e: e["t"])["id"] == me["id"] if same_rank else True
        return "joue" if not blockers and first_in_line else "attend"
    # Ce qui m'empeche de parler : une source aussi ou plus prioritaire en
    # cours. Une moins prioritaire en cours ne bloque pas : elle passe
    # dessous, attenuee (current_gain), le temps que je parle.
    blockers = [p for p in playing if p["rang"] <= me["rang"]]
    if me["mode"] == "abandonne" and any(b["rang"] < me["rang"] for b in blockers):
        return "abandonne"
    # File : les "attend" passent par ordre de priorite puis d'arrivee.
    waiting = sorted((e for e in entries if e["etat"] == "attente" and e.get("mode") != "attenue"),
                     key=lambda e: (e["rang"], e["t"]))
    first_in_line = not waiting or waiting[0]["id"] == me["id"]
    if not blockers and first_in_line:
        return "joue"
    return "attend"


def acquire(source=None, timeout=ACQUIRE_TIMEOUT_SEC):
    """Attend son tour selon les priorites (au plus timeout secondes, puis
    joue quand meme). Rend un Ticket a repasser a release() dans TOUS les
    cas (abandon : ticket.dropped = True)."""
    source = source if source in SOURCES else default_source()
    conf = settings()
    ticket = Ticket(source)
    if source in SILENT_WHEN_JOURNAL and journal_open():
        ticket.dropped = ticket.silent = True
        _local.ticket = ticket
        print(f"[audio_lock] journal ouvert : {source} capte en silence.")
        return ticket
    if source != "console" and calibrage_actif():
        if source in ATTEND_CALIBRAGE:
            fin_attente = time.time() + CALIBRAGE_MAX_SEC
            while calibrage_actif() and time.time() < fin_attente:
                time.sleep(1.0)
        else:
            ticket.dropped = ticket.silent = True
            _local.ticket = ticket
            print(f"[audio_lock] calibrage en cours : {source} se tait.")
            return ticket
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


def rafraichir(ticket=None):
    """Son tres long (alarme sans limite, lib/alarmes.py) : son entree n'est pas
    purgee au bout d'ENTRY_MAX_AGE_SEC, les autres sources restent dessous."""
    ticket = ticket or getattr(_local, "ticket", None)
    if ticket is None or ticket.dropped:
        return
    try:
        with _Registry() as reg:
            for entry in reg.entries:
                if entry["id"] == ticket.id:
                    entry["t"] = time.time()
    except OSError as exc:
        print(f"[audio_lock] rafraichissement impossible : {exc}")


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


def ticket_courant():
    """Ticket du fil appelant : a passer aux fils qui jouent le son pour lui
    (espeak_out relaie le PCM dans son propre fil, 2026-10-16)."""
    return getattr(_local, "ticket", None)


def current_gain(ticket=None):
    """1.0 ; "volume_attenue" (reglage, 0.3 par defaut) si une source plus
    prioritaire joue : on passe dessous sans rien perdre ; 0.0 si la source
    a abandonne ou pendant un calibrage. Relu au plus toutes les
    GAIN_REFRESH_SEC. ticket : a passer depuis un fil autre que celui qui a
    fait acquire()."""
    t = ticket or getattr(_local, "ticket", None)
    if t is None:
        return 1.0
    if t.dropped:
        return 0.0
    now = time.time()
    if now - t._gain_at >= GAIN_REFRESH_SEC:
        t._gain_at = now
        if t.source != "console" and calibrage_actif():
            t.gain = 0.0
            return t.gain
        entries = _read()
        me = next((e for e in entries if e.get("id") == t.id), None)
        above = me is not None and any(e.get("etat") == "joue" and e.get("rang", 99) < me["rang"]
                                       for e in entries)
        t.gain = max(0.0, min(1.0, float(settings()["volume_attenue"]))) if above else 1.0
    return t.gain


def _ramp(t, gain):
    """(debut, fin) du volume sur ce morceau : fondu depuis le volume du
    morceau precedent, pour que la bascule attenue/plein ne claque pas."""
    t = t or getattr(_local, "ticket", None)
    start = getattr(t, "_applied", None)
    if start is None:  # premier morceau : directement au bon volume
        start = gain
    if t is not None:
        t._applied = gain
    return start, gain


def _volume(ticket, canal):
    """Curseurs de la commande "volume" (lib/volumes.py, 2026-10-22) pour la
    source du ticket (ou du service) et ce canal."""
    t = ticket or getattr(_local, "ticket", None)
    try:
        import volumes
        return volumes.facteur(t.source if t is not None else default_source(), canal)
    except Exception:
        return 1.0


def scale(data, ticket=None, canal=None):
    """Volume courant applique a un morceau de PCM entier 16 bits : priorites
    x curseurs de volume (canal : "ecoute", "bruitages"... ; aucun = le
    premier canal de la veille, ses annonces)."""
    gain = current_gain(ticket) * _volume(ticket, canal)
    start, gain = _ramp(ticket, gain)
    if (gain == 1.0 and start == 1.0) or not data:
        return data
    data = data[: len(data) - len(data) % 2]
    if start == gain:
        try:
            return audioop.mul(data, 2, gain)
        except audioop.error:
            return data
    import numpy as np
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    samples *= np.linspace(start, gain, len(samples), dtype=np.float32)
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


def scale_float(data, ticket=None, canal=None):
    """Meme chose pour du PCM flottant 32 bits (RTLSDR-Airband)."""
    gain = current_gain(ticket) * _volume(ticket, canal)
    start, gain = _ramp(ticket, gain)
    if (gain == 1.0 and start == 1.0) or not data:
        return data
    import numpy as np
    samples = np.frombuffer(data[: len(data) - len(data) % 4], dtype=np.float32)
    samples = samples * np.linspace(start, gain, len(samples), dtype=np.float32)
    return samples.astype(np.float32).tobytes()
