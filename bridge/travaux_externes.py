"""Travaux externes du pont (2026-11-17) : un logiciel installe a part sur la machine
compagne (par exemple Prismaforge, ses agents par compte) apporte des capacites au parc
sans que son code entre dans nearfield.

Declaration (locale, ecrite par l'installeur de ce logiciel, jamais par le reseau) :
  ~/.nearfield-bridge/externes/<nom>.json
      {"nom": "prismaforge", "programme": ["/usr/bin/python3", "-I", "/chemin/travail.py"]}
  Le programme est un chemin absolu ; ses arguments sont fixes par la declaration.

Contrat (ferme) avec le programme :
  programme --capacites          stdout : liste JSON de noms simples (ses capacites du
                                 moment ; il les sonde lui-meme), en 30 s au plus
  programme --travail <dossier>  stdin : le travail VERIFIE (signature de l'ordonnateur
                                 controlee par le pont) {id, type, params, ticket} ;
                                 stdout : une ligne JSON par evenement
      {"etape": "<nom simple>", "phrase": "<texte>", "pourcentage": 0..100}
                                 relaye au Pi en message v2 "progression" (type ferme)
      {"fin": {"etat": "fait"|"echec", "sorties": ["fiche.md"], "message": "..."}}
                                 les sorties sont des fichiers du dossier du travail ;
                                 le pont les signe dans le resultat v2 comme d'habitude.
Le programme ne recoit aucune cle du pont ; il tourne avec un environnement reduit, dans son
propre groupe de processus, arrete a l'echeance.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time

NOM = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
NOM_SORTIE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
CACHE_SEC = 300          # les capacites se sondent au plus toutes les 5 minutes
MAX_PHRASE = 2000
MAX_EVENEMENTS = 3000    # par travail : au-dela, plus rien n'est relaye (le travail continue)
MAX_SECONDES = 6 * 3600
GARDEES = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR")

_cache = {"t": 0.0, "externes": []}
_verrou = threading.Lock()


def environnement():
    env = {k: v for k, v in os.environ.items() if k in GARDEES or k.startswith("LC_")}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return env


def declarations(dossier):
    """[(nom, argv)] des declarations valides du dossier."""
    sortie = []
    try:
        noms = sorted(os.listdir(dossier))
    except OSError:
        return sortie
    for f in noms:
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(dossier, f), encoding="utf-8") as x:
                d = json.load(x)
            nom, argv = d["nom"], d["programme"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if (isinstance(nom, str) and NOM.match(nom) and isinstance(argv, list) and 0 < len(argv) <= 8
                and all(isinstance(a, str) and a and "\0" not in a for a in argv) and os.path.isabs(argv[0])
                and os.access(argv[0], os.X_OK)):
            sortie.append((nom, argv))
    return sortie


def _sonder(argv):
    try:
        r = subprocess.run(argv + ["--capacites"], capture_output=True, text=True, timeout=30,
                           env=environnement(), stdin=subprocess.DEVNULL)
        caps = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    if not isinstance(caps, list):
        return []
    return sorted({c for c in caps if isinstance(c, str) and NOM.match(c)})[:32]


def externes(dossier, force=False):
    """[{"nom", "programme", "capacites"}], sondes au plus toutes les CACHE_SEC."""
    with _verrou:
        if force or time.time() - _cache["t"] > CACHE_SEC:
            _cache["externes"] = [{"nom": n, "programme": a, "capacites": _sonder(a)}
                                  for n, a in declarations(dossier)]
            _cache["t"] = time.time()
        return list(_cache["externes"])


def capacites(dossier):
    return sorted({c for e in externes(dossier) for c in e["capacites"]})


def pour(dossier, type_):
    """La declaration qui sait faire ce type, ou None."""
    for e in externes(dossier):
        if type_ in e["capacites"]:
            return e
    return None


def _echeance(job):
    d = (job.get("params") or {}).get("duree")
    if isinstance(d, int) and not isinstance(d, bool) and 0 < d <= MAX_SECONDES:
        return d + 120  # le programme borne lui-meme son agent ; ceci est le filet
    return MAX_SECONDES


def executer(externe, job, dossier, relayer, journal=print):
    """Lance le programme sur ce travail : (etat, sorties, message), comme les autres
    traitements du pont. relayer(etape, phrase, pourcentage) envoie la progression."""
    p = subprocess.Popen(externe["programme"] + ["--travail", dossier], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environnement(),
                         cwd=dossier, start_new_session=True)
    arret = {"delai": False}

    def tuer():
        arret["delai"] = True
        try:
            os.killpg(p.pid, signal.SIGTERM)
            time.sleep(5)
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    minuterie = threading.Timer(_echeance(job), tuer)
    minuterie.daemon = True
    minuterie.start()
    erreurs = []
    lecteur = threading.Thread(target=lambda: erreurs.append(p.stderr.read()[-2000:]), daemon=True)
    lecteur.start()
    fin, n = None, 0
    try:
        p.stdin.write(json.dumps({k: job.get(k) for k in ("id", "type", "params", "ticket")}))
        p.stdin.close()
        for ligne in p.stdout:
            try:
                m = json.loads(ligne)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            if isinstance(m.get("fin"), dict):
                fin = m["fin"]
                continue
            etape, phrase = m.get("etape"), m.get("phrase")
            if not (isinstance(etape, str) and NOM.match(etape)) or n >= MAX_EVENEMENTS:
                continue
            pct = m.get("pourcentage")
            pct = pct if isinstance(pct, int) and not isinstance(pct, bool) and 0 <= pct <= 100 else 0
            phrase = "".join(c if c.isprintable() else " " for c in str(phrase or ""))
            phrase = " ".join(phrase.split())[:MAX_PHRASE] or None
            n += 1
            try:
                relayer(etape, phrase, pct)
            except Exception as exc:  # une coupure du lien n'arrete pas le travail
                journal("progression non relayee : %r" % exc)
        p.wait()
    finally:
        minuterie.cancel()
        if p.poll() is None:
            tuer()
        lecteur.join(10)
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except OSError:
                pass
    if arret["delai"]:
        return "echec", [], "delai depasse"
    if not fin:
        return "echec", [], "le programme externe n'a pas rendu de fin (%s)" % (
            (erreurs[0] if erreurs else "").strip().splitlines()[-1:] or ["code %s" % p.returncode])[0][:300]
    etat = fin.get("etat") if fin.get("etat") in ("fait", "echec") else "echec"
    sorties = [s for s in fin.get("sorties") or [] if isinstance(s, str) and NOM_SORTIE.match(s)
               and ".." not in s and os.path.isfile(os.path.join(dossier, s))][:16]
    return etat, sorties, str(fin.get("message") or "")[:1000]
