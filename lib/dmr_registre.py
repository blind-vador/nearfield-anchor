"""Registre DMR (2026-10-07) : ce que le scanner apprend des frequences DMR
captees, pour y revenir methodiquement (fenetre "dmr" de radio_watch) et
le raconter (commande console "dmr").

Lu dans la sortie de dsd-fme (lib/decodeurs.py, dsd_line -> line()) :
  code couleur (CC)   0 a 15 : "cle" d'un relais ; les postes d'un meme
                      reseau ont le meme, il separe deux reseaux voisins
  intervalle (slot)   le DMR partage la frequence en 2 intervalles de temps :
                      deux conversations a la fois
  groupe (TGT)        groupe d'appel : equipe, service, canal logique
  emetteur (SRC)      numero de la radio qui parle
  appel de groupe / appel prive (d'un poste a un autre) / donnees
  chiffre             contenu chiffre : jamais dechiffre, rien n'est joue
  relais              le relais emet en continu pendant un appel, intervalle
                      libre compris (IDLE), et donne des nouvelles d'activite
                      (Activity Update, SLCO)

Regle (utilisateur) : pas d'audio prive, seulement des signaux ouverts,
aucun dechiffrement. Un appel prive ou chiffre n'est ni joue ni enregistre :
seules ses donnees sont notees (stats["prive"], stats["chiffre"]).

Registre : carte SD (REGISTRY_PATH), une entree par frequence (pas de
6,25 kHz) ; journal brut des sessions sur le NAS (veille_donnees.archive).
"""
import json
import math
import os
import re
import time

REGISTRY_PATH = "/home/vador/autopilot/dmr_frequences.json"
STEP_HZ = 6250
HALF_LIFE_DAYS = 7

CC_RE = re.compile(r"Color Code=(\d{1,2})")
SLOT_RE = re.compile(r"\[SLOT([12])\]")
CALL_RE = re.compile(r"SLOT ([12]) TGT=(\d+) SRC=(\d+)\s+([A-Za-z ]*?Call)")
ENC_RE = re.compile(r"\b(ENC|Encrypt\w*|ALG ID|Key ID|KID)\b", re.I)
REPEATER_RE = re.compile(r"Activity Update|SLCO|\bIDLE\b|TS1:.*TS2:")
SYSTEM_RE = re.compile(r"(Capacity Plus|Connect Plus|Linked Capacity Plus|Tier III|XPT|Hytera)", re.I)


def key(freq_hz):
    return str(int(round(freq_hz / STEP_HZ) * STEP_HZ))


def line(stats, text):
    """Enrichit stats d'une ligne de dsd-fme (appele par decodeurs.dsd_line)."""
    d = stats.setdefault("dmr", {"cc": {}, "slots": [], "appels": {}, "relais": False, "systeme": None})
    m = CC_RE.search(text)
    if m and "ERR" not in text and m.group(1) != "XX":
        d["cc"][m.group(1)] = d["cc"].get(m.group(1), 0) + 1
    for s in SLOT_RE.findall(text):
        if s not in d["slots"]:
            d["slots"].append(s)
    m = CALL_RE.search(text)
    if m:
        slot, tgt, src, kind = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        call = d["appels"].setdefault((slot, tgt, src), {"type": kind, "n": 0})
        call["n"] += 1
        if "private" in kind.lower():
            stats["prive"] = True
        if "data" in kind.lower():
            call["donnees"] = True
    if ENC_RE.search(text):
        stats["chiffre"] = True
    if REPEATER_RE.search(text):
        d["relais"] = True
    m = SYSTEM_RE.search(text)
    if m:
        d["systeme"] = m.group(1)


def load():
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(reg):
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False)
    os.replace(tmp, REGISTRY_PATH)


def record(stats, now=None):
    """Fin d'une session dsd-fme validee en DMR : met le registre a jour.
    Rend {"nouvelle_frequence": bool, "nouveaux_groupes": [...]}."""
    d = stats.get("dmr")
    freq = stats.get("freq_hz")
    if not d or not freq:
        return {"nouvelle_frequence": False, "nouveaux_groupes": []}
    now = now or time.time()
    reg = load()
    k = key(freq)
    new_freq = k not in reg
    e = reg.setdefault(k, {"freq_hz": freq, "premiere": now, "sessions": 0, "cc": {}, "groupes": {}, "emetteurs": {},
                           "relais": False, "systeme": None, "prive": 0, "chiffre": 0, "heures": [0] * 24})
    e["derniere"] = now
    e["sessions"] += 1
    e["freq_hz"] = round((e["freq_hz"] * (e["sessions"] - 1) + freq) / e["sessions"])  # centre mesure, moyenne
    for cc, n in d["cc"].items():
        e["cc"][cc] = e["cc"].get(cc, 0) + n
    e["relais"] = e["relais"] or d["relais"]
    e["systeme"] = d["systeme"] or e["systeme"]
    e["heures"][time.localtime(now).tm_hour] += 1
    e["prive"] += 1 if stats.get("prive") else 0
    e["chiffre"] += 1 if stats.get("chiffre") else 0
    new_groups = []
    for (slot, tgt, src), call in d["appels"].items():
        g = e["groupes"].setdefault(tgt, {"n": 0, "emetteurs": [], "slots": [], "type": call["type"]})
        if g["n"] == 0:
            new_groups.append(tgt)
        g["n"] += 1
        g["derniere"] = now
        if src not in g["emetteurs"]:
            g["emetteurs"].append(src)
        if slot not in g["slots"]:
            g["slots"].append(slot)
        s = e["emetteurs"].setdefault(src, {"n": 0, "groupes": []})
        s["n"] += 1
        s["derniere"] = now
        if tgt not in s["groupes"]:
            s["groupes"].append(tgt)
    _save(reg)
    return {"nouvelle_frequence": new_freq, "nouveaux_groupes": new_groups}


def score(e, now=None):
    """Interet d'y retourner : sessions recentes (demi-vie 7 jours), bonus
    a l'heure ou elle a deja ete active, bonus relais (il revient sur la
    meme frequence) ; jamais nul (exploration)."""
    now = now or time.time()
    age_days = (now - e.get("derniere", now)) / 86400
    recency = math.exp(-age_days * math.log(2) / HALF_LIFE_DAYS)
    hours = e.get("heures") or [0] * 24
    h = time.localtime(now).tm_hour
    same_time = (hours[h] + 0.5 * (hours[(h - 1) % 24] + hours[(h + 1) % 24])) / max(1, sum(hours))
    voice = sum(1 for g in (e.get("groupes") or {}).values() if g.get("type", "").lower().startswith("group"))
    return (0.2 + recency) * (1 + math.log1p(e.get("sessions", 0))) * (1 + 2 * same_time) * (1.5 if e.get("relais") else 1) \
        * (1.2 if voice else 1)


def targets(now=None):
    """Frequences connues, les plus prometteuses d'abord."""
    reg = load()
    return sorted(reg.values(), key=lambda e: score(e, now), reverse=True)


def main_cc(e):
    cc = e.get("cc") or {}
    return max(cc, key=cc.get) if cc else None


def networks(now=None):
    """Regroupe les frequences par code couleur dominant : un meme reseau DMR
    (ex. Capacity Plus) occupe plusieurs frequences avec le MEME code couleur.
    Rend [{"cc", "freqs_hz": [...], "relais": bool}] pour les codes vus sur
    plus d'une frequence (2026-10-08)."""
    groups = {}
    for e in load().values():
        cc = main_cc(e)
        if cc is None:
            continue
        g = groups.setdefault(cc, {"cc": cc, "freqs_hz": [], "relais": False})
        g["freqs_hz"].append(e["freq_hz"])
        g["relais"] = g["relais"] or e.get("relais", False)
    return [g for g in groups.values() if len(g["freqs_hz"]) > 1]


def resolve_callsign(src):
    """Indicatif + ville d'un emetteur DMR radioamateur (>= 6 chiffres) via
    radioid.net ; None pour les petits identifiants (relais commercial) ou si
    inconnu. Jamais le nom de la personne (lib/radioamateurs.py)."""
    try:
        import radioamateurs
        return radioamateurs.dmr_id(src)
    except Exception as exc:
        print(f"[dmr_registre] radioid {src} : {exc!r}")
        return None


def seed(lines):
    """Relit une sortie passee de radio_watch (journal systeme, "horodatage
    texte" par ligne) : chaque session dsd-fme commence a "Dev 0 Freq N".
    Pour amorcer le registre avec les captures d'avant sa creation. Les
    annonces de textes transportes doivent etre neutralisees par l'appelant."""
    import decodeurs
    stats, last_t, done = None, None, 0

    def close():
        nonlocal done
        if stats and decodeurs.dsd_validated(stats) and stats.get("protocol") == "DMR" and stats.get("dmr"):
            record(stats, now=last_t)
            done += 1
    for raw in lines:
        try:
            stamp, text = raw.split(" ", 1)
            t = float(stamp)
        except ValueError:
            continue
        m = re.search(r"Dev 0 Freq (\d+)", text)
        if m:
            close()
            stats, last_t = decodeurs.new_stats(int(m.group(1))), t
            continue
        if stats is not None and "dsd-fme: " in text:
            last_t = t
            decodeurs.dsd_line(stats, text.split("dsd-fme: ", 1)[1])
    close()
    return done
