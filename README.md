# Branche `assets` — sons de la gare (nearfield-anchor)

Binaires volumineux versionnés à part du code (branche `assets`, poussée sur
GitHub uniquement — le remote NAS `origin` ne les reçoit pas).

- `train_scaner/` : habillage sonore de la gare utilisé par `watchers/sncf_watch.py`
  et `lib/gare_ambiance.py` — SFX trains (arrivée/départ/passage/klaxon/jingles)
  et `ambi/{normal,pluie,orage,neige,vent}` (ambiances météo/gare).

## Restaurer sur le NAS (Pi)
Déposer le contenu de `train_scaner/` dans `/media/nas/nd1/server/train_scaner/`.
