"""Ce que vaut le scanner, mesure (2026-11-11, plan phase 6, audit point 9).

Quatre evenements, comptes par jour et par bande :
  prise         une ouverture gardee au journal ;
  rejet         une ouverture ecartee automatiquement (bruit, parasite) ;
  fausse_alarme une prise que l'utilisateur rejette comme bruit dans le journal ;
  manquee       un rejet repeche (par l'utilisateur ou l'IA du Mac) : il fallait le garder.
D'ou, sur les 30 derniers jours (lecture Neyman-Pearson / ROC : on suit les deux
erreurs, pas une seule) :
  precision  = 1 - fausses alarmes / prises      (ce qu'on garde est-il bon ?)
  rappel     = prises bonnes / (prises bonnes + manquees)   (ce qui est bon, le garde-t-on ?)
Lu dans la commande "bilan" ; chaque reglage peut ainsi se juger sur les deux.
"""
import datetime
import fcntl
import json
import os
import time

import chemins

PATH = chemins.base("mesures_scanner.json")
LOCK = "/tmp/mesures_scanner.lock"
EVENEMENTS = ("prise", "rejet", "fausse_alarme", "manquee")
GARDE_JOURS = 90
FENETRE_JOURS = 30
PRISES_MIN = 10      # en dessous, une bande n'est pas jugee


def _lire():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def noter(evenement, bande=None, maintenant=None):
    if evenement not in EVENEMENTS:
        return
    jour = datetime.date.fromtimestamp(maintenant or time.time()).isoformat()
    bande = " ".join(str(bande or "inconnue").split())[:60]
    try:
        with open(LOCK, "w") as verrou:
            fcntl.flock(verrou, fcntl.LOCK_EX)
            data = _lire()
            c = data.setdefault(jour, {}).setdefault(bande, {})
            c[evenement] = c.get(evenement, 0) + 1
            limite = (datetime.date.fromtimestamp(time.time()) - datetime.timedelta(days=GARDE_JOURS)).isoformat()
            for j in [j for j in data if j < limite]:
                del data[j]
            tmp = PATH + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, PATH)
    except OSError as exc:
        print(f"[mesures] {evenement} non note : {exc}")


def totaux(jours=FENETRE_JOURS, maintenant=None):
    """{bande: {evenement: n}} sur la fenetre."""
    debut = (datetime.date.fromtimestamp(maintenant or time.time()) - datetime.timedelta(days=jours)).isoformat()
    out = {}
    for jour, bandes in _lire().items():
        if jour < debut:
            continue
        for bande, c in bandes.items():
            t = out.setdefault(bande, {e: 0 for e in EVENEMENTS})
            for e, n in c.items():
                if e in t:
                    t[e] += n
    return out


def taux(c):
    """(precision, rappel, part des ouvertures ecartees) ; None quand on ne sait pas."""
    prises, fausses, manquees, rejets = c["prise"], c["fausse_alarme"], c["manquee"], c["rejet"]
    precision = 1 - fausses / prises if prises else None
    bonnes = max(prises - fausses, 0)
    rappel = bonnes / (bonnes + manquees) if (bonnes + manquees) else None
    ecartees = rejets / (rejets + prises) if (rejets + prises) else None
    return precision, rappel, ecartees


def phrase():
    """Une phrase pour le bilan, ou "" sans assez de prises."""
    from phrases import dire
    t = totaux()
    tout = {e: sum(c[e] for c in t.values()) for e in EVENEMENTS}
    if tout["prise"] < PRISES_MIN:
        return ""
    precision, rappel, ecartees = taux(tout)
    morceaux = [dire("mesures.general", prises=tout["prise"], precision=round(100 * precision),
                     fausses=tout["fausse_alarme"], manquees=tout["manquee"],
                     rappel=round(100 * rappel) if rappel is not None else 100)]
    # la bande qui se trompe le plus, si elle a assez de prises
    pires = [(taux(c)[0], b) for b, c in t.items() if c["prise"] >= PRISES_MIN and taux(c)[0] is not None]
    if pires:
        p, b = min(pires)
        if p < 0.8:
            morceaux.append(dire("mesures.pire_bande", bande=b, precision=round(100 * p)))
    return " ".join(morceaux)
