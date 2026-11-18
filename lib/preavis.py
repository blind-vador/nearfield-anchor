"""Préavis aux logiciels installés à part et aux machines (2026-11-18).

nearfield est un point de passage : il prévient, attend une réponse bornée, puis n'agit que
sur le Pi lui-même (l'arrêter). Ce que les logiciels font d'un préavis ne le regarde pas.

Genres (liste fermée, comme le message v2 « preavis ») :
  arret  {action: redemarrage|extinction, raison: utilisateur|wifi|orage, delai_max_s}
  orage  {niveau: approche|proche|fini, source: utilisateur|capteur, distance_km}

Destinataires :
  - extensions du point d'accès abonnées aux événements (genre « preavis »), qui répondent
    par la requête « preavis_pret » (lib/extensions.py) ;
  - machines connectées en v2 : message « preavis », réponse « preavis_recu » ; leur pont
    passe le préavis aux programmes installés à part (programme --preavis).
On attend que tous aient répondu « prêt », ou la borne du préavis.

Un seul chemin pour arrêter le Pi : arreter_pi() (console, réparation du Wi-Fi). La demande
est un fichier local que compagnon-watch (qui tient les sessions) prend en charge ; s'il ne
la prend pas (service arrêté), l'appelant arrête le Pi lui-même.

Orage (sources déjà présentes dans nearfield) :
  - observation de l'utilisateur (console « observation » : tonnerre au loin, proche, tout
    près), valable OBS_SEC ;
  - capteur de foudre sans fil (capteurs_watch) : le sien à 5 km ou moins donne « proche » ;
    un capteur voisin, ou plus loin (15 km au plus), « approche ».
  Les orages des aéroports (METAR, souvent à 30 km ou plus) ne déclenchent pas de préavis :
  arrêter les travaux pour eux serait trop fréquent.
  Une source antérieure au démarrage du Pi ne compte pas (on ne s'éteint pas en boucle).
  « fini » : plus aucune source depuis FIN_SEC.
Foudre « proche » (source fiable : l'utilisateur ou son capteur) : les logiciels se mettent en
sécurité ; puis, si le Pi n'est pas déclaré protégé (console « protection électrique »), il
s'éteint en invitant l'utilisateur à débrancher les machines du secteur.

État : LOCAL_DIR/v2/preavis/ (protection.json, demande.json) ; l'état de l'orage est dans
/tmp (il repart de zéro à chaque démarrage).
"""
import json
import os
import subprocess
import threading
import time
import uuid

import chemins

DOSSIER = os.path.join(chemins.base("compagnon"), "v2", "preavis")
DEMANDE = os.path.join(DOSSIER, "demande.json")
PROTECTION = os.path.join(DOSSIER, "protection.json")
ETAT_ORAGE = "/tmp/nearfield_orage.json"
OBSERVATION = "/tmp/observation_meteo.json"      # meteo_obs.RECENT_PATH
FOUDRE = "/tmp/foudre_locale.json"               # capteurs_watch._publish_foudre

ACTIONS = {"redemarrage": "reboot", "extinction": "poweroff"}
DELAI_ARRET_S = 90        # borne d'un préavis d'arrêt demandé par l'utilisateur
DELAI_WIFI_S = 20         # sans réseau, les machines ne répondront pas
DELAI_ORAGE_S = 90
DELAI_PRISE_S = 15        # compagnon-watch prend une demande d'arrêt en moins de PAS_S
PAS_S = 5
OBS_SEC = 30 * 60
FOUDRE_SEC = 15 * 60
FIN_SEC = 30 * 60
PROCHE_KM = 5
APPROCHE_KM = 15
NIVEAUX = {None: 0, "fini": 0, "approche": 1, "proche": 2}

_attente = {}             # préavis en cours -> {"attendus", "prets", "pas_prets"}
_verrou = threading.Lock()
_arret_en_cours = threading.Event()


def _dire(cle, defaut, **v):
    """La phrase de phrases/fr.ini, ou le texte du programme."""
    try:
        from phrases import base, dire
        if cle in base():
            return dire(cle, **v)
    except Exception:
        pass
    return defaut.format(**v)


def _annoncer(texte):
    print(f"[preavis] {texte}", flush=True)
    try:
        import sons
        import veille_donnees as vd
        vd.announce(texte, sons.chemin("meteo_foudre", None), "preavis", "preavis")
    except Exception as exc:
        print(f"[preavis] annonce impossible : {exc!r}", flush=True)


def _lire(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _ecrire(chemin, d):
    os.makedirs(os.path.dirname(chemin), mode=0o700, exist_ok=True)
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, chemin)


def demarrage_du_pi():
    try:
        with open("/proc/uptime") as f:
            return time.time() - float(f.read().split()[0])
    except (OSError, ValueError):
        return 0.0


# --- protection électrique déclarée par l'utilisateur -------------------------------------

def protection():
    """La déclaration de l'utilisateur ({le, t}) ou None."""
    return _lire(PROTECTION)


def declarer_protection():
    d = {"t": time.time(), "le": time.strftime("%Y-%m-%d %H:%M"), "declare_par": "utilisateur",
         "responsabilite": "acceptee"}
    _ecrire(PROTECTION, d)
    return d


def retirer_protection():
    try:
        os.remove(PROTECTION)
        return True
    except FileNotFoundError:
        return False


# --- diffusion d'un préavis et réponses ---------------------------------------------------

def reponse(pid, qui, pret, attente_s=None):
    """Réponse d'un destinataire (extension ou machine) à un préavis en cours."""
    with _verrou:
        a = _attente.get(pid)
        if a is None or qui not in a["attendus"]:
            return False
        if pret:
            a["prets"].add(qui)
            a["pas_prets"].pop(qui, None)
        else:
            a["pas_prets"][qui] = attente_s
        return True


def _machines_v2():
    try:
        import pont_v2
        return list(pont_v2._sessions)
    except Exception:
        return []


def emettre(genre, donnees, delai_s, envoyer_machine=None, diffuser_extensions=None, maintenant=time.time):
    """Diffuse le préavis et attend les « prêt », au plus delai_s secondes.
    Rend {"preavis", "attendus", "prets", "manquants"}."""
    pid = "pv-" + uuid.uuid4().hex[:12]
    if envoyer_machine is None:
        import pont_v2
        envoyer_machine = pont_v2.envoyer_brut
    if diffuser_extensions is None:
        import extensions
        diffuser_extensions = extensions.preavis_diffuser
    attendus = set()
    with _verrou:
        _attente[pid] = {"attendus": attendus, "prets": set(), "pas_prets": {}}
    try:
        for eid in diffuser_extensions(pid, genre, donnees) or []:
            with _verrou:
                attendus.add("extension:" + eid)
        for cid in _machines_v2():
            if envoyer_machine(cid, {"type": "preavis", "id": pid, "genre": genre, "donnees": donnees}):
                with _verrou:
                    attendus.add("machine:" + cid)
        fin = maintenant() + delai_s
        while maintenant() < fin:
            with _verrou:
                if attendus <= _attente[pid]["prets"]:
                    break
            time.sleep(0.5)
        with _verrou:
            a = _attente.pop(pid)
        manquants = sorted(a["attendus"] - a["prets"])
        print(f"[preavis] {genre} {donnees} : {len(a['prets'])} prêt(s) sur {len(a['attendus'])}"
              + (f", sans réponse : {', '.join(manquants)}" if manquants else ""), flush=True)
        return {"preavis": pid, "attendus": sorted(a["attendus"]), "prets": sorted(a["prets"]),
                "manquants": manquants}
    finally:
        with _verrou:
            _attente.pop(pid, None)


# --- arrêt du Pi : un seul chemin ----------------------------------------------------------

def _systemctl(action):
    proc = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", ACTIONS[action]],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode:
        print(f"[preavis] systemctl {ACTIONS[action]} : {proc.stderr.strip()}", flush=True)
    return proc.returncode == 0


def arreter_pi(action, raison, attendre=True):
    """Demande l'arrêt du Pi (action : redemarrage ou extinction). compagnon-watch prévient les
    logiciels et les machines, puis arrête le Pi ; s'il ne prend pas la demande, on arrête ici.
    attendre=False : arrêt immédiat, sans préavis (« redémarre maintenant »).
    Rend "preavis" (pris en charge), "direct" (arrêté ici) ou "echec"."""
    if action not in ACTIONS:
        raise ValueError(action)
    if attendre:
        rid = uuid.uuid4().hex[:8]
        _ecrire(DEMANDE, {"id": rid, "action": action, "raison": raison, "t": time.time(), "pris": False})
        fin = time.time() + DELAI_PRISE_S
        while time.time() < fin:
            d = _lire(DEMANDE)
            if d and d.get("id") == rid and d.get("pris"):
                return "preavis"
            time.sleep(0.5)
        try:
            os.remove(DEMANDE)
        except OSError:
            pass
    return "direct" if _systemctl(action) else "echec"


def _executer_demande(d):
    """Dans compagnon-watch : préavis, puis arrêt."""
    if _arret_en_cours.is_set():
        return
    _arret_en_cours.set()
    try:
        delai = DELAI_WIFI_S if d.get("raison") == "wifi" else DELAI_ARRET_S
        emettre("arret", {"action": d["action"], "raison": d.get("raison") or "utilisateur", "delai_max_s": delai},
                delai)
        _systemctl(d["action"])
    finally:
        _arret_en_cours.clear()


def prendre_demande():
    d = _lire(DEMANDE)
    if not d or d.get("pris") or d.get("action") not in ACTIONS or time.time() - d.get("t", 0) > 60:
        return None
    d["pris"] = True
    _ecrire(DEMANDE, d)
    threading.Thread(target=_executer_demande, args=(dict(d),), daemon=True).start()
    return d


# --- orage --------------------------------------------------------------------------------

def sources_orage(maintenant=None, demarrage=None):
    """[(niveau, source, distance_km, t)] des sources d'orage présentes."""
    maintenant = maintenant or time.time()
    demarrage = demarrage_du_pi() if demarrage is None else demarrage
    out = []
    obs = _lire(OBSERVATION)
    if obs and demarrage <= obs.get("t", 0) and maintenant - obs["t"] <= OBS_SEC:
        o = (obs.get("reponses") or {}).get("orage")
        if o in ("proche", "tout_pres"):
            out.append(("proche", "utilisateur", None, obs["t"]))
        elif o == "loin":
            out.append(("approche", "utilisateur", None, obs["t"]))
    f = _lire(FOUDRE)
    if f and demarrage <= f.get("t", 0) and maintenant - f["t"] <= FOUDRE_SEC:
        km = f.get("storm_dist_km")
        km = km if isinstance(km, (int, float)) else None
        if f.get("mien") and km is not None and km <= PROCHE_KM:
            out.append(("proche", "capteur", km, f["t"]))
        elif km is None or km <= APPROCHE_KM:
            out.append(("approche", "capteur", km, f["t"]))
    return out


def evaluer(etat, sources, maintenant):
    """Nouvel état de l'orage et préavis à diffuser (ou None). Fonction pure."""
    etat = dict(etat or {"niveau": None})
    meilleure = max(sources, key=lambda s: NIVEAUX[s[0]], default=None)
    if meilleure:
        etat["vu"] = maintenant
    courant = etat.get("niveau")
    if meilleure and NIVEAUX[meilleure[0]] > NIVEAUX[courant]:
        etat.update(niveau=meilleure[0], source=meilleure[1], distance_km=meilleure[2], depuis=maintenant)
        return etat, {"niveau": meilleure[0], "source": meilleure[1], "distance_km": meilleure[2]}
    if courant in ("approche", "proche") and maintenant - etat.get("vu", maintenant) >= FIN_SEC:
        etat.update(niveau="fini", depuis=maintenant)
        return etat, {"niveau": "fini", "source": etat.get("source"), "distance_km": None}
    return etat, None


def _texte_orage(d, protege):
    n, s, km = d["niveau"], d["source"], d.get("distance_km")
    de = {"utilisateur": _dire("preavis.source_utilisateur", "tu entends le tonnerre"),
          "capteur": _dire("preavis.source_capteur", "capteur de foudre")}.get(s, s)
    if n == "approche":
        return _dire("preavis.orage_approche",
                     "Orage en approche ({de}). Les machines finissent proprement leur travail et n'en prennent plus.",
                     de=de)
    if n == "fini":
        return _dire("preavis.orage_fini", "Plus de signe d'orage depuis une demi-heure : les machines reprennent.")
    loin = _dire("preavis.distance", ", à environ {km} kilomètres", km=round(km)) if km is not None else ""
    texte = _dire("preavis.orage_proche",
                  "Foudre tout près ({de}{loin}). Les machines mettent leurs travaux en sécurité et s'éteignent.",
                  de=de, loin=loin)
    if protege:
        return texte + " " + _dire("preavis.pi_reste",
                                   "Le Pi reste allumé : tu l'as déclaré protégé, sous ta responsabilité.")
    return texte + " " + _dire("preavis.pi_s_eteint",
                               "Le Pi s'éteindra ensuite. Quand il se sera tu, débranche du secteur le Pi et les "
                               "autres machines.")


def _diffuser_orage(d):
    protege = protection() is not None
    _annoncer(_texte_orage(d, protege))
    emettre("orage", d, DELAI_ORAGE_S)
    if d["niveau"] == "proche" and not protege and not _arret_en_cours.is_set():
        _arret_en_cours.set()
        _annoncer(_dire("preavis.extinction_orage",
                        "Extinction du Pi pour l'orage. Débranche maintenant le Pi et les machines du secteur."))
        time.sleep(8)
        _systemctl("extinction")


def tour(maintenant=None, diffuser=None):
    """Un tour de la veille (compagnon-watch) : demande d'arrêt, puis orage."""
    prendre_demande()
    maintenant = maintenant or time.time()
    etat, d = evaluer(_lire(ETAT_ORAGE), sources_orage(maintenant), maintenant)
    _ecrire(ETAT_ORAGE, etat)
    if d:
        threading.Thread(target=diffuser or _diffuser_orage, args=(d,), daemon=True).start()
    return d


def _boucle():
    while True:
        try:
            tour()
        except Exception as exc:  # la veille ne tombe jamais
            print(f"[preavis] erreur : {exc!r}", flush=True)
        time.sleep(PAS_S)


def demarrer():
    threading.Thread(target=_boucle, daemon=True, name="preavis").start()
