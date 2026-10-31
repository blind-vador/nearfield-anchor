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
                 Mac : palier 2b ;
  - pics       : survol rapide biaise vers le balayage (Close-Call) --
                 meme vivacite que dechaine mais plancher d'exploration
                 REDUIT de moitie, donc les bandes sont revues deux fois
                 plus souvent pour attraper les emissions breves ; chaque
                 pic detecte est recale sur une porteuse connue (stations,
                 DMR, TETRA) quand il y en a une proche. palier 3.

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
    "lent":       {"bonus": 1.5, "scale": 1.4, "cap": 1.5, "plancher": 1.0, "probe": 1.6},
    "entre_deux": {"bonus": 1.0, "scale": 1.0, "cap": 1.0, "plancher": 1.0, "probe": 1.0},
    "dechaine":   {"bonus": 0.0, "scale": 1.0, "cap": 1.0, "plancher": 2.0, "probe": 0.34},
    "pics":       {"bonus": 0.0, "scale": 1.0, "cap": 1.0, "plancher": 0.5, "probe": 0.34},
}
# Plancher de la sonde numerique par defaut (balayage) : le "ne s'arrete qu'en
# cas de signal" du dechaine descend jusqu'ici, pas plus bas. Litterature :
# multitrame TETRA ~1,02 s, synchro DMR/P25 sous ~1 s -> 2 s laisse une marge
# confortable pour accrocher une vraie porteuse sans attendre 6 s sur du vide.
PROBE_PLANCHER_SEC = 2.0
ORDRE = ["lent", "entre_deux", "dechaine", "pics"]
NOMS = Etiquettes("tempo.nom", {"lent": "lent", "entre_deux": "entre-deux", "dechaine": "déchaîné", "pics": "pics"},
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


def sonde(base_sec, freq_hz=None):
    """Duree de sonde numerique par defaut selon le tempo ET le spot :
    max(plancher_tempo, guet_appris[freq]). plancher_tempo = max(plancher
    litterature, base x facteur) -- entre_deux rend la base inchangee, dechaine
    survole (plancher), lent patiente. Le guet appris (lib/guet.py), borne a la
    sonde pleine, rallonge la sonde sur un spot connu comme lent a confirmer :
    il ne depasse la base qu'en dechaine (ou le plancher est plus bas), donc il
    protege le survol sans allonger entre_deux / lent."""
    plancher = max(PROBE_PLANCHER_SEC, base_sec * params()["probe"])
    g = 0.0
    if freq_hz:
        try:
            import guet
            g = min(guet.confirmation(freq_hz), base_sec)
        except Exception:
            g = 0.0
    return max(plancher, g)


def regler(nom):
    """Enregistre la vitesse ; False si nom inconnu ou NAS inaccessible."""
    if nom not in PARAMS:
        return False
    import reglages
    ok = reglages.save("tempo", {"mode": nom})
    _cache["nom"] = None
    return ok
