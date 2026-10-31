"""Tempo du scanner (vitesse de parcours) -- 2026-10-31. Le scanner reste
agnostique : le tempo ne dit pas QUOI viser, seulement COMMENT on parcourt
(lib/attention.plan_cycle le lit). Orthogonal au mode restreint (perimetre).
Regle via le dialogue "mode" (console/mode.py). Stocke dans reglages (NAS),
clef "tempo" -> {"mode": ...}.

  - lent       : on s'attarde, patient (dwell allonge, bonus de piqure majore) ;
  - entre_deux : rythme de reference (DEFAUT, facteur 1.0) ;
  - dechaine   : survol rapide, pas de flanerie sur le vide (dwell = base, sans
                 bonus), plancher d'exploration RELACHE x2 (decision 2026-10-31 :
                 relache, jamais suspendu). Survol profond du balayage + offload
                 Mac : palier 2b. pics (Close-Call) : palier 3.

Parametres lus par plan_cycle :
  bonus    : multiplie le bonus de dwell venu de la piqure (0 = pas de flanerie) ;
  scale    : multiplie le dwell final ;
  cap      : multiplie le plafond de dwell et le budget de surplus du cycle ;
  plancher : multiplie SWEEP_FLOOR_SEC (couverture mini des bandes balayees).
"""
import time

from phrases import Etiquettes

DEFAUT = "entre_deux"
PARAMS = {
    "lent":       {"bonus": 1.5, "scale": 1.4, "cap": 1.5, "plancher": 1.0},
    "entre_deux": {"bonus": 1.0, "scale": 1.0, "cap": 1.0, "plancher": 1.0},
    "dechaine":   {"bonus": 0.0, "scale": 1.0, "cap": 1.0, "plancher": 2.0},
}
ORDRE = ["lent", "entre_deux", "dechaine"]
NOMS = Etiquettes("tempo.nom", {"lent": "lent", "entre_deux": "entre-deux", "dechaine": "déchaîné"},
                  "nom d'une vitesse de parcours du scanner")

CACHE_SEC = 5
_cache = {"t": 0.0, "nom": None}


def courant(fresh=False):
    """Vitesse en cours (DEFAUT si rien d'enregistre), relue au plus toutes les CACHE_SEC."""
    if fresh or _cache["nom"] is None or time.time() - _cache["t"] > CACHE_SEC:
        nom = DEFAUT
        try:
            import reglages
            val = (reglages.get("tempo") or {}).get("mode")
            if val in PARAMS:
                nom = val
        except Exception as exc:
            print(f"[tempo] reglage illisible : {exc!r}")
        _cache.update(t=time.time(), nom=nom)
    return _cache["nom"]


def params(nom=None):
    nom = nom or courant()
    return PARAMS.get(nom, PARAMS[DEFAUT])


def regler(nom):
    """Enregistre la vitesse ; False si nom inconnu ou NAS inaccessible."""
    if nom not in PARAMS:
        return False
    import reglages
    ok = reglages.save("tempo", {"mode": nom})
    _cache["nom"] = None
    return ok
