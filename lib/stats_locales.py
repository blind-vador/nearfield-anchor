"""Statistiques locales des capteurs, sur la carte SD du Pi (demande
utilisateur 2026-10-02) : ce qui compte, c'est le long terme (moyennes,
extremes, habitudes) vrai pour CETTE machine, son antenne, sa piece. Les
releves bruts (server/radio_log/donnees/capteurs_<jour>.jsonl) servent au
journal capteurs pendant 48 heures ; ensuite chaque jour est resume ici,
puis ses releves bruts sont effaces.

  STATS_PATH : {"capteurs": {cle: {nom, type, premier, dernier, blague,
                 jours: {date: {n, t_min, t_max, t_moy, h_min, h_max, h_moy,
                 pile_faible}}}}, "jours_resumes": [dates]}

Les voitures ont leur propre etat local (lib/voitures.py).
"""
import chemins
import datetime
import glob
import json
import os
import re
import time

DATA_DIR = "/media/nas/nd1/server/radio_log/donnees"
STATS_PATH = chemins.base("stats_locales/capteurs.json")
RAW_KEEP_HOURS = 48
CHECK_EVERY_SEC = 6 * 3600


def load():
    try:
        with open(STATS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"capteurs": {}, "jours_resumes": []}


def _save(stats):
    os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
    tmp = STATS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False)
    os.replace(tmp, STATS_PATH)


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


def _agg(values):
    return (round(min(values), 1), round(max(values), 1), round(sum(values) / len(values), 1)) if values else (None,) * 3


def summarize_day(path, stats):
    """Ajoute le resume d'un fichier de releves du jour aux statistiques."""
    try:
        import capteurs_watch
        kind_of = capteurs_watch._kind
    except Exception:
        kind_of = lambda event: "autre"
    day = re.search(r"(\d{4}-\d{2}-\d{2})\.jsonl$", path)[1]
    jokes = {r["modele"]: r["texte"] for r in _rows(path.replace("capteurs_", "capteurs_gemini_"))
             if r.get("modele") and r.get("texte")}
    groups = {}
    for row in _rows(path):
        if row.get("doublon") or not row.get("t"):
            continue
        key = "|".join(str(row.get(k, "")) for k in ("model", "id", "channel"))
        groups.setdefault(key, []).append(row)
    for key, rows in groups.items():
        kind = kind_of(rows[0])
        if kind == "pneu":
            continue  # voitures : lib/voitures.py
        temps = [r["temperature_C"] for r in rows if isinstance(r.get("temperature_C"), (int, float))]
        hums = [r["humidity"] for r in rows if isinstance(r.get("humidity"), (int, float))]
        t_min, t_max, t_moy = _agg(temps)
        h_min, h_max, h_moy = _agg(hums)
        entry = stats["capteurs"].setdefault(key, {"nom": str(rows[0].get("model", "capteur")), "type": kind,
                                                   "premier": rows[0]["t"], "jours": {}})
        entry["dernier"] = max(entry.get("dernier", 0), rows[-1]["t"])
        if rows[0].get("model") in jokes:
            entry["blague"] = jokes[rows[0]["model"]]
        entry["jours"][day] = {"n": len(rows), "t_min": t_min, "t_max": t_max, "t_moy": t_moy,
                               "h_min": h_min, "h_max": h_max, "h_moy": h_moy,
                               "pile_faible": any(r.get("battery_ok") == 0 for r in rows)}
    stats["jours_resumes"] = sorted(set(stats["jours_resumes"]) | {day})


def consolidate():
    """Resume puis efface les jours de releves de plus de RAW_KEEP_HOURS."""
    cutoff = datetime.date.today() - datetime.timedelta(hours=RAW_KEEP_HOURS)
    stats = load()
    done = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "capteurs_????-??-??.jsonl"))):
        day = datetime.date.fromisoformat(re.search(r"(\d{4}-\d{2}-\d{2})\.jsonl$", path)[1])
        if day >= cutoff:
            continue
        summarize_day(path, stats)
        _save(stats)  # le resume est sur la carte SD AVANT d'effacer le brut
        for raw in (path, path.replace("capteurs_", "capteurs_gemini_")):
            try:
                os.remove(raw)
            except OSError:
                pass
        done.append(day.isoformat())
    if done:
        print(f"[stats_locales] jours resumes puis releves bruts effaces : {', '.join(done)}")
    return done


def watch():
    while True:
        try:
            consolidate()
        except Exception as exc:
            print(f"[stats_locales] {exc!r}")
        time.sleep(CHECK_EVERY_SEC)
