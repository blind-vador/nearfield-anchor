"""Ecoute resserree quand un avion emet 7500, 7600 ou 7700.

Le parcours d'avant (mode restreint et tempo) est memorise puis rendu
quand le code n'est plus recu. On ne devine pas ce que fait l'equipage :
distance, cap, phase et trajet sont ceux des donnees deja recues
(antenne, OpenSky, fiche de vol en cache).
"""
import json
import time

# En dessous, l'exemple donne (20 km) est dedans, avec de la marge.
PROCHE_KM = 40
# On garde l'ecoute resserree un moment apres la derniere position,
# le temps qu'un trou de reception ne rende pas le parcours d'un coup.
TENIR_SEC = 600
# On ne reparle de la distance que si elle a vraiment change.
PAS_KM = 8

CIBLES_AVIATION = (
    "aviation", "milair",
    "aviation_atis", "aviation_atis_st_etienne", "aviation_st_etienne",
    "aviation_tour", "aviation_sol", "aviation_approche",
    "acars", "vdl2", "adsb",
)
CIBLES_PROCHE = CIBLES_AVIATION + ("vhf_business", "uhf_business", "pagers")

PHASES = {
    "sol": "au sol", "roulage": "en roulage", "decollage": "au décollage",
    "montee": "en montée", "croisiere": "en croisière", "palier": "en palier",
    "descente": "en descente", "approche": "en approche",
}
CAPS = ("nord", "nord est", "est", "sud est", "sud", "sud ouest", "ouest", "nord ouest")

_etat = {}


def _chemin():
    import chemins
    return chemins.base("alerte_avion.json")


def _charger():
    global _etat
    try:
        with open(_chemin(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _etat = data
    except (OSError, ValueError):
        _etat = {}


def _sauver():
    path = _chemin()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_etat, f)
        import os
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[alerte_avion] etat non ecrit : {exc}")


def _cap_mot(deg):
    if deg is None:
        return None
    try:
        deg = float(deg)
    except (TypeError, ValueError):
        return None
    return CAPS[int((deg % 360) / 45 + 0.5) % 8]


def _phase_code(info):
    try:
        import avions_info
        return avions_info.phase({
            "sol": info.get("sol"), "altitude": info.get("alt"),
            "vz": info.get("vz"), "vitesse": info.get("vitesse"),
        })
    except Exception:
        return None


def _proche(info):
    dist = info.get("dist")
    return dist is not None and dist <= PROCHE_KM


def _memoriser():
    if _etat.get("avant"):
        return
    import restreint
    import tempo
    _etat["avant"] = {
        "restreint": restreint.load(fresh=True),
        "tempo": tempo.courant(fresh=True),
    }


def _appliquer(proche):
    """Pose la restriction. Rend True si le perimetre ou le tempo change."""
    import restreint
    import tempo
    cibles = list(CIBLES_PROCHE if proche else CIBLES_AVIATION)
    voulu_tempo = "pics" if proche else (_etat.get("avant") or {}).get("tempo")
    change = False
    cur = restreint.load(fresh=True)
    if not (cur.get("actif") and set(cur.get("cibles") or []) == set(cibles)):
        restreint.save(True, cibles)
        change = True
    if voulu_tempo and tempo.courant(fresh=True) != voulu_tempo:
        tempo.regler(voulu_tempo)
        change = True
    _etat["actif"] = True
    _etat["proche"] = bool(proche)
    return change


def _trajet(info):
    if _etat.get("trajet_dit"):
        return None
    cs = (info.get("callsign") or "").strip()
    if len(cs) < 3:
        return None
    try:
        import avions_info
        rt = avions_info.route(cs)
        if not rt:
            return None
        depart = avions_info._airport(rt.get("origin"))
        arrivee = avions_info._airport(rt.get("destination"))
    except Exception as exc:
        print(f"[alerte_avion] trajet {cs} : {exc}")
        return None
    if not depart or not arrivee:
        return None
    _etat["trajet_dit"] = True
    from phrases import dire
    return dire("avion.alerte_trajet", depart=depart, arrivee=arrivee)


def ouvrir(info):
    """Premiere annonce : pose l'ecoute et renvoie les phrases a dire avec l'alarme."""
    from phrases import dire
    _memoriser()
    proche = _proche(info)
    _appliquer(proche)
    _etat["dernier"] = time.time()
    _etat["icao"] = info.get("icao")
    _etat["dist"] = info.get("dist")
    _etat["cap"] = _cap_mot(info.get("track"))
    _etat["phase"] = _phase_code(info)
    _etat["vient"] = True
    _sauver()
    parts = [dire("avion.alerte_proche" if proche else "avion.alerte_aviation")]
    if _etat["phase"] in PHASES:
        parts.append(dire("avion.alerte_phase", phase=PHASES[_etat["phase"]]))
    if _etat["cap"]:
        parts.append(dire("avion.alerte_cap", cap=_etat["cap"]))
    trajet = _trajet(info)
    if trajet:
        parts.append(trajet)
    print(f"[alerte_avion] ecoute resserree ({'proche' if proche else 'aviation'})")
    return parts


def suivre(actifs, now):
    """Maintient l'ecoute et dit un changement utile. None s'il n'y a rien a dire."""
    from phrases import dire
    if not actifs:
        if _etat.get("actif") and now - _etat.get("dernier", 0) > TENIR_SEC:
            return _relacher()
        return None
    info = min(actifs, key=lambda a: a["dist"] if a["dist"] is not None else 1e9)
    if not _etat.get("avant"):
        _memoriser()
    proche = _proche(info)
    etait = _etat.get("proche")
    _appliquer(proche)
    _etat["dernier"] = now
    _etat["icao"] = info.get("icao")
    if _etat.pop("vient", False):
        _sauver()
        return None
    parts = []
    if etait is not None and proche != etait:
        if proche:
            parts.append(dire("avion.alerte_revient_proche"))
        else:
            parts.append(dire("avion.alerte_seulement_aviation"))
    prev = _etat.get("dist")
    dist = info.get("dist")
    if prev is not None and dist is not None and abs(dist - prev) >= PAS_KM:
        key = "avion.alerte_approche" if dist < prev else "avion.alerte_eloigne"
        parts.append(dire(key, dist=format(dist, ".0f")))
        _etat["dist"] = dist
    phase = _phase_code(info)
    if phase in PHASES and phase != _etat.get("phase"):
        parts.append(dire("avion.alerte_phase", phase=PHASES[phase]))
        _etat["phase"] = phase
    cap = _cap_mot(info.get("track"))
    if cap and cap != _etat.get("cap"):
        parts.append(dire("avion.alerte_cap", cap=cap))
        _etat["cap"] = cap
    trajet = _trajet(info)
    if trajet:
        parts.append(trajet)
    _sauver()
    return " ".join(parts) or None


def _relacher():
    import restreint
    import tempo
    avant = _etat.get("avant") or {}
    try:
        r = avant.get("restreint") or {}
        restreint.save(bool(r.get("actif")), list(r.get("cibles") or []))
        if avant.get("tempo"):
            tempo.regler(avant["tempo"])
    except Exception as exc:
        print(f"[alerte_avion] parcours d'avant non rendu : {exc}")
        return None
    _etat.clear()
    _sauver()
    print("[alerte_avion] parcours d'avant rendu")
    from phrases import dire
    return dire("avion.alerte_fini")


def piqures():
    """Bonus d'attention pour les cibles de l'alerte en cours."""
    if not _etat.get("actif"):
        return {}
    noms = CIBLES_PROCHE if _etat.get("proche") else CIBLES_AVIATION
    return {nom: 3.0 for nom in noms}


_charger()
