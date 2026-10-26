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

Ecoute passive de bonne foi ; on ne lit que des metadonnees diffusees en clair
et la voix non chiffree que la machine compagne a su decoder. Aucun
dechiffrement, ici comme ailleurs dans le scanner. Voir la memoire
nearfield-tetra et ROADMAP.
"""
import json
import os
import socket
import threading
import time

ETAT = os.path.expanduser("~/autopilot/tetra_porteuses.json")
TETRA_SOCKET = "/tmp/tetra_pont.sock"   # prise tenue par compagnon_serveur (meteo_watch)
ARRET = os.path.expanduser("~/autopilot/tetra_off")   # interrupteur, comme prise_unique_off

# Bandes ou une porteuse peut etre du TETRA civil en France (zone d'ecoute de
# bonne foi). 380-400 MHz = Tetrapol/INPT (secours, chiffre) : volontairement
# EXCLU. Sert au mode manuel et a la decouverte au balayage.
BANDES = ((410_000_000, 430_000_000), (450_000_000, 470_000_000))

RASTER_HZ = 25_000          # pas des porteuses TETRA
TOLERANCE_HZ = 15_000       # une frequence a moins de ca d'une porteuse connue = la meme
GAIN_DEFAUT = 32            # verrou propre a Saint-Etienne sans ecretage (2026-10-26)
GAIN_MIN = 12
GAIN_MAX = 40
GAIN_PAS_BAISSE = 4         # a l'ecretage
GAIN_PAS_HAUSSE = 2         # signal faible, pas d'ecretage, pas de verrou

# Graines : la porteuse de controle confirmee + quelques pistes a explorer
# (releve de puissance du 2026-10-26 et notes de nuit). L'autodecouverte
# ajoute les autres.
GRAINES = [
    {"freq_hz": 429_600_000, "confirmee": True},   # controle, reseau 208-29, zone 11
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


def _ecrire(data):
    tmp = ETAT + ".tmp"
    os.makedirs(os.path.dirname(ETAT), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ETAT)


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


def candidats(nb=2):
    """Porteuses a sonder dans une fenetre : les confirmees les plus
    anciennement revues d'abord (on tient a jour leur identite et on y guette la
    voix), puis une piste d'exploration (autodecouverte). Rend une liste de
    (freq_hz, gain)."""
    with _verrou:
        data = _charger()
    ps = data["porteuses"]
    confirmees = sorted((p for p in ps if p.get("confirmee")), key=lambda p: p.get("dernier_essai", 0))
    autres = sorted((p for p in ps if not p.get("confirmee")), key=lambda p: p.get("dernier_essai", 0))
    choix = confirmees[:max(1, nb - 1)] if confirmees else []
    if autres and len(choix) < nb:
        choix.append(autres[0])      # une exploration par fenetre
    if not choix and ps:
        choix = ps[:nb]
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
        p["dernier_essai"] = time.time()
        if res.get("bsch"):
            p["verrous"] = p.get("verrous", 0) + 1
            p["dernier_verrou"] = time.time()
            p["confirmee"] = True
            if res.get("reseau"):
                p["reseau"] = res["reseau"]
        _ajuster_gain(p, res)
        _ecrire(data)
    return p


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


def decoder(freq_hz, gain, secondes, host=None, port=1234, ppm=0, delai=None):
    """Demande a compagnon_serveur (prise locale) un decodage TETRA en direct de
    freq_hz. Le Pi doit DEJA servir rtl_tcp sur host:port. Rend le dict resultat
    (voir bridge/tetra_neo), ou {"ok": False, "erreur": ...} si pas de machine
    compagne / pas de reponse. Ne leve pas."""
    req = {"freq_hz": int(freq_hz), "gain": gain, "secondes": int(secondes), "port": int(port), "ppm": ppm}
    if host:
        req["host"] = host
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(delai or (int(secondes) + 60))
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
