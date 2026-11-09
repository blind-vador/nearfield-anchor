# Branche `assets` — sons de notification (nearfield-anchor)

Binaires versionnés à part du code (branche `assets`), poussée sur le dépôt
du NAS (`origin`) et sur GitHub (dépôt privé : une partie des sons vient
d'Epidemic Sound, sous licence, à ne jamais publier).

L'arborescence est celle du dossier `server` du NAS (`config/sound_paths.py`) :

- `train_scaner/` : gare (`watchers/sncf_watch.py`, `lib/gare_ambiance.py`) —
  arrivées, départs, passages, jingles SNCF et `ambi/{normal,pluie,orage,neige,vent}`.
- `radio_scaner/` : scanner radio (`watchers/radio_watch.py`, `airband_bridge.py`).
- `flight_scaner/` : avions et hélicoptères (`watchers/adsb_watch.py`).
- `meteo_scaner/` : alertes météo, crues, bulletin des capteurs.
- `printer/` : imprimante 3D (`lib/sons_imprimante.py`), sons et boucles de mouvement.
- `camera/` : caméra.
- `plantes/plante_alert1.mp3` : alertes des plantes.
- `sonneries/default_ringtone/` : sonneries d origine du scanner (`lib/sonneries.py`, commande
  "sonneries") ; `radio_scaner/` en porte les copies actives. Credits des sons telecharges :
  `sonneries/credits_sons_telecharges.txt`. Le dossier `sonneries/depot` (sons deposes par
  l utilisateur) reste personnel : jamais ici.
- `jingles/` : jingles des bulletins (flash info, meteo, sport, transports).

Jamais ici : caches de voix (`tts_cache`, `voix_cache`), journaux, données.

## Restaurer sur le NAS (Pi)
`install.sh` le fait tout seul (sans écraser l'existant) ; à la main :
`git archive origin/assets | tar -xk -C /media/nas/nd1/server`
