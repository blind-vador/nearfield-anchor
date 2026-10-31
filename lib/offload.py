"""Politique de delegation du decodage vers une machine compagne, graduee par
la temperature du Pi (idee utilisateur 2026-10-31). lib/compagnon.py fournit
deja le pont, l'appairage et le choix de la compagne la plus libre (rank) ;
ici on decide seulement, selon la chaleur du Pi, COMBIEN on delegue.

Mode AGNOSTIQUE : rien a voir avec le tempo du scanner -- des qu'une compagne
est connectee et libre, le Pi lui confie d'autant plus de decodage qu'il
chauffe, dans tous les modes.

Posture selon la temperature CPU du Pi :
  < FROID_C (60 C)   le Pi se debrouille seul : il garde TOUT, meme les gros,
                     tant qu'il est au frais ;
  FROID_C..CHAUD_C   gradient -- il garde les petits decodages et confie les
   (60..70 C)        plus gros ; le seuil de poids delegue descend lineairement
                     (a 60 seuls les plus gros partent ; a 65 la moitie ; vers
                     70 presque tout) ;
  >= CHAUD_C (70 C)  il delegue TOUT aux compagnes disponibles.

Alignement avec le regime thermique de radio_watch : TEMP_RESUME_C=60 (frais),
TEMP_PAUSE_C_BASE=72 (pause dure). Le Pi repose vers ~65 C, donc en pratique il
confie deja des gros morceaux au repos et vide tout vers 70 C -- justement pour
ne PAS atteindre la pause 72 C et continuer a balayer.

Toujours gracieux : pas de compagne connectee EN DIRECT et libre -> on decode
en local (on n'EXIGE jamais la compagne, doctrine). Le "poids" est une
estimation du travail de decodage en SECONDES de calcul attendues, fournie par
l'appelant (p.ex. la duree de sonde tempo.sonde(), ou max_secondes d'un appel
suivi) ; un gros morceau >= POIDS_GROS_SEC.
"""
import subprocess

import compagnon

FROID_C = 60.0          # sous ca : le Pi garde tout (TEMP_RESUME_C de radio_watch)
CHAUD_C = 70.0          # a partir de ca : tout part (2 C sous TEMP_PAUSE_C_BASE 72)
POIDS_GROS_SEC = 20.0   # un decodage d'au moins ca = "gros morceau"
PART_LIBRE_MIN = 0.15   # compagne jugee libre si au moins ~15 % dispo


def temp_pi():
    """Temperature CPU du Pi (vcgencmd, integre a Raspberry Pi OS), ou None.
    Meme source que la securite thermique de radio_watch."""
    try:
        res = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, timeout=5, text=True)
        return float(res.stdout.strip().split("=")[1].rstrip("'C"))
    except Exception:
        return None


def seuil_poids(temp_c):
    """Poids minimal qu'un decodage doit avoir pour etre delegue a cette
    temperature : +inf sous FROID_C (rien ne part), 0 a/au-dela de CHAUD_C
    (tout part), decroissance lineaire de POIDS_GROS_SEC a 0 entre les deux."""
    if temp_c is None or temp_c < FROID_C:
        return float("inf")
    if temp_c >= CHAUD_C:
        return 0.0
    frac = (temp_c - FROID_C) / (CHAUD_C - FROID_C)   # 0 a 60 C, 1 a 70 C
    return POIDS_GROS_SEC * (1.0 - frac)


def compagne_libre(capacite):
    """Compagne de confiance CONNECTEE EN DIRECT, capable de `capacite` et
    libre (part libre >= PART_LIBRE_MIN), celle qui finirait le plus tot
    (compagnon.rank). Un decodage en direct ne peut pas attendre un reveil
    Wake-on-LAN, donc on ne retient que les liaisons directes (jamais une
    simple compagne "vue cette semaine"). None si aucune."""
    for c in compagnon.rank(capacite):
        if not c.get("directe"):
            continue
        part = c.get("part_libre")
        if part is None or part >= PART_LIBRE_MIN:
            return c
    return None


def deleguer(capacite, poids_sec, temp_c=None):
    """Faut-il confier ce decodage a une compagne ? Rend un dict :
      deleguer : bool
      compagne : la fiche compagne retenue, ou None
      temp_c   : temperature lue (ou fournie)
      seuil    : seuil de poids du moment
      raison   : court code ("local_froid", "local_leger", "local_sans_compagne",
                 "delegue")
    Decision deterministe : d'abord la chaleur (seuil de poids), puis la
    disponibilite d'une compagne (gracieux : pas de compagne libre -> local)."""
    if temp_c is None:
        temp_c = temp_pi()
    seuil = seuil_poids(temp_c)
    res = {"deleguer": False, "compagne": None, "temp_c": temp_c, "seuil": seuil, "raison": "local_froid"}
    if seuil == float("inf"):
        return res                      # Pi au frais : il garde tout
    if poids_sec < seuil:
        res["raison"] = "local_leger"   # trop petit pour la chaleur actuelle : le Pi le garde
        return res
    c = compagne_libre(capacite)
    if c is None:
        res["raison"] = "local_sans_compagne"   # degradation gracieuse : personne de libre
        return res
    res.update(deleguer=True, compagne=c, raison="delegue")
    return res
