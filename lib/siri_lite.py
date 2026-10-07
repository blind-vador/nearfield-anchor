"""Lecture des perturbations publiees en SIRI Lite (JSON), 2026-10-07 :
  general-message      messages affiches sur les ecrans (GeneralMessageDelivery)
  situation-exchange   situations perturbees (SituationExchangeDelivery)
Les autres services SIRI Lite (horaires estimes, prochains passages) ne
servent pas ici : trop lourds pour le Pi a l'echelle d'un reseau.

Rend [{"id", "titre", "texte", "lignes": [codes], "fin": horodatage ou None}].
Variantes acceptees (2026-10-07) : "InfoMessage" ou "GeneralMessage" (Bordeaux),
types "SHORT_MESSAGE" ou "shortMessage", et le SIRI en XML (flux SNCF), ramene
a la meme structure par from_xml().
Les references de ligne SIRI ("STIF:Line::C01742:", "TCL:Line::C3:LOC") sont
reduites a leur code ("C01742", "C3"), compare aux route_id du GTFS.
"""
import datetime
import xml.etree.ElementTree as ET


def _texts(value):
    """Texte d'un champ SIRI : chaine, {"value"}, ou liste de traductions (le francais d'abord)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("value") or "")
    if isinstance(value, list):
        items = [v for v in value if isinstance(v, (dict, str))]
        fr = [v for v in items if isinstance(v, dict) and str(v.get("lang", "")).lower().startswith("fr")]
        return _texts((fr or items or [None])[0])
    return ""


def line_code(ref):
    """ "STIF:Line::C01742:" -> "C01742" ; "TCL:Line::C3:LOC" -> "C3" ; "C3" -> "C3" """
    parts = [p for p in str(ref).split(":") if p]
    if len(parts) > 1:
        parts = parts[1:]  # prefixe du reseau
    parts = [p for p in parts if p.upper() not in ("LINE", "LOC")]
    return parts[0] if parts else str(ref)


def _line_refs(node, out):
    """Tous les LineRef sous un element, quelle que soit la profondeur."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "LineRef":
                for ref in v if isinstance(v, list) else [v]:
                    code = _texts(ref) if isinstance(ref, (dict, list)) else str(ref)
                    if code:
                        out.append(line_code(code))
            else:
                _line_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _line_refs(v, out)
    return out


def _time(value):
    value = _texts(value) if isinstance(value, (dict, list)) else value
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _delivery(data, name):
    service = ((data or {}).get("Siri") or {}).get("ServiceDelivery") or {}
    deliveries = service.get(name) or []
    return deliveries if isinstance(deliveries, list) else [deliveries]


def general_messages(data):
    out = []
    for delivery in _delivery(data, "GeneralMessageDelivery"):
        msgs = delivery.get("InfoMessage") or delivery.get("GeneralMessage") or []
        for msg in msgs if isinstance(msgs, list) else [msgs]:
            content = msg.get("Content") or {}
            messages = content.get("Message") or []
            messages = messages if isinstance(messages, list) else [messages]
            texts = {_texts(m.get("MessageType")).replace("_", "").lower(): _texts(m.get("MessageText"))
                     for m in messages if isinstance(m, dict)}
            short = texts.get("shortmessage") or next(iter(texts.values()), "")
            long_ = texts.get("longmessage") or ""
            out.append({"id": _texts(msg.get("InfoMessageIdentifier")) or short[:80],
                        "titre": short.strip(), "texte": long_.strip(),
                        "lignes": sorted(set(_line_refs(content, []))), "fin": _time(msg.get("ValidUntilTime"))})
    return [m for m in out if m["titre"] or m["texte"]]


def situations(data):
    out = []
    for delivery in _delivery(data, "SituationExchangeDelivery"):
        holder = delivery.get("Situations") or {}
        elements = holder.get("PtSituationElement") or [] if isinstance(holder, dict) else []
        for s in elements if isinstance(elements, list) else [elements]:
            periods = s.get("ValidityPeriod") or []
            periods = periods if isinstance(periods, list) else [periods]
            ends = [_time(p.get("EndTime")) for p in periods if isinstance(p, dict)]
            out.append({"id": _texts(s.get("SituationNumber")) or _texts(s.get("Summary"))[:80],
                        "titre": _texts(s.get("Summary")).strip(), "texte": _texts(s.get("Description")).strip(),
                        "lignes": sorted(set(_line_refs(s.get("Affects") or {}, []))),
                        "fin": max([e for e in ends if e] or [None]) if any(ends) else None})
    return [m for m in out if m["titre"] or m["texte"]]


def from_xml(text):
    """SIRI XML -> meme structure que le JSON SIRI Lite (espaces de noms
    retires ; balise repetee -> liste ; texte -> {"value", "lang"})."""
    def conv(el):
        children = list(el)
        if not children:
            out = {"value": (el.text or "").strip()}
            lang = next((v for k, v in el.attrib.items() if k.endswith("lang")), None)
            if lang:
                out["lang"] = lang
            return out
        out = {}
        for c in children:
            tag = c.tag.split("}")[-1]
            out.setdefault(tag, []).append(conv(c))
        # une seule occurrence : objet simple, sauf les listes attendues
        return {k: (v if len(v) > 1 or k in LISTS else v[0]) for k, v in out.items()}
    root = ET.fromstring(text)
    return {root.tag.split("}")[-1]: conv(root)}


LISTS = {"GeneralMessageDelivery", "SituationExchangeDelivery", "InfoMessage", "GeneralMessage", "Message",
         "PtSituationElement", "ValidityPeriod", "LineRef"}


def _plain_time(value):
    return _texts(value) if isinstance(value, (dict, list)) else value


def parse(data):
    """data : JSON deja lu, ou texte (JSON ou XML)."""
    if isinstance(data, (bytes, str)):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        if text.lstrip().startswith("<"):
            data = from_xml(text)
        else:
            import json
            data = json.loads(text)
    return general_messages(data) + situations(data)
