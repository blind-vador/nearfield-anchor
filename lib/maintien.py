"""Combien de temps tenir une ecoute (2026-10-22, demande utilisateur) : "le
scanner ne doit pas hacher un echange pour aller balayer ; si on capte de
l'interessant, surtout une station a haut niveau d'interet, on laisse
terminer et on attend le delai de fermeture avant de zapper".

  - interet "aucun" ou "faible" (commande "interets") : les plafonds d'avant
    (45 s en analogique et airband, duree max DMR) restent : on peut couper
    pour aller balayer ;
  - interet "normal" et plus : plus de plafond tant qu'il y a de l'echange,
    seulement un garde-fou SECURITE_SEC contre une porteuse coincee ;
  - interet "fort" ou "passion", ou frequence DMR favorite : le delai de
    fermeture (silence avant de clore) est en plus multiplie par
    FACTEUR_FORT, pour laisser a la reponse le temps d'arriver.

Et "zapper" (console) : ZAP_PATH horodate la demande ; toute ecoute
commencee avant s'arrete au morceau suivant.
"""
import json
import os
import time

SECURITE_SEC = 900
FACTEUR_FORT = 2.0
POIDS_NORMAL, POIDS_FORT = 1.0, 1.8
ZAP_PATH = "/tmp/radio_watch_zapper.json"


def interet(target, freq_hz=None):
    try:
        import interets
        poids = interets.weight(target or "")
    except Exception:
        poids = 1.0
    if freq_hz:
        try:
            import dmr_filtres
            if dmr_filtres.freq_state(freq_hz) == "favori":
                poids = max(poids, POIDS_FORT)
        except Exception:
            pass
    return poids


def delai(base_sec, target, freq_hz=None):
    """Silence avant de clore, selon l'interet de la cible."""
    return base_sec * (FACTEUR_FORT if interet(target, freq_hz) >= POIDS_FORT else 1.0)


def plafond(ancien_sec, target, freq_hz=None):
    """Duree maximale d'une ecoute : l'ancien plafond pour ce qui interesse peu,
    sinon seulement le garde-fou."""
    return ancien_sec if interet(target, freq_hz) < POIDS_NORMAL else max(ancien_sec, SECURITE_SEC)


# --- zapper ------------------------------------------------------------------------------

def demander_zap():
    tmp = ZAP_PATH + f".{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"t": time.time()}, f)
    os.replace(tmp, ZAP_PATH)


_zap = {"lu": 0.0, "t": 0.0}


def zap_depuis(debut):
    """Vrai si "zapper" a ete demande apres debut (relu au plus toutes les 0,3 s)."""
    now = time.time()
    if now - _zap["lu"] >= 0.3:
        _zap["lu"] = now
        try:
            with open(ZAP_PATH, encoding="utf-8") as f:
                _zap["t"] = float(json.load(f).get("t", 0))
        except (OSError, ValueError, TypeError):
            _zap["t"] = 0.0
    return _zap["t"] > debut
