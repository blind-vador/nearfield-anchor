"""Le trafic radio non vocalise, heure par heure (2026-10-23, demande
utilisateur) : tout ce que le scanner decode sans le dire trame par trame
(DMR qui s'enregistre, avions, planeurs, LoRa, bipeurs sans texte, VDL2,
signalisation, AIS, sondes, RDS...) est ecrit dans les fichiers de donnees
du NAS (server/radio_log/donnees/*.jsonl). Une fois par heure, un petit
resume parle le rend visible dans le journal "donnees". La commande flash
reprend le meme resume, de minuit jusqu'a l'instant ou elle est entree.

On ouvre tous les fichiers de l'intervalle, pas une liste fermee : un
decodeur nouveau entre dans le resume des qu'il archive. Les textes deja
dits ailleurs (bulletin meteo, veille terrain, reformulations) restent
dehors, pour ne pas raconter deux fois la meme chose.
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
SOUND = sons.chemin("jingle_flashinfo", os.path.join(METEO_SCANER_DIR, "simple_message.mp3"))
ETIQUETTE = "trafic_donnees"
EXEMPLES = 6
# Deja une parole a part : le resume de donnees ne les reprend pas.
DEJA_DIT = {"aprs_gemini", "capteurs", "capteurs_gemini", "meteo_capteurs",
            "terrain", "filtre_pollution", "vdl2_traduit"}
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
    "tetra": ("reseaux TETRA professionnels : identite reseau en clair, appels reperes dans la signalisation, messages d'exploitation en clair (bus, logistique...) -- jamais rien de dechiffre", None),
    "signalisation": ("salves courtes dans l'audio analogique : FleetSync, MDC1200, MPT1327, ASN, souvent un identifiant de flotte sans conversation",
                      ["freq_hz", "canal", "texte"]),
    "ais": ("navires AIS : position et identite, trop repetitif pour etre dit a chaque trame",
            ["freq_hz", "mmsi", "texte"]),
    "asn": ("appels selectifs maritimes ASN", ["freq_hz", "texte"]),
    "rds": ("radios FM : nom de station et radiotexte RDS, meme quand rien n'a ete dit",
            ["freq_hz", "ps", "radiotext", "groupes"]),
    "sondes": ("radiosondes meteo : position, altitude, temperature, dites seulement si le ballon eclate ou passe le zero",
               ["serie", "alt", "temp", "lat", "lon"]),
    "sstv": ("images SSTV recues", ["mode", "complete", "bande"]),
    "balises": ("balises de detresse 406 megahertz, le decodage brut (l'alarme est dite a part)", ["sortie"]),
    "donnees_prise": ("trames de donnees lues sur le flux du scanner : navires AIS, alertes EAS",
                      ["freq_hz", "mode", "texte", "bande"]),
    "gsm_r": ("radio des trains, bande montante : un poste vient d'emettre. Energie seulement, le contenu est chiffre et n'est pas ouvert",
              ["freq_hz", "note"]),
}
COURT = {"dmr_ars": "DMR", "adsb_local": "d'avions en ADS-B", "helicos": "d'hélicoptères", "ogn": "de planeurs OGN",
         "lora": "LoRa", "lora_decode": "LoRa décodées", "pagers": "de bipeurs", "vdl2": "VDL2 d'avions de ligne",
         "aprs": "APRS", "dmr_resume": "de résumé DMR", "tetra": "TETRA",
         "signalisation": "de signalisation", "ais": "de navires AIS", "asn": "d'appels ASN",
         "rds": "RDS", "sondes": "de radiosondes", "sstv": "d'images SSTV", "balises": "de balises",
         "donnees_prise": "de trames de données", "gsm_r": "de postes radio de train"}
PROMPT = """Tu es la voix d'un poste d'écoute radio domestique, à {commune}, pour une personne
aveugle passionnée de radio. Tu expliques simplement, et une remarque amusée te vient
parfois au milieu d'une phrase, comme à quelqu'un assis à côté de toi.

Entre {debut} et {fin}, le scanner a capté ce trafic de données, qui n'est pas lu trame
par trame (comptes, identifiants distincts et quelques exemples) :
{donnees}

Écris un court texte à écouter, en français parlé, sans markdown, sans liste, sans
salutation, en 4 à 7 phrases. Quels systèmes parlent, à quoi ils servent, et ce que ce
trafic laisse deviner (une flotte qui démarre sa journée, un avion qui descend, un relais
de planeurs). Ne donne que des faits tirés des données ; si quelque chose est incertain,
dis « sans doute ». Pas de liste de chiffres : seulement les plus parlants, arrondis.
"""


def _jours(debut, fin):
    jour = debut.date()
    dernier = (fin - timedelta(microseconds=1)).date()
    while jour <= dernier:
        yield jour.isoformat()
        jour += timedelta(days=1)


def _fichiers(debut, fin):
    """Tous les journaux de donnees des jours touches, sauf ce qui est deja dit ailleurs."""
    for jour in _jours(debut, fin):
        suffixe = f"_{jour}.jsonl"
        for chemin in sorted(glob.glob(os.path.join(DONNEES_DIR, f"*{suffixe}"))):
            nom = os.path.basename(chemin)
            if not nom.endswith(suffixe):
                continue
            source = nom[:-len(suffixe)]
            if source and source not in DEJA_DIT:
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


def _valeur(v):
    if isinstance(v, str) and len(v) > 180:
        return v[:180] + "…"
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False, default=str)
        return (s[:180] + "…") if len(s) > 180 else v
    return v


def _resume_tetra(entrees):
    """Resume horaire du TETRA : agrege les fenetres d'ecoute de l'heure. Tout
    en clair (identite reseau, comptes d'appels, messages d'exploitation
    decodes) ; jamais rien de dechiffre."""
    reseaux = collections.Counter()
    porteuses = set()
    appels = clairs = suivis = sds = postes_max = 0
    voix = False
    textes = []
    for e in entrees:
        r = e.get("reseau") or {}
        if r.get("mcc") is not None:
            reseaux["%s-%s" % (r.get("mcc"), r.get("mnc"))] += 1
        if r.get("freq_hz"):
            porteuses.add(round(r["freq_hz"] / 1e6, 4))
        n = e.get("nouvelles") or {}
        appels += n.get("appels") or 0
        clairs += n.get("appels_clairs") or 0
        suivis += n.get("suivis") or 0
        sds += n.get("sds") or 0
        postes_max = max(postes_max, n.get("postes_actifs") or 0)
        voix = voix or bool(n.get("voix"))
        for item in (n.get("textes") or []):
            txt = item.get("texte") if isinstance(item, dict) else item
            if txt and txt not in textes:
                textes.append(txt)
        for m in (e.get("messages") or []):
            if isinstance(m, dict) and m.get("texte") and m["texte"] not in textes:
                textes.append(m["texte"])
    out = {"quoi": SOURCES["tetra"][0], "nombre": len(entrees),
           "reseaux": [k for k, _ in reseaux.most_common(4)],
           "porteuses_mhz": sorted(porteuses)[:6],
           "appels_vus": appels, "appels_en_clair": clairs, "appels_suivis_en_voix": suivis,
           "messages_courts_sds": sds, "postes_actifs_max": postes_max,
           "voix_en_clair_entendue": voix}
    if textes:
        out["messages_en_clair"] = textes[:6]
    return out


def _resume_generique(source, entrees):
    """Un decodeur sans fiche prevue : le compte, et quelques champs lisibles."""
    cles = ("texte", "ligne", "message", "comment", "decode", "mmsi", "mode", "canal",
            "freq_hz", "freq", "ps", "radiotext", "serie", "alt", "temp", "bande")
    exemples = []
    for e in entrees[::max(1, len(entrees) // EXEMPLES)][:EXEMPLES]:
        court = {}
        for k in cles:
            if e.get(k) in (None, "", []):
                continue
            court[k] = _valeur(e.get(k))
            if len(court) >= 4:
                break
        if court:
            exemples.append(court)
    return {"quoi": source.replace("_", " "), "nombre": len(entrees), "exemples": exemples}


def _resume_source(source, entrees):
    if source == "tetra":
        return _resume_tetra(entrees)
    if source not in SOURCES:
        return _resume_generique(source, entrees)
    champs = SOURCES[source][1]
    ident = {"dmr_ars": "poste", "adsb_local": "icao24", "helicos": "icao24", "ogn": "from", "aprs": "from",
             "pagers": "ligne", "ais": "mmsi", "signalisation": "texte", "rds": "ps", "sondes": "serie"}.get(source)
    if source == "pagers":  # l adresse appelee, et bip seul ou message
        import re
        for e in entrees:
            m = re.search(r"Address:\s*(\d+)|\|(\d{6,})\|", e.get("ligne", ""))
            e["adresse"] = (m.group(1) or m.group(2)).lstrip("0") if m else None
            e["bip_sans_texte"] = not re.search(r"Alpha:\s*\S|Numeric:\s*\S|\|(ALN|NUM)\|\S", e.get("ligne", ""))
        ident, champs = "adresse", ["freq", "adresse", "bip_sans_texte"]
    distincts = collections.Counter(str(e.get(ident)) for e in entrees if ident and e.get(ident) is not None)
    exemples = []
    for e in entrees[::max(1, len(entrees) // EXEMPLES)][:EXEMPLES]:
        if source == "vdl2" and isinstance(e.get("vdl2"), dict):
            v = e["vdl2"]
            e = {"freq": v.get("freq"), "sig_level": v.get("sig_level"), "noise_level": v.get("noise_level"),
                 "contenu": ", ".join(k for k in v if k not in ("app", "t", "freq", "sig_level", "noise_level", "idx"))}
        exemples.append({k: _valeur(e.get(k)) for k in (champs or e.keys()) if e.get(k) not in (None, "", [])})
    out = {"quoi": SOURCES[source][0], "nombre": len(entrees), "exemples": exemples}
    if distincts:
        out["identifiants_distincts"] = len(distincts)
        out["les_plus_actifs"] = [k for k, _ in distincts.most_common(4)]
    return out


def releve(fin=None, debut=None):
    """{source: resume} du trafic entre debut et fin, ou de l'heure qui finit a fin."""
    if fin is None and debut is None:
        fin = datetime.now().replace(minute=0, second=0, microsecond=0)
    fin = fin or datetime.now()
    if debut is None:
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
    if d.hour == 0 and d.minute == 0:
        return "minuit"
    if d.hour == 12 and d.minute == 0:
        return "midi"
    base = "1 heure" if d.hour == 1 else ("minuit" if d.hour == 0 else f"{d.hour} heures")
    if d.minute:
        return f"{base} {d.minute}"
    return base


def _texte_simple(donnees, debut, fin):
    parts = [f"{nombre(r['nombre'], 'trame')} {COURT.get(s, s.replace('_', ' '))}" for s, r in donnees.items()]
    return dire("trafic.simple", debut=_heure(debut), fin=_heure(fin), liste=", ".join(parts))


def texte(donnees, debut, fin):
    try:
        import gemini
        import reglages
        lieu = reglages.get("lieu") or {}
        reponse, probleme = gemini.ask(PROMPT.format(commune=lieu.get("commune", ""), debut=_heure(debut),
                                                     fin=_heure(fin), donnees=json.dumps(donnees, ensure_ascii=False,
                                                                                         default=str)[:9000]),
                                       temperature=0.9, timeout=90)
        if reponse and reponse.strip():
            return " ".join(reponse.split()), "gemini"
        print(f"[trafic_heure] Gemini : {probleme}")
    except Exception as exc:
        print(f"[trafic_heure] Gemini : {exc!r}")
    return _texte_simple(donnees, debut, fin), "simple"


def paragraphe(donnees, debut, fin):
    """Le titre et le recit, prets a etre dits."""
    corps, auteur = texte(donnees, debut, fin)
    titre = dire("trafic.titre", debut=_heure(debut), fin=_heure(fin))
    return f"{titre} {corps}", auteur


def depuis_minuit(moment=None):
    """Le meme recit que l'heure, de minuit jusqu'a maintenant. Chaine vide s'il n'y a rien."""
    moment = moment or datetime.now()
    debut = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if moment <= debut:
        return ""
    donnees, debut, fin = releve(moment, debut)
    if not donnees:
        return ""
    dit, auteur = paragraphe(donnees, debut, fin)
    print(f"[trafic_heure] depuis minuit ({auteur}) : {dit[:160]}")
    return dit


def faire(fin=None):
    """Le resume de l'heure ecoulee : une prise du journal "donnees" (non jouee)."""
    import prise_vocale
    donnees, debut, fin = releve(fin)
    if not donnees:
        print(f"[trafic_heure] aucun trafic de donnees entre {debut:%H:%M} et {fin:%H:%M}")
        return None
    dit, auteur = paragraphe(donnees, debut, fin)
    chemin = prise_vocale.record(dit, SOUND, ETIQUETTE,
                                 meta={"source": "trafic de donnees", "debut": str(debut), "fin": str(fin),
                                       "auteur": auteur, "donnees": donnees}, source="donnees")
    print(f"[trafic_heure] {debut:%H:%M}-{fin:%H:%M} ({auteur}) : {dit[:160]}")
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
