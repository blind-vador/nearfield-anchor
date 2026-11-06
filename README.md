# nearfield_anchor

Écosystème de veille vocale autonome pour Raspberry Pi (radio, trains,
météo, avions, crues, ACARS) et gestion NAS associée — construit pour
lutter contre l'effet tunnel du syndrome non-24h (perte de la notion du
temps, de la faim, de la fatigue) en gardant un fil sonore et informatif
avec la réalité.

Ce dépôt est la sauvegarde/référentiel "clef en main" du projet
déployé sur le Pi de référence (`ce Pi`) — permet de reconstituer
l'installation complète en cas de panne de carte SD.

## Contenu

| Dossier | Contenu |
|---|---|
| `watchers/` | Les 7 veilles autonomes (voir tableau ci-dessous) |
| `lib/` | Bibliothèques partagées (synthèse vocale, verrou audio, filtres...) |
| `nas/` | Gestion du NAS (montage/remontage automatique des disques) |
| `console/` | Console vocale sur tty1, pilotée au clavier (voir ci-dessous) |
| `radiono/` | Alarme sonore aléatoire anti-effet-tunnel |
| `tools/` | `spectral_diagnostic.py` (diagnostic spectral, voir ci-dessous), `log_ship.py` (copie des logs vers le NAS, voir ci-dessous) |
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
| `imprimante_watch.py` | Annonces de l'imprimante 3D (OctoPrint) : chauffe atteinte, impression lancée / 25-50-75 % / en pause / reprise / terminée / annulée, erreurs, rappel d'une chauffe oubliée sans impression |
| `usb_watch.py` | Entretien des périphériques USB : répare tout seul un port bloqué, la caméra figée, la carte son ou le clavier sans pilote, le pilote TNT sur le dongle, reconnecte l'imprimante rebranchée à OctoPrint ; prévient à la voix quand il faut agir à la main (gestes root via `tools/usb-repare`, commande console « usb ») |

Chaque watcher tourne comme un service systemd indépendant (`Restart=always`),
sauf `airband_bridge.py`/`acars_watch.py` qui sont des modules importés par
`radio_watch.py` (partagent le même dongle RTL-SDR en rotation séquentielle).

### Console vocale (`console/`)

Interface texte sur `tty1` au demarrage (`pi-console.service`), pilotee
par un clavier USB branche directement sur le Pi, sans ecran : chaque
touche tapee est dite a voix haute (espeak), et chaque reponse aussi.

| Fichier | Role |
|---|---|
| `main.py` | Boucle clavier (tty en mode brut, echo vocal, Entree / Retour arriere / Echap / Tab) |
| `commands.py` | Registre des commandes -- en ajouter une = un handler + une entree dans `COMMANDS` |
| `matcher.py` | Mots-cles puis repli flou (seuil 65) ; sinon "commande non reconnue" |
| `simulation.py` | 23 scenarios qui font parler les vraies fonctions d'annonce des watchers |
| `maj.py` | Mise à jour apt en arrière-plan, étapes parlées, détail dans `server/logs/mise_a_jour_*.log` |
| `imprimante_cmd.py` | Pilotage vocal de l'imprimante 3D : dialogue « pilote imprimante » (flèches X/Y, Page préc./suiv. Z, Ctrl+X / Ctrl+Z / Début origine, x y z p position, e/r filament, v ventilateur, m moteurs, n réglage du plateau), commandes chauffe, refroidis, pause / reprends / annule l'impression, imprime, ventilateur, moteurs, vitesse, débit, arrêt d'urgence ; client partagé `lib/octoprint.py` |
| `vision.py` | Photo via mjpg-streamer, refus si trop sombre, description Gemini lue puis oubliée |
| `manuel.py` | Dialogue du mode manuel ; plages publiées par le scanner (`/tmp/radio_watch_plages.json`) |
| `reglages_cmd.py` | Commandes `réglages`, `ville`, `gare`, `aéroport` : choix écrits sur le NAS, veilles concernées relancées |
| `arbre.py` | Arborescence au clavier (flèches, recherche en tapant, étoile = favori) et liste simple |
| `voix.py` | Voix de la console : Microsoft (edge-tts) en cache ou au fil de l'eau, relais espeak automatique ; cache sur la carte SD (`voix_cache`) |
| `priorites.py` | Commande `priorités` : ordre des sources sur les haut-parleurs (Majuscule + flèche pour déplacer) et comportement de chacune ; arbitre dans `lib/audio_lock.py` |
| `conseils.py` | Niveau d'aide (commande `conseils`) : consignes des dialogues à chaque fois ou à la première utilisation ; F1 les redit toujours |
| `voix_cmd.py` | Commande `voix` : choix de la voix (chacune se présente) et du débit, dans `console.json` |
| (commande `observation`) | Dire au Pi le temps qu'il fait (« il pleut fort », « tonnerre au loin », ou dialogue complet) ; effet immédiat sur la vigilance et apprentissage simple, `bilan météo` (`lib/meteo_obs.py`) |
| (commande `trafic`) | Incidents et travaux sur les grandes routes à moins de 40 km, à la demande, flux ouvert Bison Futé (`lib/trafic.py`) |
| (commande `environnement`) | Météo des aéroports traduite (METAR), rivières en temps réel (Hub'Eau), séismes (EMSC), qualité de l'air et pollens (`lib/donnees_ouvertes.py`) |
| (commande `météo`) | Bulletin local (Open-Meteo/AROME, vigilances, stations APRS et capteurs voisins, ballons-sondes SondeHub, signes avant-coureurs) et balayage météo forcé du scanner (`lib/meteo_locale.py`) |
| (commande `voitures`) | Statistiques de la rue par les capteurs de pneus, véhicules habituels appris, « nomme la voiture 3 facteur » (`lib/voitures.py`) |
| (commande `capteurs`) | Derniers relevés des capteurs sans fil (rtl_433) et des stations météo APRS |
| `flash.py` | Commande `flash` (« info locale », « quoi de neuf »...) et `flash détails` : sujets par rubrique ; moteur dans `lib/flash_info.py` |
| `tableaux.py` | Commande `tableau` : départs et arrivées d'une gare (API SNCF) ou d'un aéroport (AirLabs) ; listes avec favoris partagées avec `gare` et `aéroport` |
| `sources.py` | Listes publiques sans clé : communes (geo.api.gouv.fr), gares (open data SNCF), aérodromes (OurAirports) |
| `veilles.py` | Arrêt / reprise / relance des veilles, aiguillé avant la reconnaissance normale |
| `notation.py` | Touche N du journal : grille de 11 critères (SINPO, P.835, NISQA, erreurs de VAD) |
| `bilan.py` | Confronte notes et mesures du système, propose des réglages par bande |
| `archives.py` | Écoute des extraits gardés (storage/radio, un dossier par jour, puis par mois et par année), lecture seule, volume réglable |
| `journal.py` | Dialogue d'écoute et de tri des enregistrements, sortie son unique |
| `keys.py` | Noms parles des touches ; mode reperage (evdev pour Maj/Ctrl/Alt...) |
| `voix.py` | espeak interruptible ; reponses sous le verrou audio partage |

Commandes : `aide`, `santé` ("comment tu vas" : processeur, alimentation,
mémoire, disques, veilles), `rire` ("fais-moi rire" : lance radiono),
`décris` ("qu'est-ce que tu vois", "photo, la pièce est-elle décollée ?" :
photo de la webcam de l'imprimante décrite par Gemini, rien n'est gardé),
`mise à jour` (mise à jour complète + nettoyage, confirmée, suivie à la voix
par étapes ; retaper la commande donne l'avancement), `bilan` (notes radio par bande, propositions de réglages sans rien appliquer),
`archives` (extraits gardés : Tab = jour, haut/bas = écouter, gauche/droite = volume
temporaire, rien n'est supprimé), `journal` (enregistrements radio : Tab/Maj+Tab = jour, haut/bas = écouter,
droite = garder le rendu entendu dans le dossier du jour de storage/radio, gauche = rejeter comme
parasite, N = noter), `veilles` / `arrête` / `reprends` / `redémarre` + nom de veille ("arrête radio",
"reprends toutes les veilles" : arrêt persistant après redémarrage, reprise,
relance ; "veilles" ou "arrête" seul = liste avec l'état), `manuel` (balayage suspendu, radio en direct : flèches haut/bas = plage,
gauche/droite = pas de fréquence, page haut/bas = dix pas, Entrée = relire,
Échap = fermer en restant fixé sur la fréquence), `parasite` ("c'était du bruit" : dernière réception radio effacée du journal et
apprise pour être ignorée), `accroche` / `décroche` ("scan verrouiller",
"reprend" : fixer le scanner sur la fréquence de la dernière réception, puis
reprendre le balayage), `imprimante`, `température`, `heure`, `radio`,
`touches` (reperage des touches, 3 x Echap pour sortir), `répète`,
`redémarrer` / `éteindre` (confirmation "oui" obligatoire), `simulation`
(`simulation train`, `simulation météo`, `simulation train retard`,
`simulation tout`...). Formulation libre : "il est quelle heure", "ça
imprime ?"...

### Gestion NAS (`nas/`)

`nas_watch.py` est le script **actif** : surveille en continu deux disques
NAS montés par UUID (stable, indépendant de l'ordre d'énumération USB),
remonte automatiquement en cas de déconnexion/reconnexion, et annonce
vocalement (espeak) l'état au démarrage et sur tout changement.

**Partages réseau** (`nas/smb.conf`, déployé par `install.sh`) : `server`
(petit disque nd1 entier : système, serveur, dépôt git) et `stockage` (gros
disque nd2 entier), réservés à l'utilisateur `<user>`, avec les réglages de
compatibilité macOS (`vfs_fruit`). Le mot de passe Samba se crée une fois à
la main : `sudo smbpasswd -a <user>`. `nas/smb.conf.current` est l'ancienne
configuration (partages `nd1`/`nd2`), gardée pour référence.

`nas/legacy/` (`nas.py`, `smbv1.conf`, `smbv2.conf`) est **conservé pour
référence historique uniquement** — l'ancien script de montage au
démarrage (`@reboot`), remplacé par `nas_watch.py` qui résout mieux son
problème d'origine (inversion de l'ordre des disques `/dev/sdX`) via le
montage par UUID, et respecte la même contrainte de sécurité (ne jamais
bloquer le démarrage/SSH en cas d'échec de montage — le montage se fait
entièrement après coup, une fois le réseau up, avec un timeout dur sur
chaque vérification).

### Mode diagnostic spectral (`tools/spectral_diagnostic.py`)

Outil générique, **activable à la demande**, pour identifier des
chevauchements de porteuses (deux émetteurs proches en fréquence causant
un battement/sifflement) sur n'importe quelle bande définie dans
`radio_watch.BANDS`. Réutilise le même mécanisme de détection que la
production puis affine immédiatement dessus — élimine le délai qui fait
rater les transmissions courtes/sporadiques avec une approche en deux
scripts séparés.

Deux façons de l'utiliser :

**1. Intégré à `radio-watch` en arrière-plan** (recommandé — pas besoin
d'arrêter le service, profite du trafic naturel accumulé dans le temps
plutôt que d'une rafale ponctuelle) :

```bash
python3 tools/spectral_diagnostic.py arm aviation     # arme sur une bande
python3 tools/spectral_diagnostic.py status            # etat + captures accumulees
python3 tools/spectral_diagnostic.py disarm             # desarme
```

`radio_watch.py` appelle ce hook juste après chaque candidat trouvé par
sa détection normale — coût quasi nul quand rien n'est armé ; quand armé
sur la bande courante, retarde le démarrage de la vraie capture de ~1-2s
(même dongle, un seul balayage à la fois) tant que le mode reste actif.

**2. Rafale autonome** (le dongle doit être libre) :

```bash
sudo systemctl stop radio-watch
python3 tools/spectral_diagnostic.py scan aviation
python3 tools/spectral_diagnostic.py scan vhf_business --attempts 30 --fine-step-hz 250
sudo systemctl start radio-watch
```

Résultats (CSV brut + résumé texte par capture, horodatage lisible NVDA)
accumulés dans `radio_log/diagnostic/` sur le NAS, pour les deux modes —
signale automatiquement les captures où plusieurs porteuses distinctes
sont détectées dans la fenêtre affinée (marge calibrée par bande, pas un
seuil fixe — voir le module pour le détail).

### Copie des logs vers le NAS (`tools/log_ship.py`)

Service permanent (`log-ship.service`) qui suit en continu (`journalctl
-u <unit> -f`) les 7 watchers et copie chaque ligne dans
`logs/<unit>.log` sur le NAS — survit à la rotation/troncature
du journal systemd local (carte SD), et reste accessible même en cas de
panne de carte SD. Extrait en plus les messages décodés (ACARS, Morse/
multimon-ng) vers un fichier dédié `decoded_messages.log`, facile à
relire sans le bruit du reste.

Attend explicitement que le NAS soit réellement monté avant d'écrire
quoi que ce soit (`os.path.ismount`) — `/media/nas/nd1` n'est pas dans
`fstab` (voir `nas/nas_watch.py`), écrire avant le montage réel
risquerait de perdre les lignes une fois le disque monté par-dessus.
Rotation simple à 20 Mo par fichier (1 sauvegarde `.1`).

## Installation

```bash
git clone <ce-depot> nearfield_anchor
cd nearfield_anchor
sudo bash install.sh
```

Voir `APT_PACKAGES.md` pour le détail des dépendances (l'installeur les
gère automatiquement, y compris la compilation depuis les sources de
RTLSDR-Airband, dsd-fme/mbelib, libacars/acarsdec et RNNoise — aucun de ces
projets n'est empaqueté Debian pour ce Pi).

## Points d'attention avant un déploiement sur une autre installation

- **Réglages et clés API** : dossier `nearfield-anchor-settings` à la
  racine du disque `server` du NAS (créé par `install.sh` ou la commande
  `réglages` de la console, voir `lib/reglages.py`). `lieu.json`,
  `trains.json`, `avions.json` se règlent depuis la console (`ville`,
  `gare`, `aéroport`) ; le sous-dossier `cles` contient un fichier texte
  par clé (`sncf.txt`, `opensky_id.txt`, `opensky_secret.txt`,
  `gemini.txt`, `octoprint.txt`, `airlabs.txt`) : coller la clé seule, enregistrer, rien
  à redémarrer. Copie de secours sur la carte SD
  (`/home/<user>/autopilot/reglages_cache`) si le disque décroche.
  L'ancien `credentials.py` (jamais commité) reste lu en dernier recours.
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

## Machine compagne (calculs lourds)

Le dossier `bridge/` est un module a installer sur un Mac ou un PC Linux qui
monte le partage `server` du NAS : `bash bridge/installer.sh`. Sur Mac,
l'application "Nearfield Bridge" (raccourci sur le bureau, accessible avec
VoiceOver) gere les ponts : appairage en meme temps que la commande
`appairage` de la console (echange de cles, meme code dit des deux cotes),
statistiques, suppression, pause. Les images des satellites meteo ne sont
ecoutees que si une machine compagne est appairee. Details : ROADMAP.md,
section du 2026-10-01 sur le pont.
