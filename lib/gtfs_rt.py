"""Lecture minimale d'un flux GTFS-RT (protobuf), sans dependance
(2026-10-07 : ni protobuf ni gtfs-realtime-bindings sur le Pi, armhf).
Seuls les champs utiles aux commandes bus / tram sont decodes :

  trajets  [{"trip_id", "route_id", "annule", "retard"}]   (TripUpdate)
           retard en secondes : celui du trajet, sinon le dernier connu
           de ses arrets ; None si le flux n'en donne pas
  alertes  [{"routes": [...], "types": [...], "titre", "texte", "effet",
             "debut", "fin"}]                                (Alert)

Reference : https://gtfs.org/realtime/reference/ (numeros de champs).
"""


def _varint(buf, i):
    shift = value = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _fields(buf):
    """(numero, valeur) ; valeur = entier (varint, fixe) ou octets (longueur)."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        elif wire == 2:
            size, i = _varint(buf, i)
            value, i = buf[i:i + size], i + size
        elif wire == 5:
            value, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        else:
            raise ValueError(f"type protobuf {wire} inattendu")
        yield num, value


def _signed32(v):
    v &= 0xFFFFFFFFFFFFFFFF
    v = v - (1 << 64) if v >= 1 << 63 else v
    return v


def _text(buf):
    """TranslatedString : la traduction francaise, sinon la premiere."""
    best = None
    for num, tr in _fields(buf):
        if num != 1:
            continue
        text = lang = None
        for k, v in _fields(tr):
            if k == 1:
                text = v.decode("utf-8", "replace")
            elif k == 2:
                lang = v.decode("utf-8", "replace").lower()
        if text and (best is None or (lang or "").startswith("fr")):
            best = text
    return best or ""


def _trip(buf):
    out = {"trip_id": None, "route_id": None, "annule": False}
    for num, v in _fields(buf):
        if num == 1:
            out["trip_id"] = v.decode("utf-8", "replace")
        elif num == 5:
            out["route_id"] = v.decode("utf-8", "replace")
        elif num == 4 and v == 3:  # CANCELED
            out["annule"] = True
    return out


def _trip_update(buf):
    out = {"retard": None}
    stop_delay = None
    for num, v in _fields(buf):
        if num == 1:
            out.update(_trip(v))
        elif num == 5:
            out["retard"] = _signed32(v)
        elif num == 2 and stop_delay is None:  # premier arret a venir : arrival 2 / departure 3 -> delay 1
            delays = {}
            for k, ev in _fields(v):
                if k in (2, 3):
                    for kk, d in _fields(ev):
                        if kk == 1:
                            delays[k] = _signed32(d)
            stop_delay = delays.get(3, delays.get(2))
    if out["retard"] is None:
        out["retard"] = stop_delay
    return out


def _alert(buf):
    out = {"routes": [], "types": [], "titre": "", "texte": "", "effet": None, "debut": None, "fin": None}
    for num, v in _fields(buf):
        if num == 1:  # active_period
            for k, t in _fields(v):
                if k == 1 and (out["debut"] is None or t < out["debut"]):
                    out["debut"] = t
                elif k == 2 and (out["fin"] is None or t > out["fin"]):
                    out["fin"] = t
        elif num == 5:  # informed_entity
            for k, e in _fields(v):
                if k == 2:
                    out["routes"].append(e.decode("utf-8", "replace"))
                elif k == 3:
                    out["types"].append(e)
                elif k == 4:
                    route = _trip(e)["route_id"]
                    if route:
                        out["routes"].append(route)
        elif num == 7:
            out["effet"] = v
        elif num == 10:
            out["titre"] = _text(v)
        elif num == 11:
            out["texte"] = _text(v)
    return out


def parse(data):
    """{"horodatage", "trajets", "alertes"} d'un FeedMessage."""
    feed = {"horodatage": None, "trajets": [], "alertes": []}
    for num, v in _fields(data):
        if num == 1:  # header : timestamp 3
            for k, t in _fields(v):
                if k == 3:
                    feed["horodatage"] = t
        elif num == 2:  # entity : trip_update 3, alert 5
            for k, e in _fields(v):
                if k == 3:
                    feed["trajets"].append(_trip_update(e))
                elif k == 5:
                    feed["alertes"].append(_alert(e))
    return feed
