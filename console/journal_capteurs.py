"""Releves du journal capteurs : un element par capteur et par jour, lu a
voix haute, a partir de l'archive de TOUTES les trames recues
(server/radio_log/donnees/capteurs_<jour>.jsonl), meme celles ecartees du
bulletin meteo (cuisson, piscine, frigo, sol...). Les pneus de voitures
sont resumes en un seul element par jour.
"""
import datetime
import glob
import json
import os
import re
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

DATA_DIR = "/media/nas/nd1/server/radio_log/donnees"
PASSAGE_GAP_SEC = 10


def _rows(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _num(value, decimals=1):
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _range(values, unit):
    lo, hi = min(values), max(values)
    return f"{_num(lo)} {unit}" if lo == hi else f"de {_num(lo)} à {_num(hi)} {unit}"


def _hour(t):
    d = datetime.datetime.fromtimestamp(t)
    hour = "minuit" if d.hour == 0 else "midi" if d.hour == 12 else f"{d.hour} heures"
    return f"{hour} {d.minute:02d}"


def _sensor_text(name, rows, kind, kinds_spoken, joke):
    first, last = rows[0]["t"], rows[-1]["t"]
    parts = [f"{name} : {len(rows)} relevé{'s' if len(rows) > 1 else ''}"
             + (f", de {_hour(first)} à {_hour(last)}" if last - first > 60 else f", à {_hour(first)}")]
    temps = [r["temperature_C"] for r in rows if isinstance(r.get("temperature_C"), (int, float))]
    hums = [r["humidity"] for r in rows if isinstance(r.get("humidity"), (int, float))]
    if temps:
        parts.append("température " + _range(temps, "degrés"))
    if hums:
        parts.append("humidité " + _range(hums, "pour cent"))
    if any(r.get("battery_ok") == 0 for r in rows):
        parts.append("pile faible")
    if kind == "meteo":
        parts.append("pris en compte pour la météo locale, s'il reste plausible")
    else:
        parts.append(f"écarté du relevé météo : {kinds_spoken.get(kind, 'pas un capteur météo')}")
    text = ", ".join(parts) + "."
    if joke:
        text += " " + joke
    return text


def _tpms_key(row):
    """Meme cle que capteurs_watch._key : c'est elle que lib/voitures.py rattache aux vehicules."""
    return "|".join(str(row.get(k, "")) for k in ("model", "id", "channel"))


def _cars_text(tpms):
    """Pneus du jour -> une phrase par vehicule : marque probable, garee ou
    passages (meme regle que lib/voitures.py), pression."""
    import voitures
    data = voitures.load()
    tpms.sort(key=lambda r: r["t"])
    by_vehicle = {}
    for r in tpms:
        vid = data["ids"].get(_tpms_key(r)) or f"?{r.get('model')}"
        by_vehicle.setdefault(vid, []).append(r)
    lines = []
    for vid, rows in sorted(by_vehicle.items(), key=lambda kv: kv[1][0]["t"]):
        model = rows[0].get("model")
        desc = voitures.describe(model, len({r.get("id") for r in rows}),
                                 [r["pressure_kPa"] for r in rows if isinstance(r.get("pressure_kPa"), (int, float))])
        series, current = [], [rows[0]]
        for r in rows[1:]:
            if r["t"] - current[-1]["t"] <= voitures.PARKED_GAP_SEC:
                current.append(r)
            else:
                series.append(current)
                current = [r]
        series.append(current)
        parked = [s_ for s_ in series if s_[-1]["t"] - s_[0]["t"] >= voitures.PARKED_MIN_SEC]
        passing = len(series) - len(parked)
        name = data["vehicules"].get(vid, {}).get("nom") if not vid.startswith("?") else None
        text = (name.capitalize() + ", " if name else "") + desc
        facts = [f"garée tout près de {_hour(s_[0]['t'])} à {_hour(s_[-1]['t'])}" for s_ in parked]
        if passing:
            facts.append(f"{passing} passage{'s' if passing > 1 else ''}"
                         + (f" à {_hour(series[0][0]['t'])}" if passing == 1 and not parked else ""))
        lines.append(f"{text[0].upper()}{text[1:]} : {', '.join(facts)}")
    n = len(lines)
    return (f"Voitures : {n} véhicule{'s' if n > 1 else ''} entendu{'s' if n > 1 else ''}. "
            + ". ".join(lines) + ".")


def day_items():
    """{date: [(instant, None, texte)]} : releves par capteur, voitures."""
    try:
        import capteurs_watch
        kind_of, kinds_spoken = capteurs_watch._kind, capteurs_watch.KIND_NAMES
    except Exception:
        kind_of, kinds_spoken = (lambda event: "autre"), {}
    days = {}
    for path in glob.glob(os.path.join(DATA_DIR, "capteurs_????-??-??.jsonl")):
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})\.jsonl$", path)
        date = datetime.date(int(m[1]), int(m[2]), int(m[3]))
        jokes = {}
        for row in _rows(path.replace("capteurs_", "capteurs_gemini_")):
            if row.get("modele") and row.get("texte"):
                jokes[row["modele"]] = row["texte"]
        sensors, tpms = {}, []
        for row in _rows(path):
            if row.get("doublon") or not row.get("t"):
                continue
            kind = kind_of(row)
            if kind == "pneu":
                tpms.append(row)
                continue
            key = (row.get("model"), row.get("id"), row.get("channel"))
            sensors.setdefault(key, []).append(row)
        items = []
        for (model, sid, channel), rows in sensors.items():
            rows.sort(key=lambda r: r["t"])
            name = str(model or "capteur").replace("-", " ").replace("_", " ")
            if sid is not None:
                name += f" numéro {sid}"
            if channel is not None:
                name += f" canal {channel}"
            text = _sensor_text(name, rows, kind_of(rows[0]), kinds_spoken, jokes.get(model))
            items.append((datetime.datetime.fromtimestamp(rows[0]["t"]), None, text))
        if tpms:
            items.append((datetime.datetime.fromtimestamp(min(r["t"] for r in tpms)), None, _cars_text(tpms)))
        if items:
            days[date] = sorted(items, key=lambda it: it[0])
    return days
