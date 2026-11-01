"""TETRA dans le scanner (cote Pi), 2026-10-26.

Le Pi ne decode JAMAIS le TETRA lui-meme (chaleur, CPU) : pendant une fenetre
du scanner il sert son dongle par rtl_tcp et confie le decodage a la machine
compagne la plus libre (bridge/tetra_neo.py via la prise TETRA_SOCKET tenue par
compagnon_serveur). Ce module tient :
  - le MAGASIN des porteuses TETRA (graines connues + celles decouvertes au
    balayage), avec pour chacune son gain appris et son historique de verrous ;
  - le GAIN ADAPTATIF par porteuse (meme principe que airband_bridge : on part
    d'un gain modere, on BAISSE a l'ecretage signale par la machine compagne,
    on remonte doucement si le signal est faible) ;
  - le choix des porteuses a sonder dans une fenetre (autodecouverte : les
    confirmees d'abord, puis l'exploration) ;
  - la reconnaissance d'une frequence TETRA (bandes civiles) pour le mode
    manuel et la decouverte au balayage ;
  - le client de la prise TETRA_SOCKET.

  - le REGISTRE TETRA (2026-10-27), comme le registre DMR : par reseau, les
    postes vus (SSI), leurs inscriptions, messages d'etat et SDS (expediteur,
    taille), les appels (clair / chiffre / prive), les porteuses de trafic
    attribuees, et les heures d'activite ; c'est ce que dit la commande
    console "tetra" et ce qui pique l'attention du scanner.

Ecoute passive de bonne foi ; on ne lit que des metadonnees diffusees en clair
et la voix non chiffree que la machine compagne a su decoder. Aucun
dechiffrement, ici comme ailleurs dans le scanner. Voir la memoire
nearfield-tetra et ROADMAP.
"""
import json
import math
import os
import socket
import tempfile
import threading
import time

ETAT = os.path.expanduser("~/autopilot/tetra_porteuses.json")
REGISTRE = os.path.expanduser("~/autopilot/tetra_registre.json")
TETRA_SOCKET = "/tmp/tetra_pont.sock"   # prise tenue par compagnon_serveur (meteo_watch)
ARRET = os.path.expanduser("~/autopilot/tetra_off")   # interrupteur, comme prise_unique_off

# Perimetre ou le scanner CHERCHE des porteuses TETRA -- PAS un mur. On ne
# balaie pas tout le spectre a l'aveugle (rtl_power a besoin de plages, pour un
# raster 25 kHz : coherence physique), mais on ne s'interdit aucun canal a
# l'interieur. 380-400 MHz porte le Tetrapol/INPT (secours) qui est chiffre :
# si on tombe dessus on NOTE "chiffre" et on ignore le contenu (hors
# prerogatives), on ne se bride pas -- ailleurs ce meme 380-400 peut porter du
# TETRA en clair (doctrine, mur retire le 2026-10-28). Sert au mode manuel et a
# l'autodecouverte au balayage. Elargi le 2026-11-01 a toutes les attributions
# TETRA connues dans le monde (350-470 MHz, 806-934 MHz) : le scanner est
# distribue, et une porteuse a 415 ou 870 MHz n'a rien d'exotique ailleurs.
# Les pistes muettes sont elaguees (PISTE_ESSAIS_AVANT_ELAGAGE), le surplus de
# pistes reste donc borne.
BANDES = ((350_000_000, 470_000_000), (806_000_000, 934_000_000))

RASTER_HZ = 25_000          # pas des porteuses TETRA
TOLERANCE_HZ = 15_000       # une frequence a moins de ca d'une porteuse connue = la meme
GAIN_DEFAUT = 32            # verrou propre a Saint-Etienne sans ecretage (2026-10-26)
GAIN_MIN = 12
GAIN_MAX = 40
GAIN_PAS_BAISSE = 4         # a l'ecretage
GAIN_PAS_HAUSSE = 2         # signal faible, pas d'ecretage, pas de verrou
CENTRE_ECART_HZ = 1_000     # porteuse ecoutee a plus de ca de son centre annonce : on recentre
POSTE_MENTIONS_MIN = 2      # un SSI vu une seule fois peut etre un bloc mal lu
PREMIERE_JOURNEE_SEC = 86400  # avant : tout poste est "nouveau", on n'en fait pas une nouvelle
TEXTES_GARDES = 100         # derniers messages texte en clair, par reseau
ECHANTILLONS_GARDES = 5     # contenus bruts gardes par expediteur et nature (physionomie du reseau)

# Graines : la porteuse de controle confirmee + quelques pistes a explorer
# (releve de puissance du 2026-10-26 et notes de nuit). L'autodecouverte
# ajoute les autres.
GRAINES = [
    {"freq_hz": 429_612_500, "confirmee": True},   # controle, reseau 208-29, zone 11 (centre SYSINFO)
    {"freq_hz": 429_575_000},
    {"freq_hz": 429_625_000},
    {"freq_hz": 428_412_500},
    {"freq_hz": 466_900_000},
    {"freq_hz": 467_750_000},
]

_verrou = threading.Lock()


def active():
    return not os.path.exists(ARRET)


def _charger():
    try:
        with open(ETAT) as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("porteuses"), list):
                return data
    except (OSError, ValueError):
        pass
    # Premiere fois : on seme.
    porteuses = []
    for g in GRAINES:
        porteuses.append({"freq_hz": g["freq_hz"], "gain": GAIN_DEFAUT,
                          "confirmee": bool(g.get("confirmee")), "verrous": 0, "essais": 0,
                          "dernier_essai": 0.0, "dernier_verrou": 0.0, "reseau": {}})
    return {"porteuses": porteuses}


def _ecrire(data, chemin=None):
    # Fichier temporaire unique : radio_watch et la console peuvent ecrire en
    # meme temps (meme lecon que la fiche du pont, 2026-10-27).
    chemin = chemin or ETAT
    dossier = os.path.dirname(chemin)
    os.makedirs(dossier, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dossier, prefix="." + os.path.basename(chemin) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, chemin)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _trouver(data, freq_hz):
    for p in data["porteuses"]:
        if abs(p["freq_hz"] - freq_hz) <= TOLERANCE_HZ:
            return p
    return None


def dans_bande(freq_hz):
    return any(bas <= freq_hz <= haut for bas, haut in BANDES)


def est_tetra(freq_hz):
    """Vrai si cette frequence est une porteuse TETRA connue/candidate (mode
    manuel, decouverte)."""
    with _verrou:
        if _trouver(_charger(), freq_hz) is not None:
            return True
    return False


def porteuses():
    with _verrou:
        return list(_charger()["porteuses"])


def fiabilite(p):
    """Porteuse qui accroche souvent et beaucoup : le controle du reseau local."""
    return p.get("verrous", 0) / (p.get("essais", 0) + 1) * (1 + math.log1p(p.get("verrous", 0)))


def candidats(nb=2):
    """Porteuses a sonder dans une fenetre, rend [(freq_hz, gain)] :
      1. la PRINCIPALE, a chaque fenetre : la confirmee la plus fiable (le
         controle ou l'on suit les appels) ;
      2. puis, a tour de role, la plus anciennement revue de toutes les autres,
         confirmees ou pistes (autodecouverte), comme la veille DMR fait tourner
         ses frequences derriere les plus prometteuses.
    Avant 2026-10-27 la rotation passait par toutes les confirmees, et le
    controle n'etait ecoute qu'une fenetre sur quatre."""
    with _verrou:
        data = _charger()
    ps = [p for p in data["porteuses"] if not p.get("alias_de")]
    confirmees = [p for p in ps if p.get("confirmee")]
    choix = [max(confirmees, key=fiabilite)] if confirmees else []
    reste = sorted((p for p in ps if p not in choix), key=lambda p: p.get("dernier_essai", 0))
    choix += reste[:max(0, nb - len(choix))]
    return [(p["freq_hz"], p.get("gain", GAIN_DEFAUT)) for p in choix]


def _ajuster_gain(p, res):
    """Gain adaptatif par porteuse (prolonge l'anti-saturation d'airband) :
    baisse a l'ecretage, remonte doucement si rien n'a accroche sans ecretage."""
    g = p.get("gain", GAIN_DEFAUT)
    if res.get("clip"):
        g = max(GAIN_MIN, g - GAIN_PAS_BAISSE)
    elif not res.get("bsch"):
        g = min(GAIN_MAX, g + GAIN_PAS_HAUSSE)
    p["gain"] = g


def noter(freq_hz, res):
    """Apprend d'un resultat de decodage : verrous, identite reseau, gain,
    promotion en "confirmee" des qu'une synchro de controle est vue. Non
    destructif : une porteuse qui n'accroche jamais est gardee (on continue a
    la guetter), seulement moins prioritaire."""
    with _verrou:
        data = _charger()
        p = _trouver(data, freq_hz)
        if p is None:
            p = {"freq_hz": freq_hz, "gain": GAIN_DEFAUT, "confirmee": False,
                 "verrous": 0, "essais": 0, "dernier_essai": 0.0, "dernier_verrou": 0.0, "reseau": {}}
            data["porteuses"].append(p)
        p["essais"] = p.get("essais", 0) + 1
        p.setdefault("premier_essai", time.time())
        p["dernier_essai"] = time.time()
        if res.get("bsch"):
            p["verrous"] = p.get("verrous", 0) + 1
            p["dernier_verrou"] = time.time()
            p["confirmee"] = True
            if res.get("reseau"):
                p["reseau"] = res["reseau"]
        _ajuster_gain(p, res)
        _recentrer(data, p, res)
        _ecrire(data)
    return p


def _recentrer(data, p, res):
    """Le SYSINFO donne le numero de la porteuse et son decalage : sa frequence
    exacte (2026-10-27 : 429,6125 MHz, qu'on ecoutait a 429,600 et 429,625, deux
    entrees pour la meme porteuse). L'entree mal centree devient un alias de la
    bonne, qui herite de son gain et de ses verrous ; plus sondee a part."""
    centre = (res.get("reseau") or {}).get("freq_hz") if res.get("bsch") else None
    if not centre or abs(centre - p["freq_hz"]) <= CENTRE_ECART_HZ:
        return
    if abs(centre - p["freq_hz"]) > TOLERANCE_HZ:
        # Le SYSINFO decrit le canal de CONTROLE du reseau, pas forcement la
        # porteuse sondee : un centre a plus d'un canal (TOLERANCE_HZ) de la
        # frequence ecoutee est une AUTRE porteuse (p. ex. on a sonde une
        # porteuse de la bande 450-470 et lu le controle a 429 MHz). On NE
        # recentre PAS -- sinon des porteuses distinctes de toute la bande
        # seraient avalees en alias, jamais reexplorees, et le scanner
        # cesserait de balayer la bande entiere (2026-10-28).
        return
    bon = next((q for q in data["porteuses"] if abs(q["freq_hz"] - centre) <= CENTRE_ECART_HZ), None)
    if bon is None:
        bon = {"freq_hz": int(centre), "gain": p.get("gain", GAIN_DEFAUT), "confirmee": True,
               "verrous": 0, "essais": 0, "dernier_essai": p.get("dernier_essai", 0.0),
               "dernier_verrou": 0.0, "reseau": {}, "origine": "sysinfo"}
        data["porteuses"].append(bon)
    bon["confirmee"] = True
    bon["verrous"] = bon.get("verrous", 0) + p.get("verrous", 0)
    bon["essais"] = bon.get("essais", 0) + p.get("essais", 0)
    bon["dernier_verrou"] = max(bon.get("dernier_verrou", 0.0), p.get("dernier_verrou", 0.0))
    bon["reseau"] = res["reseau"]
    bon.pop("alias_de", None)
    p["alias_de"] = int(centre)
    p["verrous"] = p["essais"] = 0
    print(f"[tetra] {p['freq_hz'] / 1e6:.4f} MHz recentree sur {centre / 1e6:.4f} MHz (SYSINFO)", flush=True)


# --- registre (2026-10-27) -----------------------------------------------------

def _cle_reseau(reseau):
    if not reseau or reseau.get("mcc") is None:
        return None
    return "%s-%s" % (reseau["mcc"], reseau["mnc"])


def registre():
    try:
        with open(REGISTRE) as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {"reseaux": {}}


def enregistrer(freq_hz, res, now=None):
    """Apprend une ecoute TETRA dans le registre. Rend ce qu'elle a de NOUVEAU
    ou d'utile, pour les annonces et la piqure :
      {"reseau", "postes_actifs", "nouveaux_postes", "inscriptions", "etats", "sds",
       "appels", "appels_clairs", "suivis", "voix", "nouvelles_porteuses"}"""
    now = now or time.time()
    reseau = res.get("reseau") or {}
    act = res.get("activite") or {}
    cle = _cle_reseau(reseau)
    postes = {s: n for s, n in (act.get("postes") or {}).items() if n >= POSTE_MENTIONS_MIN}
    appels = act.get("appels") or {}
    news = {"reseau": cle, "postes_actifs": len(postes), "nouveaux_postes": [],
            "inscriptions": int(act.get("inscriptions") or 0),
            "etats": sum((act.get("etats") or {}).values()),
            "sds": sum(v.get("n", 0) for v in (act.get("sds") or {}).values()),
            "appels": len(appels), "appels_clairs": sum(1 for a in appels.values() if not a.get("chiffre")),
            "suivis": len(act.get("suivis") or []), "voix": bool(res.get("voix_wav")),
            "nouvelles_porteuses": [], "textes": [], "positions": 0}
    if not cle:
        return news
    with _verrou:
        reg = registre()
        r = reg.setdefault("reseaux", {}).setdefault(cle, {
            "premiere": now, "visites": 0, "zones": {}, "heures": [0] * 24, "postes": {}, "etats": {},
            "sds": {}, "appels": {"total": 0, "clairs": 0, "chiffres": 0, "prives": 0, "suivis": 0},
            "porteuses_trafic": {}, "controle": {}, "inscriptions": 0})
        r["derniere"] = now
        r["visites"] += 1
        if reseau.get("la") is not None:
            la = str(reseau["la"])
            r["zones"][la] = r["zones"].get(la, 0) + 1
        if reseau.get("freq_hz"):
            c = r["controle"].setdefault(str(reseau["freq_hz"]), {"premiere": now, "n": 0})
            c["n"] += 1
            c["derniere"] = now
            c["carrier"] = reseau.get("carrier")
            if reseau.get("voisines") is not None:
                c["voisines"] = reseau["voisines"]
        actif = bool(postes or news["sds"] or news["etats"] or appels)
        if actif:
            r["heures"][time.localtime(now).tm_hour] += 1
        rodage = now - r.get("premiere", now) < PREMIERE_JOURNEE_SEC
        for ssi, n in postes.items():
            p = r["postes"].get(ssi)
            if p is None:
                p = r["postes"][ssi] = {"premiere": now, "n": 0, "visites": 0}
                if not rodage:
                    news["nouveaux_postes"].append(ssi)
            p["n"] += n
            p["visites"] += 1
            p["derniere"] = now
        for cle_etat, n in (act.get("etats") or {}).items():
            e = r["etats"].setdefault(cle_etat, {"n": 0, "premiere": now})
            e["n"] += n
            e["derniere"] = now
        for src, v in (act.get("sds") or {}).items():
            s = r["sds"].setdefault(src, {"n": 0, "bits": 0, "premiere": now})
            s["n"] += v.get("n", 0)
            s["bits"] += v.get("bits", 0)
            s["derniere"] = now
        r["inscriptions"] += news["inscriptions"]
        a = r["appels"]
        a["total"] += len(appels)
        a["clairs"] += news["appels_clairs"]
        a["chiffres"] += sum(1 for x in appels.values() if x.get("chiffre"))
        a["prives"] += sum(1 for x in appels.values() if x.get("prive"))
        a["suivis"] += news["suivis"]
        if appels:
            a["dernier"] = now
        for f in act.get("trafic_hz") or []:
            t = r["porteuses_trafic"].get(str(f))
            if t is None:
                t = r["porteuses_trafic"][str(f)] = {"premiere": now, "n": 0, "voix": 0}
                news["nouvelles_porteuses"].append(int(f))
            t["n"] += 1
            t["derniere"] = now
        for s in act.get("suivis") or []:
            t = r["porteuses_trafic"].get(str(s["freq_hz"]))
            if t is not None and news["voix"]:
                t["voix"] += 1
        _noter_messages(r, act.get("messages") or [], news, now)
        _ecrire(reg, REGISTRE)
    return news


def centre(freq_hz):
    """Frequence a ecouter pour freq_hz : son centre exact si l'entree est un
    alias recentre, sinon elle-meme."""
    with _verrou:
        p = _trouver(_charger(), freq_hz)
    return int(p["alias_de"]) if p and p.get("alias_de") else int(freq_hz)


def archiver(freq_hz, res, news):
    """Journal interne (NAS, fichier du jour tetra_<jour>.jsonl) : chaque ecoute,
    pour relire plus tard ce qui a ete rate et pour la notation."""
    try:
        import veille_donnees
        act = res.get("activite") or {}
        veille_donnees.archive("tetra", {
            "freq_hz": int(freq_hz), "reseau": res.get("reseau"), "duree": res.get("duree"),
            "machine": res.get("machine"), "nouvelles": news,
            "postes": {s: n for s, n in (act.get("postes") or {}).items() if n >= POSTE_MENTIONS_MIN},
            "etats": act.get("etats"), "sds": act.get("sds"), "appels": act.get("appels"),
            "suivis": act.get("suivis"), "trafic_hz": act.get("trafic_hz"), "messages": act.get("messages")})
    except Exception as exc:
        print(f"[tetra] archive impossible : {exc!r}", flush=True)


def _noter_messages(r, messages, news, now):
    """Contenu des messages courts (2026-10-27, demande utilisateur : y lire la
    physionomie du reseau et ce qui s'y passe) : textes en clair, dernieres
    positions LIP par poste, nature de chaque message, et quelques contenus
    bruts par expediteur et nature pour apprendre les formats propres au reseau."""
    m = r.setdefault("messages", {"natures": {}, "textes": [], "positions": {}, "echantillons": {}})
    deja = {(x.get("src"), x.get("texte")) for x in m["textes"][-TEXTES_GARDES:]}
    for msg in messages:
        nature = msg.get("nature") or "?"
        m["natures"][nature] = m["natures"].get(nature, 0) + 1
        if msg.get("texte"):
            if (msg["src"], msg["texte"]) not in deja:
                deja.add((msg["src"], msg["texte"]))
                entree = {"t": now, "src": msg["src"], "dst": msg.get("dst"), "texte": msg["texte"]}
                m["textes"].append(entree)
                news["textes"].append(entree)
        if msg.get("position"):
            m["positions"][msg["src"]] = dict(msg["position"], t=now)
            news["positions"] += 1
        if msg.get("hex") and not msg.get("texte"):
            cle = "%s:%s" % (msg["src"], msg.get("pid"))
            if msg.get("type_appli"):
                cle += ":" + msg["type_appli"]
            ech = m["echantillons"].setdefault(cle, [])
            if msg["hex"] not in ech:
                ech.append(msg["hex"])
                del ech[:-ECHANTILLONS_GARDES]
    del m["textes"][:-TEXTES_GARDES]


def piquant(news):
    """Une ecoute qui merite une piqure d'attention : un appel, de la voix en
    clair, une porteuse de trafic ou des postes jamais vus. La signalisation
    permanente du controle (BSCH, inscriptions, etats) ne pique pas : elle est
    la tout le temps, elle ferait du TETRA un aimant."""
    return bool(news.get("appels") or news.get("voix") or news.get("nouvelles_porteuses")
                or news.get("nouveaux_postes") or news.get("textes"))


def decouvrir(freq_hz):
    """Une frequence trouvee au balayage dans une bande TETRA et ecartee par
    tous les decodeurs audio/numeriques : on l'ajoute comme piste a sonder par
    la fenetre TETRA (autodecouverte). Sans effet si deja connue ou hors bande."""
    if not dans_bande(freq_hz):
        return False
    with _verrou:
        data = _charger()
        if _trouver(data, freq_hz) is not None:
            return False
        data["porteuses"].append({"freq_hz": int(freq_hz), "gain": GAIN_DEFAUT, "confirmee": False,
                                  "verrous": 0, "essais": 0, "dernier_essai": 0.0,
                                  "dernier_verrou": 0.0, "reseau": {}, "origine": "balayage"})
        _ecrire(data)
    return True


# --- Autodecouverte : balayage periodique des bandes TETRA ---------------------
# Le scanner trouve ses porteuses lui-meme, comme la recherche DMR (touche R).
# Le balayage (rtl_power sur le Pi) ne fait que REPERER l'energie sur le raster
# 25 kHz ; c'est la fenetre TETRA, via la machine compagne, qui confirme une
# vraie porteuse de controle (noter -> confirmee). Anti-boucle par horodatage,
# et on ne garde pas indefiniment les pics muets (elaguer).
DECOUVERTE_INTERVAL = 6 * 3600        # au plus un balayage toutes les 6 h
DECOUVERTE_HORO = os.path.expanduser("~/autopilot/tetra_decouverte")
PISTE_ESSAIS_AVANT_ELAGAGE = 6        # pic de balayage muet apres tant d'essais : oublie
PISTE_AGE_AVANT_ELAGAGE = 86400       # ... et vu pour la premiere fois il y a plus d'un jour


def decouverte_due():
    """Vrai si un balayage d'autodecouverte est du (au plus une fois par
    DECOUVERTE_INTERVAL). Premier appel : du."""
    try:
        return time.time() - os.path.getmtime(DECOUVERTE_HORO) >= DECOUVERTE_INTERVAL
    except OSError:
        return True


def decouverte_faite():
    """Marque un balayage comme fait maintenant (meme s'il n'a rien trouve :
    on ne reessaie pas avant DECOUVERTE_INTERVAL)."""
    try:
        with open(DECOUVERTE_HORO, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def forcer_decouverte():
    """Fait qu'un balayage soit du a la prochaine fenetre TETRA (commande
    console "cherche tetra")."""
    try:
        os.remove(DECOUVERTE_HORO)
    except OSError:
        pass


def elaguer(now=None):
    """Oublie les pistes nees du balayage qui n'ont jamais accroche apres
    plusieurs essais : le registre ne grossit pas indefiniment de pics de
    bruit. Les graines et les porteuses confirmees sont gardees. Rend le
    nombre de pistes oubliees."""
    now = now or time.time()
    graines = {g["freq_hz"] for g in GRAINES}
    with _verrou:
        data = _charger()
        avant = len(data["porteuses"])
        data["porteuses"] = [
            p for p in data["porteuses"]
            if p.get("confirmee") or p.get("alias_de") or p["freq_hz"] in graines
            or p.get("origine") != "balayage" or p.get("verrous", 0) > 0
            or p.get("essais", 0) < PISTE_ESSAIS_AVANT_ELAGAGE
            or (now - p.get("premier_essai", p.get("dernier_essai", now))) < PISTE_AGE_AVANT_ELAGAGE
        ]
        oubliees = avant - len(data["porteuses"])
        if oubliees:
            _ecrire(data)
    return oubliees


def decoder(freq_hz, gain, secondes, host=None, port=1234, ppm=0, delai=None, max_secondes=None):
    """Demande a compagnon_serveur (prise locale) un decodage TETRA en direct de
    freq_hz. Le Pi doit DEJA servir rtl_tcp sur host:port. Rend le dict resultat
    (voir bridge/tetra_neo), ou {"ok": False, "erreur": ...} si pas de machine
    compagne / pas de reponse. Ne leve pas."""
    max_secondes = max(int(secondes), int(max_secondes or secondes))   # prolonge pendant un appel suivi
    req = {"freq_hz": int(freq_hz), "gain": gain, "secondes": int(secondes), "max_secondes": max_secondes,
           "port": int(port), "ppm": ppm}
    if host:
        req["host"] = host
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(delai or (max_secondes + 60))
    try:
        s.connect(TETRA_SOCKET)
    except OSError as exc:
        return {"ok": False, "erreur": "prise tetra injoignable (%r)" % exc}
    try:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            bloc = s.recv(65536)
            if not bloc:
                break
            buf += bloc
        if not buf.strip():
            return {"ok": False, "erreur": "pas de reponse"}
        return json.loads(buf.split(b"\n", 1)[0])
    except (OSError, ValueError) as exc:
        return {"ok": False, "erreur": repr(exc)[:200]}
    finally:
        s.close()
