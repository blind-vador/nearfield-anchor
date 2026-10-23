"""Le trafic radio non vocalise, heure par heure (2026-10-23, demande
utilisateur) : tout ce que le scanner decode sans jamais le dire (DMR qui
s'enregistre aupres d'un serveur, avions ADS-B, planeurs OGN, LoRa, bipeurs
sans texte, trames VDL2...) n'est ecrit que dans les fichiers de donnees du
NAS (server/radio_log/donnees/*.jsonl). Une fois par heure, un petit resume
parle le rend visible dans le journal "donnees" : Gemini explique simplement
ce que c'est et ce que ca laisse deviner (sans rien inventer), puis ajoute un
trait d'esprit sur la situation que ca evoque.

Le resume est une prise vocale du journal "donnees" (fichier trafic_donnees_...,
voir lib/journaux.py), fabriquee SANS etre jouee : on l'ecoute quand on veut,
pas d'annonce toutes les heures. Heure sans aucun trafic : pas de prise.
Thread de meteo_watch (watch()).
"""
import collections
import glob
import json
import os
import time
from datetime import datetime, timedelta

from phrases import dire, nombre
from sound_paths import METEO_SCANER_DIR

DONNEES_DIR = "/media/nas/nd1/server/radio_log/donnees"
import sons
SOUND = sons.chemin("jingle_flashinfo", os.path.join(METEO_SCANER_DIR, "simple_message.mp3"))  # son propre (2026-10-23, sons.py)
ETIQUETTE = "trafic_donnees"
EXEMPLES = 6
# source -> (ce que c'est, pour Gemini ; champs gardes dans les exemples)
SOURCES = {
    "dmr_ars": ("DMR : enregistrements automatiques ARS de postes radio aupres d'un serveur (flotte professionnelle)",
                ["freq_hz", "poste", "serveur", "ip", "slot", "refus"]),
    "adsb_local": ("ADS-B 1090 MHz recu par l'antenne : avions", ["callsign", "icao24", "alt_m", "vrate", "squawk",
                                                                   "on_ground", "category"]),
    "helicos": ("helicopteres reperes au-dessus de la ville", ["nom", "alt_m", "distance_km"]),
    "ogn": ("OGN / FLARM / SafeSky : planeurs, ULM, petits avions et leurs relais (par internet)",
            ["from", "via", "comment"]),
    "lora": ("LoRa 433/868 MHz detecte (Meshtastic, LoRa APRS)", ["profil", "freq", "sf", "duree_s", "decode"]),
    "lora_decode": ("tentatives de decodage LoRa", ["profil", "trames", "trames_abimees", "par"]),
    "pagers": ("bipeurs POCSAG/FLEX (pompiers, hopitaux, astreintes)", ["freq", "ligne"]),
    "vdl2": ("VDL mode 2 : liaison de donnees des avions de ligne", ["freq", "sig_level", "noise_level"]),
    "aprs": ("APRS radioamateur : positions, objets, meteo", ["from", "to", "object_name", "comment"]),
    "dmr_resume": ("resumes d'activite DMR", None),
}
COURT = {"dmr_ars": "DMR", "adsb_local": "d'avions en ADS-B", "helicos": "d'hélicoptères", "ogn": "de planeurs OGN",
         "lora": "LoRa", "lora_decode": "LoRa décodées", "pagers": "de bipeurs", "vdl2": "VDL2 d'avions de ligne",
         "aprs": "APRS", "dmr_resume": "de résumé DMR"}
PROMPT = """Tu es la voix d'un poste d'écoute radio domestique, à {commune}, pour une personne
aveugle passionnée de radio. Entre {debut} et {fin}, le scanner a capté ce trafic de données,
qui n'est jamais lu à voix haute (comptes, identifiants distincts et quelques exemples) :
{donnees}

Écris un court texte à écouter, en français parlé, sans markdown, sans liste, sans salutation :
1. En 3 à 6 phrases, explique simplement ce que c'est : quels systèmes parlent, à quoi ils
   servent, et ce que ce trafic laisse deviner de ce qui se passe (une flotte qui démarre sa
   journée, un avion qui descend vers Lyon, un relais de planeurs...). Ne donne que des faits
   tirés des données ; si quelque chose est incertain, dis « sans doute ». Pas de liste de
   chiffres : seulement les plus parlants, arrondis.
2. Termine par une seule phrase qui commence par « Et pour sourire : », un trait d'esprit ou
   une petite blague bienveillante sur la situation que ce trafic évoque."""


def _fichiers(debut, fin):
    jours = {debut.strftime("%Y-%m-%d"), (fin - timedelta(seconds=1)).strftime("%Y-%m-%d")}
    for source in SOURCES:
        for jour in jours:
            chemin = os.path.join(DONNEES_DIR, f"{source}_{jour}.jsonl")
            if os.path.exists(chemin):
                yield source, chemin


def _temps(e):
    t = e.get("t")
    if isinstance(t, dict):  # dumpvdl2 : {"sec", "usec"}
        t = t.get("sec")
    if t is None and isinstance(e.get("vdl2"), dict):
        t = (e["vdl2"].get("t") or {}).get("sec")
    try:
        return float(t)
    except (TypeError, ValueError):
        return None


def _resume_source(source, entrees):
    champs = SOURCES[source][1]
    ident = {"dmr_ars": "poste", "adsb_local": "icao24", "helicos": "icao24", "ogn": "from", "aprs": "from",
             "pagers": "ligne"}.get(source)
    if source == "pagers":  # l adresse appelee, et bip seul ou message
        import re
        for e in entrees:
            m = re.search(r"Address:\s*(\d+)|\|(\d{6,})\|", e.get("ligne", ""))
            e["adresse"] = (m.group(1) or m.group(2)).lstrip("0") if m else None
            e["bip_sans_texte"] = not re.search(r"Alpha:\s*\S|Numeric:\s*\S|\|(ALN|NUM)\|\S", e.get("ligne", ""))
        ident, champs = "adresse", ["freq", "adresse", "bip_sans_texte"]
    distincts = collections.Counter(str(e.get(ident)) for e in entrees if ident and e.get(ident) is not None)
    exemples = []
    for e in entrees[:: max(1, len(entrees) // EXEMPLES)][:EXEMPLES]:
        if source == "vdl2" and isinstance(e.get("vdl2"), dict):
            v = e["vdl2"]
            e = {"freq": v.get("freq"), "sig_level": v.get("sig_level"), "noise_level": v.get("noise_level"),
                 "contenu": ", ".join(k for k in v if k not in ("app", "t", "freq", "sig_level", "noise_level", "idx"))}
        exemples.append({k: e.get(k) for k in (champs or e.keys()) if e.get(k) not in (None, "", [])})
    out = {"quoi": SOURCES[source][0], "nombre": len(entrees), "exemples": exemples}
    if distincts:
        out["identifiants_distincts"] = len(distincts)
        out["les_plus_actifs"] = [k for k, _ in distincts.most_common(4)]
    return out


def releve(fin=None):
    """{source: resume} du trafic de l'heure qui finit a `fin` (heure pile), ou {}."""
    fin = fin or datetime.now().replace(minute=0, second=0, microsecond=0)
    debut = fin - timedelta(hours=1)
    t0, t1 = debut.timestamp(), fin.timestamp()
    par_source = collections.defaultdict(list)
    for source, chemin in _fichiers(debut, fin):
        try:
            with open(chemin, encoding="utf-8", errors="replace") as f:
                for ligne in f:
                    try:
                        e = json.loads(ligne)
                    except ValueError:
                        continue
                    t = _temps(e)
                    if t is not None and t0 <= t < t1:
                        par_source[source].append(e)
        except OSError:
            continue
    return {s: _resume_source(s, rows) for s, rows in par_source.items() if rows}, debut, fin


def _heure(d):
    return {0: "minuit", 1: "1 heure", 12: "midi"}.get(d.hour, f"{d.hour} heures")


def _texte_simple(donnees, debut, fin):
    parts = [f"{nombre(r['nombre'], 'trame')} {COURT.get(s, s)}" for s, r in donnees.items()]
    return dire("trafic.simple", debut=_heure(debut), fin=_heure(fin), liste=", ".join(parts))


def texte(donnees, debut, fin):
    try:
        import gemini
        import reglages
        lieu = reglages.get("lieu") or {}
        reponse, probleme = gemini.ask(PROMPT.format(commune=lieu.get("commune", ""), debut=_heure(debut),
                                                     fin=_heure(fin), donnees=json.dumps(donnees, ensure_ascii=False,
                                                                                         default=str)[:6000]),
                                       temperature=0.9, timeout=90)
        if reponse and reponse.strip():
            return " ".join(reponse.split()), "gemini"
        print(f"[trafic_heure] Gemini : {probleme}")
    except Exception as exc:
        print(f"[trafic_heure] Gemini : {exc!r}")
    return _texte_simple(donnees, debut, fin), "simple"


def faire(fin=None):
    """Le resume de l'heure ecoulee : une prise du journal "donnees" (non jouee)."""
    import prise_vocale
    donnees, debut, fin = releve(fin)
    if not donnees:
        print(f"[trafic_heure] aucun trafic de donnees entre {debut:%H:%M} et {fin:%H:%M}")
        return None
    corps, auteur = texte(donnees, debut, fin)
    titre = dire("trafic.titre", debut=_heure(debut), fin=_heure(fin))
    chemin = prise_vocale.record(f"{titre} {corps}", SOUND, ETIQUETTE,
                                 meta={"source": "trafic de donnees", "debut": str(debut), "fin": str(fin),
                                       "auteur": auteur, "donnees": donnees}, source="donnees")
    print(f"[trafic_heure] {debut:%H:%M}-{fin:%H:%M} ({auteur}) : {corps[:160]}")
    return chemin


def watch():
    """Thread de meteo_watch : un resume a hh:02, apres le bulletin meteo."""
    while True:
        now = time.time()
        time.sleep(3600 - now % 3600 + 120)
        try:
            faire()
        except Exception as exc:
            print(f"[trafic_heure] {exc!r}")
