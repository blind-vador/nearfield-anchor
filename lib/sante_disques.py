"""Sante des disques (2026-11-12, demande utilisateur) : tous les supports de
stockage du Pi -- la carte SD et les disques du NAS -- relus chaque jour, avec
un auto-test court le dimanche et un auto-test long le premier dimanche du mois.

  disques (USB, SATA)  attributs SMART (smartctl, via /usr/local/sbin/disque-sante,
                       seul geste root) : verdict global, secteurs realloues, en
                       attente, illisibles, erreurs de liaison (CRC), temperature,
                       heures, cycles de tete, dernier auto-test ;
  carte SD             pas de SMART : compteur d'erreurs du systeme de fichiers
                       (/sys/fs/ext4), volume ecrit depuis sa creation (usure) ;
  tous                 erreurs d'entree-sortie du noyau des dernieres 24 h,
                       partition remontee en lecture seule.

Chaque disque recoit un niveau (bon, surveiller, preoccupant, critique) et les
evenements d'alarme qu'il justifie ; le bilan est ecrit dans RAPPORT (carte SD,
pour rester lisible sans le NAS). La commande "sante" en lit le dernier, la
veille systeme en tire les alarmes (watchers/veille_systeme.py, classe
_SanteDisques), et surveille en direct le noyau et la chaleur des disques.

Seuils : les attributs que les releves de pannes (Backblaze, Google 2007)
lient le plus aux defaillances : 5, 187, 188, 197, 198 non nuls ; 199 (CRC)
designe le cable ou l'alimentation, pas le plateau."""
import chemins
import datetime
import json
import os
import re
import subprocess
import time

RAPPORT = chemins.base("sante_disques.json")
HISTORIQUE = chemins.base("sante_disques_historique.json")
AIDE = "/usr/local/sbin/disque-sante"
NIVEAUX = ["bon", "surveiller", "preoccupant", "critique"]
CHAUD_C = 55                 # disque 2,5 pouces : 60 degres au plus en marche
TIEDE_C = 50
REALLOUES_GRAVE = 50
CYCLES_TETE_PREVUS = 600_000
HISTORIQUE_MAX = 90          # releves gardes par disque
TEST_LONG_JOUR_MAX = 7       # premier dimanche du mois : auto-test long

# evenements d'alarme (lib/sons.py, lib/alarmes.py) par ordre de gravite
DEFAILLANT, SECTEURS, ERREURS, CHAUD = "disque_defaillant", "disque_secteurs", "disque_erreurs", "disque_chaud"
EVENEMENTS = [DEFAILLANT, SECTEURS, ERREURS, CHAUD]

ERREUR_NOYAU = re.compile(r"(?:I/O error, dev (\w+)|Buffer I/O error on dev (\w+?)\d*,|critical medium error, dev (\w+)"
                          r"|EXT4-fs error \(device (\w+?)p?\d*\)|EXT4-fs \((\w+?)p?\d*\): Remounting filesystem read-only"
                          r"|(mmc\d+): .*(?:error|timeout))", re.I)


def _log(texte):
    print(f"[sante_disques] {texte}", flush=True)


def _sortie(args, timeout=60):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _json(texte):
    try:
        return json.loads(texte)
    except ValueError:
        return None


# --- inventaire --------------------------------------------------------------

def peripheriques():
    """Disques entiers presents : [{id, type (hdd|ssd|sd), modele, serie, montages}]."""
    data = _json(_sortie(["lsblk", "-J", "-o", "NAME,TYPE,TRAN,MODEL,SERIAL,ROTA,MOUNTPOINTS"], 10)) or {}
    out = []
    for d in data.get("blockdevices", []):
        if d.get("type") != "disk" or re.match(r"(loop|zram|ram)", d["name"]):
            continue
        montages = [m for p in [d] + (d.get("children") or []) for m in (p.get("mountpoints") or []) if m]
        parts = [p["name"] for p in d.get("children") or []] or [d["name"]]
        if d["name"].startswith("mmcblk"):
            genre = "sd"
            serie = _lire(f"/sys/block/{d['name']}/device/serial") or d.get("serial")
            modele = _lire(f"/sys/block/{d['name']}/device/name") or ""
        else:
            genre = "hdd" if d.get("rota") else "ssd"
            serie, modele = d.get("serial"), (d.get("model") or "").strip()
        out.append({"id": d["name"], "type": genre, "modele": modele, "serie": serie or d["name"],
                    "montages": montages, "parties": parts})
    return out


def _lire(chemin):
    try:
        with open(chemin) as f:
            return f.read().strip()
    except OSError:
        return None


def nom(disque):
    """Nom dit a voix haute : la carte SD, le disque nd1 du NAS..."""
    from phrases import dire
    if disque["type"] == "sd" or "/" in disque.get("montages", []):
        return dire("sante_disques.nom_carte_sd")
    for m in disque.get("montages", []):
        if m.startswith("/media/nas/"):
            return dire("sante_disques.nom_nas", disque=os.path.basename(m))
    return dire("sante_disques.nom_autre", disque=disque["id"], modele=disque.get("modele") or "")


# --- mesures -----------------------------------------------------------------

def smart(dev, quoi="smart"):
    """Releve smartctl (JSON) via l'aide root ; None si illisible."""
    return _json(_sortie(["sudo", "-n", AIDE, quoi, f"/dev/{dev}"], 90))


def temperature(dev):
    """Temperature actuelle sans reveiller un disque endormi ; None sinon."""
    d = smart(dev, "temp")
    return _temperature(d) if d else None


def _temperature(d):
    t = (d.get("temperature") or {}).get("current")
    if t is None:
        a = _attributs(d).get(194) or _attributs(d).get(190)
        if a:
            t = a["raw"]["value"] & 0xFF
    return t


def _attributs(d):
    return {a["id"]: a for a in (d.get("ata_smart_attributes") or {}).get("table", [])}


def _brut(attrs, i):
    a = attrs.get(i)
    if not a:
        return None
    v = a["raw"]["value"]
    return v & 0xFFFFFFFF if i in (5, 196, 197, 198) else v     # octets hauts : compteurs constructeur


def mesures_smart(d):
    attrs = _attributs(d)
    echecs = [a["name"] for a in attrs.values() if a.get("when_failed") == "now"]
    tests = ((d.get("ata_smart_self_test_log") or {}).get("standard") or {}).get("table") or []
    dernier = None
    if tests:
        t = tests[0]
        dernier = {"type": (t.get("type") or {}).get("string", ""), "resultat": (t.get("status") or {}).get("string", ""),
                   "reussi": (t.get("status") or {}).get("passed"), "heures": t.get("lifetime_hours")}
    en_cours = ((d.get("ata_smart_data") or {}).get("self_test") or {}).get("status") or {}
    temp_max = None
    a194 = attrs.get(194)
    if a194:
        m = re.search(r"Min/Max \d+/(\d+)", a194["raw"].get("string", ""))
        temp_max = int(m.group(1)) if m else None
    return {
        "verdict": (d.get("smart_status") or {}).get("passed"),
        "attributs_en_echec": echecs,
        "heures": (d.get("power_on_time") or {}).get("hours"),
        "temperature": _temperature(d),
        "temperature_max": temp_max,
        "realloues": _brut(attrs, 5),
        "evenements_reallocation": _brut(attrs, 196),
        "incorrigibles_signales": _brut(attrs, 187),
        "delais_commande": _brut(attrs, 188),
        "en_attente": _brut(attrs, 197),
        "illisibles": _brut(attrs, 198),
        "crc": _brut(attrs, 199),
        "cycles_tete": _brut(attrs, 193),
        "dernier_test": dernier,
        "test_en_cours": (en_cours.get("value", 0) >> 4) == 0xF,
    }


def mesures_fs(disque):
    """Erreurs ext4 et volume ecrit (toutes partitions), partitions en lecture seule."""
    erreurs, ecrit_ko, lecture_seule = 0, 0, []
    for p in disque["parties"]:
        e = _lire(f"/sys/fs/ext4/{p}/errors_count")
        if e is not None:
            erreurs += int(e or 0)
        k = _lire(f"/sys/fs/ext4/{p}/lifetime_write_kbytes")
        if k:
            ecrit_ko += int(k)
    try:
        with open("/proc/mounts") as f:
            for ligne in f:
                dev, point, _fs, options = ligne.split()[:4]
                if os.path.basename(dev) in disque["parties"] and "ro" in options.split(",") and point != "/boot/firmware":
                    lecture_seule.append(point)
    except OSError:
        pass
    return {"erreurs_fs": erreurs, "ecrit_go": round(ecrit_ko / 1e6, 1) if ecrit_ko else None,
            "lecture_seule": lecture_seule}


def erreurs_noyau(depuis="-24h"):
    """{disque: nombre} d'erreurs d'entree-sortie dans le journal du noyau."""
    compte = {}
    for ligne in _sortie(["journalctl", "-k", "--since", depuis, "-o", "cat", "--no-pager"], 60).splitlines():
        dev = disque_de_ligne(ligne)
        if dev:
            compte[dev] = compte.get(dev, 0) + 1
    return compte


def disque_de_ligne(ligne):
    """Disque entier vise par une ligne d'erreur du noyau (sda, mmcblk0), sinon None."""
    m = ERREUR_NOYAU.search(ligne)
    if not m:
        return None
    dev = next(g for g in m.groups() if g)
    if dev.startswith("mmc") and not dev.startswith("mmcblk"):
        return "mmcblk" + dev[3:]
    m2 = re.match(r"(sd[a-z]+|mmcblk\d+|nvme\d+n\d+)", dev)
    return m2.group(1) if m2 else None


# --- evaluation --------------------------------------------------------------

def evaluer(disque, m, precedent=None):
    """Niveau, evenements d'alarme et constats (phrases) d'un disque."""
    from phrases import dire, nombre
    p = (precedent or {}).get("mesures") or {}
    niveau, evenements, constats = 0, [], []

    def note(n, phrase, evenement=None):
        nonlocal niveau
        niveau = max(niveau, n)
        constats.append(phrase)
        if evenement and evenement not in evenements:
            evenements.append(evenement)

    if m.get("verdict") is False or m.get("attributs_en_echec"):
        note(3, dire("sante_disques.verdict_echec"), DEFAILLANT)
    t = m.get("dernier_test")
    if t and t.get("reussi") is False:
        note(3, dire("sante_disques.test_echec", resultat=t.get("resultat", "")), DEFAILLANT)
    attente, illisibles = m.get("en_attente") or 0, m.get("illisibles") or 0
    if attente or illisibles:
        note(2, dire("sante_disques.secteurs_attente", nombre=nombre(attente + illisibles, "secteur")), SECTEURS)
    if m.get("incorrigibles_signales"):
        note(2, dire("sante_disques.incorrigibles", nombre=nombre(m["incorrigibles_signales"], "erreur")), SECTEURS)
    realloues = m.get("realloues") or 0
    avant = p.get("realloues")
    if realloues and avant is not None and realloues > avant:
        note(2, dire("sante_disques.realloues_hausse", avant=avant, maintenant=realloues), SECTEURS)
    elif realloues >= REALLOUES_GRAVE:
        note(2, dire("sante_disques.realloues", nombre=nombre(realloues, "secteur")), SECTEURS)
    elif realloues:
        note(1, dire("sante_disques.realloues", nombre=nombre(realloues, "secteur")))
    if m.get("erreurs_noyau"):
        note(2, dire("sante_disques.erreurs_noyau", nombre=nombre(m["erreurs_noyau"], "erreur")), ERREURS)
    if m.get("erreurs_fs") and m["erreurs_fs"] > (p.get("erreurs_fs") or 0):
        note(2, dire("sante_disques.erreurs_fs", nombre=nombre(m["erreurs_fs"], "erreur")), ERREURS)
    if m.get("lecture_seule"):
        note(3, dire("sante_disques.lecture_seule"), ERREURS)
    crc, crc_avant = m.get("crc") or 0, p.get("crc")
    if crc and crc_avant is not None and crc > crc_avant:
        note(1, dire("sante_disques.crc_hausse", nombre=nombre(crc - crc_avant, "erreur")))
    temp = m.get("temperature")
    if temp is not None and temp >= CHAUD_C:
        note(2, dire("sante_disques.chaud", degres=temp), CHAUD)
    elif temp is not None and temp >= TIEDE_C:
        note(1, dire("sante_disques.tiede", degres=temp))
    cycles = m.get("cycles_tete")
    if cycles and cycles >= 0.8 * CYCLES_TETE_PREVUS and disque["type"] == "hdd":
        note(1, dire("sante_disques.cycles_tete", cycles=f"{cycles:,}".replace(",", " "),
                     prevus=f"{CYCLES_TETE_PREVUS:,}".replace(",", " ")))
    return NIVEAUX[niveau], evenements, constats


def _resume(disque, niveau, m):
    """Phrase courte d'un disque pour la commande "sante"."""
    from phrases import dire, nombre
    morceaux = [dire(f"sante_disques.niveau_{niveau}")]
    if m.get("temperature") is not None:
        morceaux.append(dire("sante_disques.degres", degres=m["temperature"]))
    if m.get("heures"):
        morceaux.append(nombre(m["heures"], "heure"))
    if disque["type"] == "sd" and m.get("ecrit_go"):
        morceaux.append(dire("sante_disques.ecrits", go=round(m["ecrit_go"])))
    t = m.get("dernier_test")
    if m.get("test_en_cours"):
        morceaux.append(dire("sante_disques.test_en_cours"))
    elif t and t.get("reussi"):
        morceaux.append(dire("sante_disques.test_reussi"))
    return f"{nom(disque)} : " + ", ".join(morceaux)


# --- bilan -------------------------------------------------------------------

def _charger(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def _ecrire(chemin, data):
    tmp = f"{chemin}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)


def bilan(lancer_test=None):
    """Releve tous les disques, ecrit RAPPORT et l'historique ; rend le rapport.
    lancer_test : "court" ou "long" pour lancer un auto-test SMART apres la
    lecture (resultat lu au bilan suivant)."""
    from phrases import dire
    historique = _charger(HISTORIQUE, {})
    noyau = erreurs_noyau()
    disques = []
    for d in peripheriques():
        m = {"erreurs_noyau": noyau.get(d["id"], 0)}
        m.update(mesures_fs(d))
        lisible = True
        if d["type"] != "sd":
            s = smart(d["id"])
            if s and s.get("ata_smart_attributes"):
                m.update(mesures_smart(s))
            else:
                lisible = False
        hist = historique.get(d["serie"], [])
        niveau, evenements, constats = evaluer(d, m, hist[-1] if hist else None)
        if not lisible:
            constats.append(dire("sante_disques.smart_illisible"))
        disques.append({**d, "nom": nom(d), "niveau": niveau, "alarmes": evenements, "constats": constats,
                        "mesures": m, "smart": lisible, "resume": _resume(d, niveau, m)})
        hist.append({"t": time.time(), "mesures": m})
        historique[d["serie"]] = hist[-HISTORIQUE_MAX:]
        if lancer_test and d["type"] == "hdd" and lisible and not m.get("test_en_cours"):
            _sortie(["sudo", "-n", AIDE, f"test-{lancer_test}", f"/dev/{d['id']}"], 30)
            _log(f"{d['id']} : auto-test {lancer_test} lance")
    rapport = {"t": time.time(), "date": time.strftime("%Y-%m-%d %H:%M"), "disques": disques,
               "test_lance": lancer_test}
    rapport["texte"] = texte(rapport)
    _ecrire(RAPPORT, rapport)
    _ecrire(HISTORIQUE, historique)
    return rapport


def texte(rapport):
    """Bilan complet, disque par disque."""
    from phrases import dire
    parts = []
    for d in rapport["disques"]:
        p = d["resume"]
        if d["constats"]:
            p += ". " + " ".join(c.rstrip(".") + "." for c in d["constats"])
        parts.append(p.rstrip(".") + ".")
    if not parts:
        return dire("sante_disques.aucun")
    return " ".join(parts)


def dernier():
    return _charger(RAPPORT, None)


def _quand(t):
    from phrases import dire, nombre
    jours = (datetime.date.today() - datetime.date.fromtimestamp(t)).days
    heure = time.strftime("%H h %M", time.localtime(t)).lstrip("0")
    if jours <= 0:
        return dire("sante_disques.quand_aujourdhui", heure=heure)
    if jours == 1:
        return dire("sante_disques.quand_hier", heure=heure)
    return dire("sante_disques.quand_jours", jours=nombre(jours, "jour"))


def pour_sante():
    """(problemes, details) pour la commande "sante" : le dernier bilan."""
    from phrases import dire
    r = dernier()
    if not r:
        return [], [dire("sante_disques.pas_de_bilan")]
    problemes, details = [], []
    mauvais = [d for d in r["disques"] if d["niveau"] != "bon"]
    for d in mauvais:
        if d["niveau"] in ("preoccupant", "critique"):
            problemes.append(f"{d['nom']} : " + "; ".join(c.rstrip(".") for c in d["constats"]))
    resumes = "; ".join(d["resume"] for d in r["disques"])
    details.append(dire("sante_disques.sante_detail", quand=_quand(r["t"]), resumes=resumes))
    for d in mauvais:
        if d["niveau"] == "surveiller":
            details.append(f"{d['nom']} : " + "; ".join(c.rstrip(".") for c in d["constats"]) + ".")
    if time.time() - r["t"] > 3 * 86400:
        problemes.append(dire("sante_disques.bilan_ancien"))
    return problemes, details


def test_du_jour(jour=None):
    """Dimanche : auto-test court, ou long le premier dimanche du mois."""
    jour = jour or datetime.date.today()
    if jour.weekday() != 6:
        return None
    return "long" if jour.day <= TEST_LONG_JOUR_MAX else "court"
