"""Puissance reelle et disponibilite d'une machine, commun au Pi et aux
machines compagnes (Python 3.9 et plus, bibliotheque standard seule).

Demande utilisateur (2026-10-25) : la machine la plus libre epaule le Pi, et
"libre" se juge sur la puissance REELLE de chaque machine, sa chaleur et sa
charge du moment, pas sur un nom ou un nombre de coeurs.

- Etalonnage : le meme petit calcul (un filtre en Python pur, proche du
  traitement du signal des decodeurs) tourne sur tous les coeurs a la fois ;
  on garde le meilleur score de la semaine (une machine occupee pendant
  l'etalonnage ne se sous-estime pas pour de bon). Les scores sont donc
  comparables d'une machine a l'autre : un Mac M4 et un Pi 4 se mesurent avec
  la meme regle.
- A chaque battement : charge (moyenne 1 min / coeurs), chaleur (sonde du Pi,
  limitation thermique de macOS), memoire disponible, et un mini-test de
  vitesse sur un coeur (0,1 s) qui trahit un processeur ralenti par la chaleur.
- disponible = puissance totale x part libre x facteur chaleur x facteur
  memoire, moins une reserve pour la personne au clavier (elle peut s'en
  servir pendant que la machine travaille, mais garde de la marge).

En ligne de commande : python3 puissance.py        -> mesure en JSON
                       python3 puissance.py --bench S  (usage interne)
"""
import json
import os
import re
import subprocess
import sys
import time

BENCH_SEC = 0.5               # etalonnage, par coeur
VITESSE_SEC = 0.1             # mini-test a chaque mesure
ETALON_VALIDE_SEC = 7 * 86400  # meilleur score garde une semaine
ETALON_REFAIT_SEC = 6 * 3600   # on retente plus haut toutes les 6 h
# Chaleur sous Linux (Pi) : pleine dispo sous FROID_C, plus rien a LIMITE_C
# (le scanner du Pi se met en pause vers 72 C).
FROID_C = 55.0
LIMITE_C = 75.0
MEMOIRE_PLEINE_MO = 1024       # au-dessus : aucune gene
MEMOIRE_MIN_MO = 200           # en dessous : on ne charge plus
RESERVE_UTILISATEUR = 0.25     # part laissee libre quand quelqu'un est au clavier
CLAVIER_ACTIF_S = 120


def _noyau():
    """Une passe du calcul de reference : filtre de 16 coefficients sur 240
    echantillons. Le meme partout, c'est l'unite de mesure."""
    x = [((i * 7919) % 1000) / 1000.0 for i in range(256)]
    h = [1.0 / (k + 1) for k in range(16)]
    acc = 0.0
    for i in range(16, 256):
        s = 0.0
        for k in range(16):
            s += x[i - k] * h[k]
        acc += s
    return acc


def bench(seconds):
    """Passes par seconde sur un coeur."""
    n, start = 0, time.perf_counter()
    end = start + seconds
    while time.perf_counter() < end:
        _noyau()
        n += 1
    return n / (time.perf_counter() - start)


def coeurs():
    return os.cpu_count() or 1


def etalonner(seconds=BENCH_SEC):
    """(score total, score d'un coeur) : le calcul lance sur tous les coeurs en
    meme temps (processus separes, pas de verrou Python commun)."""
    procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "--bench", str(seconds)],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
             for _ in range(coeurs())]
    scores = []
    for p in procs:
        try:
            out, _ = p.communicate(timeout=seconds * 20 + 10)
            scores.append(float(out.strip() or 0))
        except (subprocess.TimeoutExpired, ValueError):
            p.kill()
    return sum(scores), bench(seconds)


def etalon(chemin):
    """Etalon garde dans chemin (JSON) : refait si absent, trop vieux, ou toutes
    les 6 h pour tenter mieux ; on garde le meilleur de la semaine."""
    try:
        with open(chemin, encoding="utf-8") as f:
            e = json.load(f)
    except (OSError, ValueError):
        e = {}
    now = time.time()
    if e.get("total") and now - e.get("t", 0) < ETALON_VALIDE_SEC and now - e.get("essai", 0) < ETALON_REFAIT_SEC:
        return e
    total, coeur = etalonner()
    perime = not e.get("total") or now - e.get("t", 0) >= ETALON_VALIDE_SEC
    if perime or total > e["total"]:
        coeur = coeur if perime else max(coeur, e.get("coeur", 0))
        e = {"total": round(total, 1), "coeur": round(coeur, 1), "coeurs": coeurs(), "t": now}
    e["essai"] = now
    try:
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(e, f)
        os.replace(tmp, chemin)
    except OSError:
        pass
    return e


# --- etat du moment -------------------------------------------------------------------

def _borne(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def charge():
    try:
        return _borne(os.getloadavg()[0] / coeurs(), 0.0, 4.0)
    except OSError:
        return 0.0


def chaleur():
    """(facteur 0..1, degres ou None). Linux : sonde du processeur. macOS :
    limites imposees par le systeme (pmset), le mini-test fait le reste."""
    if sys.platform == "darwin":
        facteur = 1.0
        try:
            out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=5).stdout
            for cle in ("CPU_Speed_Limit", "CPU_Scheduler_Limit"):
                m = re.search(cle + r"\s*=\s*(\d+)", out)
                if m:
                    facteur *= int(m.group(1)) / 100
            if re.search(r"(?i)thermal warning level\s*=?\s*[1-9]", out):
                facteur *= 0.5
        except (OSError, subprocess.TimeoutExpired):
            pass
        return _borne(facteur), None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            t = int(f.read()) / 1000
        return _borne((LIMITE_C - t) / (LIMITE_C - FROID_C)), round(t, 1)
    except (OSError, ValueError):
        return 1.0, None


def memoire_libre_mo():
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+)", out).group(1))
            pages = sum(int(re.search(r"Pages %s:\s*(\d+)" % k, out).group(1))
                        for k in ("free", "inactive", "speculative", "purgeable"))
            return pages * page // (1024 * 1024)
        with open("/proc/meminfo") as f:
            m = re.search(r"MemAvailable:\s*(\d+)", f.read())
        return int(m.group(1)) // 1024
    except (OSError, AttributeError, ValueError, subprocess.TimeoutExpired):
        return None


def clavier_actif(etat):
    """etat : session_state() du pont (verrouille, inactif_s), ou None."""
    if not etat or etat.get("verrouille"):
        return False
    inactif = etat.get("inactif_s")
    return inactif is not None and inactif < CLAVIER_ACTIF_S


def mesure(chemin_etalon, etat=None):
    """Ce que la machine dit d'elle au Pi a chaque battement."""
    e = etalon(chemin_etalon)
    total, coeur = e.get("total") or 0.0, e.get("coeur") or 0.0
    ch = charge()
    fact_chaleur, temp = chaleur()
    vitesse = _borne(bench(VITESSE_SEC) / coeur) if coeur else 1.0
    if ch < 0.7:  # processeur peu charge mais lent : c'est la chaleur qui le bride
        fact_chaleur = min(fact_chaleur, vitesse)
    mem = memoire_libre_mo()
    fact_mem = 1.0 if mem is None else _borne((mem - MEMOIRE_MIN_MO) / (MEMOIRE_PLEINE_MO - MEMOIRE_MIN_MO))
    reserve = RESERVE_UTILISATEUR if clavier_actif(etat) else 0.0
    libre = _borne(1.0 - ch - reserve)
    return {"puissance": total, "puissance_coeur": coeur, "coeurs": coeurs(), "charge": round(ch, 2),
            "vitesse": round(vitesse, 2), "chaleur": round(fact_chaleur, 2), "chaleur_c": temp,
            "memoire_libre_mo": mem, "reserve": reserve,
            "disponible": round(total * libre * fact_chaleur * fact_mem, 1), "t": time.time()}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--bench":
        print(bench(float(sys.argv[2])))
    else:
        import tempfile
        print(json.dumps(mesure(os.path.join(tempfile.gettempdir(), "puissance_etalon.json")), indent=1))
