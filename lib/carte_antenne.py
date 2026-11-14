"""Carte de l'antenne entretenue en continu (2026-11-14, demande utilisateur : "le
graphe de l'antenne devrait etre fait automatiquement tout le temps, entretenu a
chaque fois qu'on passe sur une frequence, et on devrait pouvoir lire un
audiographe actualise, voire l'activer en direct pour ecouter les variations et
les deplacements de l'antenne").

Entretien : chaque passe rtl_power du scanner (balayages des bandes, recherches
DMR et TETRA, mesure "antenne") passe ici (noter). Rien n'est mesure en plus : on
garde ce que le scanner voit de toute facon. Pour chaque segment de spectre :
  courbe      niveaux au-dessus du bruit de fond (lib/audiographe.courbe, POINTS)
  plancher    bruit de fond (20e percentile), et son historique (PLANCHERS_MAX)
  pic         frequence et niveau du signal le plus fort ; pics au-dessus du seuil
  t           heure de la passe
La carte est sur la carte SD (CARTE) : elle survit aux redemarrages.

Audiographe actualise : "antenne son" rend toute la carte en son, bande par bande
(console/antenne_cmd.py).

En direct ("antenne direct") : la console demande un segment (demander_direct, a
renouveler toutes les quelques secondes) ; le scanner suspend son cycle et repasse
ce segment en boucle (radio_watch._antenne_direct -> mesure_directe), chaque
resultat publie (publier_direct) ; la console le rend en son a chaque passe : le
balayage, puis la FORCE (moyenne des niveaux les plus hauts) en hauteur de son,
un carillon montant quand l'antenne fait mieux que tout ce qu'on a eu dans la
seance (le "centrage"), un ton grave quand elle perd nettement. Scanner arrete :
la console fait les passes elle-meme.
"""
import json
import os
import statistics
import time

import chemins

CARTE = chemins.base("carte_antenne.json")
BANDES = chemins.base("carte_antenne_bandes.json")   # bandes du scanner, pour la console
DIRECT_DEMANDE = "/tmp/antenne_direct.json"
DIRECT_RESULTAT = "/tmp/antenne_direct_resultat.json"
DIRECT_FRAIS_SEC = 8           # la console renouvelle sa demande ; sans nouvelles : fin du direct
LARGEUR_MIN_HZ = 1_000_000     # passes plus etroites (verification d'une frequence) : pas une carte
POINTS = 200
PLANCHERS_MAX = 48
FORCE_PART = 0.05              # force : moyenne des 5 % de points les plus hauts de la courbe
UHF = {"nom": "uhf_400_470", "dit": "UHF 400 à 470 mégahertz", "low_hz": 400_000_000,
       "high_hz": 470_000_000, "step_hz": 25_000}
POINTS_DIRECT = 500            # pas d'une passe en direct : la bande en environ 500 points

_bandes = []                   # [(low, high, nom, dit)] : radio_watch les declare au demarrage


def declarer(bands):
    """Bandes connues (radio_watch.BANDS, type sweep) : pour nommer les segments."""
    _bandes[:] = [(b["low_hz"], b["high_hz"], b["name"], b.get("name_spoken", b["name"]))
                  for b in bands if b.get("type") == "sweep" and b.get("low_hz")]
    _ecrire(BANDES, _bandes)


def _connues():
    if not _bandes:
        _bandes[:] = [tuple(b) for b in (_lire(BANDES) or [])]
    return _bandes


def _nommer(f0, f1):
    uhf = (UHF["low_hz"], UHF["high_hz"], UHF["nom"], UHF["dit"])
    for low, high, nom, dit in _connues() + [uhf]:
        recouvre = min(f1, high) - max(f0, low)
        if recouvre > 0.8 * (f1 - f0) and recouvre > 0.8 * (high - low):
            return nom, dit
    a, b = round(f0 / 1e6, 1), round(f1 / 1e6, 1)
    return f"{a:g}_{b:g}", f"{a:g} à {b:g} mégahertz".replace(".", ",")


def _lire(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _ecrire(chemin, data):
    tmp = f"{chemin}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, chemin)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def force(courbe):
    """Ce que l'antenne apporte sur le segment : moyenne des niveaux les plus hauts
    au-dessus du bruit de fond (dB). Monte quand l'antenne est mieux orientee."""
    db = sorted(courbe.get("db") or [], reverse=True)
    if not db:
        return 0.0
    n = max(1, int(len(db) * FORCE_PART))
    return round(sum(db[:n]) / n, 1)


def resumer(bins, t=None, nom=None, dit=None):
    """bins [(freq, db)] d'une passe -> entree de carte (dict), ou None."""
    import audiographe
    if len(bins) < 10:
        return None
    f0, f1 = min(f for f, _ in bins), max(f for f, _ in bins)
    if f1 - f0 < LARGEUR_MIN_HZ:
        return None
    if not nom:
        nom, dit = _nommer(f0, f1)
    niveaux = sorted(db for _, db in bins)
    plancher = niveaux[len(niveaux) // 5]
    c = audiographe.courbe(bins, POINTS)
    pic_f, pic_db = max(bins, key=lambda b: b[1])
    return {"nom": nom, "dit": dit, "f0": f0, "f1": f1, "t": t or time.time(),
            "plancher": round(plancher, 1), "pic_mhz": round(pic_f / 1e6, 4),
            "pic": round(pic_db - plancher, 1), "pics": audiographe.pics(c),
            "force": force(c), "courbe": c["db"]}


def noter(bins, t=None, nom=None, dit=None):
    """Une passe rtl_power du scanner entretient la carte. Ne leve jamais."""
    try:
        e = resumer(bins, t, nom, dit)
        if not e:
            return None
        carte = _lire(CARTE) or {}
        avant = carte.get(e["nom"]) or {}
        e["planchers"] = (avant.get("planchers") or [])[-(PLANCHERS_MAX - 1):] + [[round(e["t"]), e["plancher"]]]
        e["forces"] = (avant.get("forces") or [])[-(PLANCHERS_MAX - 1):] + [[round(e["t"]), e["force"]]]
        carte[e["nom"]] = e
        _ecrire(CARTE, carte)
        return e
    except Exception as exc:  # la carte ne doit jamais gener le scanner
        print(f"[carte_antenne] {exc!r}", flush=True)
        return None


def lire():
    """Segments de la carte, du plus bas au plus haut en frequence."""
    carte = _lire(CARTE) or {}
    return sorted((e for e in carte.values() if isinstance(e, dict) and e.get("courbe")), key=lambda e: e["f0"])


def ecart_habituel(e, cle="planchers"):
    """Valeur de la derniere passe moins la mediane des precedentes (dB), ou None."""
    serie = [v for _, v in (e.get(cle) or [])]
    if len(serie) < 4:
        return None
    return round(serie[-1] - statistics.median(serie[:-1]), 1)


def en_courbe(e):
    """Entree de carte -> courbe pour lib/audiographe (f0, f1, db)."""
    return {"f0": e["f0"], "f1": e["f1"], "db": e["courbe"]}


def graduation(e):
    """Pas des tics de l'audiographe (MHz) : une dizaine de tics par segment."""
    largeur = (e["f1"] - e["f0"]) / 1e6
    for pas in (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 25, 50, 100):
        if largeur / pas <= 12:
            return pas
    return 200


# --- direct ------------------------------------------------------------------------

def segments_direct():
    """Ce qu'on peut suivre en direct : les bandes du scanner, plus l'UHF 400-470."""
    vus = {nom for _, _, nom, _ in _connues()}
    segs = [{"nom": nom, "dit": dit, "low_hz": low, "high_hz": high} for low, high, nom, dit in _connues()]
    if UHF["nom"] not in vus:
        segs.append(dict(UHF))
    return sorted(segs, key=lambda s: s["low_hz"])


def demander_direct(nom):
    _ecrire(DIRECT_DEMANDE, {"t": time.time(), "nom": nom})


def direct_demande():
    """Nom du segment a suivre en direct, ou None."""
    d = _lire(DIRECT_DEMANDE)
    if not d or time.time() - d.get("t", 0) > DIRECT_FRAIS_SEC:
        return None
    return d.get("nom")


def arreter_direct():
    for chemin in (DIRECT_DEMANDE, DIRECT_RESULTAT):
        try:
            os.remove(chemin)
        except OSError:
            pass


def publier_direct(res):
    _ecrire(DIRECT_RESULTAT, res)


def lire_direct():
    return _lire(DIRECT_RESULTAT)


def mesure_directe(nom, segs=None):
    """Une passe rapide du segment, notee dans la carte. Rend l'entree, ou
    {"erreur": ...}."""
    import mesure_antenne
    seg = next((s for s in (segs or segments_direct()) if s["nom"] == nom), None)
    if seg is None:
        return {"erreur": "segment", "nom": nom}
    pas = max(1000, int((seg["high_hz"] - seg["low_hz"]) / POINTS_DIRECT))
    _niveaux, bins = mesure_antenne._passe(seg["low_hz"], seg["high_hz"], pas)
    if _niveaux is None:
        return {"erreur": bins, "nom": nom}
    return noter(bins, nom=seg["nom"], dit=seg["dit"]) or {"erreur": "rien", "nom": nom}
