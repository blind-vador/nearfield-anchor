# pi-console-watchers

Écosystème de veille vocale autonome pour Raspberry Pi (radio, trains,
météo, avions, crues, ACARS) et gestion NAS associée — construit pour
lutter contre l'effet tunnel du syndrome non-24h (perte de la notion du
temps, de la faim, de la fatigue) en gardant un fil sonore et informatif
avec la réalité.

Ce dépôt est la sauvegarde/référentiel "clef en main" du projet
déployé sur le Pi de référence (`deepspace9`) — permet de reconstituer
l'installation complète en cas de panne de carte SD.

## Contenu

| Dossier | Contenu |
|---|---|
| `watchers/` | Les 7 veilles autonomes (voir tableau ci-dessous) |
| `lib/` | Bibliothèques partagées (synthèse vocale, verrou audio, filtres...) |
| `nas/` | Gestion du NAS (montage/remontage automatique des disques) |
| `radiono/` | Alarme sonore aléatoire anti-effet-tunnel |
| `tools/` | `spectral_diagnostic.py` — mode diagnostic spectral générique, activable à la demande (voir ci-dessous) |
| `config/` | `sound_paths.py` — chemins des banques sonores, un seul endroit à modifier |
| `systemd/` | Unités systemd de tous les services |
| `install.sh` | Installeur complet (apt + compilation + déploiement) |
| `APT_PACKAGES.md` | Détail des dépendances système et des bibliothèques compilées depuis les sources |
| `ROADMAP.md` | Historique complet du développement, en prose, un point par fonctionnalité |

### Les watchers

| Fichier | Rôle |
|---|---|
| `radio_watch.py` | Scanner radio principal — balayage large bande (CB, radioamateur, aviation militaire, VHF/UHF pro...) + décodage numérique (multimon-ng, dsd-fme) |
| `airband_bridge.py` | Squelch réel (RTLSDR-Airband) pour PMR446 et les fréquences aéronautiques locales fixes — utilisé par `radio_watch.py` |
| `acars_watch.py` | Décodage ACARS (messages numériques avion/compagnie) — utilisé par `radio_watch.py` |
| `sncf_watch.py` | Annonces temps réel gare Saint-Étienne Châteaucreux (départs/arrivées/perturbations, style annonce en gare) |
| `meteo_watch.py` | Vigilance météo officielle + alerte hyperlocale (modèle AROME) |
| `crue_watch.py` | Vigilance crues (Vigicrues, Loire forézienne) |
| `adsb_watch.py` | Survols d'avions à proximité (API OpenSky) |
| `stas_watch.py` | Perturbations réseau de bus/tram (STAS Saint-Étienne) |

Chaque watcher tourne comme un service systemd indépendant (`Restart=always`),
sauf `airband_bridge.py`/`acars_watch.py` qui sont des modules importés par
`radio_watch.py` (partagent le même dongle RTL-SDR en rotation séquentielle).

### Gestion NAS (`nas/`)

`nas_watch.py` est le script **actif** : surveille en continu deux disques
NAS montés par UUID (stable, indépendant de l'ordre d'énumération USB),
remonte automatiquement en cas de déconnexion/reconnexion, et annonce
vocalement (espeak) l'état au démarrage et sur tout changement.

`nas/legacy/` (`nas.py`, `smbv1.conf`, `smbv2.conf`) est **conservé pour
référence historique uniquement** — l'ancien script de montage au
démarrage (`@reboot`), remplacé par `nas_watch.py` qui résout mieux son
problème d'origine (inversion de l'ordre des disques `/dev/sdX`) via le
montage par UUID, et respecte la même contrainte de sécurité (ne jamais
bloquer le démarrage/SSH en cas d'échec de montage — le montage se fait
entièrement après coup, une fois le réseau up, avec un timeout dur sur
chaque vérification).

### Mode diagnostic spectral (`tools/spectral_diagnostic.py`)

Outil générique, **activable à la demande** (pas un service systemd),
pour identifier des chevauchements de porteuses (deux émetteurs proches
en fréquence causant un battement/sifflement) sur n'importe quelle bande
définie dans `radio_watch.BANDS`. Réutilise le même mécanisme de
détection que la production puis affine immédiatement dessus, dans le
même processus — élimine le délai qui fait rater les transmissions
courtes/sporadiques avec une approche en deux scripts séparés.

```bash
sudo systemctl stop radio-watch      # libère le dongle
python3 tools/spectral_diagnostic.py aviation
python3 tools/spectral_diagnostic.py vhf_business --attempts 30 --fine-step-hz 250
sudo systemctl start radio-watch     # rend le dongle à la production
```

Résultats (CSV brut + résumé texte par capture, horodatage lisible NVDA)
accumulés dans `radio_log/diagnostic/` sur le NAS — signale
automatiquement les captures où plusieurs porteuses distinctes sont
détectées dans la fenêtre affinée.

## Installation

```bash
git clone <ce-depot> pi-console-watchers
cd pi-console-watchers
sudo bash install.sh
```

Voir `APT_PACKAGES.md` pour le détail des dépendances (l'installeur les
gère automatiquement, y compris la compilation depuis les sources de
RTLSDR-Airband, dsd-fme/mbelib et libacars/acarsdec — aucun de ces
projets n'est empaqueté Debian pour ce Pi).

## Points d'attention avant un déploiement sur une autre installation

- **Identifiants** : isolés dans `config/credentials.py`, **jamais
  commité** (voir `.gitignore`). `config/credentials.example.py` est le
  modèle (committé, valeurs vides) — `install.sh` le copie
  automatiquement en `credentials.py` si absent. Édite ensuite ce
  fichier avec tes propres identifiants (`SNCF_TOKEN`,
  `OPENSKY_CLIENT_ID`/`OPENSKY_CLIENT_SECRET`) puis redémarre
  `sncf-watch`/`adsb-watch`.
- **Chemins des banques sonores** : tout centralisé dans
  `config/sound_paths.py` (`NAS_BASE` + un chemin par catégorie) — adapte
  simplement `NAS_BASE` si ton NAS est organisé différemment. Les
  fichiers audio eux-mêmes (chimes, annonces enregistrées...) ne sont
  **pas** inclus dans ce dépôt (contenu personnel/acheté, voir plus bas).
- **radiono** : projet d'alarme anti-effet-tunnel — joue un son
  aléatoire piochée dans des dossiers de clips (dans le cas de
  l'installation de référence, des capsules audio de François Pérusse,
  **achetées, non incluses dans ce dépôt pour raison de droits d'auteur**)
  pour rappeler l'heure/attirer l'attention. N'importe qui peut y mettre
  son propre contenu — organise juste `RADIONO_DIR` (voir
  `config/sound_paths.py`) en sous-dossiers thématiques, un fichier son
  aléatoire est choisi à chaque exécution.
