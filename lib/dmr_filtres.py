"""Ciblage DMR (demande utilisateur 2026-10-17 : la veille restait bloquee
sur un groupe bavard pendant que des appels plus urgents passaient ailleurs).
Partage entre le scanner (watchers/radio_watch.py), l'ecoute manuelle
(lib/manual_tuner.py) et le dialogue "exploration dmr" de la console.

  groupe ignore      ni joue ni enregistre, et ne retient plus le scanner
                     (seules les paroles audibles prolongent l'ecoute) ;
  groupe favori      seul entendu quand le mode cible est allume ;
  frequence ignoree  la veille DMR n'y retourne plus ; favorite : en tete ;
  focus              groupe ecoute dans l'exploration (les autres se taisent) ;
  duree max          une ecoute DMR ne depasse jamais ca, meme si ca parle.

Le DMR partage la frequence en deux intervalles (slots) : deux
conversations a la fois. Le filtre coupe l'intervalle du groupe ignore et
laisse entendre l'autre.

Fichier REGLAGES (carte SD), relu au plus toutes les CACHE_SEC.
"""
import chemins
import json
import os
import time

REGLAGES = chemins.base("dmr_filtres.json")
SEARCH_FLAG = "/tmp/dmr_recherche.json"
CACHE_SEC = 2
DUREES = [60, 120, 300]
DEFAULT = {"groupes": {}, "frequences": {}, "seulement_favoris": False, "duree_max_s": 120,
           "focus": None, "exploration": False}
STEP_HZ = 6250
_cache = {"t": 0.0, "data": None}


def load(fresh=False):
    if fresh or _cache["data"] is None or time.time() - _cache["t"] > CACHE_SEC:
        data = dict(DEFAULT)
        try:
            with open(REGLAGES, encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                data.update(saved)
        except (OSError, ValueError):
            pass
        _cache.update(t=time.time(), data=data)
    return _cache["data"]


def _save(data):
    tmp = REGLAGES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, REGLAGES)
    _cache.update(t=time.time(), data=data)


def _update(**changes):
    data = dict(load(fresh=True))
    data.update(changes)
    _save(data)
    return data


def freq_key(freq_hz):
    return str(int(round(freq_hz / STEP_HZ) * STEP_HZ))


# --- etats -----------------------------------------------------------------------------

def group_state(tg):
    return load()["groupes"].get(str(tg)) if tg is not None else None


def freq_state(freq_hz):
    return load()["frequences"].get(freq_key(freq_hz))


def set_group(tg, state):
    """state : "ignore", "favori" ou None (neutre)."""
    groups = dict(load(fresh=True)["groupes"])
    if state:
        groups[str(tg)] = state
    else:
        groups.pop(str(tg), None)
    _update(groupes=groups)


def set_freq(freq_hz, state):
    freqs = dict(load(fresh=True)["frequences"])
    if state:
        freqs[freq_key(freq_hz)] = state
    else:
        freqs.pop(freq_key(freq_hz), None)
    _update(frequences=freqs)


def set_targeted(on):
    _update(seulement_favoris=bool(on))


def targeted():
    return bool(load()["seulement_favoris"])


def max_duration():
    try:
        return max(30, int(load()["duree_max_s"]))
    except (TypeError, ValueError):
        return DEFAULT["duree_max_s"]


def next_duration():
    """Duree max suivante dans DUREES (touche D de l'exploration)."""
    now = max_duration()
    following = next((d for d in DUREES if d > now), DUREES[0])
    _update(duree_max_s=following)
    return following


def set_focus(tg):
    _update(focus=str(tg) if tg is not None else None)


def set_exploration(on):
    _update(exploration=bool(on), focus=None if not on else load(fresh=True)["focus"])


def exploring():
    return bool(load()["exploration"])


# --- decision, pour ceux qui jouent le son -----------------------------------------------

def audible(tg):
    """Ce groupe peut-il etre entendu ? tg None (pas encore identifie) :
    oui, sauf focus ou mode cible (on attend de savoir qui parle)."""
    data = load()
    try:  # commande "ignorer" (2026-10-22) : groupe mis de cote pour une duree
        import ignores
        if ignores.groupe_ignore(tg):
            return False
    except Exception:
        pass
    if data.get("focus"):
        return tg is not None and str(tg) == data["focus"]
    state = group_state(tg)
    if state == "ignore":
        return False
    if data.get("seulement_favoris"):
        return state == "favori"
    return True


def slot_gains(stats):
    """(gain intervalle 1, gain intervalle 2, quelque chose d'audible) d'apres
    les appels en cours vus par dsd-fme (dmr_registre.line -> "en_cours")."""
    current = ((stats or {}).get("dmr") or {}).get("en_cours") or {}
    parle = ((stats or {}).get("dmr") or {}).get("parle") or {}
    if not current:
        ok = audible(None)
        return (1.0, 1.0, ok) if ok else (0.0, 0.0, False)
    try:
        import ignores
        muet = ignores.appelant_ignore
    except Exception:
        muet = lambda src: False  # noqa: E731

    def ok(slot):
        return audible(current[slot]) and not muet(parle.get(slot))
    gains = []
    for slot in ("1", "2"):
        gains.append(1.0 if (slot not in current or ok(slot)) else 0.0)
    heard = any(ok(slot) for slot in current)
    return gains[0], gains[1], heard


def apply(data, stats, channels):
    """PCM 16 bits de dsd-fme filtre : intervalle d'un groupe ignore coupe.
    Rend (pcm, audible)."""
    import audioop
    g1, g2, heard = slot_gains(stats)
    if not heard:
        return b"", False
    if channels == 2 and (g1, g2) != (1.0, 1.0):
        data = data[: len(data) - len(data) % 4]
        left = audioop.tostereo(audioop.tomono(data, 2, 1, 0), 2, g1, 0)
        right = audioop.tostereo(audioop.tomono(data, 2, 0, 1), 2, 0, g2)
        data = audioop.add(left, right, 2)
    return data, True


# --- recherche de nouvelles frequences (touche R de l'exploration) -----------------------

def request_search():
    with open(SEARCH_FLAG, "w", encoding="utf-8") as f:
        json.dump({"t": time.time()}, f)


def search_requested():
    try:
        with open(SEARCH_FLAG, encoding="utf-8") as f:
            return time.time() - json.load(f)["t"] < 600
    except (OSError, ValueError, KeyError):
        return False


def search_done():
    try:
        os.remove(SEARCH_FLAG)
    except OSError:
        pass
