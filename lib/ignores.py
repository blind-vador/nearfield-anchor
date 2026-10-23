"""Liste des cibles ignorees par le scanner, pour une duree ou pour toujours
(2026-10-22, commande console "ignorer") : une frequence precise, un groupe
DMR ou un appelant DMR. Permet de laisser le scanner balayer une plage en
mettant de cote ce qui encombre, pour se concentrer sur autre chose.

Fichier local (carte SD : relu a chaque candidat, le scanner ne doit pas
dependre du NAS) ; entrees perimees retirees a la lecture. "toujours" pour
une frequence ou un groupe DMR passe aussi par les reglages DMR existants
(lib/dmr_filtres.py, etat "ignore"), visibles dans l'exploration dmr.
"""
import json
import os
import time

CHEMIN = "/home/vador/autopilot/ignores.json"
RELIRE_SEC = 2.0
TOLERANCE_HZ = 12_500        # meme canal (pas de 25 kHz, decalages du recentrage)
DUREES = [(300, "5 minutes"), (900, "15 minutes"), (1800, "30 minutes"), (3600, "une heure"),
          (7200, "2 heures"), (21600, "6 heures"), (86400, "24 heures"), (None, "toujours")]
DUREE_DEFAUT = 3              # "une heure"
TYPES = ("frequence", "groupe", "appelant")
_cache = {"t": 0.0, "mtime": None, "liste": []}


def _lire_fichier():
    try:
        with open(CHEMIN, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _ecrire(liste):
    tmp = CHEMIN + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CHEMIN)
    _cache.update(t=0.0, mtime=None)


def actifs():
    """Entrees en vigueur : [{type, valeur, jusqu_a (None = toujours), libelle, depuis}]."""
    now = time.time()
    if now - _cache["t"] >= RELIRE_SEC:
        _cache["t"] = now
        try:
            mtime = os.path.getmtime(CHEMIN)
        except OSError:
            mtime = None
        if mtime != _cache["mtime"]:
            _cache.update(mtime=mtime, liste=_lire_fichier())
    return [e for e in _cache["liste"] if e.get("jusqu_a") is None or e["jusqu_a"] > now]


def ajouter(type_, valeur, duree_sec, libelle):
    """Ignore cette cible pendant duree_sec (None : toujours). Remplace une
    entree existante sur la meme cible."""
    assert type_ in TYPES
    valeur = int(valeur) if type_ == "frequence" else str(valeur)
    liste = [e for e in _lire_fichier() if (e.get("jusqu_a") is None or e["jusqu_a"] > time.time())
             and not _meme(e, type_, valeur)]
    entree = {"type": type_, "valeur": valeur, "libelle": libelle, "depuis": time.time(),
              "jusqu_a": None if duree_sec is None else time.time() + duree_sec}
    liste.append(entree)
    _ecrire(liste)
    if duree_sec is None:  # toujours : comme la touche "ignorer" de l'exploration dmr
        try:
            import dmr_filtres
            if type_ == "groupe":
                dmr_filtres.set_group(valeur, "ignore")
            elif type_ == "frequence":
                dmr_filtres.set_freq(valeur, "ignore")
        except Exception as exc:
            print(f"[ignores] reglage DMR non mis a jour : {exc!r}")
    return entree


def _meme(e, type_, valeur):
    if e.get("type") != type_:
        return False
    if type_ == "frequence":
        return abs(int(e["valeur"]) - int(valeur)) <= TOLERANCE_HZ
    return str(e["valeur"]) == str(valeur)


def retirer_temporaires():
    """Efface tout ce qui est ignore pour une duree (les "toujours" restent)."""
    garde = [e for e in _lire_fichier() if e.get("jusqu_a") is None]
    n = len(actifs()) - len(garde)
    _ecrire(garde)
    return max(0, n)


def frequence_ignoree(freq_hz):
    return any(_meme(e, "frequence", freq_hz) for e in actifs())


def groupe_ignore(tg):
    return tg is not None and any(_meme(e, "groupe", tg) for e in actifs())


def appelant_ignore(src):
    return src is not None and any(_meme(e, "appelant", src) for e in actifs())
