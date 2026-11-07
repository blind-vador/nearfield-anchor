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

Regle (utilisateur, 2026-10-25, redite le 2026-10-27) : un appel "prive" en
clair n'a rien de cache -- c'est un appel de poste a poste, adresse ainsi pour
le trafic -- il est joue et enregistre comme un appel de groupe. Seul le
CHIFFRE n'est ni joue ni enregistre (ses donnees sont notees), et rien n'est
jamais dechiffre. stats["prive"] compte les appels poste a poste.

Registre : carte SD (REGISTRY_PATH), une entree par frequence (pas de
6,25 kHz) ; journal brut des sessions sur le NAS (veille_donnees.archive).

Connexions ARS (2026-10-14) : un poste Motorola qui s'allume ou revient a
portee s'annonce au serveur de dispatch (enregistrement automatique, ARS).
Ce n'est pas un message : on en garde le poste, le serveur, l'heure, et si
le reseau a refuse (serveur injoignable, le poste reessaie alors en boucle).
Chaque connexion va dans un fichier du jour sur le NAS (dmr_ars_<jour>.jsonl),
les postes dans le registre ; une connexion recente fait revenir la veille
DMR sur ce relais (des postes qui s'allument : la journee de l'equipe
commence). Un resume de la journee est range au journal messages a 23 h 50
(daily_tick, appele par radio_watch), et dit par "dmr aujourd'hui".
"""
import chemins
import datetime
import json
import math
import os
import re
import time

REGISTRY_PATH = chemins.base("dmr_frequences.json")
STEP_HZ = 6250
HALF_LIFE_DAYS = 7
ARS_DIR = "/media/nas/nd1/server/radio_log/donnees"   # meme dossier que veille_donnees.archive
ARS_FRESH_SEC = 1800
ARS_STANDARD_SERVER = "16448250"   # identifiant du serveur ARS Motorola par defaut (IP 13.250.250.250)
SUMMARY_STATE = chemins.base("dmr_resume.json")
SUMMARY_AT = (23, 50)
import sons
from phrases import Etiquettes
SUMMARY_SOUND = sons.chemin("dmr_vocal", "/media/nas/nd1/server/radio_scaner/decoding_numeric.mp3")  # son propre (2026-10-23, sons.py)

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
        if "group" in kind.lower():  # groupe qui parle sur cet intervalle (filtres : lib/dmr_filtres.py)
            d.setdefault("en_cours", {})[slot] = tgt
            d.setdefault("parle", {})[slot] = src  # appelant (commande "ignorer", 2026-10-22)
            d["dernier_intervalle"] = slot
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
    for ev in d.get("ars", []):
        p = e.setdefault("postes_ars", {}).setdefault(ev["poste"], {"n": 0, "refus": 0, "premiere": ev["t"]})
        p["n"] += 1
        p["refus"] += 1 if ev.get("refus") else 0
        p["derniere"] = ev["t"]
        p["serveur"] = ev.get("serveur")
        e["derniere_ars"] = max(e.get("derniere_ars", 0), ev["t"])
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
    # Postes qui viennent de s'annoncer (ARS) : des appels suivent souvent.
    fresh_ars = math.exp(-max(0.0, now - e.get("derniere_ars", 0)) / ARS_FRESH_SEC)
    return (0.2 + recency) * (1 + math.log1p(e.get("sessions", 0))) * (1 + 2 * same_time) * (1.5 if e.get("relais") else 1) \
        * (1.2 if voice else 1) * (1 + fresh_ars)


def targets(now=None):
    """Frequences connues, les plus prometteuses d'abord."""
    reg = load()
    return sorted(reg.values(), key=lambda e: score(e, now), reverse=True)


def main_cc(e):
    cc = e.get("cc") or {}
    return max(cc, key=cc.get) if cc else None


NETWORK_SPAN_HZ = 5_000_000   # memes canaux d'un reseau : dans la meme zone de 5 MHz


def networks(now=None):
    """Regroupe les frequences qui appartiennent au MEME reseau DMR (ex.
    Capacity Plus qui repartit ses appels sur plusieurs canaux). Critere fiable
    (2026-10-08) : deux frequences proches (< 5 MHz) qui PARTAGENT au moins un
    groupe d'appel. Le code couleur seul ne suffit pas -- le CC 1 est partout
    (il melait a tort le relais radioamateur 145 et les relais 467). Rend
    [{"cc", "freqs_hz": [...], "relais": bool}] pour les reseaux multi-canaux."""
    entries = [e for e in load().values() if e.get("groupes")]
    parent = {e["freq_hz"]: e["freq_hz"] for e in entries}

    def find(f):
        while parent[f] != f:
            parent[f] = parent[parent[f]]
            f = parent[f]
        return f

    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if abs(a["freq_hz"] - b["freq_hz"]) < NETWORK_SPAN_HZ and set(a["groupes"]) & set(b["groupes"]):
                parent[find(a["freq_hz"])] = find(b["freq_hz"])
    clusters = {}
    for e in entries:
        root = find(e["freq_hz"])
        g = clusters.setdefault(root, {"freqs_hz": [], "cc": main_cc(e), "relais": False})
        g["freqs_hz"].append(e["freq_hz"])
        g["relais"] = g["relais"] or e.get("relais", False)
    return [g for g in clusters.values() if len(g["freqs_hz"]) > 1]


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


def ars_event(stats, packet, now=None):
    """Un poste s'annonce au serveur de dispatch (lib/decodeurs.py) : garde
    pour le registre (fin de session) et ecrit tout de suite au fichier du jour."""
    src = packet.get("src")
    if not src:
        return
    now = now or time.time()
    ev = {"t": now, "freq_hz": stats.get("freq_hz"), "poste": src, "serveur": packet.get("dst"),
          "ip": packet.get("ip"), "slot": packet.get("slot"), "refus": bool(packet.get("refus"))}
    d = stats.setdefault("dmr", {"cc": {}, "slots": [], "appels": {}, "relais": False, "systeme": None})
    d.setdefault("ars", []).append(ev)
    archive_ars(ev)


def archive_ars(ev):
    try:
        os.makedirs(ARS_DIR, exist_ok=True)
        day = datetime.date.fromtimestamp(ev["t"]).isoformat()
        with open(os.path.join(ARS_DIR, f"dmr_ars_{day}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[dmr_registre] connexion ARS non archivee : {exc}")


def ars_rows(day):
    try:
        with open(os.path.join(ARS_DIR, f"dmr_ars_{day.isoformat()}.jsonl"), encoding="utf-8") as f:
            for raw in f:
                try:
                    yield json.loads(raw)
                except ValueError:
                    continue
    except OSError:
        return


# Memes tableaux que la console (console/commands.py) : sections [jour.N] et [mois.N] de la langue.
_JOURS = Etiquettes("jour", dict(enumerate(["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
                                            "dimanche"])), "jour de la semaine, 0 = lundi")
_MOIS = Etiquettes("mois", dict(enumerate(["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
                                           "septembre", "octobre", "novembre", "décembre"], 1)), "nom du mois")


def _hm(t):
    return time.strftime("%H h %M", time.localtime(t))


def _liste(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " et " + items[-1]


def daily_summary(day):
    """Resume parle des connexions ARS d'un jour (datetime.date) ; None si aucune."""
    from phrases import dire
    rows = [r for r in ars_rows(day) if r.get("poste")]
    if not rows:
        return None
    reg = load()
    by_freq = {}
    for r in rows:
        by_freq.setdefault(key(r["freq_hz"]) if r.get("freq_hz") else "?", []).append(r)
    jour = f"{_JOURS[day.weekday()]} {day.day} {_MOIS[day.month]}"
    texts = [dire("dmr.ars_titre", jour=jour)]
    all_servers, listen, relays = set(), [], []
    for k, rs in sorted(by_freq.items(), key=lambda kv: -len(kv[1])):
        e = reg.get(k, {})
        hz = e.get("freq_hz") or (int(k) if k != "?" else None)
        mhz = fr_mhz(hz) if hz else "inconnu"
        posts = {}
        for r in rs:
            posts[r["poste"]] = posts.get(r["poste"], 0) + 1
        ts = [r["t"] for r in rs]
        refus = sum(1 for r in rs if r.get("refus"))
        texts.append(dire("dmr.ars_reseau", mhz=mhz, postes=nombre_fr(len(posts), "poste"),
                          connexions=nombre_fr(len(rs), "connexion"), debut=_hm(min(ts)), fin=_hm(max(ts))))
        top = sorted(posts.items(), key=lambda kv: -kv[1])[:6]
        texts.append(dire("dmr.ars_postes", liste=_liste(
            dire("dmr.ars_poste_fois", poste=p, fois=nombre_fr(n, "fois", "fois")) for p, n in top)))
        if refus:
            texts.append(dire("dmr.ars_refus", refus=nombre_fr(refus, "connexion refusée", "connexions refusées"))
                         if refus < len(rs) else dire("dmr.ars_refus_tous"))
        all_servers |= {r.get("serveur") for r in rs} - {None}
        speakers = []
        for p in posts:
            info = (e.get("emetteurs") or {}).get(p)
            if info and info.get("groupes"):
                g = info["groupes"][:2]
                speakers.append(dire("dmr.ars_poste_groupe", poste=p, groupe=g[0]) if len(g) == 1
                                else dire("dmr.ars_poste_groupes", poste=p, groupes=_liste(g)))
        if speakers:
            texts.append(dire("dmr.ars_parlent", liste=_liste(speakers[:5])))
        groups = sorted((e.get("groupes") or {}).items(), key=lambda kv: -kv[1].get("n", 0))[:3]
        if groups:
            cc = main_cc(e)
            listen.append(dire("dmr.ars_ecoute_cc", mhz=mhz, cc=int(cc), groupes=_liste([g for g, _ in groups])) if cc
                          else dire("dmr.ars_ecoute", mhz=mhz, groupes=_liste([g for g, _ in groups])))
        relays.append((hz, set(posts), set((e.get("groupes") or {}))))
    for i, (fa, pa, ga) in enumerate(relays):   # meme reseau : postes ET groupes en commun
        for fb, pb, gb in relays[i + 1:]:
            if fa and fb and len(pa & pb) >= 2 and ga & gb:
                texts.append(dire("dmr.ars_meme_reseau", mhz_a=fr_mhz(fa), mhz_b=fr_mhz(fb),
                                  postes=nombre_fr(len(pa & pb), "poste")))
    if all_servers == {ARS_STANDARD_SERVER}:
        texts.append(dire("dmr.ars_serveur"))
    elif all_servers:
        texts.append(dire("dmr.ars_serveurs", liste=_liste(sorted(all_servers))))
    if listen:
        texts.append(dire("dmr.ars_ecouter", liste=" ; ".join(listen)))
    return " ".join(texts)


def fr_mhz(hz):
    return f"{hz / 1e6:.4f}".replace(".", ",")


def nombre_fr(n, singulier, pluriel=None):
    from phrases import nombre
    return nombre(n, singulier, pluriel)


def daily_tick(now=None):
    """Appele par la boucle de radio_watch : a 23 h 50, range le resume du jour
    au journal messages, sans le dire (prise_vocale.record). Le dernier jour
    traite est note (SUMMARY_STATE) : un seul resume par jour."""
    now = now or datetime.datetime.now()
    if (now.hour, now.minute) < SUMMARY_AT:
        return
    day = now.date().isoformat()
    try:
        with open(SUMMARY_STATE, encoding="utf-8") as f:
            done = json.load(f).get("fait")
    except (OSError, ValueError):
        done = None
    if done == day:
        return
    with open(SUMMARY_STATE, "w", encoding="utf-8") as f:
        json.dump({"fait": day}, f)

    def work():
        try:
            text = daily_summary(now.date())
            if text:
                import prise_vocale
                prise_vocale.record(text, SUMMARY_SOUND, "message_DMR_resume",
                                    meta={"protocole": "DMR", "genre": "resume", "jour": day})
        except Exception as exc:
            print(f"[dmr_registre] resume du jour : {exc!r}")
    import threading
    threading.Thread(target=work, daemon=True).start()


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
