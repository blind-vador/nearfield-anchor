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
                                 controlee par le pont) {id, type, params, ticket,
                                 bornes} ; bornes = titre, plan, mode, capacites, zones,
                                 budget et empreinte du ticket valide par l'humain ;
                                 autorisation = pour un travail sensible, l'autorisation
                                 de l'humain (entree n du journal, empreinte, action) ;
                                 stdout : une ligne JSON par evenement
      {"etape": "<nom simple>", "phrase": "<texte>", "pourcentage": 0..100}
                                 relaye au Pi en message v2 "progression" (type ferme)
      {"fin": {"etat": "fait"|"echec", "sorties": ["fiche.md"], "message": "..."}}
                                 les sorties sont des fichiers du dossier du travail ;
                                 le pont les signe dans le resultat v2 comme d'habitude.
  programme --etat               stdout : UN objet JSON, l etat que le logiciel veut faire
                                 connaitre de cette machine a sa partie sur l ordonnateur ;
                                 nearfield ne le lit pas : il le joint au battement
                                 (champ "extensions" {nom: etat}), au plus une fois par
                                 minute, 8 Kio et 5 niveaux au plus, en 20 s au plus ; un
                                 programme qui ne connait pas --etat n envoie rien.
  programme --preavis            stdin : {"id", "genre": "arret"|"orage", "donnees"} ; le
                                 programme se met en securite comme il l'entend, puis rend
                                 UN objet JSON {"pret": true|false, "attente_s": n} ; borne
                                 par le preavis (90 s au plus). Un programme qui ne connait
                                 pas --preavis (code de sortie non nul) n'a rien a faire : pret.
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

ETAT_SEC = 60            # l etat se demande au plus une fois par minute
MAX_ETAT = 8192
MAX_TOTAL_ETATS = 20000  # le module du pont borne chaque donnee d un message a 24 000 octets
PROFONDEUR_ETAT = 5
MAX_LISTE_ETAT = 200
MAX_TEXTE_ETAT = 2000

_cache = {"t": 0.0, "externes": []}
_etats = {"t": 0.0, "etats": {}}
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


def _borne(v, prof=0):
    """Memes bornes que le module du pont (donnees d un message) : sinon tout le
    battement serait refuse."""
    if prof > PROFONDEUR_ETAT:
        return False
    if isinstance(v, str):
        return len(v.encode()) <= MAX_TEXTE_ETAT
    if isinstance(v, list):
        return len(v) <= MAX_LISTE_ETAT and all(_borne(x, prof + 1) for x in v)
    if isinstance(v, dict):
        return (len(v) <= MAX_LISTE_ETAT
                and all(isinstance(k, str) and len(k.encode()) <= 128 and _borne(x, prof + 1) for k, x in v.items()))
    return v is None or isinstance(v, (bool, int, float))


def _etat(argv):
    try:
        r = subprocess.run(argv + ["--etat"], capture_output=True, text=True, timeout=20,
                           env=environnement(), stdin=subprocess.DEVNULL)
        if r.returncode != 0 or len(r.stdout.encode()) > MAX_ETAT:
            return None
        e = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return e if isinstance(e, dict) and _borne(e) else None


def etats(dossier):
    """{nom: etat} des logiciels externes qui en donnent un (opaque pour nearfield)."""
    with _verrou:
        if time.time() - _etats["t"] <= ETAT_SEC:
            return dict(_etats["etats"])
        _etats["t"] = time.time()
    sortie, taille = {}, 0
    for nom, argv in declarations(dossier):
        e = _etat(argv)
        if e is None:
            continue
        taille += len(json.dumps(e)) + len(nom) + 8
        if taille > MAX_TOTAL_ETATS:  # le module du pont refuserait tout le battement
            break
        sortie[nom] = e
    with _verrou:
        _etats["etats"] = sortie
    return dict(sortie)


def _preavis(argv, preavis, delai):
    try:
        r = subprocess.run(argv + ["--preavis"], input=json.dumps(preavis), capture_output=True, text=True,
                           timeout=delai, env=environnement())
    except subprocess.TimeoutExpired:
        return False, None
    except OSError:
        return True, None
    if r.returncode != 0:
        return True, None
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return False, None
    if not isinstance(d, dict):
        return False, None
    a = d.get("attente_s")
    return bool(d.get("pret")), (a if isinstance(a, int) and not isinstance(a, bool) and 0 <= a <= 3600 else None)


def preavis(dossier, preavis_, delai=80):
    """Passe le preavis a chaque programme installe, en parallele ; (pret, attente_s)."""
    resultats = []
    fils = []
    for _nom, argv in declarations(dossier):
        t = threading.Thread(target=lambda a=argv: resultats.append(_preavis(a, preavis_, delai)), daemon=True)
        t.start()
        fils.append(t)
    for t in fils:
        t.join(delai + 5)
    if len(resultats) < len(fils):
        return False, None
    attentes = [a for p, a in resultats if not p and a]
    return all(p for p, _ in resultats), (max(attentes) if attentes else None)


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
        p.stdin.write(json.dumps({k: job.get(k) for k in ("id", "type", "params", "ticket", "bornes", "autorisation")}))
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
        # Un travail peut changer ce que sait faire le programme (outil installe, compte
        # connecte) : ses capacites se relisent au prochain battement, pas 5 minutes plus tard.
        with _verrou:
            _cache["t"] = 0
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
