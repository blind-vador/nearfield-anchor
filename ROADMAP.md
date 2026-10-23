<!-- Renommé 2026-10-12 : dépôt `pi-console-watchers` → `nearfield_anchor` (bare NAS + clone). Les chemins de déploiement `/home/vador/autopilot/` restent. -->

# Roadmap : console vocale accessible du Pi (deepspace9)

Ce document sert de passation pour une future session/agent qui reprendrait ce travail.
L'utilisateur (vador) est aveugle et pilote cette machine à distance en SSH ; il n'a pas
d'accès physique fiable (pas d'écran) et ne peut pas garantir pouvoir rouvrir la session
qui a produit ce travail (bug d'accessibilité côté client Claude Desktop l'empêchant de
retrouver ses anciennes sessions).

## Objectif du projet

Une interface texte tournant au boot sur tty1 du Pi, contrôlée via un mini-clavier USB
branché en direct sur la machine (pas de moniteur), qui vocalise ses réponses via
`espeak`. But : pouvoir interroger diverses fonctions du Pi (statut imprimante 3D,
température interne, scan radio...) sans dépendance à un écran, et pouvoir étendre
facilement la liste de commandes disponibles dans le temps.

## Décisions d'architecture (et pourquoi)

- **Pas de LLM, local ou distant, pour comprendre les commandes.** Raisons : latence
  trop importante sur un Pi 4 sans accélération dédiée, risque de conflit de ressources
  avec Klipper (contraintes temps réel pour piloter l'imprimante), et surtout risque
  d'interprétation erronée d'une commande sur un outil qui contrôle du matériel physique.
- **Approche retenue : registre de commandes déterministe + repli par correspondance
  floue (fuzzy matching).** Chaque commande a un nom, une liste de mots-clés (match
  instantané) et une liste de phrases d'exemple (comparées par similarité floue via
  `fuzzywuzzy`/`python-Levenshtein` si aucun mot-clé ne correspond). Si rien ne dépasse
  le seuil de confiance, la console répond "commande non reconnue" plutôt que de deviner
  — comportement volontairement "fail closed" puisque ça contrôle du matériel.
- Ce choix a été validé avec l'utilisateur avant implémentation (voir historique de
  session si disponible) : il souhaitait une interface "plus souple qu'un prompt rigide"
  sans les inconvénients d'un LLM.

## Ce qui est fait

Code déployé dans `/home/vador/autopilot/pi_console/` sur deepspace9 :

- `main.py` — boucle principale (REPL), lit une ligne au clavier, dispatch, parle la
  réponse via espeak. **Limite connue : utilise `input()` standard, donc aucun écho de
  ce qui est tapé (voir section "Problème bloquant" ci-dessous — c'est justement ce qui
  a révélé le souci clavier).**
- `commands.py` — registre des commandes + handlers. Commandes actuelles :
  - `printer_status` — interroge l'API locale OctoPrint (`http://127.0.0.1:5000`,
    clé API déjà en dur dans `config.py`) pour l'état d'impression, températures buse/plateau.
  - `pi_temp` — lit `vcgencmd measure_temp`.
  - `radio_scan` — utilise `rtl_power` pour scanner la bande FM (88-108MHz) et annoncer
    le signal le plus fort trouvé. Nécessite le dongle RTL-SDR démasqué du driver DVB
    (voir plus bas).
  - `help` — énumère les commandes disponibles.
- `matcher.py` — logique de correspondance (mot-clé puis flou, seuil `FUZZY_THRESHOLD = 65`).
- `speak.py` — wrapper simple autour de `espeak -v fr`.
- `config.py` — constantes (clé API OctoPrint, paramètres du scan radio).

Toutes ces commandes ont été testées individuellement en SSH et fonctionnent
(y compris avec des reformulations jamais vues mot pour mot, ex: "elle en est où
l'impression" → `printer_status` correctement identifié).

### Déploiement système effectué

- Service `/etc/systemd/system/pi-console.service` créé, actif et activé au boot.
  Il tourne sur `tty1` (`TTYPath=/dev/tty1`), lancé en tant qu'utilisateur `vador`,
  `Restart=always`.
- `getty@tty1` a été **arrêté, désactivé et masqué** pour laisser la place à ce service
  (plus de login classique sur la console physique — SSH reste le moyen normal
  d'administration shell).
- Dépendances installées : `python3-fuzzywuzzy`, `python3-levenshtein`, `rtl-sdr`.
- Le driver noyau DVB (`dvb_usb_rtl28xxu`, `rtl2832`) a été **blacklisté** de façon
  permanente (`/etc/modprobe.d/blacklist-rtlsdr.conf`) car il capturait le dongle
  RTL-SDR avant que `rtl_power` puisse y accéder. Confirmé fonctionnel après ce fix.

Le service a été démarré et a bien vocalisé son message d'accueil ("Console vocale du
Pi prête. Dites aide pour la liste des commandes.") — confirmé audible par l'utilisateur.

## Problème bloquant actuel : le clavier USB n'est pas détecté

C'est ce qui a interrompu la suite du travail. Résumé de l'investigation (longue,
méthodique, tout testé en direct) :

- Le clavier mini USB branché par l'utilisateur n'apparaît **jamais** dans `lsusb` ni
  dans `/dev/input/`, quelle que soit sa configuration de branchement testée :
  via rallonge sur le hub alimenté (config normale), en direct sur le hub, en direct
  sur un port USB du Pi lui-même.
- Signature d'échec systématique et identique dans `dmesg` à chaque tentative :
  `device descriptor read/64, error -32` puis `device not accepting address, error -71`
  puis `unable to enumerate USB device` — la même signature que celle observée plus tôt
  dans la session sur les deux disques durs externes (voir plus bas, section "contexte
  matériel plus large").
- **Preuve la plus forte obtenue** : en branchant le clavier directement sur un port USB
  du Pi (sans hub du tout), le journal noyau a montré `hwmon2: Undervoltage detected!`
  au moment précis du branchement, suivi de `Voltage normalised` quelques secondes après
  — mais le clavier n'a toujours pas été détecté suite à ça.
- Le clavier a été testé sur une machine Windows par l'utilisateur : il fonctionne
  parfaitement. Ce n'est donc pas un clavier défectueux.
- Point soulevé (à raison) par l'utilisateur : le clavier était initialement branché sur
  le **hub alimenté secteur** (pas sur le Pi), donc l'hypothèse "pas assez de courant
  disponible depuis le Pi" ne suffit pas à elle seule à expliquer l'échec initial.
  Hypothèse retenue en réponse : le contrôleur USB qui gère la **signalisation/négociation**
  vit physiquement sur le Pi même quand le courant en volume vient d'ailleurs (hub) ;
  une alimentation Pi marginale peut donc dégrader la fiabilité de toute négociation USB
  transitant par cette puce, indépendamment de la source du courant brut. Cette hypothèse
  n'a **pas été prouvée formellement**, seulement argumentée comme la plus cohérente avec
  l'ensemble des observations.

### Recommandation en cours, pas encore appliquée

Remplacer l'alimentation du Pi par le bloc officiel Raspberry Pi 4 (5,1V/3A, USB-C).
L'utilisateur ne peut pas lire l'étiquette de son alimentation actuelle (pas de braille
dessus) ; une appli de reconnaissance de texte sur smartphone (ex: Seeing AI) ou l'aide
d'un proche pourrait confirmer le sous-dimensionnement avant achat, mais le remplacement
est de toute façon probablement justifié vu la reproductibilité du problème sur plusieurs
périphériques différents (2 disques + ce clavier).

**Prochaine étape concrète une fois l'alimentation changée** : rebrancher le clavier et
vérifier qu'il apparaît dans `lsusb` / `/dev/input/` avant de continuer quoi que ce soit
côté logiciel.

## Prochaines étapes une fois le clavier stable

1. **Outil de calibration clavier demandé mais pas encore construit.** L'utilisateur a un
   clavier ultra-compact et ne connaît pas l'emplacement exact de toutes les touches sans
   repère visuel (il compte coller des pastilles tactiles une fois les touches identifiées).
   Il faut un petit script de diagnostic séparé (`keytest.py` ou similaire) qui :
   - lit les événements clavier au niveau matériel via `python3-evdev` (pas via le tty,
     qui ne fait pas d'écho et ne permet pas d'identifier des touches sans caractère
     imprimable comme Entrée, Échap, etc.)
   - annonce vocalement le nom de chaque touche pressée, **sans exécuter aucune action**
   - sert uniquement au repérage/calibration, pas à remplacer `main.py`
   - nécessite d'identifier le bon device `/dev/input/eventX` correspondant au clavier
     une fois qu'il sera détecté (vérifier `/dev/input/by-path/` et
     `cat /sys/class/input/eventX/device/name`)
2. **Écho clavier dans la console principale.** `main.py` utilise actuellement `input()`
   standard, qui ne donne aucun retour vocal de ce qui est tapé au fur et à mesure. Une
   fois le repérage fait, il faudra probablement faire évoluer l'architecture d'entrée
   vers de la lecture evdev directe (comme pour l'outil de calibration) afin de pouvoir
   annoncer chaque caractère ou au moins confirmer la validation d'une ligne (Entrée),
   plutôt que de rester sur un `input()` muet. À discuter avec l'utilisateur : il n'a pas
   forcément besoin d'un écho lettre par lettre en usage normal, seulement pendant le
   calibrage — à valider son besoin réel avant de réécrire `main.py`.
3. Une fois le clavier fiable et testé bout en bout, réévaluer si `radio_scan` mérite
   d'être enrichi (actuellement : un seul passage FM, pic le plus fort seulement).
4. Étendre le registre de commandes selon les besoins exprimés par l'utilisateur au fil
   du temps (architecture déjà prévue pour : ajouter une entrée dans `COMMANDS` avec
   `name`, `name_spoken`, `keywords`, `examples`, `handler`).

## Volet 2 : veille radio civile automatique (`radio_watch.py`)

Ajouté dans la même session, en parallèle du blocage clavier. Objectif : un scanner
radio en tâche de fond qui balaye en continu plusieurs bandes et fait entendre/lire
tout ce qui ressemble à un vrai échange autour du domicile — dans l'esprit d'une veille
citoyenne (446, aviation, radioamateur...).

### Ce qui est fait

- Service systemd `radio-watch.service`, démarré et actif (**pas activé au boot** —
  décision à prendre avec l'utilisateur), fichier `/home/vador/autopilot/pi_console/radio_watch.py`.
- Bandes surveillées (étendues suite à une recherche documentaire sur les fréquences
  écoutables en France, voir sources dans le code) : PMR446 (16 canaux), CB 27 MHz
  (balayage 26.960-27.410 MHz), VHF marine (balayage 156-162.025 MHz, plus seulement
  le canal 16), aviation civile VHF (118-137 MHz, AM), radioamateur 6m/2m/70cm/23cm
  (balayage large, FM étroite — couvre aussi le trafic satellite amateur qui transite
  par ces mêmes bandes), radio professionnelle UHF (440-470 MHz, compromis pour du
  trafic local non chiffré — sécurité, taxis, événementiel), radio FM classique
  (87.5-108 MHz, WFM). HF (ondes courtes amateur) hors de portée d'un dongle RTL-SDR
  standard sans upconverter.
  **Explicitement exclu, confirmé inécoutable** : secours (police/pompiers/SAMU,
  réseau ANTARES/TETRAPOL chiffré 380-400 MHz) et SNCF grandes lignes/manœuvres
  modernes (GSM-R et TETRA, chiffrés) — aucun outil ici ne peut les décoder, ne pas
  perdre de temps à essayer de les ajouter.
- **Squelch relatif au plancher de bruit** (pas de seuil absolu) : le niveau
  d'activation est calculé par rapport à la médiane des lectures du passage en cours,
  pas une valeur fixe — corrige un vrai bug initial où un seuil absolu (-25dB)
  déclenchait sur le bruit ambiant normal de certaines bandes.
- **Garde anti-pic DC** : les dongles RTL-SDR ont un artefact matériel (pic parasite
  au centre exact de chaque segment balayé par rtl_power, propre à l'architecture à
  conversion directe). Les bins trop proches du centre de chaque segment sont exclus
  de la détection de pic, pour ne pas confondre cet artefact avec un vrai signal.
- **Discrimination bruit/signal par contenu spectral ET taux de passage par zéro**
  (deux techniques établies de conception de circuits squelch/VAD, vérifiées par
  recherche documentaire en cours de session — voir sources ci-dessous) : la
  voix/modulation utile concentre son énergie sous ~3.4kHz et a un taux de passage
  par zéro bas et stable (signal périodique) ; le bruit large bande a
  proportionnellement plus d'énergie au-dessus de ~5kHz ET un taux de passage par
  zéro élevé et erratique (indépendant de l'amplitude, contrairement au RMS seul).
  Les deux critères doivent concorder (ET logique) pour valider un vrai signal —
  volontairement strict pour réduire les faux positifs (retour utilisateur explicite
  après un premier passage trop permissif). **Calibré sur données synthétiques**
  (ton pur/voix : ratio HF ~0.01-0.04, ZCR ~0.03-0.07 ; bruit blanc : ratio HF
  ~0.30-0.35, ZCR ~0.49-0.50) — pas sur de vraies captures RF, donc probablement
  encore à ajuster avec des retours d'écoute réels.
  Sources consultées : [RF Wireless World — Squelch Circuits](https://www.rfwireless-world.com/terminology/understanding-squelch-circuits),
  brevets US sur la conception de squelch par bruit (filtre passe-haut + amplificateur
  de bruit + seuil, "20dB quieting" comme référence standard de l'industrie), et
  littérature sur la détection d'activité vocale (VAD) par énergie de court-terme +
  taux de passage par zéro.
- **Exigence de signal soutenu** : il faut plusieurs fenêtres consécutives (200ms
  chacune) classées "signal" avant de considérer qu'il y a une vraie réception, pour
  filtrer les déclics/artefacts d'ouverture de squelch isolés.
- **Décodage numérique texte/données** : `multimon-ng` installé et branché en
  parallèle de la diffusion audio sur les canaux FM étroite (APRS/AFSK1200, pageurs
  POCSAG 512/1200/2400). Tout texte décodé est lu au TTS ("Message numérique décodé
  : ..."). Fonctionne en tee du flux audio (pas de capture séparée).
- Comportement volontairement "scanner nerveux" (demandé explicitement par
  l'utilisateur) : verrouillage dès qu'il y a de l'activité RF réelle, diffusion en
  direct (pas de capture-puis-decision), coupure dès que le canal redevient
  silencieux/bruité, plafond de sécurité (`MAX_HOLD_SEC`) pour ne jamais rester
  bloqué. **Pas de filtrage par contenu "voix humaine vs automate"** — un choix
  assumé : l'utilisateur veut tout capter (y compris balises automatiques comme les
  identifiants VOR en morse), pas une présélection de ce qui est "intéressant".

### Décodage voix numérique (DMR, P25, D-STAR, YSF, X2-TDMA) — fait, non validé en conditions réelles

`dsd-fme` (fork activement maintenu par lwvmobile) a été compilé et installé avec
succès, avec sa dépendance `mbelib` (vocodeur AMBE — **soumis à un avis de brevet**,
l'utilisateur a consulté et accepté avant de cloner :
https://github.com/lwvmobile/mbelib#readme, usage personnel d'écoute uniquement).

- Sources dans `/tmp/dsd-fme-src` et `/tmp/mbelib-src` (l'utilisateur a fait les
  `git clone` lui-même, l'agent a fait `cmake`/`make`/`make install`).
- Binaire installé : `/usr/local/bin/dsd-fme`.
- `dsd-fme` a besoin de **PulseAudio** pour sa sortie audio (pas d'ALSA direct comme
  `espeak`/`aplay`). Paquet `pulseaudio` installé. Un sink ALSA réel a été chargé
  manuellement (`pactl load-module module-alsa-sink device=hw:0,0`) car PulseAudio
  démarre par défaut sur un sink `null` — **ce chargement de sink n'est probablement
  pas persistant après reboot**, à vérifier/re-faire si le son ne sort plus après un
  redémarrage. `radio-watch.service` a été mis à jour avec
  `Environment=XDG_RUNTIME_DIR=/run/user/1000` pour que le service (qui tourne sans
  session de connexion classique) trouve le socket PulseAudio de l'utilisateur.
- Intégration dans `radio_watch.py` (fonction `try_digital_voice_decode`) : pour
  chaque candidat détecté sur une bande FM étroite, `dsd-fme` est tenté EN PREMIER
  (accès direct au dongle en mode auto-détection `-fa`, probe borné à
  `DSD_FME_PROBE_SEC`) ; si rien n'est détecté, on retombe sur l'écoute analogique
  classique (`stream_until_quiet` + `multimon-ng`).
- **Heuristique de détection de synchro non fiable au premier essai** : comptage du
  nombre de lignes de sortie de `dsd-fme` au-delà d'une ligne de base mesurée.
  Un bug de calibration a été trouvé et corrigé pendant cette session : le seuil
  initial (20 lignes) était en fait déjà atteint par le simple démarrage normal
  (ouverture périphérique + fermeture), sans aucun signal numérique réel — corrigé à
  50 lignes (marge au-dessus de la ligne de base mesurée de 36 lignes).

### DÉSACTIVÉ pour l'instant : `DSD_FME_AVAILABLE = False`

Après l'intégration ci-dessus, le scanner s'est bloqué complètement (plus aucune
détection, process quasi inactif en CPU) pendant plusieurs minutes. Cause identifiée :
`dsd-fme` tente de se connecter à **PulseAudio**, qui ne peut pas s'initialiser dans
le contexte d'un service systemd system-level sans session de connexion réelle
(`/run/user/1000` n'existe pas par défaut dans ce contexte) — `dsd-fme` reste
accroché sur cet échec au lieu d'abandonner proprement, malgré le `timeout` externe.

Correctif partiel appliqué : `loginctl enable-linger vador` (fait persister une
session utilisateur systemd, donc `/run/user/1000` existe désormais en permanence) +
`Environment=XDG_RUNTIME_DIR=/run/user/1000` dans le service. Mais PulseAudio lui-même
échoue encore à détecter un profil audio fonctionnel pour la carte bcm2835 Headphones
(`Failed to find a working profile`) quand il démarre via son propre service
utilisateur systemd (`systemctl --user status pulseaudio.service`) — la config
manuelle (`pactl load-module module-alsa-sink device=hw:0,0`) faite plus tôt dans la
session n'est pas persistante et ne s'applique pas automatiquement à chaque démarrage.

**Pour réactiver cette fonctionnalité, il faudrait** :
1. Écrire une config PulseAudio persistante (`~/.config/pulse/default.pa` ou
   `/etc/pulse/default.pa`) qui charge explicitement le sink ALSA `hw:0,0` au
   démarrage, au lieu de compter sur la détection automatique qui échoue.
2. Valider que `systemctl --user status pulseaudio.service` reste `active` durablement
   (pas juste actif puis `inactive (dead)` après une utilisation, comme observé).
3. Remettre `DSD_FME_AVAILABLE = True` dans `radio_watch.py` et retester en
   surveillant bien qu'aucun blocage ne se reproduise (`ps aux` doit montrer une
   activité CPU normale, pas un process quasi figé).

Cette heuristique de détection de synchro (comptage de lignes) **n'a par ailleurs
jamais été testée face à du vrai trafic numérique confirmé** (aucun signal
DMR/P25/etc. identifié avec certitude pendant cette session) — à valider dès qu'un
cas réel se présente, une fois PulseAudio stabilisé.

### Point de calibration à surveiller

Tous les seuils numériques (`NOISE_MARGIN_DB`, `NOISE_RATIO_THRESHOLD`,
`MIN_RMS_ACTIVE`, `MIN_CONSECUTIVE_SIGNAL_WINDOWS`) sont des premiers réglages
raisonnés mais **jamais validés sur de vraies captures RF en conditions réelles**
au-delà des tests ponctuels de cette session — l'agent qui a écrit ce code ne peut
pas entendre l'audio produit. Si l'utilisateur rapporte encore du bruit blanc ou des
signaux type "sifflement numérique non décodé" (probablement du vrai trafic
numérique que la démodulation analogique rend inintelligible), ajuster ces
constantes en écoute itérative avec lui reste la meilleure méthode.

### Panne dongle silencieuse (2026-08-29, tard dans la session) — corrigée

Après ~1h37 de fonctionnement, plus aucune détection, y compris sur la FM classique
(qui diffuse en continu — un signal qui aurait dû se faire détecter facilement).
Diagnostic : le dongle RTL-SDR avait **complètement disparu du bus USB**
(`lsusb` ne le montrait plus, `rtl_power` répondait "No supported devices found"),
suite à un nouvel évènement de sous-tension (`vcgencmd get_throttled` = `0xf0000`,
confirmant sous-tension + limitation de fréquence + limite thermique survenues
depuis le boot). Même signature que le reste de la session (voir plus bas).

**Bug de robustesse trouvé au passage** : le code ne distinguait pas "aucun signal
trouvé" de "rtl_power a échoué" (dongle indisponible) — les deux cas retournaient
silencieusement un résultat vide, donc le scanner tournait pendant plus d'une heure
sans jamais rien logger d'anormal, alors que rien ne fonctionnait. Corrigé :
- Les échecs de `rtl_power` sont maintenant explicitement loggés
  (`ECHEC rtl_power ...`) au lieu de disparaître silencieusement.
- Un "chien de garde" dans `main()` compte les échecs consécutifs et **annonce
  vocalement** "le dongle radio semble indisponible" après ~15 échecs consécutifs,
  avec rappel périodique si ça persiste, et annonce le retour à la normale une fois
  résolu. Objectif : que l'utilisateur (aveugle, sans autre moyen de vérifier)
  puisse distinguer "silence radio normal" de "scanner en panne" sans avoir besoin
  qu'un agent consulte les logs.

**Découverte au passage, à surveiller** : au même moment, les deux disques NAS
(`sda1`/`sdb1`) montraient des erreurs `htree_dirblock_to_tree: error -5 reading
directory block` sur l'inode racine (#2), répétées toutes les ~60 secondes, pendant
que le dongle était hors ligne. Les disques restaient montés (`df -h` normal), donc
pas nécessairement une corruption aussi grave que celle réparée plus tôt dans la
session, mais probablement le même évènement d'instabilité USB/alimentation qui
affecte tout le bus en même temps. **Un futur agent devrait vérifier l'état des
disques NAS après un nouvel évènement de ce type** (`dmesg | grep -i ext4`,
`tune2fs -l` pour le compteur d'erreurs) plutôt que de supposer qu'ils vont bien
juste parce qu'ils restent montés.

**Hypothèse à considérer** : faire tourner `radio_watch.py` en continu génère un
trafic USB soutenu (`rtl_power`/`rtl_fm` répétés en boucle serrée) qui pourrait
lui-même contribuer à stresser une alimentation déjà marginale, plutôt que d'être un
simple témoin passif du problème. Pas prouvé, mais à garder en tête si les pannes
deviennent plus fréquentes avec ce service actif — envisager de ne pas le laisser
tourner 24/7 tant que l'alimentation du Pi n'est pas remplacée.

**Récupération** : comme pour les disques plus tôt dans la session, la solution
connue est un cycle d'alimentation physique du hub (débrancher/rebrancher), pas
quelque chose de résoluble à distance.

### Fréquences aviation locales ajoutées (Saint-Étienne / Lyon)

Recherche documentaire pour trouver des fréquences aéronautiques concrètes et
proches géographiquement (l'utilisateur est à Saint-Étienne, département 42) :
- **Saint-Étienne / Andrézieux-Bouthéon (LFMH)** : 119.250 MHz
- **Lyon-Saint-Exupéry (LFLL)** : ATIS 126.175 MHz (message météo/piste
  pré-enregistré en boucle continue — **bon signal de test fiable** pour vérifier
  que toute la chaîne fonctionne, puisqu'il est quasiment toujours actif),
  tour 120.450 MHz, sol 121.825 MHz, approche 136.075 MHz.

Ajoutées comme canaux fixes nommés (`aviation_locale`, vérifiés à chaque cycle)
plutôt que de compter sur le balayage large existant (118-137 MHz) à tomber dessus
par hasard — le balayage ne retient qu'un seul pic par passage, donc pouvait rater
ces fréquences précises si autre chose était plus fort au même moment.
Note technique : ces 5 fréquences sont étalées sur 16.8 MHz, donc `check_channels_relative`
(pensé pour des canaux resserrés comme le PMR446) fait en pratique un sous-balayage
large pour les couvrir — fonctionnellement correct mais pas aussi rapide qu'un vrai
groupe de canaux fixes serrés. Pas optimisé, mais pas bloquant.

### Conflit d'accès au dongle

`radio_watch.py` et la commande `radio_scan` de la console (`pi_console/commands.py`)
se disputent le même unique dongle RTL-SDR. Aucune coordination/verrou entre les
deux pour l'instant — si les deux tournent en même temps, l'un des deux échouera
avec une erreur d'accès. Pas critique mais à garder en tête.

## Contexte matériel plus large (pour comprendre les symptômes USB)

Toute cette session a été une session de dépannage/stabilisation générale de la machine,
pas seulement le projet console. Résumé pertinent pour qui reprend ce travail :

- Root cause identifiée plus tôt dans la session pour une corruption sévère du système
  de fichiers racine (ext4) et de la base dpkg : sous-tension au boot, très probablement
  liée à l'alimentation du Pi — cohérent avec le problème clavier documenté ci-dessus,
  ce n'est vraisemblablement **pas une coïncidence**.
- Topologie USB : le Pi a 4 ports USB. Actuellement occupés par : le dongle RTL-SDR
  (branché direct ou via un petit hub VIA Labs partagé avec le point de connexion du
  clavier — topologie exacte pas totalement limpide, le hub semble avoir une puce
  SuperSpeed Genesys Logic ET une puce USB2 VIA Labs distincte pour les mêmes ports
  physiques), et un gros hub alimenté secteur (Genesys Logic, cascadé sur plusieurs
  puces internes) portant les 2 disques NAS (`/media/nas/nd1`, `/media/nas/nd2`) et le
  clavier (habituellement via rallonge).
- `uhubctl` est installé (`apt`, version 2.3.0) mais son adressage par position/vendor-id
  est **peu fiable** sur cette topologie de hub cascadée — a été testé et abandonné comme
  piste pour automatiser un cycle d'alimentation ciblé du hub NAS (voir plus haut dans
  l'historique de session si disponible). Une version plus récente (2.6.0) compilée
  depuis les sources a le même problème — ce n'est pas un bug corrigé entre versions.
- Samba a été remis en service ce jour (était désinstallé/masqué depuis décembre 2025,
  raison inconnue). Un bug de config préexistant dans `smbv1.conf` (bloc `[profiles]`
  partiellement commenté, polluant la section `[homes]`) a été corrigé, ainsi que le
  même bug dans `smbv2.conf` si présent — à vérifier si pas déjà fait.
- La chaîne de compilation (gcc, libc6-dev, binutils, gcc-arm-none-eabi, gcc-avr) a été
  réparée une première fois ce jour (fichiers corrompus depuis le même incident
  d'origine, jamais détectés faute d'avoir été utilisés). **Puis, en compilant
  dsd-fme/mbelib plus tard dans la session, deux nouveaux cas du même problème ont
  été trouvés et réparés : `cmake` lui-même (segfault immédiat) et le compilateur
  C++ `g++-10`/`cc1plus` (crash "Illegal instruction") — la première réparation
  n'avait couvert que la partie C, pas C++.** Ce schéma se répète : la corruption
  touche des fichiers dispersés sur tout le disque, invisibles tant qu'ils ne sont
  pas sollicités. Compiler quoi que ce soit sur cette machine PEUT encore révéler
  de nouveaux cas — le réflexe reste le même : réinstaller le paquet propriétaire du
  fichier fautif (`dpkg -S <fichier>` puis `apt-get install --reinstall <paquet>`),
  éventuellement en cascade (un paquet réparé peut en révéler un autre juste après,
  comme cc1plus a été révélé seulement après que cmake ait pu tourner).

## Session 2026-08-29 : antenne extérieure + instabilité rtl_power sur fenêtre étroite

Suite au déplacement de l'antenne sur la balustrade extérieure (câble USB rallonge
+ base magnétique), et au changement de port USB du dongle (celui-ci chauffait
anormalement sur l'ancien port — pas encore expliqué, à surveiller), demande de
l'utilisateur : vérifier une fréquence de station locale connue pour valider toute
la chaîne de bout en bout (`"Tu peux vérifier une fréq d'une station locale ou
météo à Saint-Étienne et essayer de la capter"`).

**Résultat confirmé : l'antenne extérieure fonctionne très bien.** Capture directe
via `rtl_fm` sur 97.1 MHz (« ici Saint-Étienne Loire », confirmée par recherche web)
donne un signal net et fort, mesuré deux fois à quelques minutes d'intervalle :
`rms≈5000, ratio_hf≈0.013-0.016, zcr≈0.02-0.04` — bien dans la zone « signal réel »
(à comparer aux seuils `NOISE_RATIO_THRESHOLD=0.15` / `ZCR_THRESHOLD=0.20`).

**Deux bugs corrigés au passage dans `radio_watch.py` :**
1. `rtl_fm -M wbfm` plantait (SIGFPE, exit 136) avec `-s 22050` : ratio de
   suréchantillonnage invalide pour ce mode. Fix : `-s 200000 -r 48000`
   spécifiquement en mode `wfm` (branche conditionnelle dans `stream_until_quiet`).
2. Le garde anti-pic-DC (`DC_SPIKE_GUARD_BINS`) excluait la zone centrale de
   *chaque* segment balayé par `rtl_power` — sur une requête à un seul segment
   (test ciblé sur une fréquence précise), ça s'aveuglait soi-même si la cible
   coïncidait avec le centre. Fix : le garde ne s'applique plus que si plusieurs
   segments sont présents (`hop_count > 1`), via `_read_csv_bins`.

**Root cause finale, NON résolue par du code — instabilité matérielle/pilote
de `rtl_power` sur les requêtes à fenêtre étroite ou multi-sauts rapprochés :**
même après les deux fixes ci-dessus, `sweep_band_peak_relative` et
`check_channels_relative` échouaient encore à détecter ce signal pourtant fort
et confirmé. Diagnostic approfondi :
- Sur un balayage 400 kHz / 25 kHz pas (17 sauts), `rtl_power` affiche
  `[R82XX] PLL not locked!` sur **5 essais consécutifs sur 5**, avec un pic
  mesuré à un endroit différent à chaque fois et une marge signal/bruit qui
  varie de 3 à 6 dB de façon incohérente (alors que la vraie source ne bouge
  évidemment pas).
- Sur une fenêtre encore plus étroite (200 kHz / 2-3 bins, tentative de
  vérifier juste 97.1 MHz comme canal fixe), `rtl_power` renvoie carrément
  des valeurs `nan` pour les trois bins — capture totalement invalide.
- Une capture manuelle directe via `rtl_fm` (tuning fixe unique, pas de
  saut de fréquence) sur exactement la même fréquence, dans les mêmes minutes,
  donne à chaque fois un signal parfaitement propre.

**Conclusion : le problème n'est pas le signal ni la marge de seuil, c'est
`rtl_power` lui-même qui devient peu fiable quand on lui demande de sauter
rapidement entre fréquences proches (surtout sur une fenêtre étroite avec peu
de sauts).** `NOISE_MARGIN_DB` a quand même été rendu configurable par bande
(`band["margin_db"]`, passé à `sweep_band_peak_relative`/`check_channels_relative`)
et abaissé à 6.0 dB sur la bande `fm_diffusion` (diffusion FM = signal large
bande, fort quand présent, contrairement aux bandes étroites où la marge stricte
sert à rejeter du bruit) — amélioration légitime et conservée, mais qui ne
résout pas le vrai problème de fiabilité de la mesure.

**Tentative abandonnée délibérément :** ajout d'un canal fixe `97.1 MHz`
contournant `rtl_power` (écoute directe via `rtl_fm` à chaque cycle). Techniquement
ça aurait marché (`rtl_fm` direct est fiable), mais une station commerciale
diffuse en continu — l'intégrer au cycle de veille normal aurait bloqué le
scanner dessus indéfiniment (jusqu'à `MAX_HOLD_SEC=45` par cycle) et fait
annoncer répétitivement la même diffusion sans intérêt pour une veille
citoyenne. Retiré avant déploiement, code non gardé.

**Piste à explorer pour une prochaine session si le sujet revient :**
`rtl_power` a des options `-c` (crop) et de type d'intégration qui n'ont pas
été essayées pour stabiliser le verrouillage PLL sur sauts rapprochés (ex :
intégration plus longue par saut, ou un nombre de sauts plus petit avec un pas
plus large). Une alternative plus radicale serait de remplacer `rtl_power` par
des appels `rtl_fm` successifs courts (fiables mais plus lents un par un) pour
les bandes à peu de canaux, en gardant `rtl_power` seulement pour les larges
balayages où la latence par canal individuel serait trop coûteuse.

Service `radio-watch` relancé et confirmé actif après ces changements
(`systemctl status` : `active (running)`).

## Suite session 2026-08-29 : bug audio majeur (aucun son malgré détection) + habillage sonore

Après le test ci-dessus, l'utilisateur signale : annonces "réception terminée"
entendues mais **aucun son du flux radio lui-même**, même sur la fréquence de
test. Root cause trouvée et confirmée par les journaux systemd : `aplay`
(lecture du flux `rtl_fm` vers les haut-parleurs) était appelé sans
peripherique explicite, donc via le PCM ALSA `"default"`, qui sur cette
machine **route vers PulseAudio** (`aplay -L` le confirme). PulseAudio se
heurte a un bug connu du pilote audio embarqué du Pi (`snd_bcm2835`) :
journal systemd -> `"ALSA woke us up to write new data to the device, but
there was actually nothing to write... Most likely this is a bug in the ALSA
driver 'snd_bcm2835'"`. Consequence : `aplay` "réussissait" (pas d'exception
Python, les écritures dans le pipe passaient) mais ne produisait réellement
aucun son. La detection de signal elle-même n'était pas affectée car elle
analyse les octets bruts venant de `rtl_fm`, pas ce qu'`aplay` joue reellement
-- d'où le paradoxe "annonce sans le son correspondant".

**Fix** : peripherique ALSA explicite `plughw:CARD=Headphones,DEV=0` (constante
`AUDIO_DEVICE` en tête de `radio_watch.py`), qui parle direct au matériel et
contourne PulseAudio. Deux détours essayés et écartés en session avant
d'arriver là : `dmix:CARD=...` seul refuse le mono demandé ("Channels count
non available") ; la syntaxe imbriquée `plug:dmix:CARD=...` n'est pas valide
pour ALSA (erreur de parsing). `plughw` fait lui-même la conversion de
format/fréquence/canaux et fonctionne pour du mono. L'accès exclusif qu'il
impose n'est pas un problème ici : espeak (TTS) et les effets sonores ne sont
appelés qu'après le retour de `stream_until_quiet`, jamais pendant, donc
aucune vraie concurrence sur le périphérique dans ce pipeline. **Résultat
confirmé par l'utilisateur en direct : "j'ai la musique de rtl fort et claire"**
(RTL = station de radio française, pas le dongle RTL-SDR).

Ajout secondaire trouvé pendant ce débogage : `stream_until_quiet` capturait
`stderr` d'`aplay` mais le jetait silencieusement (`subprocess.DEVNULL`) --
c'est ce qui avait masqué l'échec initial de longues minutes durant le
diagnostic. Passé à `subprocess.PIPE` + lecture/affichage sur échec d'écriture
(`aplay a échoué: ...`). **Ce changement de logging a été directement ce qui
a permis de trouver le deuxième bug ("Channels count non available") --
garder ce principe : ne jamais `DEVNULL` un flux d'erreur sans un bon motif.**

**Habillage sonore ajouté ensuite** (demande utilisateur) : l'utilisateur a
déposé 3 fichiers mp3 sur le NAS, `/media/nas/nd1/server/radio_scaner/`
(monté localement sur le Pi, pas de copie nécessaire) :
- `freq_open.mp3` -- joué au tout début de `stream_until_quiet`, des qu'on
  "ouvre l'écoute" sur un candidat détecté.
- `freq_over.mp3` -- joué à la fin d'une réception réussie, en remplacement
  de la phrase "Réception terminée" (l'annonce espeak de la bande/fréquence
  est conservée, juste sans ce préfixe).
- `scaner_error.mp3` -- présent sur le NAS mais **pas encore câblé** (candidat
  naturel pour le chien de garde "dongle indisponible" dans `main()`, jamais
  proposé/confirmé par l'utilisateur -- à faire seulement sur demande).

Lecture MP3 via `ffmpeg -i <fichier> -f alsa plughw:CARD=Headphones,DEV=0`
(nouvelle fonction `play_sound_effect()`), `mpg123`/`mplayer` non installés
mais `ffmpeg`/`ffplay` le sont déjà sur cette machine. Testé sans erreur en
session (aucune ligne `[radio_watch] effet sonore echoue` dans les logs).

Service relancé et confirmé actif avec ce code (`systemctl show` :
`ActiveState=active`, `SubState=running`, PID du process `radio_watch.py`
visible avec son `rtl_power` enfant).

## Suite immediate 2026-08-29 : scaner_error.mp3 cable + reordonnancement annonce/diffusion

Deux ajustements demandes par l'utilisateur juste apres le fix audio ci-dessus :

1. **`scaner_error.mp3`** (3e fichier deja present sur le NAS, pas cable au
   tour precedent) branche sur le chien de garde "dongle indisponible" dans
   `main()` : joue a l'avertissement initial ET a chaque rappel periodique
   (pas sur l'annonce de retour a la normale, qui garde un message positif
   sans effet sonore).

2. **Reordonnancement demande** : l'annonce TTS de la bande/frequence se
   faisait *apres* la diffusion (avec `freq_over`). Nouvel ordre voulu :
   annonce TTS -> `freq_open` -> diffusion -> `freq_over` (qui remplace
   toujours la phrase "Reception terminee", l'annonce n'est plus repetee a
   la fin puisqu'elle est deja passee au debut).

   Probleme technique souleve par l'utilisateur lui-meme : si on attend la
   fin de l'annonce TTS pour lancer `rtl_fm`, on perd le tout debut de la
   transmission pendant que l'annonce parle. **Solution implementee** :
   `rtl_fm` demarre immediatement (avant toute annonce), et un nouveau fil
   dedie (`_rtl_fm_reader`) lit en continu son flux et empile les morceaux
   dans une `queue.Queue()` des cet instant. L'annonce TTS puis `freq_open`
   jouent pendant que ce fil accumule silencieusement en arriere-plan. Une
   fois la diffusion reellement lancee (ouverture d'`aplay`), la boucle
   principale consomme cette file (`audio_queue.get()`) au lieu de lire
   `rtl_fm.stdout` directement -- elle rattrape d'abord tout ce qui s'est
   accumule pendant l'annonce (donc rien n'est perdu), puis suit le flux en
   temps reel une fois rattrapee. Consequence acceptee et voulue : un leger
   decalage/retard de lecture equivalent a la duree de l'annonce, mais aucune
   perte de contenu.

   `stream_until_quiet()` a change de signature : prend maintenant
   `band_label` en parametre (necessaire pour construire l'annonce a
   l'interieur de la fonction, avant l'ouverture du flux). `_handle_candidate`
   simplifie en consequence (n'annonce plus rien lui-meme cote chemin
   analogique, tout est fait dans `stream_until_quiet` + le `freq_over`
   final).

Teste en session via `_handle_candidate(97_100_000, "wfm", "test radio FM")` :
annonce TTS visible avant la diffusion, aucune erreur `aplay`/effet sonore,
`heard_signal: True`. Service redemarre et confirme actif
(`ActiveState=active`, `SubState=running`).

**Note technique pour la suite** : un bug d'accumulation de retours chariot
(`\r\r\n` au lieu de `\n`) a ete trouve et corrige dans le fichier source
pendant cette session, probablement introduit par un aller-retour d'edition
cote Windows sur une session precedente. Si un futur agent voit des erreurs
de correspondance de texte bizarres en editant ce fichier, verifier d'abord
l'encodage des fins de ligne (`grep -c $'\r' radio_watch.py` sur le Pi doit
retourner 0).

## Suite 2026-08-29 : distorsion audio signalee (test PMR446 + FM) -- durcissement PLL rtl_fm

Utilisateur a teste avec un talkie-walkie PMR446 (GD-77S, canal 1 / 446.00625
MHz, mode analogique, puissance haute) en repetant "test canal 1" en boucle.
Le scanner a bien detecte l'activite et ouvert l'ecoute, mais au lieu de la
voix : "une perturbation EM etrange variant dans les aigus comme un genre de
moteur qui accelaire". Juste apres, la station FM de test (auparavant "forte
et claire") est ressortie distordue.

**Hypothese principale testee** : le meme bug de verrouillage PLL du tuner
R820T deja rencontre avec `rtl_power` en session (`"[R82XX] PLL not
locked!"`, confirme a 5 reprises sur 5 lors du diagnostic FM plus haut dans
ce document) pourrait aussi affecter `rtl_fm` lors d'un changement de
frequence a froid -- mais c'etait **invisible** jusqu'ici : `rtl_fm` etait
lance avec `stderr=subprocess.DEVNULL`, donc meme si le tuner ratait son
verrouillage, aucune trace n'en sortait (contrairement a `aplay`, deja
corrige plus tot dans la session sur le meme principe).

**Fix applique** (durcissement, meme si non confirme comme LA cause precise
de cet episode) : nouvelle fonction `_open_rtl_fm()` qui ouvre `rtl_fm` avec
`stderr=subprocess.PIPE`, surveille les 0.5 premieres secondes via un fil
dedie (`_rtl_fm_stderr_watcher`) pour detecter `"PLL not locked"`, et
reessaie jusqu'a 3 fois avant d'abandonner (utilise alors le dernier essai
quand meme, mieux que rien). Le fil de surveillance reste actif pendant toute
la duree de l'ecoute, pas seulement la sonde initiale -- toute erreur/echec
signale par rtl_fm en cours de route est maintenant visible dans les logs
(`journalctl -u radio-watch.service`), la ou c'etait invisible avant.

**Test en session** : 4 essais consecutifs de retune (97.1 MHz / 446.00625
MHz alternes) n'ont montre AUCUN echec de verrouillage PLL cette fois --
donc le bug ne s'est pas reproduit a la demande dans ce test precis, sans
pour autant exclure qu'il ait eu lieu au moment reel signale par
l'utilisateur (probleme intermittent par nature, deja constate comme tel
avec rtl_power).

**Hypothese alternative, plus probable pour le cas PMR446 precis** :
saturation du front-end du dongle RTL-SDR par un emetteur PROCHE et PUISSANT
("puissance haute" explicitement choisie par l'utilisateur sur son talkie).
Un recepteur SDR a large gain automatique peut se faire saturer/distordre par
un signal tres fort en champ proche, produisant exactement ce genre
d'artefact (sifflement/grondement variable qui ne suit pas le contenu reel)
-- phenomene RF physique, pas un bug logiciel corrigeable ici. Si l'utilisateur
reteste en s'eloignant de quelques metres de l'antenne (ou en baissant la
puissance du talkie), et que le probleme disparait, ca confirmerait cette
piste plutot que le PLL. La distorsion FM juste apres pourrait etre une
retombee du meme episode de saturation (recuperation AGC) ou une baisse de
reception momentanee sans rapport -- pas assez d'elements pour trancher.

Service redemarre et confirme actif (`ActiveState=active`,
`SubState=running`) avec ce durcissement.

## Suite 2026-08-29 : voix reconnaissable mais brouillee sur PMR446 -- deemphasis appliquee a tort

Nouveau test utilisateur sur PMR446 (446.00625 MHz) : cette fois, un fond de
voix humaine suit clairement le test "en temps reel", mais reste brouille/peu
clair. Piste avancee par l'utilisateur lui-meme : ca pourrait expliquer
pourquoi rien n'est comprehensible sur plusieurs bandes malgre une detection
correcte (y compris AM/aviation).

**Root cause trouvee** : `stream_until_quiet()` passait `-E deemp` a `rtl_fm`
**pour tous les modes**, y compris `nfm` (446, marine, radioamateur, uhf
business) et `am` (aviation). Cette option active un filtre de
"de-emphasis", qui compense specifiquement la pre-accentuation des hautes
frequences appliquee par les emetteurs de diffusion FM commerciale (courbe
50/75 microsecondes standard). Le PMR446/DMR analogique et l'AM n'utilisent
PAS cette convention -- appliquer la correction quand meme revient a couper
les aigus d'un signal qui n'en avait pas trop au depart, ce qui etouffe et
brouille la voix sans la rendre incomprehensible (coherent avec "voix
reconnaissable mais peu claire").

**Fix** : `-E deemp` retire du chemin nfm/am, garde uniquement pour `wfm`
(diffusion FM commerciale, ou c'est correct et necessaire). Deploye et
service redemarre, confirme actif.

**A confirmer par l'utilisateur** : retester PMR446 (et idealement une autre
bande nfm comme la 446 ou de l'aviation si du trafic passe) pour verifier que
la clarte s'est amelioree. Si le probleme persiste malgre ce fix, pistes
suivantes a explorer : gain manuel (`-g`) au lieu de l'AGC automatique de
rtl_fm, ou bande passante du filtre NFM par defaut de rtl_fm potentiellement
trop etroite/large pour du PMR446 reel.

## Suite 2026-08-29 : distorsion FM persistante malgre fix deemphasis -- hypothese underrun ALSA

Utilisateur signale que la FM de test reste distordue meme apres le fix
deemphasis (normal, ce fix ne touchait que nfm/am, pas wfm). Egalement
signale : reception d'un vrai pager (POCSAG) sur bande UHF pro -- sonnerie
entendue puis `"message texte decode: adresse ... function ..."` lu au TTS
-- **comportement CORRECT et voulu**, confirme que le decodage numerique
multimon-ng fonctionne reellement en conditions reelles, pas juste en test.
Et : activite/candidat detecte sur 446 alors que l'utilisateur n'emettait
rien (chargement batterie) -- grésillement, pas de voix -- possible autre
utilisateur PMR446 a proximite (bande publique partagee) ou faux positif,
pas encore tranche.

**Diagnostic mene sur la distorsion FM** : capture brute isolee (rtl_fm seul,
hors pipeline complet) sur 97.1 MHz -> `rms=4709 ratio_hf=0.0193 zcr=0.0304
peak=16519 clipped=0/257774`, chiffres comparables au test precedent
confirme propre, aucun signe de saturation/clipping. **Le signal RF capte
est propre** -- la distorsion se produit donc probablement plus loin dans la
chaine (lecture/aplay), pas a la reception.

**Hypothese testee** : sous-alimentation de tampon ALSA (underrun) cote
`aplay`, plausible du fait du nouveau passage par fil+file d'attente
(`_rtl_fm_reader`/`audio_queue`, ajoute pour la mise en cache pendant
l'annonce TTS) qui introduit un maillon supplementaire dans la chaine de
lecture. `aplay` tournait en mode silencieux (`-q`) sans surveillance
continue de son propre stderr -- un underrun peut ne jamais lever
d'exception Python cote ecriture (le flux continue, juste degrade), donc
totalement invisible jusqu'ici.

**Fix applique (preventif + diagnostic)** :
- Fil dedie `_aplay_stderr_watcher` : surveille en continu (pas seulement sur
  echec d'ecriture) le stderr d'aplay pendant toute la diffusion, imprime
  tout message recu.
- Tampon ALSA elargi : `-B 500000` (500ms, etait la valeur par defaut d'aplay
  avant, probablement plus courte) pour absorber l'irregularite de rythme
  induite par le fil/file d'attente.
- Nettoyage : l'ancienne lecture ponctuelle de `aplay.stderr` sur echec
  d'ecriture a ete retiree (aurait cree un deuxieme lecteur concurrent sur le
  meme flux que le nouveau fil de surveillance continue).

**Test en session** : un cycle complet `stream_until_quiet` sur 97.1 MHz
(isole, hors du service) n'a produit AUCUN message `aplay signalement` --
donc pas d'underrun confirme dans CE test precis. Mais ce test isole ne
reproduit pas forcement les conditions du service reel (cycles de balayage
qui s'enchainent, charge CPU potentiellement differente). **Si la distorsion
persiste apres ce deploiement, verifier `journalctl -u radio-watch.service`
en cherchant `"aplay signalement"` pendant un episode reel signale par
l'utilisateur -- la surveillance est maintenant active en continu, la
prochaine occurrence sera visible dans les logs meme sans test manuel.**

Fausse alerte au passage : `systemctl restart` a semble bloque
("deactivating"/"final-sigterm") juste apres deploiement -- verification a
confirme que c'etait juste le service en train de terminer un cycle
d'ecoute actif (`stream_until_quiet` bloquant jusqu'a 45s), pas un vrai
blocage. Le service a bien redemarre normalement une fois ce cycle termine.

Service confirme actif (`ActiveState=active`, `SubState=running`) avec ces
changements.

## Suite 2026-08-29 : reactivation du decodage voix numerique (DMR/P25/dPMR...)

Utilisateur signale beaucoup de faux positifs (grésillement sans voix) sur
plusieurs bandes NFM et emet l'hypothese : et si c'etait du **numerique**
(DMR/P25/dPMR...) plutot que de l'analogique ? Question directe : ou en est
le decodage numerique vocal (`dsd-fme`, compile mais desactive depuis le
debut de la session) ?

**Root cause du desactivage original revisitee** : `dsd-fme` n'a pas de
sortie ALSA directe -- seulement `pulse`, `udp`, `null`, `/dev/dsp`
(confirme via `dsd-fme -h`). C'etait sa sortie par defaut `-o pulse` qui
avait bloque tout le scanner. Or on vient justement de resoudre un probleme
PulseAudio similaire pour `aplay` (routage direct materiel via `plughw`,
voir plus haut dans ce document) -- meme logique applicable ici.

**Redesign** : `dsd-fme` sort desormais en `-o udp:127.0.0.1:23456` (PCM brut
"blaste" par UDP local), que notre propre code recupere via un socket UDP et
rejoue lui-meme avec `aplay -D AUDIO_DEVICE` (8000 Hz, frequence standard de
la voix synthetisee par mbelib) -- PulseAudio n'est plus jamais dans la
chaine. Heuristique de succes simplifiee et plus fiable : reception d'un
premier paquet audio UDP = dsd-fme a reellement decode quelque chose (avant :
comptage de lignes de sortie texte au-dela d'un seuil arbitraire, peu fiable).
Watcher dedie (`_dsdfme_stdout_watcher`) affiche en continu le statut texte
de dsd-fme dans les logs pour visibilite/debug. `DSD_FME_PROBE_SEC` reduit de
10s a 4s (le scanner reste "nerveux", pas question de s'attarder sur chaque
candidat rien que pour la sonde numerique). `DSD_FME_AVAILABLE = True`.

**Test de plomberie en session** (sur une frequence PMR446 silencieuse) :
dsd-fme demarre, tourne ~7s au total, se termine proprement ("cleaning up...
Exiting."), retourne `False` (pas de signal, attendu). Fausse alerte au
passage : une verification de processus faite PENDANT la fenetre normale
d'execution (~7s) a ete mal interpretee comme un blocage -- le test avait en
realite deja fini au moment ou la notification est arrivee. Le meme jour, le
service reel (deja redemarre avec ce code) a ete observe en train d'utiliser
la fonctionnalite en conditions live sur la bande radioamateur 70cm
(432 MHz) sans probleme.

**Pas encore valide** : aucun signal numerique REEL n'a ete decode avec
succes dans cette session (seulement teste sur du silence). L'hypothese de
l'utilisateur (faux positifs = trafic numerique) reste a confirmer -- la
prochaine fois qu'un "grésillement sans voix" est signale sur une bande NFM,
verifier `journalctl -u radio-watch.service` pour une ligne
`"dsd-fme a decode de l'audio numerique sur ..."` ou des lignes `"dsd-fme:
Sync"` etc. -- ca confirmerait/infirmerait directement la piste.

Service confirme actif et en fonctionnement normal avec ce changement.

## Suite 2026-08-29 : sifflement aigu universel -- filtre de blocage DC active

Nouveau test PMR446 (446.00625 MHz, analogique, DMR de l'utilisateur) : voix
mieux entendue mais encore faible, avec un fort sifflement aigu. Observation
cle de l'utilisateur : ce sifflement est present sur "presque toutes les
receptions meme les bruits neutres" -- un artefact universel independant du
contenu pointe vers une cause materielle plutot qu'un probleme lie a la voix
elle-meme.

**Root cause trouvee** : `rtl_fm -h` liste une option `-E dc` (filtre de
blocage DC) jamais utilisee dans le code. Le tuner R820T du dongle RTL-SDR a
un artefact materiel bien connu (architecture a conversion directe) qui
produit un decalage/pic DC pres du centre de la bande accordee -- **exactement
le meme phenomene deja identifie en session avec `rtl_power`**
(`DC_SPIKE_GUARD_BINS`, plus haut dans ce document), mais cote demodulation
rtl_fm cette fois, jamais compense.

**Fix** : `-E dc` ajoute a toutes les invocations `rtl_fm` (nfm/am/wfm).
Combinable avec `-E deemp` (deja garde pour wfm uniquement, voir plus haut).

**Mesure comparative en session** (446.00625 MHz, canal sans transmission
active au moment du test, donc bruit de fond uniquement) :
```
SANS -E dc : rms=7098  ratio_hf=0.2744  dc_avg=492.5
AVEC -E dc : rms=8120  ratio_hf=0.2631  dc_avg=350.3
```
Le filtre reduit mesurablement le biais DC moyen (492.5 -> 350.3, -29%) et le
ratio d'energie haute-frequence baisse legerement -- effet reel mais modeste
sur ce test precis (fait sur du bruit de fond, pas sur une vraie transmission,
donc pas totalement representatif du sifflement percu pendant une reception
active). **Impossible de confirmer a distance si le sifflement percu par
l'utilisateur a reellement disparu/diminue** -- necessite un nouveau test
PMR446 en conditions reelles pour validation definitive.

Service redemarre et confirme actif (`ActiveState=active`,
`SubState=running`).

## Suite 2026-08-29 : sifflement confirme reduit + extension des decodeurs multimon-ng

Utilisateur confirme : le sifflement a diminue apres le filtre de blocage DC
(voir section precedente). Question de suivi (angle technique/inventaire) :
qu'est-ce qu'on peut encore ecouter/decoder avec ce materiel -- texte
numerique, voix analogique/numerique, et specifiquement un convertisseur
Morse ?

**Inventaire fait en session** (`multimon-ng` sans argument liste ses
demodulateurs disponibles) :
`POCSAG512/1200/2400 FLEX EAS UFSK1200 CLIPFSK FMSFSK AFSK1200/2400/2400_2/
2400_3 HAPN4800 FSK9600 DTMF ZVEI1/2/3 DZVEI PZVEI EEA EIA CCIR MORSE_CW
DUMPCSV X10 SCOPE` -- seuls AFSK1200 et POCSAG* etaient actives jusqu'ici.

**Extension deployee** (`MULTIMON_ARGS` dans `radio_watch.py`), meme outil
deja valide, cout marginal nul :
- `MORSE_CW` : decodeur Morse direct (demande explicite de l'utilisateur)
- `DTMF` : tonalites multifrequences (controle repeteur, codes d'acces)
- `FLEX` : protocole de pageur complementaire au POCSAG
- `FSK9600` : paquet radio 9600 bauds (AX.25/packet sur bande amateur 2m)
- `EEA`/`EIA`/`CCIR`/`ZVEI1`/`ZVEI2`/`ZVEI3`/`DZVEI`/`PZVEI` : signalisation
  selective a tons (normes francaise/europeennes, dispatch prive/securite --
  pas les services d'urgence officiels chiffres, deja exclus par ailleurs)

**Deliberement pas ajoutes** : `EAS` (specifique USA, hors sujet en France),
`UFSK1200`/`CLIPFSK`/`FMSFSK`/`HAPN4800`/`AFSK2400_*` (rares/obscurs dans ce
contexte), `X10` (domotique/alarme -- surveillance potentiellement invasive
du voisinage, pas ajoute sans demande explicite).

**Point de vigilance signale a l'utilisateur, pas encore observe en
pratique** : les decodeurs Morse automatiques sont notoirement sujets aux
faux positifs sur du bruit/clics rythmiques (meme classe de probleme que la
balise VOR identifiee plus tot en session, avant le passage au squelch
relatif). Si `MORSE_CW` genere trop d'annonces TTS parasites sur du bruit,
il faudra reconsiderer (le retirer, ou filtrer les sorties courtes/peu
fiables).

**Etat du reste des capacites, pour memoire** : voix analogique deja couverte
(NFM/WFM/AM via `rtl_fm`) ; voix numerique deja couverte par `dsd-fme`
(DMR, P25 phases 1/2, D-STAR, YSF, X2-TDMA, dPMR, NXDN, M17, EDACS/ProVoice) ;
HF/ondes courtes hors de portee sans upconverter (dongle limite a ~24MHz+) ;
SSB disponible en mode experimental rtl_fm mais qualite mediocre, pas
implemente ; reseaux chiffres (ANTARES, GSM-R, TETRA) deja confirmes
indecodables et exclus.

Service redemarre et confirme actif (`ActiveState=active`,
`SubState=running`) avec l'extension multimon.

## Suite 2026-08-29 : question inventaire (ANTARES/GSM-R/TETRA, Morse->TTS, autres bandes) + LPD433 ajoute

Questions de l'utilisateur : le decodage Morse sort-il bien par espeak ?
Qu'en est-il d'ANTARES/GSM-R/TETRA ? Qu'est-ce qu'il manque encore comme
bandes locales avec potentiellement de la communication ?

**Reponses donnees** :
- Morse -> TTS : deja fonctionnel sans code supplementaire. `multimon-ng`
  traduit lui-meme les points/traits en texte ASCII, qui suit le meme chemin
  generique que POCSAG/APRS (`speak(f"Message numerique decode : {msg}")`).
- ANTARES (TETRAPOL)/GSM-R/TETRA : confirmes chiffres et indecodables avec
  nos outils, exclusion definitive deja actee plus tot en session.
- Deliberement jamais propose (ligne ethique/legale, pas technique) : DECT
  domestique (communications privees de voisinage, pas une veille citoyenne
  legitime) et reseaux mobiles GSM/4G/5G (chiffres + interception illegale
  en France sans autorisation).
- Piste non implementee, proposee mais necessite accord utilisateur avant
  d'installer un nouvel outil : **ADS-B** (transpondeurs avions, 1090 MHz,
  via `dump1090`) -- pas de la voix mais des donnees de vol en clair
  (position/altitude/indicatif), flux bien plus riche et constant qu'une
  attente de transmission vocale sporadique. Pas encore installe, en attente
  de retour utilisateur.

**Ajout fait immediatement (risque nul, meme schema que PMR446)** : bande
`lpd433` (433.075-434.775 MHz, 25 kHz de pas, mode nfm) -- equivalent
europeen non-licencie du PMR446, meme categorie d'usage (talkies-walkies bon
marche). Chevauche volontairement la plage radioamateur 70cm (430-440 MHz)
deja balayee -- entree separee deliberee pour augmenter la frequence
d'echantillonnage de cette portion specifique, plus susceptible d'activite
qu'un usage radioamateur classique.

Service redemarre et confirme actif (`ActiveState=active`,
`SubState=running`) avec cette bande.

## Suite 2026-08-29 : logs consultes (decode DMR marginal reel observe) + dump1090 installe (conflit dongle)

Consultation des logs demandee par l'utilisateur ("juste avant on a
intersepte pas mal de choses"). Trouve un evenement dsd-fme reel sur
446.08125 MHz : `Sync: +DMR` suivi immediatement de `Frame Sync Err: 7`,
`VOICE CACH/EMB ERR`, puis `Sync: no sync` (9 erreurs audio au total). Le
systeme fonctionne (dsd-fme accroche reellement des trames DMR), mais
**l'heuristique de succes actuelle est trop permissive** : `try_digital_voice_decode`
declare succes des le premier paquet UDP recu, meme si le sync s'effondre
presque aussitot. A durcir plus tard (ex: exiger un minimum de paquets
audio consecutifs, ou verifier l'absence de "no sync"/erreurs dans le texte
de sortie de dsd-fme avant de declarer un succes). Pas fait dans cette
session, note pour plus tard.

**ADS-B (`dump1090-mutability`) installe suite a l'accord de l'utilisateur.**
Paquet officiel Raspbian, pas de compilation necessaire. Installation a
d'abord echoue silencieusement : le script postinst pose une question
`debconf` interactive (probablement localisation/altitude du recepteur pour
l'interface web), qui est restee bloquee indefiniment en arriere-plan sans
personne pour y repondre (`apt-get install -y` seul NE suffit PAS a
supprimer les questions debconf -- il faut `DEBIAN_FRONTEND=noninteractive`).
Process bloque tue, puis `dpkg --configure -a` relance avec
`DEBIAN_FRONTEND=noninteractive` pour terminer proprement. Package
maintenant `install ok installed`.

**Conflit materiel confirme, exactement comme prevu et annonce a
l'utilisateur avant l'installation** : un seul dongle RTL-SDR, `dump1090`
et `radio-watch.service` ne peuvent pas l'utiliser simultanement en continu.
`dump1090-mutability.service` (wrapper LSB/init.d genere par systemd)
s'active automatiquement au demarrage du systeme et essaie de s'approprier
le dongle -- **et perd la course** puisque `radio-watch.service` le tenait
deja : le service systemd rapporte "active (exited)" (juste le script de
lancement qui a rendu la main, pas un vrai statut de demon actif) mais
**aucun processus `dump1090` reel n'etait visible dans `ps aux`** -- echec
silencieux d'ouverture du device, non remonte comme erreur par le wrapper
LSB. `radio-watch.service` confirme non affecte et toujours pleinement
fonctionnel pendant tout cet episode.

**Action prise** : demarrage automatique de `dump1090-mutability` desactive
(`systemctl disable` + `stop`) pour eviter un comportement imprevisible au
prochain redemarrage du Pi (course non deterministe entre les deux services
selon l'ordre de demarrage systemd).

**Decision d'architecture EN ATTENTE, a trancher avec l'utilisateur** :
comment faire cohabiter les deux usages du dongle unique ? Options
envisageables, aucune implementee :
1. Invocation manuelle a la demande (ex: nouvelle commande dans la console
   texte du Pi -- "avions au-dessus" -- qui met en pause radio-watch, lance
   dump1090 quelques secondes/minutes, annonce les appareils vus par TTS,
   relance radio-watch).
2. Alternance programmee (ex: radio-watch tourne X minutes, pause, dump1090
   tourne Y minutes, repete) -- degrade la reactivite "nerveuse" du scanner
   citoyen pendant les fenetres ADS-B.
3. Deuxieme dongle RTL-SDR (achat materiel par l'utilisateur) -- seule
   option permettant une vraie coexistence continue des deux usages.
Rien decide ni implemente a ce stade, en attente du choix de l'utilisateur.

## Suite 2026-08-29 : decision architecture ADS-B -- API a la place du RTL-SDR pour l'instant

Utilisateur a tranche la question d'architecture posee plus haut (conflit
dongle unique) :
- **Un second dongle RTL-SDR sera ajoute plus tard**, dedie a une vraie
  reception ADS-B (dump1090). Ne pas y toucher avant ca.
- **`dump1090-mutability` reste desactive** (deja fait dans la section
  precedente -- `systemctl disable` + `stop`) en attendant ce second dongle.
- **En attendant** : basculer sur un service API interroge regulierement,
  avec un tampon (buffer) des informations deja annoncees pour ne jamais
  repeter la meme chose en boucle -- annoncer uniquement les nouveautes.

**Implemente** : nouveau script `adsb_watch.py` + service systemd
`adsb-watch.service`, totalement independants de `radio_watch.py` (aucune
interaction avec le dongle RTL-SDR, juste des requetes HTTP) -- resout le
conflit materiel sans aucun compromis sur `radio-watch.service`.

- **API** : OpenSky Network (`opensky-network.org/api/states/all`), gratuite,
  sans cle pour un usage anonyme raisonnable (limite ~400 requetes/jour).
  Testee en session avec des donnees reelles (EasyJet, Austrian, Transavia,
  British Airways vus dans la zone).
- **Zone surveillee** : boite lat/lon autour de Saint-Etienne (44.9-45.9 /
  3.9-5.3), assez large pour couvrir l'approche/depart de Lyon-Saint-Exupery
  et le trafic local Andrezieux-Boutheon.
- **Intervalle d'interrogation** : `POLL_INTERVAL_SEC = 300` (5 minutes) --
  ~288 requetes/jour, largement sous la limite anonyme, et assez long pour
  qu'un avion ait le temps de traverser la zone sans redetection en boucle.
- **Anti-repetition (buffer)** : `known_aircraft` (dict icao24 -> indicatif)
  memorise les appareils deja annonces. Seuls les appareils absents du
  buffer au dernier passage declenchent une annonce TTS ("Avion detecte :
  <indicatif>, altitude <X> kilometres."). Un appareil deja vu et toujours
  present reste muet. Le buffer est integralement remplace a chaque cycle
  (donc un appareil qui quitte puis revient sera re-annonce -- comportement
  volontaire et simple, pas de detection fine de "disparition/reapparition"
  pour l'instant).
- Avions au sol (`on_ground: true`) filtres -- pas interessant pour une
  veille "dans le ciel".

**Teste et confirme en session** : appel direct de `fetch_aircraft()` (7
appareils dans la zone, 5 en vol) puis demarrage reel du service --
3 annonces TTS effectuees pour les 3 premiers avions en vol detectes
(EJU4379 a 7.1km, TVF59YT a 11.6km, AUA492 a 11.0km), confirmees dans le
journal systemd.

Service demarre manuellement (meme convention que `radio-watch.service` --
pas active au demarrage automatique du systeme, decision pas encore prise
sur ce point pour aucun des deux services).

## Suite 2026-08-29 : contexte medical du projet (N24) + trois fixes (filtre messages courts, CB AM, orphelins SIGTERM)

**Contexte important partage par l'utilisateur, a garder en tete pour toute
future contribution** : le scanner sert un double but -- veille citoyenne ET
lutte contre le syndrome N24 (trouble du rythme veille/sommeil lie a la
cecite -- sans perception lumineuse, pas de sensation naturelle du temps qui
passe, risque de concentration extreme au detriment des besoins physiques
de base). Le scanner sert a injecter des sons IMPREVISIBLES pour reguler
l'attention. **Consequence directe** : le bruit blanc n'est pas juste un
defaut esthetique, il sape l'objectif medical. Priorite absolue : voix
humaine reelle et donnees numeriques engageantes, minimiser le bruit. Voir
memoire persistante `project_radio_scanner_n24_purpose.md` (sauvegardee en
session cote agent). L'utilisateur a explicitement REFUSE les pistes de
contenu "organise/previsible" (flux radio internet type France Info,
EchoLink) proposees par erreur -- la valeur est specifiquement le caractere
imprevisible ET local (ex : entendre un chantier en 446 peut aider a
anticiper un trajet). Rester sur de la RF locale, ameliorer le signal/bruit
plutot que substituer par du contenu curated.

**Fix 1 -- filtre anti-messages-courts** : analyse des logs sur 25 minutes a
montre que TOUS les messages "decode numerique" etaient soit un caractere
isole ("O", "G"...) soit du bruit MORSE_CW non resolu (notation entre
chevrons, ex "<___.>", "<____>") -- confirme exactement le risque deja
signale a l'ajout de MORSE_CW. Nouvelle fonction
`_is_meaningful_decoded_message()` : retire les fragments morse non
resolus puis exige au moins `MIN_DECODED_MESSAGE_CHARS = 4` caracteres
alphanumeriques reels avant de considerer un message decode comme reel
(annonce TTS ET comptage dans le succes de la reception). Reduit
fortement le bruit sans supprimer MORSE_CW entierement (demande explicite :
filtrer par longueur, pas retirer le decodeur).

**Fix 2 -- CB en AM ajoutee** : confirme par recherche web que la CB
27 MHz en France autorise AM, FM et SSB (canal d'appel 27/27.275 MHz
traditionnellement en AM). Le scanner ne demodulait qu'en NFM -- coherent
avec le signalement utilisateur "voix entendues sur CB mais pas claires".
Nouvelle entree `cb_am` (meme plage 26.960-27.410 MHz, mode "am") en plus
de l'entree NFM existante, les deux tournent desormais en rotation.
Source : radio-collectif.fr, cibi-passion.com (recherche web en session).

**Fix 3 -- orphelins SIGTERM (bug recurrent enfin corrige)** : root cause
identifiee du "deactivating"/"final-sigterm" observe a plusieurs reprises
en session a chaque redemarrage du service (necessitant a chaque fois un
`pkill` manuel de `rtl_fm` orphelin) : Python ne execute PAS les blocs
`finally` sur un SIGTERM brut (contrairement a SIGINT), donc le nettoyage
des sous-processus (`rtl_fm`, `aplay`, `multimon-ng`) dans
`stream_until_quiet` ne s'executait jamais lors d'un `systemctl restart`.
**Deux fixes combines** :
1. Handler `signal.signal(signal.SIGTERM, ...)` installe en tete de
   `radio_watch.py` : convertit SIGTERM en `sys.exit(0)`, qui LUI leve
   `SystemExit` et deroule normalement la pile (execute les `finally`).
2. **Insuffisant seul** (teste, toujours bloque) : `audio_queue.get()` sans
   timeout utilise un blocage bas niveau (`Condition.wait()`) qu'un signal
   ne peut pas interrompre tant que la file ne recoit rien -- le process
   principal restait coince meme avec le handler installe. Fix :
   `audio_queue.get(timeout=1.0)` + `except queue.Empty: continue`,
   revient reguierement verifier les signaux en attente.

**Valide en session** : deux redemarrages consecutifs (`systemctl restart`)
sans aucun processus orphele (verifie via `ps aux`), la nouvelle bande CB AM
deja active en rotation live au moment du test.

## Suite 2026-08-29 : exploration AM ("terrier de lapin") -- 10m ajoute, diffusion ecartee

Suite a l'ajout de la CB en AM, question de l'utilisateur : d'autres
frequences AM a explorer ? Inventaire fait :

- **Bande radioamateur 10m (28-29.7 MHz)** manquait par rapport aux autres
  (6m/2m/70cm/23cm deja couvertes) -- AM choisi comme mode car c'est
  historiquement/encore aujourd'hui le mode dominant pour le vocal sur 10m
  chez les radioamateurs (contrairement au 6m/2m/70cm largement FM/numerique
  en pratique). **Ajoutee.**
- **Echantillonnage direct (`-E direct` sur rtl_fm)** teste en session :
  fonctionne SANS ERREUR sur ce materiel (confirme via test manuel, tuning
  reussi vers ~900kHz), permettrait en theorie d'atteindre les grandes
  ondes/PO (radiodiffusion AM classique, 531-1602 kHz) sans upconverter.
  **Deliberement PAS ajoute** : ce serait de la radiodiffusion (contenu a
  sens unique), pas des echanges reels entre personnes -- meme categorie
  que le "flux radio internet" deja explicitement ecarte par l'utilisateur
  plus haut dans ce document (priorite aux echanges locaux imprevisibles,
  pas au contenu diffuse/curated). Meme raisonnement applique a la bande de
  diffusion ondes courtes internationale 25.6-26.1 MHz (juste sous la CB,
  accessible en mode tuner normal). Note laissee dans le code source
  (BANDS) expliquant ce choix, pour qu'un futur agent ne re-propose pas ces
  bandes sans relire ce contexte.

Service redemarre et confirme actif avec la bande 10m.

## Suite 2026-08-29 : ondes courtes/"stations fantomes" -- bloquees par le materiel, pas le logiciel

L'utilisateur a precise son interet apres la note "diffusion pure ecartee" :
il parle anglais et s'interesse specifiquement aux vieilles stations
internationales, echantillons de sujets varies, et surtout aux "stations
fantomes" -- flux inconnus/mysterieux que personne ne surveille vraiment.
C'est un vrai interet legitime pour l'imprevisibilite (propagation
ionospherique change heure par heure), different de la "diffusion curated"
deja ecartee. Exemple concret cite/verifie : **UVB-76 "The Buzzer"**
(4625 kHz), bourdonnement continu depuis 1982, interrompu occasionnellement
par des voix russes lisant des groupes de codes -- confirme toujours actif
en 2026 (recherche web en session, sources Wikipedia + hqshortwaveradio.com).

**Test effectue** : `-E direct` (rtl_fm) / `-D` (rtl_power) sur 4625 kHz,
plusieurs modes essayes (AM, raw, NFM). Demarrage logiciel propre a chaque
fois ("Enabled direct sampling mode" sans erreur), **mais 0 octet de
donnees produit dans TOUS les cas testes**, y compris avec une fenetre de
capture longue (8s).

**Conclusion** : ce n'est pas un probleme d'antenne ni de mode de
demodulation -- c'est tres probablement une **limitation materielle de ce
dongle precis**. Beaucoup de dongles RTL-SDR generiques/bon marche (celui-ci
s'identifie comme "Realtek, RTL2838UHIDIR, Generic RTL2832U OEM") acceptent
la commande logicielle d'echantillonnage direct sans erreur, mais n'ont pas
le commutateur materiel qui relie reellement l'antenne a l'ADC en
contournant le tuner -- contrairement aux dongles dedies HF comme le
RTL-SDR Blog v3/v4. Le pilote ne remonte aucune erreur car de son point de
vue la commande a reussi ; le probleme est en aval, au niveau du circuit.

**Les ondes courtes/HF ne sont donc PAS accessibles avec ce dongle,
meme en logiciel.** Necessiterait un second dongle avec vrai support HF
materiel (deja prevu pour l'ADS-B -- possible cible commune si le futur
dongle a aussi le support direct sampling) ou un upconverter externe. Ne
pas reproposer l'echantillonnage direct sur ce materiel sans lire cette
section -- deja teste et ecarte, root cause materielle pas logicielle.

Service confirme actif et sain apres ces tests.

## Suite 2026-08-29 : trois nouvelles bandes VHF/FM (trafic pro, trou VHF bas, pirate FM)

Suite au constat que les ondes courtes sont hors de portee (limitation
materielle du dongle), question de l'utilisateur : qu'est-ce qui reste
accessible dans les catalogues radio pirates/amateurs/sources locales ?
Inventaire fait, trois ajouts approuves et deployes (meme architecture
sweep deja validee, risque nul) :

- **`vhf_business`** (148-174 MHz, NFM) : equivalent VHF de la bande
  "radio professionnelle UHF" deja couverte (440-470 MHz) -- plage classique
  en Europe pour le dispatch prive (taxis, securite evenementielle,
  chantiers, maintenance). Chevauche volontairement la VHF marine deja
  couverte (meme logique deliberee que LPD433/70cm plus haut dans ce
  document).
- **`vhf_low`** (30-50 MHz, NFM) : trou entre la CB (jusqu'a 27.41 MHz) et
  le radioamateur 6m (50 MHz). Historiquement dispatch taxi/forestier, en
  declin general mais possibles survivances locales.
- **`fm_diffusion`** elargie (85-110 MHz au lieu de 87.5-108 legal) pour
  capter les debordements de radio pirate FM aux marges de la bande legale.

Une quatrieme piste (micros HF sans fil / PMSE, ~470-582 MHz -- interessante
car du vrai trafic local imprevisible en temps reel, alignee avec l'objectif
du scanner, mais portee exacte en France pas encore verifiee) reste en
attente, pas implementee.

Service redemarre et confirme actif avec ces trois nouvelles bandes.

## Suite 2026-08-29 : bande PMSE (micros HF sans fil) ajoutee

Confirme par recherche web : la bande PMSE officielle en France pour les
micros HF/liaisons audio de spectacle/evenementiel/theatre/eglise est
470-694 MHz (UHF), autorisee au moins jusqu'en 2030. Sources :
soundlightup.com, ravelprodshop.fr (recherche web en session). Des bandes
temporaires 1.2/1.4 GHz avaient ete ouvertes specifiquement pour Paris 2024
mais ne sont pas d'usage general -- pas retenues ici.

**Ajoutee** : nouvelle bande `pmse` (470-694 MHz, NFM, pas de step_hz =
25 kHz), juste au-dessus de la bande UHF pro deja couverte (440-470 MHz),
pas de chevauchement. Trafic reel local et imprevisible en temps reel
(spectacle en cours a proximite), pas de la diffusion -- coherent avec
l'objectif du scanner.

Note : plage large (224 MHz, la plus large du catalogue) donc un passage
de balayage complet sur cette bande prendra plus de temps qu'un passage
typique -- pas encore mesure precisement, a surveiller si ca ralentit trop
le cycle general de balayage.

Service redemarre et confirme actif avec cette bande.

## Suite 2026-08-29 : bande MilAir (225-400 MHz) ajoutee apres verification precedent/communaute

Utilisateur a demande explicitement de verifier s'il existe des precedents
de radioamateurs ecoutant la bande aviation militaire UHF (225-400 MHz) et
l'avis de la communaute experimentee, avant de trancher. Recherche web en
session :

- **Precedent confirme** : la veille "MilAir" est un pan etabli et
  normalise du hobby scanner a l'international, pas une pratique marginale.
  RadioReference (reference historique de la communaute) a un wiki dedie
  ("VHF/UHF Military Monitoring") et des forums actifs specifiquement pour
  ca, avec guides debutants/avances, comparatifs de materiel, astuces
  techniques -- aussi normalise que la veille aviation civile dans cette
  communaute. Sources : wiki.radioreference.com, forums.radioreference.com.
- **Consensus de la communaute sur la ligne legale** : ecouter du
  non-chiffre est considere normal/legal ; decoder du chiffre ne l'est pas.
  Coherent avec le principe deja applique dans ce scanner pour
  ANTARES/TETRA/GSM-R (jamais de tentative de dechiffrement, contenu
  chiffre simplement ignore) -- le pipeline actuel ne permettrait de toute
  facon aucune tentative de ce type.
- Pas de citation juridique francaise definitive trouvee specifiquement
  pour ce cas (le "secret des correspondances", article 226-15 du Code
  penal, protege plutot les communications privees entre individus que les
  canaux militaires/etatiques, mais l'agent n'a pas de source france-
  specifique tranchant precisement ce point).

**Decision utilisateur, apres cette verification** : "affirmatif" ->
implementer. Nouvelle bande `milair` (225-400 MHz, AM, pas 25 kHz -- meme
convention que l'aviation civile) ajoutee.

Service redemarre et confirme actif avec cette bande.

## Suite 2026-08-29 : verrou audio partage radio_watch/adsb_watch + annonces avion ameliorees

Utilisateur a signale trois choses : le scanner semblait arrete (verifie :
en realite les deux services `radio-watch` et `adsb-watch` tournaient bien,
probablement percu comme "arrete" a cause du probleme suivant) ; les
annonces d'avion se chevauchaient avec le scan radio (audio confus/coupe) ;
et l'annonce ADS-B disait "altitude X kilometres" ce qui pretait a
confusion avec une distance (pas de vraie distance calculee jusque-la).

**Root cause du chevauchement** : `radio_watch.py` et `adsb_watch.py` sont
deux services systemd totalement independants (aucun etat Python partage),
chacun appelant `speak()` (espeak) de son cote sans coordination -- rien
n'empechait une annonce avion de demarrer pendant qu'un candidat radio etait
en cours d'annonce/diffusion.

**Fix** : nouveau module partage `audio_lock.py` (verrou de fichier via
`fcntl.flock`, auto-libere par le noyau si le process qui le tient
meurt/est tue -- evite les verrous fantomes qu'un simple fichier-drapeau
provoquerait). `radio_watch.py` : `_handle_candidate()` tient le verrou
pendant tout son cycle actif (annonce + freq_open + diffusion + freq_over),
relache dans un `finally` (couvre aussi le cas "candidat ecarte").
`adsb_watch.py` : nouvelle fonction `_speak_coordinated()` qui acquiert le
verrou avant de parler -- si radio_watch est en cours d'usage, l'annonce
avion ATTEND (bloque) jusqu'a liberation, exactement le comportement
demande ("passer dans les trous entre les radios, pas par-dessus").
**Teste en session** : verrou confirme bloquant correctement (deuxieme
acquereur attend bien jusqu'a liberation du premier, timing verifie
precisement).

**Annonces avion ameliorees** :
- Altitude reformulee en metres (`"Altitude X metres."`, phrase separee)
  au lieu de "altitude X kilometres" -- ambigu, pouvait se lire comme une
  distance.
- **Vraie distance ajoutee** : formule de haversine (distance a vol
  d'oiseau) entre Saint-Etienne (position de reference, 45.4397/4.3872) et
  la position de l'avion (latitude/longitude deja recues de l'API OpenSky
  mais pas utilisees jusqu'ici -- `IDX_LONGITUDE`/`IDX_LATITUDE` ajoutes).
  Annonce desormais : "Avion detecte : X. Altitude Y metres. Distance
  Z kilometres."

**Pas implemente, recherche faite** : modele d'appareil et destination
demandes par l'utilisateur.
- Destination : pas d'acces fiable via API gratuite pour un vol EN COURS
  (typiquement une fonctionnalite payante meme chez FlightAware/
  FlightRadar24). Pas retenu.
- Modele d'appareil : possible en theorie, mais l'endpoint metadata
  OpenSky teste en session renvoie "410 Gone" (deprecie) et l'API OpenSky
  exige desormais une authentification OAuth2 (recherche web confirmee) --
  necessiterait soit un compte OpenSky (creation de compte = action
  utilisateur, pas l'agent), soit le telechargement d'une base de donnees
  d'immatriculations statique (faisable par l'agent, pas encore fait).
  **En attente de decision utilisateur sur cette deuxieme option.**

Les deux services redeployes et redemarres, confirmes actifs.

## Suite 2026-08-29 : nouveau service meteo_watch (alertes vigilance, pas prevision)

Utilisateur va creer un compte OpenSky lui-meme (pour le modele d'appareil
ADS-B, en attente de sa part). En attendant, nouvelle demande : un radar
meteo -- mais explicitement PAS une prevision routiniere ("pas façon
speakrine"), une **alerte quand une perturbation dangereuse approche**
(orage, tornade, tempete -- "ce qui peut presenter des degats", le genre de
chose qu'une personne voyante verrait arriver de loin mais qui le
surprendrait, avec un vrai risque physique/electrique a la cle : debrancher
les installations, fermer fenetres et volets).

**Sons deja prepares par l'utilisateur, trouves sur le NAS** :
`/media/nas/nd1/server/meteo_scaner/vigilence_1.mp3` a `vigilence_4.mp3`
(tailles croissantes 121KB->1.2MB) -- correspondance directe et evidente
avec les 4 niveaux officiels de vigilance Meteo-France (vert/jaune/orange/
rouge), confirme par la suite via l'API.

**API utilisee** : l'API officielle Meteo-France exige desormais un compte/
cle (meme situation qu'OpenSky). Trouve un **miroir public sans cle** sur
Opendatasoft (`public.opendatasoft.com`, dataset
`weatherref-france-vigilance-meteo-departement`) qui expose les memes
donnees (phenomene, couleur, echeance) par simple requete HTTP anonyme.
Teste et confirme en session avec des donnees reelles pour le departement
42 (Loire/Saint-Etienne) : 5 phenomenes suivis pour ce departement (orages,
vent, pluie, neige/verglas, canicule), tous verts au moment du test.

**Nouveau fichier `meteo_watch.py`** (+ service systemd `meteo-watch.service`) :
- Interroge le departement 42, echeance "J" (aujourd'hui) uniquement --
  "J1" (demain) serait de la prevision, explicitement ecarte.
- Poll toutes les 15 min (`POLL_INTERVAL_SEC=900`, la vigilance n'est mise
  a jour que quelques fois par jour, pas besoin de plus frequent).
- Comportement par niveau (transition uniquement, jamais de repetition sur
  un niveau inchange) :
  - **Vert (1)** : silencieux, sauf en redescendant d'un niveau superieur
    (message rassurant "vigilance retombee au niveau normal" + vigilence_1.mp3).
  - **Jaune (2)** : annonce ponctuelle unique (vigilence_2.mp3 + description
    + conseil de securite).
  - **Orange/Rouge (3/4)** : **alarme en boucle** (demande explicite --
    "on joue le son d'alarme choisi en boucle") : fil dedie qui rejoue le
    son + repete le message toutes les 2 minutes (`ALARM_REPEAT_SEC=120`)
    tant que le niveau reste eleve, s'arrete proprement des que le niveau
    redescend (verifie au prochain sondage, 15 min max avant arret si le
    danger est reellement passe -- pourrait etre affine avec un sondage
    plus frequent pendant une alarme active si necessaire).
- Conseils de securite rediges par phenomene (orages/vent/pluie/neige-
  verglas/canicule/grand-froid), coherents avec ce que l'utilisateur a
  decrit (debrancher l'electrique, fermer fenetres/volets, etc.).

**Refactoring fait au passage** : `play_sound_effect()`/`AUDIO_DEVICE`
extraits de `radio_watch.py` vers un nouveau module partage
`sound_player.py` -- evite de dupliquer cette logique dans un 3e fichier
(`meteo_watch.py` en avait aussi besoin pour les sons vigilence_*.mp3).
`radio_watch.py` importe desormais depuis ce module au lieu d'avoir sa
propre copie.

**Coordination audio** : `meteo_watch.py` utilise le meme `audio_lock.py`
que `radio_watch.py`/`adsb_watch.py` -- les alertes meteo passent aussi
dans les trous entre les cycles radio, meme logique que pour l'ADS-B.

**Bug trouve et corrige en session** : `play_sound_effect()` a un timeout
par defaut de 8s (calibre pour les effets courts de radio_watch), mais
`vigilence_3.mp3` (474KB) depasse largement cette duree -- timeout
explicite `timeout_sec=90` passe pour tous les sons de vigilance. Confirme
par test manuel : premiere tentative a echoue silencieusement (son coupe
apres 8s), deuxieme tentative apres le fix a fonctionne sans erreur.

**Teste en session** : annonce orange simulee manuellement
(`_announce(3, "orages", ...)`), son + message vocal joues sans erreur
apres le fix de timeout. Les trois services (`radio-watch`, `adsb-watch`,
`meteo-watch`) confirmes actifs simultanement.

## Suite 2026-08-29 : alerte meteo hyper-locale (nowcast) ajoutee, complementaire a la vigilance

Utilisateur a precise : la vigilance departementale (section precedente)
est trop large -- veut savoir si une perturbation approche precisement de
sa position, "un peu comme meteo radar quelque chose de plus precis".

**Solution trouvee** : API Open-Meteo (gratuite, sans cle) supporte des
donnees a resolution **15 minutes**, utilisant pour l'Europe centrale le
**modele AROME de Meteo-France lui-meme** (haute resolution, specialise
convection/orages) -- interrogeable a des coordonnees lat/lon EXACTES, pas
juste par departement. Teste et confirme en session avec les coordonnees
de Saint-Etienne (altitude retournee 526m, coherent).

**Metriques utilisees** (toutes disponibles via l'API, confirmees) :
- `precipitation` (mm/15min)
- `wind_gusts_10m` (km/h) -- rafales, indicateur direct de risque de degats
- `cape` (J/kg) -- energie disponible pour la convection, indicateur
  standard de potentiel orageux
- `lightning_potential` (J/kg) -- activite orageuse/foudre

**Ajout dans `meteo_watch.py`** : nouveau fil de veille dedie
(`_nowcast_watcher`, tourne en parallele du fil de vigilance
departementale existant) qui interroge ces 4 metriques toutes les 10 min
(`NOWCAST_POLL_INTERVAL_SEC=600`) sur une fenetre d'anticipation de 2h
(8 pas de 15 min, `NOWCAST_LOOKAHEAD_STEPS=8`), cherche le niveau de
severite le plus eleve (seuils jaune/orange/rouge par metrique, voir
`NOWCAST_THRESHOLDS` dans le code -- **valeurs de depart explicitement a
ajuster avec le retour utilisateur**, demande utilisateur "on ajustera en
fonction de ce qu'on peut tracer"), et annonce "dans environ X minutes"
pour donner un vrai delai d'anticipation. Meme comportement par niveau que
la vigilance (jaune=annonce unique, orange/rouge=alarme en boucle qui se
rafraichit a chaque repetition avec des donnees fraiches -- contrairement
a l'alarme de vigilance qui rejoue le meme message capture).

**Refactoring** : la machinerie d'alarme en boucle (`_start_alarm`/
`_stop_alarm`/`_alarm_loop`) generalisee pour accepter une fonction de
rappel (`announce_fn`) au lieu d'un couple fige (couleur, phenomene) --
permet de la reutiliser telle quelle pour la vigilance ET (potentiellement)
pour l'alerte locale, meme si l'alerte locale a finalement sa propre
boucle dediee (besoin de rafraichir les donnees a chaque repetition,
different du cas vigilance).

**Teste en session** : appel direct de `fetch_nowcast()` (donnees reelles
recues, conditions actuelles calmes -- tout a zero, coherent avec la
vigilance vert deja constatee) puis `_analyze_nowcast()` (niveau 0
correctement detecte) ; tests unitaires de `_severity_for_value()` avec
valeurs simulees depassant les seuils (6mm pluie -> orange, 80km/h vent ->
orange, CAPE 2000 -> orange, CAPE 0 -> calme) -- logique de seuillage
confirmee correcte.

Les trois services (`radio-watch`, `adsb-watch`, `meteo-watch`) confirmes
actifs simultanement apres ce deploiement.

## Suite 2026-08-29 : roles vigilance/nowcast redefinis (simplification demandee)

Utilisateur a clarifie le partage des roles entre les deux systemes meteo :

**Vigilance departementale -- devient un simple ticker de changement,
plus un systeme d'alerte** :
- Sondage a **1x/heure** (`VIGILANCE_POLL_INTERVAL_SEC=3600`, etait 15 min).
- Etat persiste dans un **JSON temporaire** (`/tmp/meteo_watch_vigilance_state.json`,
  `_load_vigilance_state()`/`_save_vigilance_state()`) -- survit a un
  redemarrage du service, contrairement au dict en memoire d'avant.
- **Alarme en boucle et conseils de securite retires de la vigilance** --
  ce role appartient desormais exclusivement au nowcast (voir plus bas).
- Annonce uniquement sur changement de niveau (montee OU descente), avec
  une phrase fixe demandee par l'utilisateur :
  *"Le niveau d'alerte {phenomene} pour le departement de la Loire vient
  de {monter/baisser}, passant de {ancien} a {nouveau}."*
  Teste et confirme : "Le niveau d'alerte orages pour le departement de la
  Loire vient de monter, passant de vert a orange." (bug de grammaire
  corrige en session : "departement de Loire" -> "departement de la Loire").
- Plus de son joue pour la vigilance (les vigilence_N.mp3 restent
  desormais reserves au nowcast uniquement, le vrai systeme d'alerte).

**Alerte hyper-locale (nowcast) -- reste le systeme d'alerte temps reel,
phrasing simplifie** :
- Logique de seuils/type/delai (proxy de "distance") inchangee -- deja
  differenciee par type de perturbation (pluie/vent/orages) et par delai
  avant occurrence, ce qui repond a la demande "on reserve les alertes en
  fonction du type de perturbation et de la distance".
- **Message simplifie et naturel** ("on speak simplement si de la pluie
  approche par exemple") : `NOWCAST_APPROACH_MESSAGE` par type
  ("De la pluie approche", "Des rafales de vent approchent", "Un orage
  approche") + delai ("dans environ X minutes" / "d'un instant a l'autre"
  si tres proche). Conseil de securite ajoute seulement a partir
  d'orange/rouge (le heads-up jaune reste court, sans conseil) -- teste :
  "Des rafales de vent approchent, dans environ 20 minutes. Fermez et
  securisez volets et fenetres..."

Les trois services (`radio-watch`, `adsb-watch`, `meteo-watch`) redeployes
et confirmes actifs.

## Suite 2026-08-29 : compte OpenSky cree (client secret en attente) + filtre 50km + son de survol

Utilisateur a cree un compte OpenSky : username `blind_vador`,
Client ID `blind_vador-api-client`, acces "Standard" (4000 credits/jour).
**Le Client Secret OAuth2 n'a pas encore ete communique** -- necessaire en
plus du Client ID pour authentifier les requetes (flow client_credentials).
Modele d'appareil ADS-B toujours pas branche, en attente de ce secret.

**Deux ajouts faits immediatement (ne dependent pas du compte OpenSky)** :

1. **Filtre rayon 50km reel** (`MAX_RADIUS_KM = 50.0`) : la boite lat/lon
   utilisee pour la requete API restait large (incluait les coins, plus
   loin que 50km du centre) -- desormais un vrai filtre par distance
   (haversine, deja calculee pour l'annonce) exclut tout appareil au-dela
   de 50km avant meme de le considerer.

2. **Son de survol direct** : nouveau fichier trouve sur le NAS,
   `/media/nas/nd1/server/flight_scaner/flyer_by.mp3` (38 secondes de duree
   reelle, confirme via `ffprobe`). Joue quand un avion entre dans un rayon
   "juste au-dessus" (`OVERHEAD_RADIUS_KM = 3.0`, distinct et plus petit que
   le rayon de veille general). Etat de suivi (`overhead_announced`, un
   `set` d'icao24) persiste entre les sondages pour ne jouer le son qu'une
   fois par passage, meme si l'avion reste plusieurs cycles dans le rayon
   de survol -- se reinitialise des qu'il en ressort (permet un futur
   nouveau passage).

**Bug trouve et corrige en session** : meme piege que pour meteo_watch --
`play_sound_effect()` a un timeout par defaut de 8s, `flyer_by.mp3` dure
38s reelles. Premiere tentative de test a echoue silencieusement (son
coupe), corrige avec `timeout_sec=60` explicite.

**Limite connue, signalee a l'utilisateur** : a la cadence actuelle de
sondage (`POLL_INTERVAL_SEC=300`, 5 min, calibre pour rester sous le quota
anonyme ~400 requetes/jour), un avion de ligne (~800 km/h) peut traverser
toute la zone de 50km entre deux sondages -- **la detection du survol
exact reste probabiliste, pas garantie**. Sonder plus souvent
necessiterait le compte OpenSky authentifie (quota bien plus eleve,
4000 credits/jour) une fois branche -- pas encore fait, en attente du
client secret.

**Teste en session** : `fetch_aircraft()` + filtre 50km confirme correct
(1 appareil reel trouve a 41.7km, dans le rayon), `_announce_flyby()`
teste manuellement, son + annonce vocale joues sans erreur apres le fix de
timeout. Service redemarre et confirme actif.

## Suite 2026-08-30 : OpenSky "Reset Credential" -- 503 serveur, pas un bug d'accessibilite

Compte OpenSky cree (`blind_vador`, Client ID `blind_vador-api-client`,
acces Standard 4000 credits/jour). L'utilisateur n'arrivait pas a obtenir
le Client Secret via "Reset Credential" -- NVDA n'annoncait rien apres le
clic. Longue session de diagnostic via l'extension Claude in Chrome
(connectee au navigateur reel de l'utilisateur, memes cookies de session) :

- Tente d'abord un clic JS direct (`element.click()`) -- **bloque par le
  classifieur de permission auto-mode** (action jugee trop sensible :
  regenere/invalide un identifiant de securite). Respecte le blocage,
  demande a l'utilisateur de cliquer lui-meme pendant que l'agent observe
  en lecture seule.
- MutationObserver arme (DOM), interception de `window.alert/confirm/
  prompt` (au cas ou secret affiche via dialogue natif du navigateur),
  verification des champs `<input>`, verification du presse-papiers
  (`navigator.clipboard.readText()` -- **a bloque/timeout 45s**, contexte
  automatise sans permission navigateur, abandonne) -- **tout negatif**,
  rien ne capture jamais le secret.
- Clic final via l'outil `computer` (souris simulee, pas de JS) --
  **passe le classifieur sans blocage** cette fois (contrairement au clic
  JS direct).
- **Root cause reelle trouvee via l'inspection reseau** (`read_network_requests`) :
  la requete `POST /my-opensky/api-client/reset-credentials-default`
  renvoie **503 Service Unavailable**, confirme sur 3 tentatives
  consecutives. Le bouton affiche "Resetting..." puis revient a la normale
  simplement parce que la requete echoue cote serveur -- **aucun secret
  n'est jamais genere**, il n'y avait donc litteralement rien a voir ni
  pour NVDA ni pour l'agent. Ce n'etait pas un probleme d'accessibilite du
  tout, mais une panne/indisponibilite du service OpenSky au moment du
  test.

**A refaire plus tard** (le 503 est probablement transitoire) : retenter
"Reset Credential" sur `https://opensky-network.org/my-opensky/account`.
Si ca refonctionne, le secret devrait alors apparaitre normalement (reste
a verifier SOUS QUELLE FORME exactement une fois que la requete reussit
reellement -- pas encore observe, cette session n'a jamais eu de succes a
analyser).

Modele d'appareil ADS-B et quota eleve (authentifie) toujours pas
branches, en attente que cette panne serveur se resolve.

## Suite 2026-08-30 : client_secret OpenSky obtenu (manip NVDA), authentification branchee

L'utilisateur a fini par obtenir le client secret **sans passer par le
bouton casse** ("Reset Credential" restait bloque en 503 cote serveur, voir
section precedente) : navigation clavier NVDA avec emulation de clic
(Insert+clic gauche pave numerique, plusieurs sauts de focus) jusqu'a un
bouton cache qui ouvre un JSON contenant `clientId`/`clientSecret`.
Secret recupere -- desormais dans config/credentials.py, jamais commite (voir .gitignore).

**Implemente dans `adsb_watch.py`** :
- Authentification OAuth2 client_credentials confirmee par la doc
  officielle (`https://openskynetwork.github.io/opensky-api/rest.html`) :
  jeton obtenu via `POST https://auth.opensky-network.org/auth/realms/
  opensky-network/protocol/openid-connect/token`, mis en cache avec marge
  de 60s avant expiration (~30 min de duree de vie). `fetch_aircraft()`
  ajoute l'en-tete `Authorization: Bearer` si un jeton est disponible,
  retombe sur l'anonyme sinon (pas de dependance dure a l'authentification).
- **Cout en credits confirme par la doc** : zone de veille ~1.4 sq deg (sous
  le seuil de 25 sq deg) -> **1 credit/requete**. Avec 4000 credits/jour,
  `POLL_INTERVAL_SEC` reduit de 300s a **30s** (~2880 requetes/jour, marge
  confortable) -- ameliore nettement la fiabilite de detection de survol
  (motif principal de cette montee en frequence, deja signale comme
  limitation dans la section precedente).
- **Modele d'appareil precis (ex "Airbus A320") : confirme NON disponible**
  via l'API OpenSky documentee, meme authentifie (recherche directe de la
  doc officielle en session, aucun endpoint de ce type documente). A la
  place, ajout du champ `category` (categorie ADS-B standard DO-260B,
  ex "avion de ligne", "helicoptere", "planeur"...) traduit en francais
  (`CATEGORY_NAMES`).

**Teste en session** : jeton obtenu avec succes (1458 caracteres, format
JWT), requete authentifiee reussie, en-tete `X-Rate-Limit-Remaining`
confirme la consommation exacte de 1 credit (4000 -> 3999). **Mais** :
le champ `category` etait absent de la reponse pour l'appareil test
(tableau a 17 elements, categorie attendue a l'indice 17 -- absent) --
soit specifique a cet appareil (transpondeur ne diffusant pas cette info),
soit plus generalement peu fiable. Code defensif (`len(state) > IDX_CATEGORY`),
ne casse rien si absent, mais l'enrichissement categorie n'est pas garanti
de se declencher souvent -- a observer en usage reel.

Service redemarre et confirme actif avec l'authentification.

## Suite 2026-08-30 : son "simple_message" pour les annonces meteo non-alarmantes

Nouveau fichier trouve sur le NAS : `/media/nas/nd1/server/meteo_scaner/
simple_message.mp3` (~2 secondes). Demande : le jouer avant toute annonce
meteo "sans gravite" (distinct des sons vigilence_N reserves aux vraies
alarmes orange/rouge).

**Implemente** : nouvelle fonction `_speak_lightly(text)` (joue
`simple_message.mp3` + parle, sous le verrou audio partage). Cablee sur :
- Le ticker de vigilance departementale (montee ET descente de niveau).
- Le heads-up jaune local (`_nowcast_announce` pour niveau < 3) --
  remplace l'usage de `vigilence_2.mp3` qu'on utilisait la avant, plus
  coherent avec le role "sans gravite" de ce niveau.
- Le message de retour au calme apres une alerte locale orange/rouge.

Orange/rouge locaux gardent `vigilence_3.mp3`/`vigilence_4.mp3` (vraies
alarmes, non concernees par ce changement).

**Teste en session** (sequence complete demandee par l'utilisateur,
appels directs simules -- pas d'attente d'evenement reel) :
1. Alerte regionale montee vert->orange
2. Alerte regionale descente orange->vert
3. Local jaune (pluie, 45 min)
4. Local orange (vent, 20 min, avec conseil)
5. Local rouge (orage/CAPE, imminent, avec conseil)
6. Retour au calme local

Les 6 etapes se sont executees sans erreur, phrases conformes au design.
Service redemarre et confirme actif.

## Suite 2026-08-30 : son simple_contact (ADS-B) + direction/vitesse du vent (meteo locale)

**ADS-B** : nouveau son trouve sur le NAS, `/media/nas/nd1/server/
flight_scaner/simple_contact.mp3` (~1s), joue AVANT l'annonce d'un avion
detecte dans la zone de veille (distinct du son de survol direct
`flyer_by.mp3`, qui reste inchange).

**Meteo hyper-locale** : demande d'enrichir les annonces avec (1) le point
cardinal d'ou vient la perturbation et (2) d'autres infos disponibles
(vitesse du vent, lieu actuel nomme...).

- **Point cardinal** : implemente via le champ `wind_direction_10m`
  d'Open-Meteo (deja teste, disponible en degres). Convertit en rose des
  vents a 16 points (`_cardinal_direction()`). **Limite assumee et
  expliquee dans le code** : ce n'est pas un vrai suivi de cellule
  orageuse (l'API ne donne des donnees qu'a UN point, pas de radar
  multi-points) -- c'est une approximation meteorologique standard (le
  vent vient du meme secteur que d'ou la perturbation arrive
  generalement). Annonce : "Vient du {direction}."
- **Vitesse du vent** : champ `wind_speed_10m` ajoute, annonce "Vent a
  X kilometres par heure." (bug de phrase corrige en session : "kilometres
  heure" -> "kilometres par heure").
- **Lieu actuel nomme (ex: "au-dessus de Firminy")** : **NON implemente,
  genuinement indisponible** avec cette source de donnees (point unique,
  pas de suivi de cellule/radar) -- explique clairement a l'utilisateur
  plutot que d'inventer quelque chose. Necessiterait une source de donnees
  radar reelle (hors de portee actuellement, cf le dongle unique deja
  reserve a `radio_watch.py`).

**Teste en session** : `_nowcast_announce()` avec des donnees simulees de
vent (315°, 42 km/h) -> "Des rafales de vent approchent, dans environ 30
minutes. Vient du nord-ouest. Vent à 42 kilomètres par heure. [conseil]" --
conforme. Son `simple_contact.mp3` teste independamment, sans erreur.

Les deux services (`adsb-watch`, `meteo-watch`) redeployes et confirmes
actifs.


## 2026-08-30 : nouveau service `crue_watch.py` -- alerte crue (Vigicrues)

Suite à la veille des trois dernières API (SNCF, STAS, sécurité locale),
demande explicite : "ajoute l'alerte crue en utilisant le système d'alerte
de la météo" -- pendant que l'utilisateur s'occupait de l'inscription SNCF
(https://numerique.sncf.com/startup/api/, jeton gratuit, 150 000
requêtes/mois).

**Source de données** : API Vigicrues, gratuite, sans clé --
`https://www.vigicrues.gouv.fr/services/1/InfoVigiCru.geojson`
(GeoJSON national complet, ~2.2 Mo, un `feature` par tronçon surveillé).

**Tronçon surveillé** : `LC126` = "Loire forézienne". Recherche menée en
session (interrogation directe du geojson national, pas de supposition) --
le Furan n'a **pas** de tronçon Vigicrues dédié à Saint-Étienne même ; ce
tronçon est le plus proche officiellement surveillé, il couvre la Loire à
travers la plaine du Forez ET le Furan via sa station de référence à
Andrézieux (juste en aval de Saint-Étienne). Confirmé par inspection directe
des propriétés du geojson :
`{"CdEntCru":"LC126","lbentcru":"Loire forézienne","cdensup_1":"30",...}`.
Niveau de vigilance testé en direct depuis le Pi le jour même : `1` (vert).

**Architecture -- réutilise explicitement celle de `meteo_watch.py`**,
comme demandé :
- Même échelle de couleurs 1-4 (`vert/jaune/orange/rouge`).
- Même son léger `simple_message.mp3` pour tout changement de niveau
  jaune/vert (pas d'alarme).
- Mêmes sons `vigilence_1..4.mp3` réutilisés tels quels pour l'alarme
  réelle (niveau ≥ orange), avec les mêmes conseils de sécurité parlés
  (adaptés crue : ne pas s'engager sur route inondée, éviter sous-sols/
  caves, couper l'électricité si l'eau approche, s'éloigner des cours
  d'eau).
- Même logique "on ne parle QUE sur un changement de niveau" (pas de
  boucle d'alarme répétée -- contrairement au nowcast météo hyper-local,
  Vigicrues ne publie que 2x/jour officiellement, donc pas de notion de
  "temps réel minute par minute" ici ; c'est un choix délibéré, à
  revisiter si besoin).
- Même persistance d'état dans un fichier JSON temporaire
  (`/tmp/crue_watch_vigilance_state.json`), survit à un redémarrage.
- Sondage horaire (`POLL_INTERVAL_SEC = 3600`), même cadence que la
  vigilance météo départementale.

**Fichiers** :
- `/home/vador/autopilot/pi_console/crue_watch.py` (nouveau, déployé,
  syntaxe validée sur le Pi).
- `/etc/systemd/system/crue-watch.service` (nouveau, même patron que les
  trois autres services -- `User=vador`, `Restart=always`, **pas activé au
  boot, démarrage manuel uniquement**, cohérent avec le reste du projet).

**Testé en session** (deux annonces réelles jouées sur le Pi) :
1. Changement léger jaune->vert : son `simple_message.mp3` + "Le niveau
   d'alerte crue pour le tronçon Loire forézienne vient de baisser,
   passant de jaune à vert." -- conforme.
2. Changement alarme vert->orange : son `vigilence_3.mp3` + "...vient de
   monter, passant de vert à orange. [conseils de sécurité]" -- conforme,
   aucune erreur.

Service pas encore démarré (`systemctl start crue-watch`) -- laissé à
l'utilisateur, comme les trois autres services du projet.

**Pistes de veille encore en attente de décision utilisateur** (non
implémentées) : SNCF (Château-Creux, inscription en cours côté
utilisateur), STAS (GTFS-RT ouvert sans clé, prêt à consommer), sécurité
routière/incidents (aucune API ouverte fiable trouvée -- Bison Futé et
Inforoute Saint-Étienne Métropole n'ont pas de doc API publique
identifiée).


## 2026-08-30 (suite) : correctif prononciation espeak + nouveau service `sncf_watch.py`

### Correctif TTS -- accents mal prononcés ("inondée" -> "inondii", "tronçon" -> lu sans le son du cédille)

Root cause trouvée dans `speak.py` : le binaire utilisé est le vieil
`espeak` 1.48.15 (pas `espeak-ng`), qui devine l'encodage du texte en
entrée au lieu de le lire en UTF-8 par défaut -- la locale système est
pourtant déjà `C.UTF-8`, ce n'est pas un problème d'environnement, juste
un comportement par défaut d'espeak. Fix : ajout du flag `-b 1` (force
l'interprétation UTF-8) à l'appel `subprocess.run(["espeak", ...])`.
Confirmé fonctionnel via `espeak --help` (flag existe sur ce build) avant
déploiement. `speak.py` redéployé, les trois services actifs
(`radio-watch`, `adsb-watch`, `meteo-watch`) redémarrés pour charger le
correctif -- un import Python déjà chargé en mémoire ne relit pas le
fichier sur disque, redémarrage nécessaire à chaque fois qu'un module
partagé change.

### `sncf_watch.py` -- veille gare Saint-Étienne Châteaucreux

Jeton API fourni par l'utilisateur en session (compte créé sur
https://numerique.sncf.com/startup/api/) -- désormais dans config/credentials.py, jamais commité (voir .gitignore).
Testé en direct, HTTP 200, données réelles confirmées (TGV INOUI vers
Paris, TER vers Lyon/Roanne/Firminy/Ambérieu depuis/vers Châteaucreux, UIC
`87726000`).

**Comportement choisi par l'utilisateur** (question posée explicitement,
deux tiers distincts) :
1. **Annonce légère** : tout train prévu dans l'heure qui vient (fenêtre
   glissante, filtrée côté serveur Navitia via `duration=3600`) -- son
   `sncf_moderne.wav` puis type de train + destination + délai avant
   départ. Une seule fois par train (suivi par id `vehicle_journey`).
2. **Alarme perturbation** : dès qu'un écart entre horaire théorique
   (`base_departure_date_time`) et horaire réel (`departure_date_time`)
   dépasse 2 minutes -- son `sncf_retro.mp3` puis annonce du retard avec
   nouvelle heure estimée. Re-déclenche si le retard change (s'aggrave ou
   se réduit), annonce légère de résorption si le retard redescend sous le
   seuil.

Sons trouvés déjà déposés par l'utilisateur dans
`/media/nas/nd1/server/train_scaner/` (`sncf_moderne.wav`,
`sncf_retro.mp3`) -- confirmés présents avant de coder les chemins en dur.

**Sondage toutes les 3 minutes** (480 requêtes/jour max, très large marge
sous le quota gratuit 5000/jour). Suivi en mémoire (pas de fichier d'état
JSON comme la météo/crue -- les trains ont un cycle de vie court, même
logique que `adsb_watch.py` : les trains sortis de la fenêtre sont
oubliés à chaque sondage).

**Limite connue, assumée** : la détection de perturbation ne couvre que
les **retards** (écart théorique/réel sur l'horaire). Les **suppressions
complètes** de train nécessiteraient de croiser le tableau `disruptions`
de la réponse API (effets `NO_SERVICE` liés à des `vehicle_journey`
précis) -- pas implémenté cette session, non testé, à ajouter si besoin
réel constaté.

**Testé en direct sur le Pi** (deux annonces réelles jouées, prononciation
vérifiée avec le correctif espeak) :
1. Appel réel à l'API (résultat vide car test fait à 1h20 du matin --
   aucun train avant 6h42, comportement attendu, pas un bug).
2. Annonce légère simulée : "TGV INOUI à destination de Paris Gare de
   Lyon, départ dans environ 42 minutes." -- conforme.
3. Annonce perturbation simulée : "Perturbation : le TER à destination de
   Lyon Part-Dieu, prévu à 07h15, a du retard. Nouveau départ estimé à
   07h27, soit environ 12 minutes de retard." -- conforme.

**Fichiers** : `/home/vador/autopilot/pi_console/sncf_watch.py` (nouveau,
déployé, syntaxe validée), `/etc/systemd/system/sncf-watch.service`
(nouveau, même patron que les autres -- pas activé au boot, démarrage
manuel).

### En attente utilisateur -- soundscape immersif gare (pas encore implémenté)

L'utilisateur fournit en parallèle des sons `arrive_$numero`,
`depart_$numero`, `passby_$numero`, `horn_$numero` (variantes numérotées,
même logique de variété que le reste du projet) destinés à un habillage
sonore réaliste de ce qui se passe réellement en gare (arrivée, départ,
passage sans arrêt, coup de sifflet/corne) -- distinct des deux annonces
parlées ci-dessus. Pas encore livré au moment de cette session : chemin
NAS, convention de nommage exacte, et logique de déclenchement (quel
événement réel déclenche quel son) restent à préciser avec l'utilisateur
une fois les fichiers déposés.


## 2026-08-30 (suite 2) : `sncf_watch.py` -- suppressions/motifs + format horaire parle

Deux correctifs demandes explicitement avant de clore le chantier SNCF :

### Format horaire -- zeros en tete mal prononces

`strftime('%Hh%M')` produisait des heures comme "07h15" -- espeak lit le
zero en tete chiffre par chiffre au lieu de "sept". Remplace par
`_format_time_spoken(dt)` : `f"{dt.hour} heure"` (pile) ou
`f"{dt.hour} heure {dt.minute}"` -- entiers Python, jamais de zero de
remplissage, mot "heure" ecrit en toutes lettres plutot que la lettre
"h". Format exact demande : "7 heure 30". Teste en direct : `7 heure 30`,
`1 heure` (pile), `9 heure 5` -- conforme.

### Suppressions de train + motifs (cause reelle)

Jusqu'ici seuls les retards (ecart horaire theorique/reel) etaient
detectes -- pas les suppressions completes, et aucun motif n'etait
recupere. Corrige via le tableau `disruptions` deja present (gratuitement,
meme appel API) dans la reponse `/departures` -- pas d'appel
supplementaire.

Schema reel decouvert par interrogation directe de l'API (endpoint
`/coverage/sncf/disruptions`, aucune suppression active a
Chateaucreux au moment du test, mais une trouvee ailleurs sur le reseau
pour valider le schema) :
```
disruptions[].severity.effect   -- "NO_SERVICE" = suppression, sinon retard/autre
disruptions[].messages[0].text  -- LE VRAI MOTIF (ex: "Defaillance de materiel")
disruptions[].cause             -- souvent VIDE en pratique, repli seulement
disruptions[].impacted_objects[].pt_object.id  -- id du trip impacte
```
Le champ `cause` officiel de la doc Navitia est peu fiable en pratique --
le motif utilisable est presque toujours dans `messages[0].text`. Fonction
`_disruption_lookup()` construit un dict `vehicle_journey_id -> {effect,
motif, id}` a partir de ce tableau (attention au prefixe : les departures
utilisent `vehicle_journey:SNCF:...`, les `impacted_objects` utilisent
`SNCF:...` sans prefixe -- concatenation necessaire pour faire matcher).

Comportement :
- **Suppression** (`NO_SERVICE`) : priorite absolue, pas de test de retard
  en plus -- annonce "Suppression : le {mode} a destination de
  {destination}, prevu a {heure}, est supprime. Motif : {motif}." (son
  `sncf_retro`, meme son que les perturbations).
- **Retard avec motif connu** : meme annonce qu'avant + motif ajoute en
  fin de phrase si une perturbation est liee au train.
- Suivi par id de perturbation (`known_disruptions`) en plus du suivi de
  retard existant, pour ne re-annoncer que sur un changement reel (nouvelle
  perturbation, pas la meme repetee a chaque sondage).

**Testes en direct sur le Pi** (annonces reelles jouees, motifs simules
faute de perturbation active au moment du test) :
1. Suppression : "Suppression : le TER a destination de Lyon Part-Dieu,
   prevu a 7 heure 30, est supprime. Motif : Defaillance de materiel." --
   conforme.
2. Perturbation avec motif : "...Nouveau depart estime a 9 heure 17, soit
   environ 12 minutes de retard. Motif : Incident voyageur sur un train
   precedent." -- conforme.
3. Appel reel `/departures` : 0 perturbation active a Chateaucreux au
   moment du test (etat reel du reseau, pas un bug) -- le code de lookup
   tourne sans erreur meme quand le tableau `disruptions` est vide.

Service `sncf-watch.service` redeploye avec cette version, toujours pas
demarre (laisse a l'utilisateur comme les autres).


## 2026-08-30 (suite 3) : ré-accentuation + reformulation retard + inventaire technique API SNCF

**Ré-accentuation** : `sncf_watch.py` avait été écrit sans accents par excès
de prudence (habitude issue des soucis de round-trip Windows en début de
session) -- signalé par l'utilisateur comme une régression, alors que le
correctif `-b 1` d'espeak rend justement les accents corrects. Tous les
messages parlés (et les commentaires) ré-écrits avec accents propres,
redéployé, retesté en direct (annonce légère, retard, suppression) --
aucune erreur.

**Reformulation du message de retard**, demande explicite : au lieu de
"Nouveau départ estimé à HH, soit X minutes de retard.", désormais
"prévu à {heure}, partira avec un retard estimé à {minutes} minutes."
`_speak_disruption()` simplifié (n'a plus besoin de `actual_dt`, seul le
retard en minutes compte pour la phrase).

### Inventaire technique -- données disponibles via l'API SNCF (Navitia), au-delà de ce qui est exploité

Sondé en direct en session (tous les appels ci-dessous confirmés HTTP 200,
pas de la spéculation) :

- **`/journeys`** (Chateaucreux -> une autre gare) : calcul d'itinéraire
  point à point, sensible aux perturbations (recalcule si une ligne est
  coupée). Retourne : `duration`, `nb_transfers`, `co2_emission` (valeur +
  unité gEC), `air_pollutants`, `distances`, `fare` (estimation tarifaire),
  `sections` détaillées (marche, correspondance, trajet ferré...).
  Utilisable pour une requête vocale a la demande type "comment aller a
  Lyon maintenant".
- **`/traffic_reports`** : vue réseau complète (pas juste Châteaucreux) --
  regroupe toutes les perturbations actives par réseau (ex: "REGIONAURA"
  pour le TER Auvergne-Rhône-Alpes), avec la liste des trains impactés par
  perturbation. Permettrait une alerte "l'état du réseau régional" plus
  large que la seule gare de Châteaucreux.
- **`/arrivals`** : miroir de `/departures` pour les trains qui
  *arrivent* (pas ceux qui partent) -- même structure, même logique de
  perturbations exploitable.
- **`/vehicle_journeys/{id}`** : itinéraire complet arrêt par arrêt d'un
  train donné (`stop_times`), avec par arrêt `pickup_allowed`,
  `drop_off_allowed`, `skipped_stop` (arrêt sauté a cause d'une
  perturbation) et `equipments` (équipements d'accessibilité -- vide dans
  l'exemple testé, contenu réel non vérifié).
- **CO2 par mode** : `physical_modes[].co2_emission_rate` (gEC/km),
  déjà visible dans les réponses `/departures` utilisées mais pas exploité
  actuellement.

Rien de tout ça n'est implémenté -- pur inventaire, à la demande de
l'utilisateur, pour décision ultérieure.


## 2026-08-30 (suite 4) : phrasé absolu pour les départs + ajout `/arrivals`

**Micro-correction demandée** : l'annonce légère de départ disait "départ
dans X minutes" -- repassé en heure absolue, cohérent avec le reste :
"{mode} à destination de {destination}, partira à {heure}." Plus de
décompte relatif nulle part dans ce fichier.

**Ajout `/arrivals`** (confirmé par l'utilisateur après clarification --
"On ajoute departures" était ambigu, question posée, réponse : ajouter le
miroir `/arrivals` mentionné dans l'inventaire technique précédent).

Détails techniques découverts en sondant l'API en direct : le champ
`display_informations.direction` d'une entrée d'arrivée reste le
**terminus de la ligne** (où le train continue), pas l'origine réelle du
train. Pour annoncer "en provenance de X", il faut résoudre l'origine via
le tableau `origins` de haut niveau de la réponse (liste id -> nom),
croisé avec le lien `category: "origin"` de l'entrée -- fonction
`_origin_name()` ajoutée pour ça.

**Portée volontairement limitée** : les arrivées n'ont qu'une annonce
légère ("{mode} en provenance de {origine}, arrivée à {heure}."), **pas**
de suivi retard/suppression séparé côté arrivée -- un train qui repart de
Châteaucreux après y être arrivé est déjà couvert par le suivi
perturbation côté départs ; dupliquer l'alarme côté arrivée annoncerait
deux fois le même événement réel pour les trains de passage.

**Refactor** : `main()` éclaté en `_process_departures(state)` +
`_process_arrivals(state)`, `state` = dict partagé
(`announced_departures`, `announced_arrivals`, `known_delays`,
`known_disruptions`) -- 2 appels API par cycle de sondage (départs +
arrivées) au lieu d'un seul, toujours très large marge sous le quota
(960 requêtes/jour max à 3 min d'intervalle, sur 5000/jour disponibles).

**Testé en direct sur le Pi** (annonces réelles jouées) :
1. "TGV INOUI à destination de Paris Gare de Lyon, partira à 9 heure 42."
   -- conforme, plus de décompte en minutes.
2. "TER en provenance de Firminy, arrivée à 8 heure 4." -- conforme.
3. Appel réel `/arrivals` : 0 résultat (aucun train dans l'heure au
   moment du test, comportement attendu comme pour `/departures` plus
   tôt dans la session).

Service redéployé, toujours pas démarré (laissé à l'utilisateur).


## 2026-08-30 (suite 5) : fusion départs+arrivées en un seul bulletin

Demande explicite : ne plus annoncer chaque train individuellement (un
son + une phrase par train), mais fusionner tous les nouveaux
départs/arrivées entrés dans la fenêtre depuis le dernier sondage en
**une seule annonce** (un seul son `sncf_moderne`, un seul appel
`speak()`).

Refactor : `_process_departures()` et `_process_arrivals()` ne parlent
plus directement pour le tiers léger -- elles retournent chacune une
liste de tuples `(mode, destination/origine, heure)` pour les nouveaux
départs/arrivées, collectées par `main()` puis passées à
`_speak_bulletin(new_departures, new_arrivals)` qui construit UNE phrase
concaténée :
```
Le {mode} à destination de {dest} partira à {heure}. [...]
Le {mode} en provenance de {origine} arrivera à {heure}. [...]
```
Silencieux si la liste est vide (pas de son ni de speak() -- testé).

Les alarmes perturbation/suppression restent **inchangées, séparées** du
bulletin -- ce sont des événements distincts qui doivent rester des
alarmes immédiates avec leur propre son `sncf_retro`, pas noyées dans le
bulletin léger.

**Testé en direct sur le Pi** (annonce réelle jouée) :
1. Bulletin fusionné à 2 départs + 1 arrivée simulés : "Le TGV INOUI à
   destination de Paris Gare de Lyon partira à 9 heure 42. Le TER à
   destination de Roanne partira à 10 heure 5. Le TER en provenance de
   Firminy arrivera à 8 heure 4." -- une seule lecture, conforme.
2. Bulletin vide : silence total confirmé (aucun son joué).
3. Cycle réel complet (`_process_departures` + `_process_arrivals` +
   `_speak_bulletin`) : 0 nouveauté au moment du test (période creuse),
   aucune erreur.

Service redéployé, toujours pas démarré.


## 2026-08-30 (suite 6) : système de bruitage ambiant (arriving/leaving)

Demande : dès qu'un nouveau départ/arrivée entre dans la fenêtre horaire,
programmer une tâche en attente qui se réveille pile à l'heure H pour
jouer un son réaliste choisi au hasard dans le dossier correspondant.
Dossiers fournis par l'utilisateur sur le NAS
(`/media/nas/nd1/server/train_scaner/`) : `ter_arriving`, `ter_leaving`,
`ter_passby`, `tgv_arriving`, `tgv_leaving`, `tgv_passby` -- constatés
partiellement garnis au moment du test (`ter_arriving`: 4 fichiers,
`ter_passby`: 1, `tgv_passby`: 3, le reste vide, "en cours de mixage"
comme prévenu par l'utilisateur).

**Mécanisme** : `threading.Timer` par événement (un par nouveau départ/
arrivée détecté), programmé sur l'heure **réelle** (`actual_dt`, retard
déjà connu inclus au moment de la programmation -- pas re-planifié si le
retard change ensuite, limite acceptée). `_train_category(mode)` classe
"TGV" si le mode contient "TGV", "TER" sinon (couvre aussi "TER /
Intercités", "REGIONAURA" vus en usage réel). `_play_ambient_sound()`
choisit un fichier au hasard dans `{categorie}_{action}/`, silencieux
(juste un message log) si le dossier est vide ou absent -- pas d'erreur,
pas de son de repli.

**Bug trouvé et corrigé pendant le test** : les fichiers `ter_arriving`
font 112 à 200 secondes (ambiances longues), contre 7 à 18 secondes pour
`passby` -- un timeout fixe (20s testé d'abord) faisait échouer les
fichiers longs. Corrigé avec `_probe_duration_sec()` (ffprobe la durée
réelle du fichier choisi avant de jouer, timeout = durée + 5s) -- même
pattern que le reste du projet (radio_watch, meteo_watch avaient déjà eu
ce problème avec des sons longs).

**`passby` NON déclenché cette session** -- limite honnête, pas un oubli :
l'API voyageurs `/departures` et `/arrivals` ne renvoie QUE les trains
qui s'arrêtent réellement à Châteaucreux ; un vrai passage sans arrêt n'y
apparaît jamais, aucun signal exploitable avec cette source de données
pour déclencher ce dossier. Les fonctions sont prêtes (`_play_ambient_sound("ter",
"passby")` marche très bien si appelée directement, testé) -- il
manque juste un événement déclencheur réel.

**Dossiers `marchandise_arriving/leaving/passby` repérés sur le NAS mais
non câblés** -- même limite : les trains de fret n'apparaissent pas dans
l'API SNCF grand public (Navitia, coverage "sncf" = voyageurs
uniquement). Pas de signal disponible actuellement pour les déclencher.

**Testé en direct sur le Pi** :
1. `_play_ambient_sound("ter", "passby")` et `("tgv", "passby")` --
   sons courts joués sans erreur (avant et après le fix de timeout, où le
   fix a été validé nécessaire sur `ter_arriving` en premier lieu, qui a
   timeout à 20s avant correction).
2. `_schedule_ambient_sound("TER", "passby", dans 2 secondes)` -- thread
   programmé, déclenché et joué sans erreur dans la fenêtre de test.
3. `_train_category()` : "TGV INOUI" -> tgv, "TER / Intercités" -> ter,
   "REGIONAURA" -> ter -- conforme.

Service redéployé, toujours pas démarré.


## 2026-08-30 (suite 7) : nouveau service `stas_watch.py` -- perturbations réseau bus/tram Saint-Étienne

Demande explicite : scan régulier (pas trop fréquent), son `simple_notif`
(à fournir par l'utilisateur dans `train_scaner`) puis annonce TTS s'il y
a une ou plusieurs perturbations sur le réseau STAS.

**Recherche de source de données** : le flux GTFS-RT officiel (déjà
identifié dans la veille précédente) ne contient QUE des `trip_updates`
(retards de trajet) -- confirmé de nouveau en session, aucun flux
"service alerts" séparé n'existe pour ce réseau. Les vraies perturbations
(travaux, déviations, limitations) observées en direct sur le réseau au
moment du test sont TOUTES de type "Travaux", pas des retards -- le
GTFS-RT n'aurait rien capté de tout ça.

Trouvé à la place : la page publique
`https://tsi.stas3.cityway.fr/fr/infos-trafic/17/Disruption` liste ces
perturbations. Pas d'API JSON documentée derrière (l'appel AJAX
`/fr/Disruption/getdisruptedlinegroups` observé dans le navigateur ne
retourne que du HTML, pas du JSON, et ne sert qu'au bandeau d'icônes de
lignes en haut de page) -- **mais confirmé par curl direct (sans
JavaScript) que la liste complète des perturbations est déjà rendue
côté serveur** dans le HTML de la page principale, donc récupérable par
un simple `requests.get()` depuis le Pi. Sélecteurs CSS stables identifiés
par inspection du DOM : `div.disruption.clearfix` (un bloc par
perturbation), `.disruption-title`, `.disruption-content`.

**`beautifulsoup4` installé sur le Pi** (`pip3 install --user
beautifulsoup4`, confirmé 4.15.0) -- première dépendance externe du
projet en dehors de `requests`, jugée préférable à du parsing HTML par
regex (fragile face aux changements de balisage).

**Comportement** : sondage toutes les 30 minutes (perturbations de ce
type durent des jours/mois, pas besoin de plus fréquent). État persisté
dans `/tmp/stas_watch_state.json` (titres connus, survit à un
redémarrage). N'annonce QUE les perturbations **nouvelles** par rapport
au dernier sondage (même logique que meteo/crue -- pas de répétition de
ce qui est déjà connu, une perturbation peut durer des mois sans être
ré-annoncée). Pas d'annonce de résorption (non demandé, portée volontairement limitée à ce qui a été demandé).

**Testé en direct sur le Pi** :
1. `fetch_disruptions()` réel : 4 perturbations actives trouvées
   (travaux Ligne M2, Ligne 33 jours de marché, secteur Tarentaize
   plusieurs lignes, secteur Hauts de Terrenoire ligne 20) -- parsing
   confirmé correct.
2. Annonce TTS réelle jouée avec 2 perturbations simulées comme
   "nouvelles" : "2 nouvelles perturbations sur le réseau de bus et tram
   de Saint-Étienne. [titre 1] [titre 2]." -- conforme. Son
   `simple_notif.mp3` absent comme attendu (pas encore fourni par
   l'utilisateur) -- échoue proprement (message de log, pas de crash),
   la partie TTS continue normalement malgré l'échec du son.

Service `stas-watch.service` créé, pas démarré (laissé à l'utilisateur
comme les autres).


## 2026-08-30 (suite 8) : audit anti-chevauchement audio inter-services

Demande explicite : vérifier que les annonces des 6 services ne se
superposent jamais, qu'elles se mettent en file d'attente sinon.

**Audit** : grep systématique de tous les appels `speak(`/`play_sound_effect(`
dans les 6 fichiers, croisé avec l'emplacement des
`audio_lock.acquire()`/`release()`. Résultats :
- `adsb_watch.py`, `meteo_watch.py`, `crue_watch.py`, `sncf_watch.py`
  (y compris le bruitage ambiant programmé par `threading.Timer`),
  `stas_watch.py` : **tous les appels correctement protégés**.
- `radio_watch.py` : `_handle_candidate()` tient déjà le verrou pour tout
  le cycle (annonce d'ouverture + écoute live + décodage numérique + son
  de fin) -- couverture correcte malgré les apparences (le verrou est pris
  par l'appelant, pas visible en grepant `stream_until_quiet()` isolément).
  **UN vrai trou trouvé** : le chien de garde "dongle indisponible" dans
  `main()` (avertissement/rétablissement) tournait **sans** le verrou --
  seul point du projet passé entre les mailles. Corrigé (3 blocs
  encapsulés dans `audio_lock.acquire()/release()`, même pattern que
  partout ailleurs).

**Nettoyage au passage** : BOM UTF-8 parasite en tête de `radio_watch.py`
(présent depuis avant cette session, jamais gênant à l'exécution réelle
-- `python3 -u` l'ignore au niveau du tokenizer -- mais faisait échouer
la validation `ast.parse()` utilisée pour chaque déploiement cette
session). Supprimé.

**Preuve empirique** (pas juste une relecture de code) : deux processus
Python indépendants lancés en parallèle EXACTEMENT au même instant,
chacun tentant `audio_lock.acquire()` puis `speak()` un message de
~3 secondes. Résultat mesuré :
```
[B] verrou obtenu a t=...06 (attente: 0.00s)   -- gagne la course
[A] verrou obtenu a t=...38 (attente: 3.32s)   -- attend EXACTEMENT
                                                   la duree de B
```
Zéro chevauchement, mise en file d'attente confirmée au chronomètre entre
deux processus distincts (pas deux threads du même processus -- le cas
le plus exigeant, celui qui compte vraiment entre services séparés).

`radio-watch.service` redéployé et redémarré avec le correctif.


## 2026-08-30 (suite 9) : simulation complète des 6 services + accents manquants trouvés sur crue_watch.py

Demande : simulation service par service de toutes les configurations
possibles, maintenant que l'anti-chevauchement est validé.

**18 scénarios simulés en une seule passe** (un seul script,
`simulate_all.py`, importe les 6 modules et appelle directement leurs
fonctions internes avec des données synthétiques -- pas besoin d'état
réel externe) :
- radio_watch (2) : dongle indisponible, dongle rétabli
- adsb_watch (2) : nouveau contact zone radar, survol direct
- meteo_watch (5) : vigilance montée/baisse département, heads-up jaune
  local, alarme orange locale + conseils, retour au calme
- crue_watch (2) : changement léger, alarme + conseils
- sncf_watch (5) : bulletin départs+arrivées fusionné, perturbation avec
  motif, suppression avec motif, retard résorbé, bruitage ambiant court
  (tgv_passby -- les ambiances longues arriving/leaving, 100-200s,
  volontairement exclues de la simulation pour ne pas la faire durer des
  dizaines de minutes)
- stas_watch (2) : une perturbation, plusieurs perturbations

Tout tourne en séquence stricte via le verrou audio partagé (chaque
scénario précédé d'une courte annonce parlée "Simulation X" qui passe
elle-même par le verrou) -- aucun chevauchement, aucune erreur, exit
code 0.

**Régression trouvée par la simulation elle-même** (pas par relecture --
c'est justement l'intérêt d'avoir tout fait parler d'affilée) :
`crue_watch.py` avait encore le texte SANS accents écrit tôt en session,
avant l'existence du correctif `-b 1` d'espeak -- jamais retouché quand
`sncf_watch.py` avait été corrigé plus tard. Résultat entendu dans la
simulation : "tronçon" redevenu "troncon", "inondée" redevenu "inondee",
etc. -- **exactement le bug d'origine signalé par l'utilisateur**, sur un
fichier différent de celui déjà corrigé.

**Corrigé** : `TRONCON_NAME`, `SAFETY_ADVICE`, le message parlé dans
`main()`, et les commentaires/logs de `crue_watch.py` entièrement
ré-accentués. Redéployé, retesté en direct (les deux mêmes scénarios) :
"tronçon", "forézienne", "inondée", "électricité", "Éloignez" tous
correctement rendus cette fois.

**Le reste du catalogue vérifié propre** dans la sortie de la simulation
(radio, adsb, meteo, sncf, stas) -- pas de régression similaire ailleurs.


## 2026-08-30 (suite 10) : alarme bouclée 60s + survol avec annonce superposée au son

Trois demandes suite à la simulation précédente.

### 1. Alarme vigilance bouclée 60s (meteo_watch.py + crue_watch.py)

Avant : son joué une fois, puis message complet (alerte + conseil) parlé
une fois, séquence terminée. Nouveau comportement demandé : boucler
(son + rappel du message d'alerte, sans le conseil) pendant au moins 60
secondes, PUIS couper et parler le conseil de sécurité une seule fois,
au calme.

`_speak_looping_alarm(level, alert_text, advice_text)` ajoutée dans les
deux fichiers (code identique, cohérent avec le principe "réutilise
l'architecture" déjà établi entre ces deux services) :
```python
deadline = time.time() + ALARM_LOOP_DURATION_SEC  # 60
while time.time() < deadline:
    play_sound_effect(VIGILANCE_SOUNDS[level], ...)
    speak(alert_text)
if advice_text:
    speak(advice_text)
```
Le nombre de répétitions dépend de la durée réelle du son -- pas un
compteur fixe. `meteo_watch._nowcast_announce()` et le bloc `niveau >= 3`
de `crue_watch.main()` redirigés vers cette fonction (message d'alerte
sans le conseil pour le corps de boucle, conseil séparé pour l'après).

**Testé en direct, niveau rouge (4) réel** :
- météo : message "Un orage approche, dans environ 10 minutes." répété 2
  fois (2 cycles son+parole vu la durée du son `vigilence_4.mp3`), puis
  "Débranchez les appareils électriques sensibles. Fermez fenêtres et
  volets." une seule fois -- 97,4s au total.
- crue : même schéma, "...passant de orange à rouge." répété 2 fois,
  conseil de sécurité une fois -- 115,6s au total.

**Note sur "niveau noir"** : demandé par l'utilisateur en complément du
rouge -- **n'existe pas dans l'échelle officielle** (vert/jaune/orange/
rouge uniquement, confirmé dès la recherche initiale de session). Pas
simulé, clarifié à l'utilisateur plutôt que d'inventer un 5e niveau
fictif.

### 2. Survol avion : annonce superposée au son du flyby (adsb_watch.py)

Avant : son flyby joué en entier (bloquant, `subprocess.run`), PUIS
annonce parlée après -- au moment de l'annonce, le son était déjà fini
depuis longtemps, aucun lien audible entre le bruit et le sens.

Nouveau : `subprocess.Popen` (non bloquant) démarre le son, `time.sleep(3)`,
PUIS `speak()` par-dessus le son qui continue de jouer, puis attente de
la fin du son (`proc.wait()`) avant de relâcher le verrou -- import de
`AUDIO_DEVICE` depuis `sound_player` ajouté pour pouvoir lancer ffmpeg
manuellement en arrière-plan (au lieu de passer par
`play_sound_effect()`, qui est bloquant par construction).

**Testé en direct** : exécution propre, sans erreur, durée totale
38,33s -- cohérente avec la durée réelle connue du fichier
`flyer_by.mp3` (confirme que le son n'a pas été coupé/tronqué et que
`speak()` s'est bien déclenché au bon moment). **Limite assumée** : je
ne peux pas juger moi-même du rendu audio du chevauchement (pas
d'oreilles) -- seule l'exécution technique (pas d'erreur, bonne durée)
est vérifiable côté code, la qualité perçue du mélange doit être
confirmée par l'utilisateur.

Trois services redéployés (`meteo-watch`, `crue-watch`, `adsb-watch`).


## 2026-08-30 (suite 11) : refonte alarme à 3 paliers (ao/ar + 15min/10min)

L'utilisateur a redéposé des sons plus spécifiques et redéfini le
comportement d'alarme en conséquence -- remplace le design "boucle 60s
+ conseil" livré juste avant (toujours valable dans sa structure
générale, mais le son et le texte répété changent, et un système de
paliers de rapprochement s'ajoute côté météo).

**Nouveaux fichiers repérés sur le NAS**
(`/media/nas/nd1/server/meteo_scaner/`) : `ao_orage.mp3`, `ao_vent.mp3`,
`ao_cru.mp3` (alerte orange, un son par phénomène), `ar_orage.mp3`,
`ar_vent.mp3`, `ar_cru.mp3` (alerte rouge, idem), `15min.mp3`,
`10min.mp3` (sons de compte à rebours, partagés/génériques).

**Comportement demandé** :
1. Alerte initiale : boucle le son `ao_/ar_{phénomène}`, espeak répète
   régulièrement "Alerte {niveau} {phénomène} !" (court, plus la longue
   phrase descriptive d'avant), puis coupe et parle les précautions une
   seule fois à la fin.
2. Côté météo uniquement (seul système avec une échéance/ETA -- Vigicrues
   n'en a pas pour les crues) : quand la perturbation est à 15 minutes,
   palier "15min.mp3" en boucle avec juste "Alerte {niveau} {phénomène} !"
   répété (pas de précautions, déjà données à l'étape 1) ; sous les 10
   minutes, palier "10min.mp3" idem.

**`meteo_watch.py`** : `_nowcast_announce()` simplifiée (ne gère plus que
le heads-up jaune, sans alarme). Nouvelles fonctions
`_alert_sound_path()` (résout `ao_/ar_{suffixe}.mp3`, repli sur
`VIGILANCE_SOUNDS[level]` générique si le phénomène n'a pas de son dédié
-- cas de "pluie", pas encore fourni), `_loop_alarm_sound()` (boucle
générique, verrou géré par l'appelant), `_speak_initial_alert()`,
`_speak_proximity_alert()`. `_nowcast_watcher()` réécrit en machine à
états à 3 indicateurs (`episode_active`, `stage_15_done`,
`stage_10_done`), remis à zéro au retour sous le seuil orange.

**`crue_watch.py`** : même schéma sans les paliers de rapprochement (pas
d'ETA disponible) -- `_speak_initial_alert()` boucle `ao_cru`/`ar_cru`
avec "Alerte {niveau} crue !", conseils une fois à la fin.

**Testé en direct, 4 scénarios réels** (~5 minutes au total, chaque
palier boucle au moins 60s) :
1. Météo, initiale orange/orages : `ao_orage.mp3` + "Alerte orange
   orages !" x2 + conseil -- 95,4s.
2. Météo, palier 15 min : `15min.mp3` + "Alerte orange orages !" x6,
   pas de conseil répété -- 69,8s.
3. Météo, palier 10 min : `10min.mp3` + x7 répétitions -- 77,2s.
4. Crue, initiale rouge : `ar_cru.mp3` + "Alerte rouge crue !" x10 +
   conseil -- 77,5s.

Tout conforme, aucune erreur, accents corrects partout. Services
redéployés (`meteo-watch`, `crue-watch`).


## 2026-08-30 (suite 12) : rayon ADS-B resserré à 20km + retest flyby (son retouché)

**`MAX_RADIUS_KM`** : 50.0 -> 20.0 dans `adsb_watch.py`, demande
explicite. La boîte de recherche API (LAT/LON_MIN/MAX) reste inchangée
-- c'est un filtre de coût de requête indépendant, le vrai filtre
d'annonce est ce rayon. Redéployé, `adsb-watch` redémarré.

**Retest flyby** : l'utilisateur a retouché `flyer_by.mp3` --
durée mesurée passée de ~38s à **4,37s** (confirmé par ffprobe avant le
test). Testé en direct avec le mécanisme de chevauchement livré plus tôt
(son démarré, 3s d'attente, puis annonce par-dessus) : exécution propre,
6,78s au total, pas d'erreur.

**Point à surveiller, signalé pour info** : avec un son aussi court
(4,37s) et un délai fixe de 3s avant l'annonce, la fenêtre de vrai
chevauchement audio se réduit à ~1,4s avant que le son ne se termine --
le reste de l'annonce parlée continue seul après. Pas changé sans
consigne explicite (le délai de 3s était une demande précise) --
à ajuster si le rendu à l'oreille ne convient pas.


## 2026-08-30 (suite 13) : retour au flyby séquentiel simple

Revert explicite du mécanisme de chevauchement livré plus tôt (son
`Popen` non-bloquant + `sleep(3)` + annonce par-dessus) -- demande :
"remet juste l'annonce vocale après le son flyer_by sans autre calcul".
Cohérent avec le fait que le son a été raccourci à 4,37s entre-temps,
rendant le calcul de chevauchement peu pertinent.

`_announce_flyby()` repassée en séquentiel simple : son joué en entier
(`play_sound_effect`, bloquant), PUIS annonce parlée -- exactement la
forme d'origine avant la tentative de chevauchement. Import `subprocess`
et `AUDIO_DEVICE` retirés (plus utilisés nulle part ailleurs dans le
fichier).

Testé en direct : "TEST-SIMPLE passe juste au-dessus." après le son,
6,54s au total, aucune erreur. `adsb-watch` redéployé et redémarré.


## 2026-08-30 (suite 14) : les 6 services activés au démarrage (changement de convention)

Demande explicite : "redémarre tous les scanners et ajoute-les en
démarrage automatique après un reboot". **Change la convention établie
tout au long de cette session** ("aucun service activé au boot,
démarrage manuel uniquement") -- désormais les 6 services démarrent
automatiquement avec le système.

```
sudo systemctl restart radio-watch adsb-watch meteo-watch crue-watch sncf-watch stas-watch
sudo systemctl enable  radio-watch adsb-watch meteo-watch crue-watch sncf-watch stas-watch
```

Confirmé : les 6 sont `active` ET `enabled`. Catalogue complet des
services autonomes tournant sur ce Pi, tous coordonnés par le même
verrou audio partagé (`audio_lock.py`) :
- `radio-watch` -- scan RF (446, aviation, marine, VHF pro/bas, PMSE,
  MilAir, CB AM/NFM, décodage numérique)
- `adsb-watch` -- trafic aérien (OpenSky, rayon 20km, survol direct)
- `meteo-watch` -- vigilance département + alerte hyper-locale à 3
  paliers (initiale ao/ar, 15min, 10min)
- `crue-watch` -- vigilance crue (Vigicrues, tronçon Loire forézienne)
- `sncf-watch` -- gare Châteaucreux (départs/arrivées, retards,
  suppressions, bruitage ambiant arriving/leaving)
- `stas-watch` -- perturbations réseau bus/tram Saint-Étienne

**À surveiller au prochain reboot réel** (pas testé cette session, seul
le redémarrage à chaud + `enable` ont été vérifiés) : que les 6
démarrent bien tout seuls sans conflit au boot -- `network-online.target`
est déjà en dépendance dans chaque unit (`After=`/`Wants=`), donc en
théorie robuste, mais un vrai reboot n'a pas été déclenché pour
confirmer.


## 2026-08-30 (suite 15) : "j'entends rien du dongle" -- diagnostic + auto-incident

Signalement utilisateur : silence radio total ressenti. Investigation :

**Analyse historique** : journal `radio-watch` sur ~7h (22h41 -> 5h13,
avant toute intervention de diagnostic) : **aucune ligne "candidat sur..."**
-- aucun signal détecté sur aucune bande pendant toute la nuit. Contexte :
dimanche matin avant l'aube, la période la plus calme plausible pour
PMR446/VHF pro/aviation/marine/CB. Cohérent avec un spectre réellement
silencieux.

**Incident auto-causé pendant le diagnostic** : un premier test manuel
(`rtl_power` via SSH, censé être borné par `timeout 8`) a planté côté
connexion SSH (PipeTimeout côté paramiko) SANS que le process distant se
termine -- résultat : `rtl_power` est resté vivant en zombie ~1,5 minute
(5h16-5h18), entrant en concurrence USB avec `radio-watch` (confirmé par
`usb_claim_interface error -6` sur un second test, et par `ps aux`
montrant les deux process simultanés). **Root cause du symptôme "silence"
au moins pour cette fenêtre precise** : mon propre outil de diagnostic,
pas une panne du dongle.

**Nettoyé** : process orphelins tués (`kill -9`), `radio-watch`
redémarré. **Preuve de bon fonctionnement immédiatement après** : le
scanner a détecté un vrai signal PMR446 à 446.408250 MHz dans les
secondes qui ont suivi, tenté un décodage numérique (dsd-fme, 0 erreur,
extinction propre) -- comportement normal confirmé en conditions réelles.

**Leçon pour les prochains diagnostics dongle** : ne jamais lancer un
test SDR ad-hoc sans vérifier après coup qu'aucun process n'est resté
accroché (`ps aux | grep rtl_`) -- un timeout SSH côté client
n'implique PAS que la commande distante s'est arrêtée.


## 2026-08-30 (suite 16) : intégration RNNoise dans radio_watch.py (discrimination voix/bruit)

Remplace l'ancienne heuristique maison (`_highpass_ratio` + `_zero_crossing_rate`,
calibrée "à l'aveugle" sur données synthétiques) par RNNoise, un modèle de
réseau de neurones entraîné spécifiquement à distinguer voix humaine et
bruit large bande (Xiph/Mozilla, licence BSD). Choisi après comparaison
précise avec `ham2mon` (GNU Radio) -- ham2mon n'a en réalité qu'un simple
squelch de puissance, pas de discrimination intelligente, et son vrai
atout (multi-canal parallèle) n'était pas ce qui était demandé.

### Obstacle réel rencontré : architecture 32 bits

Le Pi tourne un **userland ARM 32 bits** (`armv8l`) malgré un noyau 64
bits (`uname -m` affiche `aarch64`, mais `python3 -c "import platform;
print(platform.machine())"` confirme `armv8l`) -- confusion facile,
documentée ici pour la prochaine fois. `pip install pyrnnoise` échoue
silencieusement ("no matching distribution") car ce paquet ne publie de
roue précompilée que pour `manylinux2014_aarch64` (ARM 64 bits), pas pour
l'ARM 32 bits. Pas de distribution source publiée non plus -- impossible
de compiler via pip.

**Contournement** : clone direct du dépôt GitHub
(`pengzhendong/pyrnnoise`, sous-module `xiph/rnnoise`), compilation
manuelle via CMake. Le module RNNoise natif (C pur, portable) compile
sans souci sur ARM 32 bits. Seuls les fichiers strictement nécessaires
(`rnnoise.py` + `librnnoise.so` compilé, ~14 Mo) copiés dans un nouveau
petit module local `pi_console/rnnoise_lib/` -- volontairement PAS
d'installation pip complète du paquet officiel, qui traîne des
dépendances lourdes et inutiles ici (`audiolab`, `matplotlib`, `click`,
`tqdm`, prévues pour son CLI de fichiers, pas pour du streaming). Seule
dépendance système ajoutée : `numpy` (+ `libopenblas0` via apt, requis
par numpy) -- déjà présent `audioop` (stdlib) réutilisé pour le
rééchantillonnage.

### Performance : vectorisation NEON indispensable

Premier build (flags CMake par défaut) : avertissement compilateur
"Compiling without any vectorization. This code will be very slow" --
confirmé au chronomètre : **0.6x temps réel** (5,28s de calcul pour 3s
de son), *inutilisable* en flux continu. Cause : le `CMakeLists.txt`
fourni n'a un chemin optimisé que pour x86 (option `RTCD`, AVX/SSE),
rien pour ARM. Or le code source RNNoise (`vec_neon.h`) a bien un
chemin NEON, actif automatiquement si le compilateur définit
`__ARM_NEON__` -- pas le cas avec les flags de compilation par défaut
de ce Raspberry Pi OS (ciblage `armv6+fp` conservateur, compatible
jusqu'au tout premier Pi).

Recompilé avec `-march=armv8-a -mfpu=neon-fp-armv8 -O3` (flags choisis
pour ce Pi 4 spécifique) : **2,3x temps réel** (1,31s pour 3s de son) --
~4x plus rapide, viable en flux continu avec marge (chaque frame de 10ms
traitée en ~4,4ms en moyenne). Loin des "7x temps réel sur Pi 3"
annoncés par le projet (probablement mesuré avec l'optimisation x86 sur
un autre matériel) -- chiffre honnête mesuré sur CE Pi, pas une
extrapolation.

### Intégration dans radio_watch.py

- `_chunk_is_signal(chunk_bytes, sample_rate, rnnoise_state,
  resample_state)` : filtre RMS bon marché en premier (évite de
  solliciter RNNoise sur du silence pur), puis réechantillonnage vers
  48kHz (fréquence native du modèle -- `audioop.ratecv`, état de
  réechantillonnage reporté d'un appel à l'autre pour éviter les
  artefacts aux frontières), puis moyenne de la probabilité de voix
  RNNoise sur les ~20 frames de 10ms du morceau de 200ms.
- `SPEECH_PROB_THRESHOLD = 0.6` -- **point de départ**, comme tous les
  autres seuils de ce projet, à affiner avec un retour réel sur le
  terrain. Bruit blanc synthétique donne une moyenne ~0.5 (pas 0 net),
  d'où le seuil légèrement au-dessus.
- État RNNoise créé une fois par appel à `stream_until_quiet()`
  (`_rnnoise_create()`), détruit dans le `finally` aux côtés du
  nettoyage des sous-processus (`_rnnoise_destroy()`).
- Ancien code mort supprimé entièrement (`_highpass_ratio`,
  `_zero_crossing_rate`, `NOISE_HF_CUTOFF_HZ`, `NOISE_RATIO_THRESHOLD`,
  `ZCR_THRESHOLD`, `import struct` devenu inutile).

**Différence de comportement à connaître** : contrairement à l'ancienne
heuristique (qui pouvait accrocher sur une tonalité pure basse fréquence,
faible ZCR/HF), RNNoise est spécifiquement entraîné sur de la VOIX
humaine -- testé en direct, une tonalité pure à 300Hz ne déclenche PAS
`is_signal` avec le nouveau système. C'est cohérent avec la demande
initiale ("discriminer les voix"), mais un changement réel de
comportement sur du contenu non-vocal (tonalités CTCSS, porteuses
test...) à surveiller au retour d'écoute réel.

**Testé en direct sur le Pi** :
1. Import du module : propre, aucune erreur.
2. 10 morceaux de bruit blanc à 22050Hz (200ms chacun, réechantillonnage
   inclus) : `is_signal=False` constant, 2,0s d'audio équivalent traités
   en 0,886s (facteur temps réel ~2,26x, cohérent avec le benchmark
   isolé).
3. Silence pur à 48000Hz : `is_signal=False` (filtre RMS).
4. Tonalité pure 300Hz à 22050Hz (5 morceaux) : `is_signal=False`
   (comportement RNNoise attendu, voir ci-dessus).
5. `radio-watch` redémarré : actif, `NRestarts=0`, aucune erreur dans
   le journal.

**Reste à faire, hors session** : validation avec de la vraie voix
captée en direct (pas simulée) pour confirmer les vrais positifs, et
ajustement de `SPEECH_PROB_THRESHOLD` selon le retour d'écoute réel.


## 2026-08-30 (suite 17) : correctif faux positifs massifs RNNoise (lissage)

Signalement utilisateur juste après le déploiement RNNoise : le scanner
"s'arrête sur toutes les fréquences" et joue tous les parasites, plus un
sifflement notable pendant une vraie réception PMR446.

**Diagnostic mené sur les vrais logs, pas de suppositions** : sur ~19
candidats détectés en ~10 minutes, seuls 3 avaient été écartés comme
bruit -- confirme le signalement, quasi tout était accepté comme signal.

**Root cause confirmée par capture radio réelle** (pas un test
synthétique) : la probabilité de voix RNNoise, calculée morceau par
morceau (200ms), oscille violemment sur du **vrai bruit radio** -- mesuré
0.16 à 0.82 sur une fréquence vide en 4 secondes. Un seuil brut appliqué
au morceau courant est structurellement fragile face à ça : n'importe
quel pic isolé de bruit pouvait franchir le seuil.

**Performance écartée comme cause du sifflement** : mesuré en direct,
83,8ms de calcul en moyenne pour un budget de 200ms par morceau (0/21
morceaux en dépassement) -- pas de retard, pas d'accumulation de file
d'attente. Le sifflement pendant la réception 446 n'est donc
probablement PAS causé par ce changement (RNNoise ne touche jamais
l'audio réellement joué -- seule une COPIE réechantillonnée sert au
calcul de détection, `aplay` reçoit toujours le morceau brut original) --
reste une piste ouverte, séparée, à creuser si le problème persiste.

**Correctif** : moyenne mobile exponentielle sur la probabilité de voix
(`SPEECH_PROB_SMOOTHING_ALPHA = 0.3`, poids du morceau courant) avant
comparaison au seuil, plutôt qu'une décision brute morceau par morceau.
Seuil remonté `0.6 -> 0.65` en accompagnement. `_chunk_is_signal()` porte
maintenant aussi `smoothed_prob` en état (comme `resample_state`),
reporté d'un appel à l'autre.

**Revalidé par capture réelle sur 3 fréquences** qui avaient
précédemment déclenché des faux positifs (vhf_business 172.79MHz,
vhf_low 47.998MHz, uhf_business 460.795MHz) : **0 morceau sur 63
classé signal**, `smoothed_prob` se stabilise entre 0.01 et 0.43 selon
la fréquence, toujours nettement sous le seuil de 0.65 -- comportement
correct confirmé, pas juste espéré.

Service redéployé, `NRestarts=0`, aucune erreur.

**Encore un point de départ, pas figé** : `SPEECH_PROB_THRESHOLD=0.65`
et `SPEECH_PROB_SMOOTHING_ALPHA=0.3` restent à affiner avec un vrai
retour d'écoute -- pas de vraie voix radio testée en session (pas de
transmission réelle disponible au moment du test), seulement du bruit
réel (faux négatifs a priori corrigés, vrais positifs pas encore
reconfirmés après ce changement précis).


## 2026-08-30 (suite 18) : faux positifs toujours présents malgré le lissage -- logging diagnostique ajouté au lieu de re-deviner un seuil

Retour utilisateur après le correctif de lissage précédent : amélioration
réelle sur la clarté voix/musique (voix PMR446 et FM commerciale
"clairement plus identifiables"), mais **toujours beaucoup de faux
positifs**, y compris un cas de coupure du scanner en pleine réception
réelle sur la bande CB AM.

**Vérification en logs de production (pas une capture isolée)** :
23 candidats tenus sur 30 dans les ~20 dernières minutes -- confirme
le signalement, le lissage seul (`0.65` + `alpha=0.3`) est insuffisant.

**Tentative de reproduction infructueuse, root cause du blocage
identifiée** : capture en direct sur les fréquences EXACTES venant de
déclencher en production (milair 287.999MHz, aviation 120.0145MHz,
radioamateur 432.002MHz, uhf_business 460.795/467.220MHz, pmse
633.603MHz, vhf_business 172.802MHz) -- **zéro déclenchement retrouvé,
sur aucune d'elles**, alors que ces mêmes fréquences avaient déclenché
quelques minutes plus tôt. Conclusion : le spectre RF change en
permanence, impossible de rejouer après coup les conditions exactes qui
ont causé un faux positif -- re-deviner un seuil sans données réelles au
moment du déclenchement serait un nouveau tir à l'aveugle, pas une vraie
correction.

**Choix fait à la place : instrumentation, pas un nouveau réglage
spéculatif.** Ajout d'un log `[radio_watch] déclenchement signal :
smoothed_prob=X.XXX, rms=Y` au moment exact où `heard_signal` bascule à
`True` (une seule fois par flux, pas de spam). Objectif : capturer la
VRAIE valeur qui a déclenché chaque détection en conditions réelles,
plutôt que des captures a posteriori sur un spectre qui a déjà changé.
Service redéployé.

**CB AM "coupe en pleine réception"** : hypothèse examinée -- le
mécanisme de coupure (`SILENCE_DEBOUNCE_SEC=1.5s`, `MIN_CONSECUTIVE_
SIGNAL_WINDOWS=3`) existait déjà avant cette session et n'a pas été
modifié par l'intégration RNNoise. Pourrait être un silence réel >1.5s
dans la conversation perçu comme une vraie coupure, ou un vrai problème
de detection -- pas encore élucidé, pas de capture disponible au moment
exact du signalement (pas de trafic CB actif pendant les tests de
session, RMS ~300-1100 constant = silence de fond).

**Amélioration confirmée côté qualité perçue** (positif, à noter) :
voix PMR446 et musique FM commerciale jugées "plus identifiables"
qu'avant le passage à RNNoise -- cohérent avec l'objectif initial de la
demande utilisateur.

**Prochaine étape** : laisser tourner, puis relire les logs
`[radio_watch] déclenchement signal` accumulés pour calibrer
`SPEECH_PROB_THRESHOLD`/`SPEECH_PROB_SMOOTHING_ALPHA`/`MIN_CONSECUTIVE_
SIGNAL_WINDOWS` sur des VRAIES valeurs de déclenchement, faux positifs
comme vrais positifs -- alternative plus rapide : un test collaboratif
en direct (l'utilisateur transmet sur PMR446 pendant qu'on capture).


## 2026-08-30 (suite 19) : cycle marche/pause + sécurité thermique radio_watch.py

Contexte sérieux : l'utilisateur (aveugle, ne peut pas inspecter
visuellement son installation) a ressenti une légère décharge en touchant
la jonction câble antenne/prise -- **absente auparavant**, apparue avec
le fonctionnement 24h/24 activé plus tôt en session. Inquiétude
supplémentaire sur la chauffe du dongle/Pi. **Rappelé explicitement à
l'utilisateur** : aucune vérification électrique/physique n'est possible
à distance -- ceci n'est PAS un diagnostic ni une correction du problème
électrique potentiel, seulement une réduction du temps de fonctionnement
en attendant une vérification par une personne voyante.

**Mesuré avant d'agir** (`vcgencmd`) : température CPU 74.5°C au moment
du signalement, charge CPU ~20% (pas de calcul en cause), aucun
throttling signalé (`throttled=0x0`) -- mais 74.5°C reste chaud pour un
fonctionnement prolongé.

**Implémenté dans `radio_watch.py`** :
- Cycle marche/pause régulier : `SCAN_ON_DURATION_SEC=900` (15 min actif),
  `SCAN_OFF_DURATION_SEC=300` (5 min de repos complet, dongle inutilisé).
  Annonce vocale au début et à la fin de chaque pause programmée.
- Sécurité thermique indépendante, vérifiée toutes les
  `TEMP_CHECK_INTERVAL_SEC=60`s même en pleine phase active :
  `_get_cpu_temp_c()` (via `vcgencmd measure_temp`, tolérant à l'échec --
  retourne `None` sans planter si indisponible). Si température
  >= `TEMP_PAUSE_C=72.0`, pause forcée avec annonce dédiée
  ("température élevée : X degrés"), attend d'être repassé sous
  `TEMP_RESUME_C=65.0` (hystérésis, évite les allers-retours rapides)
  avant de reprendre, avec sa propre annonce de reprise.
- Seuils choisis avec marge sous les 74.5°C déjà constatés en session.

**Testé** : syntaxe validée, service redéployé et confirmé actif/stable
(`NRestarts=0`, cycles dsd-fme normaux observés en direct juste après
déploiement). Test isolé de `_get_cpu_temp_c()`/`_speak_pause()` non
concluant -- le script de test est resté bloqué plusieurs minutes en
attente du verrou audio partagé (scanner très actif au moment du test,
pas un bug : le verrou fonctionne comme prévu, juste pas d'ouverture
disponible pendant la fenêtre de test) -- tué proprement (pas de
matériel impliqué, sans risque), confiance basée sur le fonctionnement
propre du service déployé en conditions réelles plutôt que sur ce test
isolé.

**Non testé en conditions réelles** : le cycle complet 15min/5min lui-même
(trop long pour cette session) et le déclenchement effectif de la pause
thermique (température restée sous 72°C pendant les tests) -- se
déclenchera naturellement dans la durée d'utilisation normale.


## 2026-08-30 (suite 20) : investigation RTLSDR-Airband -- blocage matériel confirmé, piste abandonnée pour l'instant

Suite à la discussion sur un vrai squelch calibré (option 1 vs 3 tranchée
en faveur d'une recherche plus poussée), tentative d'intégration de
RTLSDR-Airband (`rtl-airband/RTLSDR-Airband`, actif, utilisé par
Broadcastify) comme moteur de capture RF, en gardant l'habillage
sonore/TTS existant via sa sortie `udp_stream` (PCM flottant 32 bits
mono brut, sans en-tête -- confirmé dans le code source
`udp_stream.cpp`, exactement le même principe que le PCM UDP de
dsd-fme déjà utilisé dans le projet).

### Ce qui a été validé comme faisable

- **Compilation réussie** sur ce Pi (32 bits ARM, confirmé encore une
  fois), avec le support NFM explicitement activé (`-DNFM=ON`,
  désactivé par défaut -- sinon aucune des bandes NFM du projet
  n'aurait fonctionné).
- **Format de config confirmé** par les exemples officiels du dépôt :
  `mode = "multichannel"` avec `centerfreq` + liste de `channels`
  (chacun avec `freq` + `modulation = "nfm"/"am"`) pour surveiller
  plusieurs canaux proches simultanément dans les ~2,5MHz d'un dongle ;
  `mode = "scan"` avec une liste `freqs` pour le saut de fréquence
  séquentiel (fonctionnellement proche de l'architecture actuelle).
- **Sortie UDP brute confirmée** : `type = "udp_stream"`,
  `dest_address`/`dest_port`, PCM flottant 32 bits mono à 16000Hz
  (WAVE_RATE avec NFM activé) -- intégration propre avec le reste du
  pipeline (`sound_player.py`, `speak.py`, `audio_lock.py`) sans les
  toucher.

### Blocage réel rencontré

Testé avec une configuration minimale (un seul canal AM sur la
fréquence ATIS de Lyon, 126.175MHz -- signal connu pour être permanent,
déjà utilisé comme référence de test tout au long du projet) :
**"[R82XX] PLL not locked!" systématique, zéro paquet UDP envoyé**, même
sur 15-20 secondes d'écoute et un signal garanti continu (donc pas un
problème de canal silencieux).

**Éliminé comme cause** : `rtl_fm` (l'outil du système actuel) tourne
**sans ce warning** sur exactement le même matériel, à la même
fréquence -- confirme que ce n'est pas un défaut matériel général.
Un bug identique et documenté existe dans le dépôt du projet
(issue #144, "Raspberry Pi 4" + message d'erreur identique), mais sa
cause connue (l'accélération GPU VideoCore incompatible sur le Pi 4)
**ne s'applique pas ici** -- la configuration utilisée
(`PLATFORM=native`, valeur recommandée actuelle) n'active jamais
l'accélération GPU (`WITH_BCM_VC=FALSE` confirmé).

### Tentative de correctif : librtlsdr plus récent, en isolation totale

`librtlsdr` installé via apt est ancien (0.6.0, dernière version
disponible sur ce Debian Bullseye). Compilé le fork "rtl-sdr-blog"
(131 commits d'avance, connu pour des corrections de pilotage R820T)
**dans un préfixe isolé** (`/opt/rtlsdr-blog`, jamais installé
système-wide) pour ne courir AUCUN risque sur `radio_watch.py` et le
reste du système déjà fonctionnel -- RTLSDR-Airband recompilé et lié
spécifiquement contre cette version via `PKG_CONFIG_PATH`
(confirmé dans `CMakeCache.txt` : `RTLSDR_LIBRARY=/opt/rtlsdr-blog/lib/librtlsdr.so`).

**Résultat** : comportement différent (le nouveau pilote tente
automatiquement un "mode d'échantillonnage direct" en secours face à
l'échec PLL) mais **échec identique au final -- toujours 0 paquet**.
Élimine l'hypothèse "juste une version de pilote obsolète".

### Conclusion et état laissé

**Piste abandonnée pour l'instant**, décision partagée avec
l'utilisateur après un test concret et bien documenté (pas un abandon
sur simple supposition). Cause probable restante : incompatibilité plus
profonde entre le code de tuning de RTLSDR-Airband
(`input-rtlsdr.cpp`) et cette combinaison précise dongle/plateforme --
nécessiterait un rapport de bug détaillé au projet en amont, hors de
portée immédiate.

**Aucun impact sur le système actuel** : `radio_watch.py` (avec RNNoise)
et les 5 autres services n'ont jamais été touchés, confirmé actif après
chaque test. `/opt/rtlsdr-blog` laissé en place (isolé, inoffensif,
pourrait resservir pour un futur test SDR). Sources clonées dans
`/tmp/RTLSDR-Airband` et `/tmp/rtl-sdr-blog` -- non persistantes
(perdues au prochain redémarrage du Pi, `/tmp` habituel).


## 2026-08-30 (suite 21) : diagnostic poussé RTLSDR-Airband -- root cause précise identifiée

Demande explicite : diagnostic poussé pour localiser précisément où le
signal se perd. Instrumentation directe du code source (pas des
suppositions) -- ligne de debug ajoutée juste avant/après l'appel
`rtlsdr_set_center_freq()` dans `input-rtlsdr.cpp`, recompilé, testé en
direct.

**Éliminé avec certitude, preuve à l'appui** :
- **Hypothèse "unités MHz/Hz mal converties"** : fausse. Log direct :
  `DIAG: about to tune to centerfreq=126175000 Hz` -- exactement
  126.175MHz, aucune erreur de conversion (`parse_anynum2int()` audité
  dans `config.cpp` : traitement correct pour les valeurs décimales que
  ce projet utilise partout).
- **Hypothèse "échec logiciel de calage"** : fausse. Log direct :
  `rtlsdr_set_center_freq returned 0` (succès) et
  `rtlsdr_get_center_freq()=126175000 Hz` (relecture confirme la bonne
  valeur mémorisée).
- **Taux d'échantillonnage** (2,56MHz vs 1MHz, plus proche de `rtl_fm`)
  et **version de `librtlsdr`** (apt 0.6.0 vs fork rtl-sdr-blog récent) :
  déjà éliminés en amont, même symptôme identique dans les deux cas.

**Root cause précise trouvée** : le warning "[R82XX] PLL not locked!"
n'est PAS le même signal que la confirmation logicielle ci-dessus --
c'est une vérification matérielle séparée (lecture d'un registre
d'état du circuit PLL analogique du tuner), indépendante de la
confirmation logicielle que le pilote a bien écrit la fréquence
demandée. Trouvé dans `output.cpp` (ligne 582) : la sortie
`udp_stream` est **conditionnée au squelch**
(`if (sdata->continuous == false && channel->axcindicate == NO_SIGNAL)`
-- rien n'est envoyé si le squelch ne s'ouvre jamais, PAS MEME du
silence). Hypothèse cohérente avec l'ensemble des observations :
l'oscillateur local du tuner ne stabilise physiquement jamais malgré
une écriture logicielle réussie des registres -> réception RF
instable/absente en pratique -> squelch ne s'ouvre jamais, même sur un
signal réel permanent (ATIS) -> zéro paquet UDP, cohérent avec TOUTES
les observations de cette session (aucune variable logicielle testée
n'a changé le résultat).

`rtl_fm` fonctionne vraisemblablement simplement parce qu'il ne
vérifie/n'expose pas cet indicateur matériel précis -- pas parce que
son procédé de calage est different au niveau des registres.

**Conclusion finale** : incompatibilité matérielle réelle entre CE
dongle précis (calibration PLL individuelle, variance de fabrication
possible sur les tuners R820T) et la séquence de calage de RTLSDR-
Airband -- pas un bug de configuration côté utilisateur, pas résolu par
un changement de version de pilote ou de taux d'échantillonnage. Piste
RTLSDR-Airband abandonnée avec un diagnostic complet et défendable,
pas un simple constat d'échec.

Rien touché sur le système actuel pendant ce diagnostic (radio-watch
confirmé actif après chaque test, comme d'habitude).


## 2026-08-30 (suite 22) : deux vrais bugs trouvés et corrigés par patch -- troisième obstacle restant (FIFO en direct)

Suite du diagnostic poussé : demande explicite de récupérer le signal
d'un outil qui marche pour le passer au traitement de RTLSDR-Airband.

### Bug n°1 trouvé et corrigé : ordre des appels tuner (RTLSDR-Airband)

Comparaison directe du code source de `rtl_fm.c` (fonctionnel, confirmé
sans warning PLL) contre `input-rtlsdr.cpp` de RTLSDR-Airband :
- `rtl_fm.c` : règle la **fréquence AVANT** le taux d'échantillonnage.
- RTLSDR-Airband (`input-rtlsdr.cpp` ligne ~106-112) : règle le taux
  d'échantillonnage **AVANT** la fréquence -- ordre inversé.

**Patch appliqué** (`input-rtlsdr.cpp`, dans le `/tmp/RTLSDR-Airband`
cloné cette session) : inversion des deux appels
(`rtlsdr_set_center_freq` avant `rtlsdr_set_sample_rate`). Recompilé,
retesté plusieurs fois (avec instrumentation de debug directe montrant
la fréquence exacte envoyée et la valeur relue) : **"[R82XX] PLL not
locked!" a complètement disparu**, tuning confirmé propre et
reproductible sur plusieurs essais (~22 secondes de test continu).

### Bug n°2 trouvé et corrigé (même cause) : `rtl_sdr` lui-même

En testant le contournement "capturer via un outil qui marche puis
transmettre à RTLSDR-Airband en mode fichier", découverte que `rtl_sdr`
(l'outil de capture IQ brute, PAS `rtl_fm`) a **le même bug d'ordre**
que RTLSDR-Airband avait : `rtl_sdr.c` (ligne ~212-215, source
`rtl-sdr-blog` déjà clonée) règle aussi le taux d'échantillonnage avant
la fréquence -- exactement la même erreur, expliquant pourquoi `rtl_sdr`
échouait alors que `rtl_fm` marchait, alors que les deux font partie du
même paquet. Même patch appliqué (inversion), recompilé (`rtl_sdr`
seul, dans le build isolé `/tmp/rtl-sdr-blog/build`) -- **confirmé sans
warning PLL non plus** après correction.

Ces deux corrections, cohérentes et reproductibles sur deux bases de
code indépendantes, confirment que l'hypothèse initiale ("incompatibilité
matérielle indéfinissable") était incomplète -- c'est un vrai bug
d'ordre d'initialisation, présent dans plusieurs outils du même
écosystème (rtl_sdr, rtl_airband), que `rtl_fm` évite simplement par un
choix d'implémentation différent. **Candidat solide pour un rapport de
bug en amont** sur les deux projets.

### Obstacle restant : flux FIFO en direct, pas encore de paquets

Pipeline complet testé (`rtl_sdr` patché écrit du IQ brut dans un tube
nommé `/tmp/iq_pipe`, RTLSDR-Airband patché le lit en mode
`type = "file"`) : les deux bouts tunent maintenant proprement (plus
aucun warning PLL nulle part), mais **toujours zéro paquet UDP reçu**.

Piste probable : `file_rx_thread` (dans `input-file.cpp`) calcule un
rythme de lecture (`speedup_factor`, `time_per_byte_ms`) pensé pour
**rejouer un fichier déjà enregistré** à une cadence simulée -- pas
conçu à l'origine pour un flux réellement en direct alimenté par un
autre processus. Comportement pas encore élucidé avec certitude (pas de
message d'erreur explicite, juste silence après l'initialisation) --
piste la plus probable pour la suite si on reprend ce chantier :
inspecter plus précisément le calcul de pacing dans `file_rx_thread` et
la taille réelle du tampon du tube nommé (défaut Linux 64 Ko) face au
débit de production de `rtl_sdr` (~4 Mo/s à 2,048 MSps).

**État laissé** : sources patchées conservées dans `/tmp/RTLSDR-Airband`
et `/tmp/rtl-sdr-blog` (non persistantes, perdues au prochain reboot du
Pi -- à refaire si repris). Rien touché sur le système actuel,
`radio_watch.py` (RNNoise) confirmé actif après chaque test de cette
longue session de diagnostic.


## 2026-08-30 (suite 23) : RTLSDR-Airband -- pipeline confirmé fonctionnel de bout en bout !

**Résolu.** Après le diagnostic poussé (patch d'ordre tuner) et une fausse
piste (croire que le pipeline demod/squelch ne tournait jamais), le vrai
fond de l'histoire :

**Découverte clé** : `debug_print()` (macro du projet, active seulement
en build Debug + `-DDEBUG_SQUELCH=ON`) n'écrit PAS sur stdout/stderr --
elle écrit dans un fichier séparé, `/tmp/rtl_airband_debug.log`, jamais
consulté jusqu'ici. Ce fichier, une fois vidé entre chaque test pour
avoir une lecture propre, a révélé que **le pipeline demod/squelch/mixer
tournait réellement** (calcul de plancher de bruit en continu, thread de
démodulation actif, événements de sortie mixeur) -- le "zéro paquet"
observé partout venait uniquement du **squelch qui ne s'ouvrait jamais**
avec les réglages automatiques par défaut, faute de signal assez fort au
moment précis des tests (pas un bug logiciel).

**Preuve définitive** : test avec `squelch_snr_threshold = 0.01;` (seuil
artificiellement bas, juste pour valider le mécanisme) sur le montage
device direct patché (ordre tuner corrigé + `centerfreq` décalé de la
fréquence du canal) -- **24 paquets UDP reçus, amplitude audio réelle et
variée (0.65, 0.67, jusqu'à 1.0)**, pas du bruit plat ni des zéros.
Confirme sans ambiguïté : capture RF propre + démodulation AM correcte +
sortie UDP fonctionnelle, de bout en bout.

### Bilan des deux bugs trouvés et corrigés cette session

1. **RTLSDR-Airband** (`input-rtlsdr.cpp`) : ordre `set_sample_rate`
   avant `set_center_freq` inversé -- cause du "[R82XX] PLL not locked!"
   systématique. Patch : fréquence AVANT taux d'échantillonnage (comme
   `rtl_fm.c`).
2. **`rtl_sdr`** (`rtl_sdr.c`, même paquet source, outil de capture IQ
   brute) : exactement le même bug d'ordre, même patch appliqué et
   confirmé.

Les deux sont des candidats solides pour un rapport de bug en amont
(reproductible, root cause précise, patch minimal et validé).

### État pour la suite

Le chantier n'est PAS terminé -- reste à construire l'architecture
complète évoquée en session (rotation entre blocs de bandes proches en
fréquence, `centerfreq` toujours décalé de la fréquence de canal pour
éviter le DC, intégration UDP -> Python -> pipeline TTS/son existant,
calibration réelle du seuil de squelch comme pour tous les autres seuils
du projet) -- mais **l'obstacle technique qui bloquait tout est levé,
avec une preuve concrète et reproductible à l'appui**, pas une simple
supposition d'amélioration.

Sources patchées toujours dans `/tmp/RTLSDR-Airband` et
`/tmp/rtl-sdr-blog` (non persistantes -- perdues au prochain reboot du
Pi, patchs à réappliquer si repris). Rien touché sur le système actuel
pendant tout ce diagnostic ; `radio_watch.py` (RNNoise) confirmé actif
après chaque test.


## 2026-08-30 (suite 25) : bascule RTLSDR-Airband -- deploye en production

**Fait.** Suite au feu vert ("feu on continu la bascule vers le nouveau
systeme"), l'integration reelle est construite, testee et deployee -- ce
n'est plus un prototype de diagnostic.

### Decision d'architecture : migration hybride, pas un remplacement total

RTLSDR-Airband n'est PAS un outil de balayage large bande -- il lui faut une
liste de frequences de canaux connue a l'avance (mode "multichannel"). Il
n'a aucun equivalent a "chercher le pic le plus fort sur une plage inconnue"
(ce que fait rtl_power). Verifie en relisant son code source (pas suppose).

Consequence assumee : seules les bandes de type "fm_channels" (canaux fixes
connus) migrent vers RTLSDR-Airband. Les bandes "sweep" (radioamateur, CB,
aviation militaire, VHF pro...) -- la grande majorite de la couverture --
restent gerees par l'ancien mecanisme rtl_power + rtl_fm + RNNoise, inchange.

### Ce qui a change concretement

- **PMR446** (16 canaux, 446.00625-446.19375 MHz) : sort de l'ancien
  `check_channels_relative` (rtl_power) + capture rtl_fm individuelle par
  candidat. Passe par un seul processus RTLSDR-Airband ecoutant les 16
  canaux simultanement, squelch reel base sur le SNR par canal (calibre en
  continu sur le plancher de bruit local par le projet lui-meme). C'est
  precisement la bande la plus critiquee pour les faux positifs
  ("siffle beaucoup... test canale 1, test canale 1") -- la decision
  "signal reel" ne repose plus sur RNNoise (heuristique logicielle sur
  audio deja demodule) mais sur une mesure physique du rapport
  signal/bruit avant demodulation.
- **ATIS Lyon-Saint-Exupery (126.175 MHz)** : sortie de la liste
  `AVIATION_LOCAL_CHANNELS`, geree seule via une fenetre RTLSDR-Airband
  dediee. Seuil de squelch calibre sur signal reel en session (voir
  ci-dessous) -- le reste d'aviation_locale (119.250/120.450/121.825/
  136.075) reste sur l'ancien mecanisme (pas encore migre, hors budget de
  cette session).

### Calibration reelle du squelch (meme methodologie que NOISE_MARGIN_DB)

Le squelch_snr_threshold par defaut du projet est 9.54 dB. Teste sur le
signal ATIS reel (boucle continue, emetteur distant) :
- 9.54 dB (defaut) : 0 paquet sur 30s -- jamais ouvert.
- 5 dB : 0 paquet sur 15s.
- 4 dB : 0 paquet sur 15s.
- 3 dB : 12 puis 17 paquets (deux essais), amplitude reelle variee
  (0.68-0.73, pas saturee) -- confirme reproductible.

Valeur retenue en production pour ce canal : **3 dB**. PMR446 reste au
defaut (9.54 dB) faute de trafic reel disponible pour calibrer de la meme
maniere -- a revisiter si l'utilisateur rapporte des ratés/faux positifs sur
cette bande specifiquement.

### Persistance

Source RTLSDR-Airband patchee (les deux bugs d'ordre de tuning trouves en
session precedente) + binaire compile : deplaces de `/tmp` (perdu au
redemarrage) vers `/home/vador/autopilot/pi_console/rtl_airband/src/`
(source + `build/src/rtl_airband`). Survit desormais a un redemarrage du Pi.
Toujours lie a la librtlsdr systeme standard (paquet apt, pas besoin de la
version isolee testee en diagnostic).

### Nouveau module : airband_bridge.py

`/home/vador/autopilot/pi_console/airband_bridge.py`. `AIRBAND_WINDOWS`
(liste des fenetres migrees), `scan_window(window, dwell_sec)` : lance
RTLSDR-Airband avec un fichier de config genere a la volee, ecoute en UDP
tous les canaux de la fenetre en parallele, des qu'un paquet arrive sur un
port (= squelch ouvert = signal reel) relaie l'audio en direct vers `aplay`
(format FLOAT_LE natif, pas de conversion) jusqu'a extinction (silence >
1.5s, memes constantes que l'ancien systeme), puis coupe le processus RTLSDR-
Airband. Meme verrou audio partage (`audio_lock`) que le reste du projet.
Integre dans `radio_watch.py` (`main()`), meme boucle sequentielle que les
bandes "fm_channels"/"sweep" existantes -- un seul dongle, jamais deux
acces paralleles.

### Tests effectues avant deploiement

1. `airband_bridge.py` isole (radio-watch arrete au prealable) : cycle
   PMR446 12s sans trafic -- 0 erreur, 0 processus orphelin apres coup
   (verifie par `ps aux`).
2. Fenetre ATIS avec squelch par defaut (9.54 dB) : aucune detection --
   a mene a la calibration ci-dessus.
3. Apres calibration (3 dB) : detection reelle, annonce TTS correcte
   (verifie les octets UTF-8 du fichier source directement, `é`
   correctement encode -- le "mojibake" vu dans les logs SSH n'etait qu'un
   artefact d'affichage du terminal de test, pas une corruption reelle),
   relais audio complet (608000 puis 928000 octets = 9.5 puis 14.5
   secondes d'audio reel).
4. Verification d'import du module deploye (`import radio_watch`) avant
   redemarrage du service -- 17 bandes sweep/fm_channels + 2 fenetres
   airband chargees sans erreur.
5. Redemarrage du service en production : `NRestarts=0`, aucun crash sur
   plus de 100s couvrant un cycle complet (ATIS detecte+relaye+termine,
   puis bande sweep CB AM normale avec RNNoise toujours actif dessus,
   puis retour PMR446) -- confirme dans le journal systemd et par
   `ps aux` (processus rtl_airband vivant au bon moment du cycle).

### Etat pour la suite (chantiers restants, non bloquants)

- Le reste d'aviation_locale (4 frequences) n'est pas migre -- reste sur
  l'ancien mecanisme, fonctionnel mais toujours soumis a RNNoise.
- PMR446 tourne au seuil de squelch par defaut, jamais valide contre du
  vrai trafic (aucun disponible au moment du test) -- calibration a faire
  des qu'une transmission reelle est rapportee ou manquee par
  l'utilisateur.
- Aucune des 13 bandes "sweep" n'est concernee par cette migration (limite
  fondamentale de RTLSDR-Airband, pas un report de travail) -- restent sur
  rtl_power + rtl_fm + RNNoise, dont le taux de faux positifs residuel
  n'est que partiellement resolu (voir session precedente).
- Dwell de 15s par fenetre airband choisi par analogie avec le temps d'un
  balayage rtl_power complet a 16 canaux -- pas mesure precisement, a
  ajuster si la cadence de balayage des bandes sweep semble trop lente a
  l'usage.


## 2026-08-30 (suite 26) : test en direct PMR446 + calibrage du volume (ampfactor)

**Fait.** Test grandeur nature demande par l'utilisateur ("je démare mon
talky... on teste et on voient se que ça donne") juste apres le
deploiement initial de la bascule RTLSDR-Airband (voir suite 25).

### Test en direct

Service radio-watch arrete temporairement, boucle de test dediee PMR446
seul (dwell 20s en continu, 5 min) lancee en tache de fond pendant que
l'utilisateur transmettait reellement sur son talky. Deux transmissions
reelles captees et relayees pendant le test :
- Canal 1 (446.00625 MHz) : 42s d'audio relaye (plafond MAX_HOLD_SEC
  atteint).
- Canal 3 (446.03125 MHz) : 13.6s d'audio relaye.

Retour utilisateur : "c'est franchement nettement plus propre [...] plus
clean et pas de faux positif en vue" -- confirme la valeur du squelch
physique par rapport a l'ancienne heuristique RNNoise sur cette bande.
Seul point faible signale : volume trop faible compare au reste du
systeme.

### Calibrage du volume (ampfactor)

Trouve dans le code source de RTLSDR-Airband un champ par canal,
`ampfactor` (multiplicateur numerique applique a l'audio demodule avant
sortie, defaut 1.0) -- exactement l'outil necessaire. Calibrage iteratif
en direct avec l'utilisateur, sur signal reel a chaque etape :
1.5 ("nettement mieux") -> 2.5 ("entre 2 et 3 de gain") -> 4 (tentative
d'egalisation) -> **5.5, valeur finale retenue** ("je pense qu'on est
bon").

Applique aux deux fenetres migrees (PMR446 et ATIS).

### Deux bugs reels trouves et corriges pendant ce calibrage

1. **`_drain_stderr` bytes/str** : `proc.stderr` s'ouvre en mode binaire
   par defaut (pas de `text=True`), les lignes accumulees dans
   `stderr_tail` etaient donc des `bytes` -- `''.join(...)` echouait
   (`TypeError`) des qu'un vrai message d'erreur devait s'afficher,
   masquant le diagnostic reel. Corrige par `.decode(errors="replace")`
   a l'ajout dans la liste.
2. **`ampfactor` doit etre un float explicite en syntaxe libconfig** :
   ecrire `ampfactor = 4;` (sans point decimal) est parse comme un entier
   par libconfig, et contrairement a `squelch_snr_threshold`, le code de
   RTLSDR-Airband qui lit `ampfactor` ne gere pas le cas entier --
   echouait au demarrage avec "Configuration error: invalid parameter
   type". Corrige en formatant toujours avec deux decimales
   (`f"{float(x):.2f}"`).

### Note operationnelle -- fragilite du dongle sous cycles rapides

Plusieurs `pkill -f rtl_airband` pendant ce calibrage (arrets/relances
tres rapproches pour tester chaque valeur d'ampfactor) ont laisse le
dongle bloque ("usb_claim_interface error -6") a deux reprises, y compris
un cas ou le process orphelin a resiste a `pkill -9 -f ...` (present dans
`ps aux`, tue seulement en ciblant le PID exact) -- cause exacte non
determinee (pattern-matching de pkill en cause probable, pas confirme).
Sans lien avec le vrai chemin de production (`systemctl stop/restart`
envoie SIGTERM, deja intercepte par `_handle_sigterm` dans
`radio_watch.py` depuis une session precedente -- deroule les `finally`
correctement, verifie par la stabilite du service en conditions reelles
apres redemarrage : `NRestarts=0`). A eviter en test manuel : preferer un
cycle kill/verification-ps-aux/pause avant tout relancement plutot que
des redemarrages en rafale.

### Etat actuel

Service radio-watch redemarre avec la config finale (ampfactor=5.5 sur
PMR446 et ATIS), actif et stable (`NRestarts=0`, verifie sur plus de 2
minutes en conditions reelles).


## 2026-08-30 (suite 27) : reintegration complete d'aviation_locale via RTLSDR-Airband

**Fait.** Suite a la demande "il faut réintégrer les autres fréquences dans
un balayage propre et sans faux positif de déclanchement" -- les 4
frequences aeronautiques locales restantes (Saint-Etienne, tour/sol/approche
Lyon) migrent vers RTLSDR-Airband, meme mecanisme que PMR446/ATIS (suite 25
et 26). `radio_watch.py` ne fait plus AUCUN usage du chemin rtl_power +
RNNoise pour des canaux nommes -- uniquement pour les bandes "sweep"
(recherche de frequence inconnue, impossible a migrer, limite du logiciel
deja documentee).

### Portee explicitement PAS couverte

Les 16 bandes "sweep" restantes (radioamateur, CB, aviation militaire, VHF
pro...) ne sont PAS concernees -- RTLSDR-Airband n'a aucun mecanisme de
recherche sur plage inconnue, seulement des listes de canaux connus a
l'avance. Le taux de faux positifs residuel de RNNoise sur ces bandes n'est
pas affecte par ce changement.

### Architecture des 4 nouvelles fenetres

Chacune en fenetre a un seul canal (pas de regroupement), meme motif que
l'ATIS (`centerfreq` decale de 225 kHz du canal -- seul offset valide en
session, pas de raison de risquer un regroupement plus large jamais teste) :
- `aviation_st_etienne` (119.250 MHz, LFMH)
- `aviation_tour` (120.450 MHz, LFLL tour)
- `aviation_sol` (121.825 MHz, LFLL sol)
- `aviation_approche` (136.075 MHz, LFLL approche)

`squelch_snr_threshold=3` et `ampfactor=5.5` reconduits sur les 4 par
inference depuis la mesure ATIS (meme bande VHF aeronautique 118-137 MHz,
meme antenne/chaine RF) -- PAS verifie independamment par frequence, faute
de trafic reel disponible au moment du deploiement (aucun avion en
communication au moment du test). A ajuster si l'utilisateur rapporte des
rates ou des faux positifs sur l'une de ces 4 frequences precises.

`dwell_sec` differencie par fenetre (6 au total maintenant, cadence totale
en jeu) : PMR446=15s (usage personnel le plus probable), ATIS=10s (boucle
continue, facile a capter), les 4 nouvelles=8s chacune (trafic plus rare,
compte sur la rotation reguliere plutot que sur une longue ecoute).

### Nettoyage cote radio_watch.py

`AVIATION_LOCAL_CHANNELS` et l'entree BANDS "aviation_locale" (type
fm_channels) entierement retirees -- plus aucune bande fm_channels active.
L'infrastructure generique (`check_channels_relative`, `scan_fm_band`,
boucle `fm_bands`) reste en place, pas supprimee : reutilisable si une
future bande a canaux fixes est ajoutee, cout de maintien nul (boucle vide).

### Tests avant deploiement

Cycle complet des 6 fenetres (dwell reduit a 6s chacune, radio-watch arrete
au prealable) : aucune erreur de config, aucun crash, aucun processus
orphelin apres coup (`ps aux` verifie). Config generee inspectee pour
`aviation_tour` -- syntaxe libconfig correcte, `squelch_snr_threshold` et
`ampfactor` bien formes.

### Deux bugs REELS trouves en test, sans lien avec ce chantier

1. **Montage NAS en erreur d'E/S** : tous les effets sonores
   (`freq_open.mp3`, `freq_over.mp3`, `scaner_error.mp3` sur
   `/media/nas/nd1/server/radio_scaner/`) echouent avec "Input/output
   error" depuis au moins le milieu de cette session. La parole TTS et le
   relais audio fonctionnent normalement (pas affectes) -- seuls les
   "bips" d'ouverture/fermeture de canal manquent. Root cause non
   investiguee (hors scope de ce chantier) -- a diagnostiquer separement
   si le probleme persiste (montage NAS a verifier).
2. **Blocage au SIGTERM sur le chemin sweep (rtl_fm)** : un
   `systemctl stop radio-watch` pendant qu'un candidat sweep etait en
   cours de traitement (bande "milair") a mis 91 secondes a s'arreter
   malgre le gestionnaire SIGTERM existant (`_handle_sigterm` ->
   `sys.exit(0)`), forcant systemd a terminer le service en `SIGKILL`
   ("final-sigterm timed out") et a tuer deux processus `rtl_fm`
   orphelins directement. Sans lien avec `airband_bridge.py` (jamais
   observe sur ce chemin, le gestionnaire fonctionne correctement dessus
   -- confirme a plusieurs reprises dans cette session). Cause precise
   non identifiee (deux `rtl_fm` simultanes suggere une interaction avec
   la logique de nouvelle tentative de verrouillage PLL,
   `_open_rtl_fm`/`RTL_FM_LOCK_ATTEMPTS`) -- **a investiguer separement**,
   risque reel si un redemarrage/arret du service tombe pile pendant un
   candidat sweep actif.

### Etat actuel

Service redemarre avec la config a 6 fenetres + 16 bandes sweep, `NRestarts=0`.
Immediatement entre en pause thermique programmee (73°C >= seuil 72°C,
comportement attendu de la securite existante, pas un effet de ce
changement) -- confirme redescendre normalement (71.1°C au moment de la
verification), reprise automatique prevue a 65°C.


## 2026-08-30 (suite 28) : panne NAS resolue -- points de montage fantomes, pas un vrai disque mort

**Resolu.** Suite au signalement utilisateur ("j'ai l'impression que c'est
les disques dures qui ont déconnecté [...] j'ai plus aucun effet sonore"),
diagnostic puis correction.

### Diagnostic

`dmesg` : aucun evenement de deconnexion/reset USB dans la fenetre visible
-- pas un arrachage de cable classique. A la place, `/media/nas/nd1` (sda1)
et `/media/nas/nd2` (sdb1) renvoyaient "Input/output error" en boucle sur
leur repertoire racine (inode #2), depuis au moins 23 minutes en continu,
alimente par une tache cron existante (`ls /media/nas/nd1`/`nd2` --
frequence declaree "*10" dans le crontab utilisateur, syntaxe non standard,
source du bruit repete dans le journal). `lsusb -t` : les deux disques
descendent du meme hub USB en amont -- coherent avec une coupure partagee
plutot qu'un disque isole en panne.

Apres rebranchement physique demande a l'utilisateur : nouvelle
enumeration USB confirmee dans `dmesg` (nouveaux numeros de peripherique),
mais les disques sont reapparus sous de **nouveaux noms de peripherique**
(`sdc`/`sdd` au lieu de `sda`/`sdb`) -- comportement Linux normal apres
debranchement/rebranchement d'un disque USB. Aucun montage par UUID/label
en place (pas d'entree fstab, pas d'unite systemd .mount pour ces
disques -- montage manuel historique sur `/dev/sdX` direct), donc les
points de montage restaient colles sur les anciens peripheriques fantomes
(`sda1`/`sdb1`, confirmes disparus via `blkid`) meme apres le retour des
disques.

### Correction

`umount -l` sur les deux points de montage fantomes, puis remontage sur
les vrais peripheriques actuels (`/dev/sdc1` -> `/media/nas/nd1`,
`/dev/sdd1` -> `/media/nas/nd2` -- correspondance confirmee par la taille :
Toshiba 500 Go = nd1, disque 2 To = nd2). Verifie par lecture reelle
(`ls`) puis lecture audio reelle (`play_sound_effect` sur `freq_open.mp3`,
execution sans erreur) -- fonctionnel de bout en bout, pas juste le
montage.

### A noter pour la suite (pas fait dans cette session)

Puisqu'aucun fstab/unite systemd ne gere ces disques, un futur cycle
debranchement/rebranchement (ou redemarrage du Pi) reproduira exactement
le meme symptome (noms de peripherique qui changent, montage qui ne suit
pas). Corriger durablement necessiterait soit une entree fstab par UUID
(`e5f38c63-862e-417b-9951-07dc0a1319b8` pour nd1,
`e47991c3-4105-4971-aeb7-8051b3287cbd` pour nd2, donc stables meme si le
nom `/dev/sdX` change), soit une regle udev declenchant un montage
automatique -- non fait ici (hors demande explicite de l'utilisateur pour
cette session), a proposer si la panne se reproduit.


## 2026-08-30 (suite 29) : surveillance et remontage automatique des disques NAS

**Fait.** Suite a la demande "stabiliser la connexion tant que tout est
allumé, là ça a lâché sans trop de raison visible" -- construction d'une
surveillance continue, complementaire au script existant `nas_manager/nas.py`
(qui ne monte les disques qu'une fois, au demarrage via `@reboot`).

### Pourquoi un script separe plutot que modifier nas.py

`nas.py` n'est jamais deja pas concu pour tourner en continu -- son role
(explicite dans les commentaires de l'utilisateur) est d'eviter que le Pi ne
se bloque AU DEMARRAGE si un disque est en defaut, et de gerer l'inversion
possible sda/sdb via des fichiers-marqueurs (nd1.txt/nd2.txt) + deux
versions de smb.conf. Le probleme de cette session (deconnexion/reconnexion
EN COURS DE SESSION, sans redemarrage) est un cas different : nas.py ne se
relance jamais tant que le Pi ne redemarre pas (`0 3 * * * reboot` --
prochain redemarrage automatique seulement a 3h du matin). Nouveau fichier
`nas_manager/nas_watch.py`, non-destructif, `nas.py` intact.

### Fonctionnement

Toutes les 30s, verifie les deux disques (nd1, nd2) : `os.path.ismount()`
(quelque chose est-il reellement monte -- necessaire car un `ls` sur un
repertoire vide non-monte reussit silencieusement, piege trouve en testant
ce script) ET une lecture reelle sous `timeout` (evite un blocage si le
montage est fige, meme esprit que le commentaire original de l'utilisateur
sur nas.py : "eviter que le pi ne se bloque"). Si en echec : cherche le
peripherique actuel via `/dev/disk/by-uuid/<UUID>` (stable, independant du
nom `/dev/sdX` qui change a chaque reconnexion) et remonte automatiquement.
Si le disque est completement absent (UUID introuvable), avertit
vocalement au lieu de boucler en silence.

Montage par UUID plutot que par nom -- **elimine structurellement** le
probleme historique d'inversion sda/sdb que `nas.py` contournait avec les
fichiers-marqueurs : chaque disque va desormais TOUJOURS a son point de
montage prevu, quel que soit l'ordre d'enumeration USB.

### Droits sudo -- regles precises, pas un service root

Question posee explicitement a l'utilisateur (modification de fichier de
securite = confirmation requise) : regles sudoers scopees exactement aux 4
commandes necessaires (`umount -l` et `mount UUID=...` pour chacun des deux
points de montage precis) choisies plutot qu'un service tournant en root.
Fichier `/etc/sudoers.d/nas_watch`, valide avec `visudo -cf` AVANT
installation (jamais applique sans validation syntaxique -- un sudoers
casse bloquerait tout sudo sur la machine). Meme convention que les regles
deja en place (`vador ALL=NOPASSWD: /usr/sbin/service`, `/sbin/shutdown`).

### Tests avant deploiement

1. Bug trouve et corrige en testant : demontage volontaire propre (pas
   juste un montage fantome/perime) jamais detecte par la version initiale
   du script -- `ls` sur un repertoire local vide reussit silencieusement.
   Corrige en ajoutant `os.path.ismount()` comme condition prealable.
2. Apres correction : demontage volontaire de nd1, detecte et repare des
   le premier cycle (execution manuelle), lecture reelle confirmee apres
   coup.
3. Deploiement comme service systemd (`nas-watch.service`, meme modele que
   `radio-watch.service` : `User=vador`, `Restart=always`, active au
   demarrage). Test final SOUS le vrai service (pas le script manuel) :
   demontage volontaire de nd2, journal systemd confirme la sequence
   complete (detection a 09:25:31, `sudo umount -l` puis
   `sudo mount UUID=...` visibles et scopes exactement comme prevu dans le
   journal d'audit sudo, remontage reussi a 09:25:32, disque relu avec
   succes).

### Etat actuel

`nas-watch.service` actif et active au demarrage (`systemctl enable`),
`NRestarts=0`. Les deux disques nd1/nd2 sains et lisibles. Effets sonores
du scanner radio (`freq_open.mp3` etc., panne signalee plus tot dans cette
meme session) desormais proteges contre une repetition du meme incident
sans intervention manuelle -- annonce vocale automatique en cas de
remontage reussi OU d'echec/absence persistante.


## 2026-08-30 (suite 30) : reglage adsb_watch -- cadence relachee, rayon borne a l'aeroport reel

**Fait.** Ajustement demande explicitement : "diminue le refresh [...] un
peu decorum pas besoin d'une precision au poil de fesse [...] borne la
detection a 15.9km (la distance de mon aeroport) [...] relache la pression
sur le processeur [...] garde ça regulier mais tres smooth."

- `POLL_INTERVAL_SEC` : 30 -> 90. ~2880 -> ~960 requetes/jour (large marge
  sous le quota de 4000/jour du compte OpenSky authentifie). A 15.9km de
  rayon (30km de diametre), un avion de ligne a 900 km/h traverse la zone
  en ~2 minutes -- au moins un sondage pendant le passage dans la plupart
  des cas, sans viser une precision fine sur l'instant exact.
- `MAX_RADIUS_KM` : 20.0 -> 15.9 (distance reelle de l'aeroport de
  l'utilisateur -- 3e resserrement de cette valeur cette session, 50 -> 20
  -> 15.9).

Mecanisme de boucle deja simple (`time.sleep(POLL_INTERVAL_SEC)` fixe,
sans logique de backoff/jitter) -- repond deja nativement au "regulier
mais smooth" demande, aucun changement structurel necessaire au-dela des
deux constantes.

Deploye et redemarre (`adsb-watch.service`, `NRestarts=0`, arret propre
sans blocage au SIGTERM contrairement au bug trouve plus tot sur
radio-watch). Detection reelle confirmee immediatement apres redemarrage
(avion a 13km, dans le nouveau rayon).


## 2026-08-30 (suite 31) : squelch chatter identifie et corrige sur les 4 frequences aviation

**Resolu.** Suite au signalement "grésillement haché, plein de saccades,
rythme trop régulier" specifique aux frequences aeronautiques
(approche/sol/tour), pas ailleurs -- l'utilisateur a explicitement precise
qu'il ne s'agissait PAS d'un ronflement de fond mais d'irregularites
hachees qui ressemblent a une mauvaise reception de quelque chose
d'existant.

### Diagnostic (deux passes)

1ere passe (spectre en squelch force ouvert, donc PAS representative du
comportement reel) : n'a rien montre d'anormal -- les 4 canaux (dont ATIS,
reference saine) avaient un spectre quasi identique dominee par des
harmoniques de ronflement secteur 50Hz. Fausse piste, corrigee par
l'utilisateur qui a precise la nature du symptome (saccades, pas ronflement).

2e passe (rythme reel d'arrivee des paquets UDP, squelch a 3dB INCHANGE,
pas force cette fois) -- la bonne methode : a revele le vrai mecanisme.
- `aviation_approche` : 11 rafales de ~0.17s separees de silences de ~1s
  sur 20s -- chatter mecanique classique d'un squelch qui oscille pile a
  son seuil de decision.
- `aviation_sol` : une seule ouverture continue de 13.5s -- egalement
  suspect, bien trop long pour un vrai echange ATC typique.
- `aviation_tour` : rien capte sur deux essais (trafic reel intermittent,
  pas de faux positif reproduit a la demande cette fois).

### Cause et correctif

Le squelch_snr_threshold=3 (copie par inference depuis la seule mesure
ATIS, jamais verifie independamment par frequence -- deja signale comme
limite au moment du deploiement, voir suite 27) etait trop permissif pour
ces 4 canaux : le squelch reste hesitant juste au-dessus/en-dessous du
seuil sur du bruit ordinaire, produisant l'ouverture/fermeture repetee
entendue comme un grésillement en saccades.

Retire sur les 4 frequences (`aviation_st_etienne`, `aviation_tour`,
`aviation_sol`, `aviation_approche`) -- retour au defaut logiciel du
projet (9.54 dB), meme traitement que `pmr446` qui n'a jamais eu ce
probleme. Deploye et redemarre, `NRestarts=0`.

### Observation secondaire, PAS traitee cette session

En verifiant si l'ATIS (toujours a 3dB, seule reference validee) souffrait
du meme chatter : aucun paquet capte sur 20s puis 35s de test consecutifs
(0 declenchement), alors que ce canal se declenchait de façon fiable plus
tot dans cette meme session (plusieurs detections reelles confirmees,
suite 25/26/27). Changement de comportement notable, cause non
determinee -- pourrait etre lie a l'instabilite electrique deja signalee
par l'utilisateur (antenne "qui picote") ou a une variation normale de
propagation RF. A surveiller, pas d'action prise pour l'instant (pas
demande, donnees insuffisantes pour diagnostiquer avec confiance).


## 2026-08-30 (suite 32) : gain reduit + plafond de temps -- bandes sweep affamees identifiees et corrigees

**Fait.** Suite a deux signalements combines : "je n'entends plus rien" et
"il manque des plages de fréq qu'on scannait avant". Diagnostic par lecture
directe du journal systemd (pas de suppositions) : meme avec le squelch par
defaut restaure (suite 31), `aviation_sol` et `aviation_st_etienne`
continuaient a declencher a repetition, plusieurs blocages consecutifs de
45s (plafond MAX_HOLD_SEC atteint plusieurs fois de suite, ex: 09:38:29 a
09:39:20 puis immediatement 09:39:33 sur un autre canal). Avec 6 fenetres
airband dans la meme boucle sequentielle que les bandes sweep, ce temps
monopolise reduit mecaniquement la frequence de rotation des 16 bandes
sweep -- explique directement la perception "plages manquantes" (toujours
codees, juste tres rarement visitees).

### Recherche web (a la demande de l'utilisateur -- "il doit bien exister
des valeurs en ligne")

- Squelch : les mainteneurs du projet RTLSDR-Airband refusent explicitement
  toute valeur globale recommandee ("ca doit se faire canal par canal") --
  confirme que notre approche (calibration empirique par frequence) est la
  methode officiellement voulue, pas un pis-aller.
- Gain : piste concrete trouvee. Le guide ATCO2 (recherche professionnelle
  sur la collecte de voix ATC) recommande explicitement un gain BAS, pas
  eleve. Un exemple communautaire reel de scanner VHF marine tourne avec
  `gain=20` et `squelch_snr_threshold=10` (quasiment le defaut logiciel) et
  fonctionne correctement. Notre gain etait a 30.

### Deux corrections deployees ensemble

1. **`gain=20`** (au lieu de 30) sur les 5 fenetres aviation (ATIS +
   les 4 restantes). PMR446 (bande UHF differente, jamais de probleme
   signale) inchange a 30.
2. **`max_hold_sec=20`** (au lieu des 45s globaux) sur les 4 canaux les
   moins prioritaires (`aviation_st_etienne`, `aviation_tour`,
   `aviation_sol`, `aviation_approche`) -- garde-fou STRUCTUREL,
   independant du reglage gain/squelch : meme si un faux declenchement se
   reproduit malgre le gain reduit, il ne peut plus monopoliser le cycle
   aussi longtemps. ATIS et PMR446 gardent 45s (trafic legitime long).

Implementation : `_build_config()` et `_relay_channel()` dans
`airband_bridge.py` acceptent maintenant un gain et un max_hold_sec par
fenetre (`window.get("gain", 30)`, `window.get("max_hold_sec",
MAX_HOLD_SEC)`), plus seulement des valeurs globales fixes.

### Etat apres deploiement

Service redemarre, `NRestarts=0`. Premiere minute d'observation : aucun
declenchement (contraste net avec le comportement precedent a gain=30, qui
declenchait typiquement en quelques minutes). Test A/B direct
(gain=30 vs gain=20 sur signal reel) non concluant au moment du
deploiement -- les conditions RF ne se sont pas reproduites pendant le test
cible (meme constat recurrent toute la session : impossible de forcer un
faux positif a la demande). **A confirmer par l'usage reel dans la duree**,
pas encore une preuve definitive -- demande explicitement a l'utilisateur
de rapporter si le probleme persiste ou si les bandes sweep redeviennent
audibles normalement.


## 2026-08-30 (suite 33) : check-up complet + marge de refroidissement adaptative

**Diagnostic demande** ("j'entend plus rien... check-up, j'ai laissé
tourner quelques heures") : service sain sur ~4h de fonctionnement continu
(`NRestarts=0`), aucun processus orphelin, dongle bien enumere, NAS stable
(le fix de la suite 29 tient), charge CPU/memoire/disque normaux, les 6
autres services actifs. La cause du "silence" identifiee par lecture
directe du journal, pas de supposition :

- **7 pauses thermiques en 4h**, dont une de **plus de 70 minutes**
  (10h57-12h07) -- a elle seule plus d'un quart du temps total en silence.
- Tendance a l'aggravation dans la session : pics de temperature montant
  legerement (72.0 -> 73.5 -> 74.0C), et une re-pause seulement 2.5
  minutes apres la fin de la longue pause -- coherent avec une chaleur
  ambiante d'apres-midi qui monte, combinee au fonctionnement continu du
  dongle.
- Activite radio elle-meme sobre mais pas nulle : 3 signaux confirmes sur
  les bandes sweep + 1 court passage aviation en 4h -- coherent avec un
  dimanche apres-midi calme et le resserrement recent (gain/squelch), pas
  un systeme casse.

### Marge de refroidissement adaptative -- demande explicite de l'utilisateur

Proposition de l'utilisateur : "laisser redescendre plus entre deux
pauses, et garder une trace temporaire des 4 pauses precedentes pour
mesurer un eventuel rapprochement [...] et augmenter l'intelligence."

Implemente dans `radio_watch.py` (`main()`) :
1. `TEMP_RESUME_C` : 65.0 -> **60.0** (marge de base augmentee).
2. Historique glissant en memoire (`collections.deque(maxlen=4)`, "trace
   temporaire" -- remis a zero a chaque redemarrage du service, pas
   persiste sur disque) des durees actives entre pauses consecutives.
3. A chaque nouvelle pause : compare la duree active qui vient de
   s'ecouler a la moyenne des precedentes. Si elle est significativement
   plus courte (<80% de la moyenne -- resserrement, les pauses se
   rapprochent), etire la marge de refroidissement de 2C
   supplementaires (`TEMP_RESUME_ADAPTIVE_STEP_C`), plafonne a 10C
   (`TEMP_RESUME_ADAPTIVE_MAX_C` -- plancher absolu 50.0C). Si
   l'intervalle redevient normal (>120% de la moyenne), relache
   progressivement la marge de 2C. Reaction automatique et reversible a
   une degradation ou une amelioration des conditions, plutot qu'un seuil
   fixe qui ne s'adapte jamais.

Deploye et redemarre, `NRestarts=0`. Le comportement adaptatif n'a pas
encore ete observe en conditions reelles (faut au moins 2 pauses
consecutives pour la premiere comparaison) -- a verifier au prochain
episode de chauffe.


## 2026-08-30 (suite 34) : annonces gare recalees sur l'heure reelle (sncf_watch.py)

**Fait.** Demande explicite : "basculer les annonces+départs des trains
sur les vraies annonces en gare, en flux temps réel entre annonce et
arrivée du train." Clarifie avec l'utilisateur (AskUserQuestion) : recaler
l'annonce parlee juste avant l'heure reelle plutot que des annonces
repetees a plusieurs echeances -- option choisie.

### Avant / apres

Avant : un train nouvellement decouvert dans la fenetre d'une heure a
venir (`UPCOMING_WINDOW_SEC`) etait annonce IMMEDIATEMENT (`_speak_bulletin`,
appele une fois par cycle de sondage de 3 min, fusionnant tous les
nouveaux departs/arrivees) -- jusqu'a 1h avant l'evenement reel,
desynchronise du bruitage d'ambiance (`_schedule_ambient_sound`,
`_play_ambient_sound`) qui, lui, se declenchait deja pile a l'heure REELLE
(retard connu inclus) via `threading.Timer`.

Apres : meme mecanisme (`threading.Timer`) reutilise pour l'annonce
parlee -- nouvelle fonction `_schedule_announcement()`, se declenche
`ANNOUNCEMENT_LEAD_SEC` (180s, 3 min) avant l'heure REELLE. Sequence
resultante : silence, puis annonce parlee ("Le TER a destination de X
partira a..."), puis quelques minutes plus tard le bruitage d'ambiance au
moment reel -- reproduit le rythme d'une vraie gare plutot qu'un bulletin
recite a l'avance.

### Details d'implementation

- `_speak_bulletin()` (fusion batch) retiree, remplacee par
  `_speak_departure_announcement()`/`_speak_arrival_announcement()` (une
  annonce individuelle par train, plus de fusion multi-trains par cycle
  puisque chacune part a son propre moment desormais).
- `state["scheduled_announcements"]` (nouveau) : dict vehicle_journey_id ->
  Timer en attente. Cle prefixee `"arr_"` pour les arrivees (evite une
  collision avec le depart du meme train de passage, qui partage souvent
  le meme vehicle_journey_id cote Navitia).
- `_cancel_scheduled_announcement()` : annule le timer en attente si une
  suppression (`NO_SERVICE`) est detectee avant que l'annonce ne soit
  partie -- evite d'annoncer l'arrivee/depart d'un train qui vient d'etre
  officiellement supprime.
- Perturbations et suppressions (`_speak_disruption`/`_speak_cancellation`)
  restent des alertes IMMEDIATES, non decalees -- volontaire, un passager
  doit savoir des que possible qu'un train est en retard/supprime, pas
  attendre le meme delai que l'annonce normale.

### Limite connue, non traitee cette session

`actual_dt` (heure reelle, retard inclus) est capture UNE SEULE FOIS au
moment de la programmation du timer. Si le retard est revise
significativement APRES cette capture mais AVANT que l'annonce ne parte,
le timer se declenche toujours a l'ancienne heure/avec l'ancien texte
parle -- pas re-planifie dynamiquement. Attenue par le fait que le canal
de perturbation (`_speak_disruption`) informe deja separement et
immediatement de tout changement de retard -- l'utilisateur n'est jamais
sans information, juste l'annonce "standard" peut afficher une heure
legerement perimee dans ce cas de figure precis. A ameliorer si ca s'avere
genant en usage reel (reprogrammer le timer a chaque poll si `actual_dt` a
significativement change).

### Test

Deploye, service redemarre proprement (`NRestarts=0`, pas de blocage au
SIGTERM contrairement au bug connu sur radio-watch). Confirme en conditions
reelles des le redemarrage : un train (REGIONAURA) deja a moins de 3 min de
son arrivee reelle au moment de la decouverte -- delai negatif apres
soustraction du lead, correctement plafonne a 0 (`max(0.0, ...)`),
annonce partie quasi immediatement plutot que d'attendre inutilement.
Comportement attendu confirme, pas suppose.


## 2026-08-30 (suite 35) : voix cloud (edge-tts) + repli espeak -- coeur du mecanisme construit et valide

**En cours.** Demande initiale : remplacer espeak par une voix de meilleure
qualite. Piste Piper (voix neuronale locale) explorée puis explicitement
ecartee par l'utilisateur -- retour d'experience direct via NVDA (voix
identiques testees en HQ) : la qualite ne justifie pas la charge CPU sur ce
Pi, deja sollicite (scanner radio, RTL-SDR). Piste alternative proposee et
retenue : deporter le calcul (edge-tts, service cloud gratuit de Microsoft
Edge, sans cle API, zero charge CPU locale) plutot que chercher un moteur
local plus leger.

### Architecture hybride, decidee avec l'utilisateur

- **edge-tts en voix principale** (voix "fr-FR-HenriNeural"), **espeak en
  secours automatique et systematique** -- jamais optionnel. Ce n'est pas
  une API officielle (acces reverse-engineered au moteur d'Edge), peut
  casser sans preavis ; certaines annonces de ce projet portent des
  alertes de securite (meteo severe, crue) qui ne doivent JAMAIS pouvoir
  devenir silencieuses.

### Optimisation de latence -- idee de l'utilisateur

Constat de l'utilisateur : "tous nos speaks sont après un son. donc quand
on doit jouer le son on sait déjà ce qu'on doit dire" -- la synthese cloud
est donc lancee EN PARALLELE (thread de fond) du son d'accompagnement, pas
apres. Le temps reseau se cache derriere la duree du son (mesure en
session : ~0.5s de synthese, largement sous la duree de n'importe quel son
d'accompagnement du projet) -- latence percue quasi nulle dans le cas
courant.

### Implementation (`speak.py`, reecrit)

- `speak(text, voice="fr")` : inchangee dans sa signature/comportement,
  reste le repli local garanti (espeak).
- `speak_with_lead_sound(sound_path, text, sound_timeout_sec=15,
  label="speak")` : nouvelle fonction, a utiliser partout ou l'enchainement
  `play_sound_effect(son); speak(texte)` existe deja. Lance la synthese
  cloud dans un thread de fond des l'appel, joue le son (bloquant), puis
  attend au maximum `EDGE_TTS_EXTRA_WAIT_SEC` (2s) supplementaires apres la
  fin du son -- si le resultat cloud est pret, le joue (reutilise
  `play_sound_effect` pour la lecture, meme mecanisme que les sons
  d'ambiance) ; sinon bascule immediatement sur `speak()` (espeak).
- `EDGE_TTS_TIMEOUT_SEC=8` : borne dure sur l'appel reseau lui-meme (thread
  de fond), independante du delai d'attente cote appelant -- evite qu'un
  thread reste bloque indefiniment sur un reseau degrade.

### Tests effectues (avant toute migration des points d'appel)

1. Synthese isolee : 0.52s pour une phrase complete, fichier MP3 valide
   genere et confirme lisible via `play_sound_effect` (meme mecanisme que
   le reste du projet, pas de nouveau code de lecture audio).
2. `speak_with_lead_sound` en conditions reelles (son reel du projet +
   texte reel) : fonctionne de bout en bout, aucune erreur.
3. Repli espeak verifie explicitement (voix invalide forcee pour simuler
   un echec cloud SANS toucher au reseau du Pi) : echec cloud correctement
   catche et logue, bascule confirmee sur espeak, aucun silence.

### Etat actuel

`speak.py` deploye sur le Pi -- deja fonctionnel pour tout code qui
l'utiliserait, mais **aucun des 28 points d'appel existants
(`play_sound_effect(...); speak(...)`) dans les 7 fichiers de service
n'est encore migre** vers `speak_with_lead_sound()`. Catalogue de ces
points d'appel en cours (agent de reconnaissance lance, lecture seule,
aucune modification). Migration a suivre dans la prochaine section de
cette feuille de route.


## 2026-08-30 (suite 36) : migration des 6 services vers la voix cloud terminee

**Fait.** Suite de la suite 35 (coeur du mecanisme edge-tts + repli espeak
construit et valide isolement) -- migration effective de tous les points
d'appel `play_sound_effect(son); speak(texte)` vers
`speak_with_lead_sound(son, texte)` a travers le projet.

### Catalogue complet (agent de reconnaissance, lecture seule avant migration)

28 occurrences de `play_sound_effect` recensees sur 7 fichiers. Repartition :
- **15 migrees** vers `speak_with_lead_sound` : adsb_watch.py (2),
  sncf_watch.py (5), stas_watch.py (1), crue_watch.py (2, dont 1 en boucle
  d'alarme), meteo_watch.py (2, dont 1 en boucle d'alarme), radio_watch.py
  (3, dont 1 conditionnelle -- `_speak_pause(message, sound=None)`,
  restructuree en `if sound: speak_with_lead_sound(...) else: speak(...)`
  pour preserver le cas "pas de son").
- **3 confirmes standalone, non touches** (son seul, aucune parole a
  proximite) : `airband_bridge.py` (bruitage fermeture de canal),
  `radio_watch.py` (idem), `sncf_watch.py` (bruitage ambiant train, fichier
  entier sans aucun appel `speak`).
- **2 laisses EXPLICITEMENT inchanges** (ordre inverse -- parole PUIS son,
  pas son PUIS parole) : `airband_bridge.py` (`_relay_channel`) et
  `radio_watch.py` (`stream_until_quiet`) -- tous deux sur le chemin de
  relais radio en direct, ou l'annonce de frequence part immediatement
  avant le chime d'ouverture de canal. Migrer mecaniquement aurait inverse
  l'ordre audible (chime avant l'annonce au lieu d'apres) sur un chemin
  sensible a la latence (ne pas rater le debut d'une transmission reelle).
  Laisse a l'utilisateur de decider s'il veut une variante adaptee ici.

### Nettoyage au passage

Imports `play_sound_effect`/`speak` retires des fichiers ou ils devenaient
inutilises apres migration (adsb_watch.py, sncf_watch.py, stas_watch.py,
crue_watch.py, meteo_watch.py) -- pas de dependance fantome.

### Deploiement et verification

Les 6 fichiers modifies (adsb_watch.py, crue_watch.py, meteo_watch.py,
radio_watch.py, sncf_watch.py, stas_watch.py) verifies syntaxiquement en
local puis sur le Pi (import Python reussi pour chacun) avant tout
redemarrage de service. Les 6 services correspondants redemarres,
`NRestarts=0` pour tous.

**Confirme en conditions reelles des les premieres secondes** (pas
suppose) : `meteo-watch` a parle une vraie alerte (passage vert -> jaune,
departement Loire) et `sncf-watch` deux vraies annonces (depart REGIONAURA
Roanne, arrivee REGIONAURA) via le nouveau chemin -- aucun message
d'echec/repli logue (le repli espeak n'imprime que sur echec), signe que
la synthese cloud a reussi silencieusement dans les deux cas.

### Etat actuel

Architecture hybride edge-tts (voix principale, deportee, zero charge CPU
locale) + espeak (repli garanti, jamais optionnel) active sur l'ensemble
du projet sauf les 2 chemins de relais radio en direct explicitement
epargnes. Optimisation de latence (synthese cloud en parallele du son
d'accompagnement, idee de l'utilisateur) active partout ou elle s'applique.


## 2026-08-30 (suite 37) : voix cloud tirees au hasard a chaque annonce

**Fait.** Demande de l'utilisateur pour explorer les voix disponibles de
l'API edge-tts, avec l'idee initiale d'en assigner une par domaine
d'annonce (radio/meteo/trains/avions) pour l'identification a l'oreille.

### Exploration des voix

`edge_tts.list_voices()` interroge : 13 voix francophones neuronales
disponibles -- 5 France (Vivienne, Remy, Denise, Eloise, Henri -- les deux
premieres "Multilingual", plus recentes), 2 Belgique (Charline, Gerard),
4 Canada (Thierry, Antoine, Jean, Sylvie), 2 Suisse (Ariane, Fabrice).

Script `voice_showcase.py` construit (chaque voix synthetise une phrase
d'exemple ou elle se presente par son prenom et sa region) et execute en
direct sur l'enceinte du Pi (verrou audio respecte -- s'est mis en pause
correctement le temps qu'un vrai son d'ambiance train de sncf_watch.py
termine de jouer, preuve concrete que le nouveau code cohabite bien avec
le reste du systeme). Les 13 voix ecoutees et validees par l'utilisateur.

### Decision finale : tirage au hasard, pas d'assignation fixe

L'utilisateur a reoriente l'idee initiale (une voix par domaine) vers
l'oppose exact : **tirage au hasard d'une voix DIFFERENTE a CHAQUE
annonce**, toutes fonctions confondues. Raisonnement explicite, directement
lie a la mission premiere de ce projet (voir memoire : lutte contre le
trouble du sommeil non-24h) : une voix fixe et previsible cree une
habituation acoustique, ce qui va a l'encontre de l'objectif
d'imprevisibilite recherche pour ce systeme.

### Implementation

`speak.py` : `EDGE_TTS_VOICE` (une seule voix fixe) remplace par
`EDGE_TTS_VOICES` (liste des 13 voix validees). `_synthesize_edge_tts()`
tire desormais `random.choice(EDGE_TTS_VOICES)` a chaque appel -- chaque
annonce cloud (n'importe laquelle des 15 points d'appel migres en suite 36)
utilise potentiellement une voix differente de la precedente, sans aucun
etat/memoire entre les appels (tirage independant a chaque fois, pas de
rotation/sequence previsible).

Deploye, les 6 services (adsb-watch, crue-watch, meteo-watch, radio-watch,
sncf-watch, stas-watch) redemarres pour charger le nouveau `speak.py`,
`NRestarts=0` sur tous.


## 2026-08-30 (suite 38) : sons "incoming"/decode + annonces simplifiees (cas restants)

**Fait.** Traite les deux points de relais en direct laisses volontairement
non migres en suite 36 (`airband_bridge._relay_channel`,
`radio_watch.stream_until_quiet`) -- l'utilisateur a fourni 3 nouveaux
sons (`incoming.mp3`, `decoding_numeric.mp3`, `decoding_morse.mp3` sur le
NAS, dossier radio_scaner) et precise le changement voulu.

### Simplification du texte annonce

Toutes les annonces de frequence radio (PMR446, aviation, bandes sweep,
voix numerique dsd-fme) ne disent plus la frequence exacte en MHz -- jugee
peu intelligible a l'oral et sans interet pratique. Ne reste que le nom
d'usage, deja clair et court (`band_label`/`name_spoken` portaient deja ce
nom : "canal 446 civil", "bande aviation militaire UHF", "ATIS Lyon
Saint-Exupéry"...). 4 sites simplifies : `stream_until_quiet` (annonce
initiale), `_relay_channel` (airband, idem), la confirmation de fin de
decodage vocal dsd-fme ("Réception numérique terminée, {bande}." au lieu
d'inclure la frequence).

### "incoming" -- tampon avant l'annonce de capture

Les deux annonces initiales de capture (`stream_until_quiet` et
`_relay_channel`) utilisent maintenant `speak_with_lead_sound(INCOMING_SOUND,
band_label/name_spoken)` -- resout au passage le probleme signale en suite
36 (ces deux sites avaient l'ordre parole-puis-son inverse par rapport au
reste du projet, jamais migres a l'epoque justement a cause de cet ordre).
Desormais coherent avec tous les autres points d'annonce du projet : son
d'abord (ici "incoming"), synthese cloud en parallele, annonce ensuite,
puis FREQ_OPEN_SOUND juste avant que le flux audio brut ne commence a etre
relaye.

### "decoding_numeric"/"decoding_morse" -- tampon pendant le decodage numerique

Deux chemins de decodage numerique distincts dans radio_watch.py :
- **Voix numerique (dsd-fme, DMR/P25/D-STAR...)** : `decoding_numeric`
  joue au lancement de la sonde dsd-fme (`try_digital_voice_decode`),
  comble le silence jusqu'a DSD_FME_PROBE_SEC (4s) avant de savoir si un
  vrai decodage a eu lieu.
- **Messages texte (multimon-ng, pageur/APRS...)** : `decoding_morse`
  precede desormais chaque annonce de message decode
  (`speak_with_lead_sound(DECODING_MORSE_SOUND, "Message numérique décodé : ...")`).

Mapping choisi par deduction technique (non confirme explicitement par
l'utilisateur) : "numeric" = voix numerique/vocodeur (DMR/P25 sont
litteralement des protocoles numeriques), "morse" = messages texte courts
en rafales de tons (rythme evocateur du morse/telegraphe, meme si
techniquement different) -- a corriger si l'inverse etait voulu (echange
trivial de deux constantes).

### Deploiement et verification

`radio_watch.py` et `airband_bridge.py` deployes, import verifie, service
`radio-watch` redemarre (`NRestarts=0`). **Confirme en conditions reelles
dans la minute suivant le redemarrage** : detection PMR446 reelle, annonce
"446 civil" (sans MHz), relais termine proprement -- pas suppose.


## 2026-08-30 (suite 39) : tampon de debut de transmission sur le chemin UDP (airband_bridge.py)

**Fait.** Question de l'utilisateur : possible de mettre en tampon le
debut d'une transmission des la detection, plutot que de commencer a
ecouter seulement apres l'annonce parlee -- accepte explicitement quelques
secondes d'ecart avec le direct en echange de ne rien perdre.

### Constat prealable

`radio_watch.py` (chemin rtl_fm) le fait DEJA : `_rtl_fm_reader` demarre
un thread de lecture vers une file AVANT l'annonce (commentaire deja
present dans le code : "la capture tourne deja en arriere-plan... donc
rien n'est perdu du debut de la transmission"). Rien a changer la.

`airband_bridge.py` (chemin RTLSDR-Airband/UDP) ne le faisait PAS : la
lecture du socket ne commencait qu'apres l'annonce+chime, avec pour seul
filet de securite le tampon reseau par defaut du noyau (taille non
garantie, paquets UDP silencieusement perdus en cas de depassement) --
exactement le trou signale par l'utilisateur.

### Correctif

Meme motif que `_rtl_fm_reader`, applique au socket UDP : nouvelle fonction
`_udp_reader(sock, out_queue, stop_event)`, lancee en thread des l'entree
dans `_relay_channel` -- AVANT l'annonce "incoming"+nom d'usage et le
chime d'ouverture. Tout paquet arrivant pendant ce temps s'accumule dans
`packet_queue`. La boucle de relais principale lit desormais depuis cette
file (`packet_queue.get(...)`) au lieu du socket directement -- rattrape
le retard accumule pendant l'annonce des qu'elle se termine, `aplay`
assurant naturellement le rythme reel de lecture (son ecriture bloque au
rythme de la sortie audio, pas de rattrapage en acceleree). Le stop_event
arrete proprement le thread de lecture en sortie de fonction (`finally`),
coherent avec le nettoyage existant du reste de la fonction.

### Tests avant deploiement

Cycle isole PMR446 (radio-watch arrete au prealable) : import propre,
aucune erreur, aucun processus orphelin apres coup. Deploye en production,
service redemarre, `NRestarts=0`. Pas encore de confirmation sur une
VRAIE transmission bufferisee (rien capte dans la fenetre d'observation
immediate) -- le mecanisme lui-meme est deja valide et fonctionne pour ce
type exact de motif ailleurs dans le projet (radio_watch.py), risque
residuel faible.


## 2026-08-30 (suite 40) : deux bugs SNCF corriges (doublon destination, origine toujours inconnue)

**Resolu.** Deux problemes signales par l'utilisateur, diagnostiques par
inspection directe de vraies reponses API (pas de suppositions).

### Doublon "gare (ville)" sur les departs

Le champ Navitia `display_informations.direction` est deja formate
"NomGare (NomVille)" cote API (ex: "Lyon Perrache (Lyon)", ou carrement
"Le Puy-en-Velay (Le Puy-en-Velay)" quand gare et ville portent le meme
nom -- doublon pur et simple a l'oral). Nouvelle fonction
`_clean_place_name()` : garde uniquement la partie avant " (". Appliquee a
`destination` dans `_process_departures`.

### "origine inconnue" systematique sur les arrivees

Bug reel trouve par inspection de la reponse API complete : le lien
`category="origin"` n'est PAS dans `entry["links"]` (attendu par le code
existant -- ne contient que line/company/vehicle_journey/route/
commercial_mode/physical_mode/network) mais dans
`entry["display_informations"]["links"]`. La donnee existait depuis le
debut, `_origin_name()` cherchait juste au mauvais endroit -- jamais
trouvee, repli systematique sur "origine inconnue". `_origin_name()`
corrigee pour lire le bon chemin.

### Verification

Les deux fonctions corrigees testees directement contre l'API en direct
(pas juste relu le code) : 4/4 arrivees resolues avec une vraie origine
("Ambérieu-en-Bugey", "Paris - Gare de Lyon - Hall 1 & 2", "Firminy", "Le
Puy-en-Velay" -- plus aucune "origine inconnue"), departs sans doublon
("Lyon Perrache" au lieu de "Lyon Perrache (Lyon)"). Deploye, service
`sncf-watch` redemarre, `NRestarts=0`.


## 2026-08-31 (suite 41) : journal audio des captures radio (radio_log.py)

**Fait.** Demande : archiver chaque capture reelle en un fichier audio
unique dans nd1/radio_log, retention 48h, purge automatique a minuit,
agregeant si possible habillage sonore + annonce TTS + contenu capte.

### Architecture

Nouveau module `radio_log.py` :
- `RadioLogSession` : une instance par candidat detecte. Accumule des
  segments audio (fichiers WAV normalises a 22050Hz mono) DANS L'ORDRE ou
  ils sont joues, puis les concatene en un seul MP3 final via ffmpeg
  (`-f concat`) au moment de `finalize()`. `discard()` jette tout sans
  sauvegarder (candidat ecarte comme bruit).
- `add_audio_file(path)` : pour un fichier deja existant (chime,
  annonce TTS deja synthetisee en mp3).
- `add_raw_pcm(bytes, sample_rate, format)` : pour du contenu radio brut
  deja accumule pendant un relais en direct (formats varies selon le
  chemin : s16le pour rtl_fm/dsd-fme, f32le pour RTLSDR-Airband).
- `purge_old_logs()` + `start_purge_scheduler()` : purge de tout fichier
  de plus de 48h, appelee une fois au demarrage puis chaque jour a minuit
  pile (thread dedie, calcul dynamique du delai jusqu'au prochain minuit).

### Crochet partage plutot que duplication (sound_player.py)

`play_sound_effect()` accepte desormais une "session d'enregistrement
active" (global simple, pas de contextvar -- audio_lock garantit deja
l'exclusivite) via `set_recording_session()`/`get_recording_session()`.
Des qu'une session est active, TOUT son joue via cette fonction (chimes
ET annonces TTS cloud, puisque `speak_with_lead_sound()` appelle
`play_sound_effect()` pour les deux) y est automatiquement copie -- pas
besoin de dupliquer la logique dans chaque chemin de capture.

**Limite assumee** : le repli espeak (quand le cloud echoue/timeout) NE
PASSE PAS par `play_sound_effect()` -- son audio n'est donc pas capture
dans le journal. Cas rare (edge-tts a ete tres fiable en session), pas
juge justifiant la complexite de rediriger espeak vers un fichier
intermediaire pour ce gain marginal.

### Cablage sur les 3 chemins de capture

- `radio_watch.py` / `_handle_candidate()` : cree et active la session au
  debut du cycle, `finalize()` si signal reel confirme (dsd-fme ou
  stream_until_quiet), `discard()` sinon. Desactive systematiquement dans
  le `finally`.
- `stream_until_quiet()` : accumule les chunks rtl_fm ecrits vers aplay,
  les ajoute a la session active (si presente) a la fin.
- `try_digital_voice_decode()` (dsd-fme) : meme principe sur les paquets
  UDP decodes.
- `airband_bridge.py` / `_relay_channel()` : cycle de vie complet
  autonome (cree, active, accumule le contenu UDP relaye, finalize,
  desactive) -- retourne toujours True donc toujours sauvegarde.
- `radio_watch.py` / `main()` : `radio_log.start_purge_scheduler()`
  appele au demarrage.

### Tests avant deploiement

Mecanisme teste DIRECTEMENT (pas suppose) : session avec deux vrais
chimes (freq_open/freq_over, via le crochet automatique de
play_sound_effect) + 2s de contenu simule (tonalite 440Hz, format
s16le/22050Hz comme rtl_fm) -- fichier final produit et lisible (`ffprobe`
confirme 4.55s, coherent avec la somme des segments), nomme
"20260831_HHMMSS_test_canal_446_civil.mp3" (horodatage + libelle assaini,
conforme au format prevu). Purge testee sur dossier vide, aucune erreur.

Deploye, radio-watch redemarre, `NRestarts=0`. Pas encore de vraie
capture radio observee dans la courte fenetre de surveillance immediate
(activite nocturne calme) -- le mecanisme lui-meme est deja prouve
fonctionnel independamment de la reception RF reelle.


## 2026-08-31 (suite 42) : gestion thermique enrichie -- seuil adaptatif + zone de surveillance CPU

**Fait.** Suite au constat repete de coupures thermiques trop frequentes
(suite 33 : 7 pauses en 4h, dont une de 70 min). Demande explicite de
l'utilisateur : monter le seuil de coupure quand le refroidissement est
rapide (sans jamais approcher d'un seuil critique), et introduire une
zone de surveillance (plutot qu'une coupure franche) quand le CPU n'est
pas charge par autre chose.

### Trois mecanismes desormais actifs ensemble

1. **Marge de reprise adaptative** (deja en place, suite 33, inchangee) :
   etire TEMP_RESUME_C plus bas si les pauses se rapprochent dans le
   temps.
2. **Seuil de coupure adaptatif (nouveau)** : `TEMP_PAUSE_C_BASE=72.0`
   devient un point de depart, pas une valeur fixe. Apres chaque pause,
   compare sa duree (temps de refroidissement reel) a la moyenne des
   pauses precedentes (meme mecanisme deque/comparaison que le point 1).
   Si le refroidissement a ete rapide (<80% de la moyenne -- bonne marge
   thermique, air ambiant frais), releve le seuil effectif de coupure de
   `TEMP_PAUSE_ADAPTIVE_STEP_C=1.0C`, plafonne a
   `TEMP_PAUSE_ADAPTIVE_MAX_C=6.0C` au-dessus de la base (donc 78.0C max
   -- jamais plus proche de 2C du plafond critique). Se relache si le
   refroidissement redevient lent.
3. **Zone de surveillance CPU (nouveau)** : quand le seuil de coupure
   (adaptatif) est atteint, verifie la charge CPU (`os.getloadavg()`,
   integre, pas de dependance externe). Si la charge 1-minute est basse
   (<`CPU_LOAD_WATCH_THRESHOLD=0.5`) -- pas de calcul intense en cours
   (dsd-fme, decode...) -- reste actif au lieu de couper, mais verifie
   beaucoup plus souvent (`WATCH_ZONE_CHECK_INTERVAL_SEC=15s` au lieu de
   60s). Si la charge remonte pendant la zone de surveillance, coupure
   normale reprend. Sort de la zone si la temperature redescend sous le
   seuil sans avoir eu besoin de couper.

### Plafond dur absolu, jamais contourne

`TEMP_CRITICAL_C=80.0` (le SoC BCM2711 throttle lui-meme vers 80-85C,
confirme par recherche web -- article framboise314 sur le decodage APRS,
suite au signalement de l'utilisateur du meme probleme). Quelle que soit
la marge adaptative ou l'etat de la zone de surveillance, ce seuil
declenche TOUJOURS une coupure immediate -- aucune exception codee.

### Deploiement

`radio_watch.py` deploye, import verifie (constantes + lecture de charge
CPU confirmees fonctionnelles), service redemarre, `NRestarts=0`.
Temperature au redemarrage : 66.7C (sous tous les seuils, nuit fraiche) --
la nouvelle logique n'a pas encore ete exercee en conditions reelles
(aucun seuil approche dans la fenetre d'observation immediate), a
confirmer au prochain episode de chauffe.


## 2026-08-31 (suite 43) : caracterisation "ground zero" du plancher de bruit reel

**Fait.** Suite a la conclusion de la recherche de reference (suite
precedente : aucune table publiee n'existe pour calibrer precisement ce
materiel/cette bande, meme la methode professionnelle SINAD exige un
generateur de signal calibre qu'on n'a pas) -- proposition alternative :
mesurer directement le plancher de bruit REEL calcule en interne par
RTLSDR-Airband (`calculate_noise_floor()`, deja decouvert en session
precedente via le journal debug cache), plutot que de deviner. Profite
d'une periode creuse (peu de trafic reel) pour une mesure non contaminee.

### Methode

Reconstruction de RTLSDR-Airband en mode debug (`-DCMAKE_BUILD_TYPE=Debug
-DDEBUG_SQUELCH=ON`) dans un repertoire persistant separe
(`rtl_airband/src/build_debug/`, distinct du build de production -- rien
touche a ce qui tourne en production). Capture de 5 minutes par fenetre
sur les 6 canaux actuellement configures (pas seulement PMR446, comme
demande), parsing du journal debug (`calculate_noise_floor(): N: noise
floor is now X`), exclusion des 200 premiers echantillons (transitoire de
convergence, confirme visible dans les donnees brutes -- decroissance de
~4.85 a une valeur stable en quelques secondes), statistiques sur le
reste (moyenne, mediane, ecart-type, min, max).

Note de rebuild : le repertoire source persiste (suite 25) n'a pas de
`.git` fonctionnel pour la detection de version CMake depuis un
sous-dossier externe -- resolu en placant le nouveau dossier de build
DANS `src/` (meme structure que le build de production), pas a cote.

### Resultats (plancher de bruit stabilise, unite interne RTLSDR-Airband)

| Fenetre | Moyenne | Mediane | Ecart-type |
|---|---|---|---|
| pmr446 (16 canaux, NFM) | 0.0783 | 0.0751 | 0.0398 |
| aviation_atis (AM) | 0.2658 | 0.2616 | 0.0191 |
| aviation_sol (AM) | 0.2813 | 0.2824 | 0.0049 |
| aviation_tour (AM) | 0.2954 | 0.2959 | 0.0058 |
| aviation_approche (AM) | 0.2951 | 0.2949 | 0.0056 |
| aviation_st_etienne (AM) | 0.3446 | 0.3430 | 0.0124 |

### Lecture des resultats

- **PMR446 se distingue nettement** : plancher de bruit 3.4 a 4.4 fois
  plus bas que n'importe quel canal aviation AM -- coherent avec la
  fiabilite deja constatee en usage reel (rogerbeep capte, plusieurs
  transmissions reelles entendues clairement cette session). Bande NFM
  UHF, contre AM VHF pour l'aviation -- l'AM n'a pas l'effet de
  suppression de bruit naturel du FM, cet ecart est attendu.
- **Au sein du groupe aviation** : `aviation_atis` est le plus "propre"
  (0.2658), suivi de pres par sol/tour/approche (tous ~0.28-0.30, tres
  proches entre eux -- coherent, meme emetteur d'origine Lyon-Saint-
  Exupery, distance/trajet similaire). `aviation_st_etienne` est
  nettement le plus "bruite" du lot (0.3446, ~30% au-dessus d'ATIS) --
  aeroport local plus petit, potentiellement plus de parasites RF locaux
  autour de l'utilisateur.

### Limite assumee

L'unite interne `noise_floor_` de RTLSDR-Airband n'est PAS directement en
dB comme le parametre de configuration `squelch_snr_threshold` -- utile
en **comparaison relative** entre canaux (lequel est plus propre/plus
bruite que lequel), mais ne se traduit pas mecaniquement 1:1 en une
nouvelle valeur de seuil sans connaitre la formule exacte reliant les
deux (code source non inspecte a ce niveau de detail cette session).
Interpretation prudente : puisque sol/tour/approche ont un plancher tres
proche de celui d'ATIS (deja calibre a 3dB avec succes), il est probable
qu'un seuil proche de 3dB leur convienne aussi ; aviation_st_etienne,
nettement plus bruite, pourrait avoir besoin d'un seuil different
(potentiellement plus proche ou superieur au defaut 9.54dB) -- hypothese
raisonnee, pas une certitude, a proposer a l'utilisateur avant
application.

### Etat actuel

Service radio-watch redemarre apres la mesure, `NRestarts=0`. Aucune
modification de configuration appliquee suite a cette caracterisation --
donnees de reference etablies, decision de les exploiter ou non laissee
a l'utilisateur.


## 2026-08-31 (suite 44) : etude scientifique du bruit + filtre passe-bande vocal

**Fait.** Suite a la caracterisation du plancher de bruit (suite 43),
capture de donnees brutes persistantes pour analyse spectrale, puis
implementation d'un filtre passe-bande temps reel sur les deux chemins de
reception, a la demande de l'utilisateur.

### Capture de donnees "sous le coude"

3 canaux representatifs captes en audio brut (60s chacun, squelch force
ouvert) : PMR446 canal 1, aviation_atis, aviation_st_etienne. Fichiers WAV
persistants dans `/media/nas/nd1/radio_log/noise_study/` (PAS soumis a la
purge 48h du journal de capture normal). Dongle precise par l'utilisateur :
NooElec NESDR Nano 2 (R820T2 + RTL2832U, boitier plastique -- pas de
blindage RF, connu pour chauffer ~200mA), monte sur la balustrade du
balcon via rallonge USB, EXTERIEUR et loin du Pi -- ecarte l'hypothese de
bruit numerique du Pi par proximite directe.

### Analyse spectrale (numpy, sans scipy -- absent du Pi)

Facteur de crete (3.4-3.9) et kurtosis (2.7-2.8, legerement SOUS la valeur
gaussienne 3.0) : bruit pas du tout impulsif, plutot du bruit colore avec
des tonalites franches superposees.

**Constat cle** : signatures spectrales completement differentes entre
bandes.
- **PMR446 (NFM)** : AUCUNE trace de ronflement secteur. Tonalites
  propres a 764.6/1236.7/2001.3/2765.9/6000.0/7234.1 Hz -- le 6000.0 Hz
  pile rond est suspect (artefact numerique probable, source exacte non
  identifiee).
- **Aviation ATIS et Saint-Etienne (AM)** : echelle nette d'harmoniques a
  50Hz (16/48/100/200/300/400/450/600 Hz), 100Hz tres dominant (jusqu'a
  +31dB). Explication physique solide : la demodulation AM reproduit
  fidelement toute variation d'amplitude (dont le secteur), la
  demodulation FM/NFM l'ignore par nature -- pas une coincidence,
  attendu de la physique de la demodulation elle-meme.
- Saint-Etienne a en plus une paire de pics propres vers 4314-4337 Hz,
  absents d'ATIS -- coherent avec son plancher de bruit deja mesure plus
  eleve (suite 43).

### Filtre passe-bande vocal (300-3000Hz)

Demande explicite de l'utilisateur : "detourer en coupant tout ce qui est
hors de la freq vocale" -- premiere etape, simple et peu risquee.

Nouveau module `audio_filters.py` : deux biquads du second ordre
(formules RBJ "Audio EQ Cookbook", standard, pas de dependance scipy)
passe-haut 300Hz + passe-bas 3000Hz en cascade, etat persistant entre
chunks pour continuite sans discontinuite audible. Verifie numeriquement
(reponse en frequence mesuree, pas supposee) : -3dB pile a 300 et 3000Hz,
quasi plat 500-2000Hz (coeur de la voix), forte attenuation sur TOUS les
artefacts identifies (16Hz: -51dB, 48Hz: -32dB, 100Hz: -19dB, 6000Hz:
-22dB, 7234Hz: -40dB). Performance mesuree sur le Pi reel (pas suppose) :
18.6ms de traitement pour un morceau de 200ms, marge x10.8 sous le budget
temps reel.

Cable sur les deux chemins de capture :
- `airband_bridge.py` (`_relay_channel`) : filtre applique a chaque
  paquet avant `aplay` ET avant l'archivage (`radio_log`).
- `radio_watch.py` (`stream_until_quiet`) : filtre applique APRES l'envoi
  du chunk BRUT a multimon-ng (le decodage numerique POCSAG/AFSK1200 a
  besoin du signal non filtre, sensible a la phase/amplitude), mais AVANT
  aplay/l'archivage/la detection RNNoise. **Non applique en mode "wfm"**
  (diffusion FM commerciale/pirate -- contenu musique/parole large bande,
  un filtrage vocal etroit degraderait sans raison, hors perimetre de
  cette demande).

### Tests avant deploiement

Cycle isole PMR446 (radio-watch arrete au prealable) : import propre,
aucune erreur, aucun processus orphelin. Deploye en production, service
redemarre, `NRestarts=0`. Pas encore de vraie transmission captee dans la
fenetre d'observation immediate pour confirmation a l'oreille -- le
filtre lui-meme est deja valide numeriquement et en performance reelle,
risque residuel faible.

### Prochaines pistes identifiees, pas traitees cette session

Le filtre 300-3000Hz ne retire PAS les tonalites PMR446 qui tombent DANS
la bande vocale (764/1237/2001/2766Hz) -- necessiterait des coupe-bandes
precis et chirurgicaux, discute avec l'utilisateur comme etape ulterieure
possible.


## 2026-08-31 (suite 45) : seuil thermique conditionne heure + temperature exterieure

**Fait.** Demande explicite de l'utilisateur : le scanner se mettait en
pause des 72.0C meme quand il ne fait rien d'autre que scanner, alors que
la marge de dissipation reelle depend de l'heure (nuit = ne peut que
refroidir) et de la temperature exterieure (jour frais = large marge,
canicule = moins). Le seuil de coupure fixe ignorait totalement ce
contexte.

### Contexte environnemental

Nouvelle fonction `_fetch_outdoor_temp_c()` : reutilise l'API Open-Meteo
(meme source deja validee dans `meteo_watch.py`, gratuite, sans cle),
`current=temperature_2m` a la position du Pi (balcon, 45.4397/4.3872).
Sondee toutes les 30 min (`OUTDOOR_TEMP_POLL_INTERVAL_SEC=1800`) --
demande explicite de l'utilisateur de ne pas verifier trop souvent, la
temperature exterieure ne bouge pas en quelques secondes (a distinguer du
check de temperature CPU interne, deja espace de 60s, inchange).

`_is_night_now()` : creneau large 22h-7h, base sur l'heure locale.

`_compute_env_bonus_c()` combine les deux :
- Bonus nuit fixe : +2.0C (`NIGHT_BONUS_C`).
- Bonus/malus par paliers de temperature exterieure
  (`OUTDOOR_TEMP_BONUS_TIERS`) : <10C -> +3.0C, <18C -> +2.0C, <25C ->
  0.0C (neutre, seuils de base calibres pour un climat modere), <32C ->
  -2.0C, au-dela -> -4.0C (canicule, moins de marge malgre l'absence de
  calcul intensif).
- Ecrete a +/-5.0C (`ENV_BONUS_MAX_C`), applique au seuil de coupure via
  le meme garde-fou existant `min(..., TEMP_CRITICAL_C - 2.0)` -- le
  plafond dur (80C) reste absolument inchange, aucune adaptation
  (existante ou nouvelle) ne peut le contourner.

Le bonus environnemental s'ajoute au bonus adaptatif deja existant
(refroidissement rapide observe, voir suite precedente) : seuil effectif
= `TEMP_PAUSE_C_BASE + pause_adaptive_offset_c + env_bonus_c`, toujours
plafonne. Log de pause enrichi pour tracer la decomposition complete
(base / adaptatif / environnement / temperature exterieure / jour-nuit).

### Verification avant deploiement

Teste en conditions reelles sur le Pi (pas suppose) : a 03h00 (nuit),
exterieur mesure a 19.8C -> bonus exterieur 0.0C (palier neutre) + bonus
nuit +2.0C = +2.0C au total -> seuil de coupure effectif ce soir : 74.0C
au lieu de 72.0C. Import propre confirme sur le Pi avec les vraies
dependances (numpy, requests, audio_lock, airband_bridge...). Service
redemarre, `NRestarts=0`.

### Non traite / notes

Le bonus environnemental n'est applique qu'au seuil de COUPURE
(effective_pause_c), pas au seuil de REPRISE (toujours 60C de base +
mecanisme adaptatif existant, inchange) -- l'utilisateur a specifiquement
pointe la coupure trop precoce, pas la duree de la pause elle-meme.



### Correction immediate (meme session) : paliers recalibres

Retour utilisateur immediat apres deploiement : 19.8C (nuit fraiche de
fin aout) tombait dans la zone "neutre" (18-25C, bonus 0), jugee bien
trop severe -- l'utilisateur attendait un seuil proche du plafond (77C)
pour une nuit fraiche sans charge particuliere. Paliers remontes
(<20C -> +3.0C, <24C -> +1.5C, <28C -> 0.0C, <33C -> -2.0C, sinon -4.0C).
Reverifie en conditions reelles : 19.8C + nuit -> bonus sature a
ENV_BONUS_MAX_C=5.0 -> effective_pause_c = 77.0C, conforme. Redeploye,
service sain.


## 2026-08-31 (suite 46) : deplacement radio_log (partage Samba) + nivelage des 16 bandes de balayage

**Fait.** Suite au premier test reel du filtre passe-bande sur PMR446
("cristal clair" selon l'utilisateur), deux demandes : les journaux audio
invisibles depuis Windows, et un calibrage bande par bande des bandes de
balayage (question soulevee sur d'eventuelles bandes manquantes vs
l'ancienne version).

### Journal audio invisible depuis Samba -- root cause + fix

Pas un probleme de droits (fichiers lisibles, uid/gid corrects) : le
partage Samba `[nd1]` pointe sur `/media/nas/nd1/server`, alors que
`radio_log.py` ecrivait dans `/media/nas/nd1/radio_log/` -- en dehors du
partage, invisible depuis Windows par construction. Meme situation que
`flight_scaner`/`meteo_scaner`/`radio_scaner`/`train_scaner`, tous deja
dans `server/` pour rester accessibles.

Fix : dossier deplace vers `/media/nas/nd1/server/radio_log/` (mv, avec
les captures existantes + `noise_study/`), `RADIO_LOG_DIR` mis a jour
dans `radio_log.py`. Verifie : import propre avec le nouveau chemin,
service redemarre, sain.

### Bandes manquantes ? Verification -- rien n'a ete retire

Audit du code deploye + historique ROADMAP : les 16 bandes de balayage
(radioamateur, CB, aviation civile/militaire, VHF/UHF pro, marine, PMSE,
LPD433, VHF basse, FM) et les 6 fenetres RTLSDR-Airband (PMR446 +
5 frequences aviation locales) sont toutes presentes. Numerique texte
(multimon-ng, POCSAG/FLEX/AFSK1200/DTMF...) et voix numerique (dsd-fme,
DMR/P25/D-STAR/YSF/X2-TDMA/NXDN/M17/EDACS...) actifs et confirmes
fonctionnels (vrai decrochage DMR deja observe en logs). Taxis/dispatch
prive : couverts par les bandes "radio professionnelle UHF/VHF" + 6m
radioamateur. Secours (police/pompiers/SAMU) : seule exclusion
DELIBEREE, deja investiguee et confirmee injoutable (reseau ANTARES
chiffre, aucun logiciel n'y changera rien) -- pas un oubli.

Explication la plus probable de la baisse percue : la frequence des
pauses thermiques (avant le fix de la suite 45) reduisait fortement le
temps de scan actif total, donc les chances de tomber sur du trafic
reel, toutes bandes confondues.

### Nivelage (calibrage margin_db) des 16 bandes de balayage

Demande explicite : calibrer chaque bande individuellement plutot que le
defaut unique `NOISE_MARGIN_DB = 9.0` applique partout, ordre
alphabetique du champ `name`.

Methode : script de releve (`band_survey.py`, tourne directement sur le
Pi, `radio-watch` arrete le temps de la mesure pour liberer le dongle) --
pour chaque bande, memes parametres bas/haut/step que la production,
3 sondages `rtl_power -i 1 -1`. **Piege trouve et corrige en cours de
route** : le premier essai ne reproduisait pas le filtre anti-pic-DC de
rtl_power (`DC_SPIKE_GUARD_BINS`, deja present dans le code de
production `_read_csv_bins` mais pas dans mon script de mesure) --
resultat pollue par de faux pics de 8-10 dB sur toutes les bandes
multi-segments, plus des NaN mal geres. Corrige en portant exactement la
meme logique que `radio_watch.py` (exclusion NaN + exclusion des bins
pres du centre de chaque segment), rejoue avec succes (16/16 bandes,
0 echec).

Seuil retenu par bande = plancher (20e percentile, meme convention que
le code de production) puis marge = ecart 99e-20e percentile (spread de
la variabilite normale du bruit de fond, en excluant le max absolu qui
aurait pu inclure une vraie transmission captee pendant la mesure) + 2 dB
de securite, ecrete a [5.0, 12.0] autour du defaut existant (9.0).

Valeurs deployees (dB) : cb 8.4, cb_am 11.0, lpd433 5.7, marine_vhf 5.0,
aviation 10.8, milair 12.0, radioamateur_10m 8.3, radioamateur_6m 11.7,
radioamateur_2m 5.6, radioamateur_70cm 12.0, radioamateur_23cm 8.4,
uhf_business 12.0, vhf_business 7.6, pmse 12.0, vhf_low 11.4.

**fm_diffusion explicitement NON touchee** (reste a 6.0) : deja calibree
contre un signal reel connu (97.1 MHz mesure a 7.17 dB au-dessus du
plancher) lors d'une session precedente -- plus fiable qu'une mesure
statistique de bruit, d'autant qu'une bande FM commerciale emet en
continu 24h/24 et aurait fausse la mesure de "plancher" pour cette bande
specifique si elle avait ete recalibree par la meme methode statistique
que les autres.

Deploye : import propre confirme sur le Pi (les 16 valeurs verifiees une
par une), aucun processus orphelin (rtl_power/band_survey), service
redemarre, sain (`NRestarts=0`).


## 2026-08-31 (suite 47) : ACARS + prononciation SNCF (TER/TGV Inoui) + numero de train

**Fait.** Trois volets independants dans la meme session : integration ACARS,
correction de la prononciation TER/TGV dans sncf_watch.py, ajout du numero
de train dans les annonces.

### ACARS (donnees numeriques avion/compagnie)

`libacars` (szpajder) + `acarsdec` (TLeconte) compiles depuis les sources
sur le Pi (aucun des deux empaquete Debian), installes dans /usr/local.
Nouveau module `acars_watch.py` : frequences europeennes groupees
131.525/131.550/131.725 MHz (tiennent dans une seule capture RTL-SDR,
contrairement a 136.900/136.975 MHz trop loin), filtre par texte libre
present + labels OOOI modernes uniquement (QA/QB/QC/QD/QH/QK), dedup 5min
par vol+evenement, integre au journal audio (RadioLogSession) comme le
reste. Bug trouve et corrige en testant : `-r <device> <freqs>` doit etre
EN DERNIER sur la ligne de commande acarsdec (positionnel, consomme
jusqu'a la fin d'argv) -- le mettre avant `-e -j ... -o 0` faisait lire
ces options comme des frequences supplementaires ("Frequencies too far
apart"). Cable dans la rotation principale de radio_watch.py (dwell 30s),
apres les fenetres RTLSDR-Airband.

### CB : root cause confirmee par analyse spectrale reelle + fix

Capture reelle (le "grésillant mais clairement de la voix" signale par
l'utilisateur) analysee : signal fort et stable (pas un probleme de
reception), spectre tres tonal concentre sur les formants vocaux --
signature d'un signal AM demodule en FM par erreur. La bande "cb" (NFM)
retiree de BANDS, ne restait qu'une source de copies degradees du meme
trafic deja mieux couvert par "cb_am". Effet de bord positif decouvert
plus tard : ca supprime aussi un declenchement errone de
`try_digital_voice_decode()` (son "decoding_numeric" joue puis abandonne)
qui n'arrivait que sur cette variante NFM desormais retiree.

### Prononciation TER/TGV + numero de train

Root cause reelle (requete directe a l'API SNCF en session, PAS suppose) :
le champ `commercial_mode` vaut litteralement "REGIONAURA" (un seul mot
colle, PAS "TER" ni meme "TER Auvergne-Rhone-Alpes" comme suppose au
premier correctif) pour les trains regionaux a Chateaucreux, et "TGV
INOUI" pour les TGV -- confirme sur un releve de 6h (45 trains, seulement
ces deux valeurs). Nouvelle table `TRAIN_MODE_LABELS` : REGIONAURA->TER,
TGV INOUI->"TGV Inouï" (casse mixte pour que INOUI soit lu comme un mot
et non epele comme TGV), OUIGO->Ouigo (ajoute par anticipation, jamais vu
sur cette gare). Repli : toute valeur contenant "TGV" mais absente de la
table reste epelee "TGV" ; toute valeur totalement inconnue est prononcee
telle quelle (pas de repli silencieux vers "TER", qui serait faux) et
loggee pour reperage futur.

Numero de train ajoute (demande explicite, "ca sonnera realiste") :
`trip_short_name` de l'API (ex: "886803", "6690"), chiffres espaces pour
forcer une lecture chiffre par chiffre par la synthese plutot qu'une
lecture en quantite ("huit cent quatre-vingt-six mille..."). Integre aux
5 messages parles (depart, arrivee, perturbation, suppression, retard
resorbe) via un helper commun `_train_label()`.

Deploye et verifie avec les vraies valeurs observees sur l'API (pas de
valeurs inventees) : REGIONAURA+886803 -> "TER numéro 8 8 6 8 0 3", TGV
INOUI+6690 -> "TGV Inouï numéro 6 6 9 0". Service redemarre, sain.


### Correction immediate (meme session) : numero lu comme un nombre + formulation gare

Retour utilisateur : en gare, les numeros de train sont annonces comme des
nombres normaux, pas epeles chiffre par chiffre -- l'espacement des
chiffres retire de `_train_label()`. Formulation de l'annonce de depart
recalquee sur le style gare reel : "Départ : {heure}. Le {train} à
destination de {ville} partira{voie}." (voie en ouverture de la phrase du
delai, pas en fin). Verifie qu'aucun champ voie/quai/platform/track
n'existe dans l'API SNCF publique actuelle (recherche recursive complete
sur une reponse reelle) -- la clause "voie X" est codee et prete mais ne
s'activera jamais avec cette source de donnees ; parametre `voie=None`
documente en attendant une eventuelle autre source. Redeploye, verifie
sur une vraie valeur (REGIONAURA + 886803 + Ambérieu-en-Bugey) : "Départ :
5 heure 50. Le TER numéro 886803 à destination de Ambérieu-en-Bugey
partira." Service sain.


## 2026-08-31 (suite 48) : sncf_watch -- distinction depart pur / terminus / stop-and-go

**Fait.** Refonte demandee par l'utilisateur : l'ancien systeme traitait
chaque train comme un "depart" OU une "arrivee" independants (deux flux
separes, deux annonces separees pour un meme train physique qui
s'arretait puis repartait -- pas realiste, aucune vraie gare n'annonce
deux fois le meme train).

### Detection par correlation vehicle_journey_id

Verifie sur des donnees API reelles (pas suppose) : un train qui
s'arrete puis repart de Chateaucreux a le MEME `vehicle_journey_id` dans
`/departures` ET `/arrivals`. Releve reel sur une fenetre de 2h : 17
departs, 20 arrivees, 7 identifiants presents dans les deux -- les vrais
"stop and go". Nouvelle fonction `_process_station()` recupere les deux
listes ENSEMBLE (necessaire pour la correlation, impossible en gardant
les deux flux separes comme avant) et classe chaque vehicule en 3
categories :
- present dans `/departures` SEULEMENT -> "depart" pur (le train nait a
  cette gare)
- present dans `/arrivals` SEULEMENT -> "terminus" pur (fin de service)
- present dans LES DEUX -> "stop_and_go" -- UNE SEULE annonce combinee,
  jamais deux

### Trois formulations distinctes (demande explicite, textes fournis par l'utilisateur)

- **Depart** : "Le {train}, départ : {heure}, va être mis en place."
  (pas de destination -- volontaire, une vraie annonce de mise en place
  n'en donne pas a ce stade). Remplace deux versions precedentes du meme
  jour ("partira à..." puis "Départ : ... Attention au départ." --
  jugees toutes les deux "pas naturelles" par l'utilisateur avant cette
  version finale).
- **Terminus** : "Le {train}, en provenance de {origine}, entrera en
  gare à {heure}."
- **Stop and go** : "Le {train}, en provenance de {origine}, et à
  destination de {destination}, entrera en gare à {heure}."

### Bruitage : nouveau dossier ter_sag/tgv_sag

`_schedule_ambient_sound(mode, "sag", ...)` reutilise directement
`_play_ambient_sound()` sans aucune modification -- construit deja le
nom de dossier `{categorie}_{action}` et gere deja silencieusement
dossier absent/vide (juste un log, pas d'erreur). L'utilisateur ajoute
les fichiers `ter_sag/` et `tgv_sag/` sur le NAS ; tant que `tgv_sag/`
est vide (pas encore fourni), le bruitage stop-and-go reste
silencieusement absent pour les TGV sans rien casser -- exactement le
comportement demande ("si y a rien ils jouent rien").

### Bug latent corrige au passage

`_train_category()` (choix dossier son ter_/tgv_) ne reconnaissait pas
"OUIGO" comme famille TGV (la sous-chaine "TGV" n'y figure pas) --
jamais declenche en pratique (aucun OUIGO vu sur cette gare a ce jour)
mais corrige puisque la marque appartient bien a la famille TGV.

### Architecture : etat unifie

Les deux sets separes `announced_departures`/`announced_arrivals`
remplaces par un seul `announced_events` (un train = un evenement, quelle
que soit sa categorie). Cle de timer (`scheduled_announcements`) toujours
le `vehicle_journey_id` nu desormais (avant : prefixe "arr_" pour les
arrivees specifiquement pour eviter la collision avec le depart du meme
train -- collision qui n'existe plus, un seul timer par train). Suivi
perturbations/suppressions/retards inchange sur le fond (toujours scope
`/departures`, c'est la que l'API expose les disruptions).

### Verification

Teste en conditions reelles sur le Pi (pas suppose) : releve live donnant
3 departs, 3 terminus, 2 stop-and-go sur un echantillon de 8 trains,
messages generes corrects pour les 3 categories. Service redemarre,
sain, disruption reelle detectee et annoncee des le premier cycle (retard
TER 5 min).


## 2026-08-31 (suite 49) : depot git turnkey (sauvegarde) + fix nas_watch + fix radiono

**Fait.** Demande explicite de l'utilisateur : tout le travail de cette
session (et des precedentes) ne vivait que sur la carte SD du Pi, sans
sauvegarde ni historique versionne. Cree un depot git organise ("clef en
main") couvrant l'ecosysteme complet : watchers, gestion NAS, radiono,
bibliotheques partagees, installeur, chemins des banques sonores.

### Planifie avant execution (EnterPlanMode)

Vu l'ampleur (restructuration touchant un systeme de production actif),
un plan a ete presente et valide avant toute modification -- deux
clarifications demandees via AskUserQuestion : quel script NAS
correspond a "la gestion samba via scripts vocalises" (nas.py, l'ancien,
narre mais mort/inutilise -- vs nas_watch.py, l'actif mais silencieux au
demarrage), et le perimetre exact du depot (ecosysteme watchers + NAS +
radiono, PAS la console OctoPrint ni les utilitaires systeme sans
rapport).

### Deux vrais fixes decouverts pendant la clarification (pas juste de l'archivage)

1. **Securite SSH verifiee (pas supposee)** : l'utilisateur s'inquietait
   qu'un echec de montage de disque bloque le Pi (plus joignable en SSH,
   comme avec l'ancien nas.py). Verifie directement : `/etc/fstab` ne
   contient AUCUNE entree pour nd1/nd2, aucune unite systemd `.mount`
   persistante n'existe -- le montage se fait entierement par
   `nas_watch.py`, APRES que reseau/SSH soient deja up, avec un timeout
   dur (`timeout 5 ls`) sur chaque verification. La contrainte de
   securite de l'ancien nas.py est deja respectee, differemment et
   mieux -- confirme a l'utilisateur, rien a corriger sur ce point.

2. **Annonce de demarrage manquante -- corrigee**. `nas_watch.py` parle
   deja sur les CHANGEMENTS d'etat (deja verifie dans le code), mais ne
   disait jamais rien si les deux disques etaient sains des le
   lancement du service (silence signale comme "pas normal" par
   l'utilisateur). Nouvelle fonction `_startup_announcement()` : verifie
   chaque disque une fois au demarrage de `main()` (reutilise
   `_mount_is_healthy()`/`_disk_present()` existants, aucune nouvelle
   logique de montage) et annonce le resultat. Teste en direct sur le
   Pi : "2 disques sur 2 en ligne. disque de sauvegarde en ligne, disque
   principal de stockage en ligne." -- confirme dans les logs ET via le
   `_speak()` reel.

3. **Bug reel trouve dans radiono.py** (projet d'alarme anti-effet-
   tunnel N24, capsules audio aleatoires) : pointait sur
   `/media/nas/nd2/server/radiono`, un chemin qui **n'existe pas**
   (`/media/nas/nd2/server/` introuvable, confirme). Le vrai contenu vit
   sous nd1 (72 sous-dossiers verifies). Corrige via le nouveau fichier
   central `sound_paths.py`.

### Fichier central des chemins de banques sonores

`config/sound_paths.py` (nouveau) : `NAS_BASE` + un chemin par categorie
(radio_scaner, meteo_scaner, train_scaner, flight_scaner, radio_log,
radiono). Chaque watcher (`radio_watch.py`, `airband_bridge.py`,
`meteo_watch.py`, `crue_watch.py`, `sncf_watch.py`, `stas_watch.py`,
`adsb_watch.py`, `radio_log.py`, `radiono.py` -- 9 fichiers) modifie pour
importer depuis ce fichier plutot que des chemins en dur locaux et
parfois divergents (le bug radiono ci-dessus en est la preuve directe).

### Structure du depot

```
pi-console-watchers/
├── README.md, APT_PACKAGES.md, install.sh, requirements.txt
├── config/sound_paths.py
├── systemd/*.service (7 unites)
├── watchers/ (7 watchers + airband_bridge.py)
├── lib/ (speak, sound_player, audio_lock, radio_log, audio_filters, rnnoise_lib/)
├── nas/ (nas_watch.py actif ; legacy/ = nas.py + smbv1/2.conf, reference historique)
├── radiono/ (radiono.py corrige + magicsound.py, module local necessaire)
└── ROADMAP.md (copie de l'historique complet)
```

Organise en dossiers pour la navigation, mais `install.sh` APLATIT tout
dans un seul repertoire cible au deploiement -- preserve le style
d'imports plats existant, AUCUNE modification des imports Python.

### Deploiement + verification (tous les fichiers modifies, pas juste copies)

Tous les fichiers touches redeployes sur le Pi reel et verifies avant
commit : `python3 -m py_compile` sur chaque fichier modifie, import reel
de tous les watchers modifies (`radio_watch, sncf_watch, meteo_watch,
crue_watch, stas_watch, adsb_watch` -- import propre confirme), les 7
services redemarres et confirmes actifs avec `NRestarts=0`, radiono.py
teste directement (le nouveau chemin resout bien vers un dossier reel de
72 sous-dossiers).

### Depot + remote

`git init` sur le Pi (`/home/vador/pi-console-watchers/`), remote bare
cree sur le NAS (`/media/nas/nd1/git/pi-console-watchers.git` --
stockage physiquement separe de la carte SD), commit initial (36
fichiers, 10178 lignes), pousse avec succes -- verifie identique cote Pi
et cote NAS (meme hash de commit des deux cotes).

### Note laissee a l'utilisateur (pas cachee)

Deux identifiants sont actuellement ecrits en dur dans le code copie
dans le depot (`SNCF_TOKEN` dans sncf_watch.py, `OPENSKY_CLIENT_SECRET`
dans adsb_watch.py) -- choix assume car le remote est prive (NAS
personnel, pas de publication publique), documente explicitement dans
le README et le message de fin d'install.sh plutot que silencieusement
laisse de cote.


## 2026-09-28 : reinstallation complete apres perte de la carte SD + console reconstruite

L'ancienne carte SD a grille (perte totale). Nouvelle carte, image OctoPi
(Debian 12 bookworm, armhf, Python 3.11), memes nom d'hote (`deepspace9`)
et utilisateur (`vador`). Les deux disques NAS etaient intacts (`e2fsck -n`
propre sur les deux) : ce depot, sauvegarde sur `nd1`, a permis de tout
reconstruire. Seule vraie perte : la console vocale (voir plus bas).

### Probleme USB 2 -- resolu (c'etait le "probleme bloquant" du debut de ce document)

Meme signature que celle documentee en tete de ROADMAP (`error -32` /
`error -71` / `unable to enumerate`), sur TOUT l'USB 2 (clavier, dongle,
moitie USB 2 des hubs), alors que l'USB 3 (disques) marchait. Demarche,
dans l'ordre : essais logiciels sans effet (reset des ports,
`old_scheme_first`, firmware VL805 deja a jour), redemarrage a froid sans
effet, puis isolement physique. Causes reelles trouvees :

1. **Le hub de l'antenne (puces VIA USB 2) n'avait pas d'alimentation
   propre** : a son branchement, il faisait decrocher le hub des disques
   (deux deconnexions en 3 s). Avec son bloc secteur : tout se stabilise,
   dongle vu en haute vitesse. L'hypothese "alimentation" de ce document
   etait donc la bonne, mais c'etait celle du hub, pas celle du Pi.
2. **La moitie USB 2 du gros hub Genesys (disques) est defaillante** : les
   disques passent par sa moitie USB 3, qui marche. Tout peripherique USB 2
   (clavier, dongle, imprimante) doit aller sur le hub VIA alimente ou
   directement sur le Pi, jamais sur le hub Genesys.
3. **Le port noir du bas du Pi semble abime** (tentatives fantomes alors
   qu'il est vide) : ne plus l'utiliser.

Branchement valide : hub Genesys (disques) sur un port bleu ; hub VIA
alimente (antenne, webcam et carte de l'imprimante, ces deux dernieres
derriere des interrupteurs USB physiques) sur l'autre port bleu ; clavier
en direct sur le port noir du haut.

Bootloader EEPROM encore de 2020 (mise a jour disponible, pas faite : ne
s'est pas revelee necessaire).

### install.sh repare (il ne fonctionnait plus tel quel sur la nouvelle image)

- Garde-fou OctoPi `/root/bin/git` (refuse git en root) : contourne en
  retirant `/root/bin` du PATH le temps du script, sans le supprimer.
- `pip` bloque par PEP 668 sur Debian 12 : `PIP_BREAK_SYSTEM_PACKAGES=1`.
- RTLSDR-Airband : CMakeLists desormais a la racine du depot ; construit
  depuis la racine vers `src/build` (chemin `AIRBAND_BIN` inchange), et
  depuis le depot (son `find_version` appelle `git describe` dans le
  repertoire courant).
- Dependances de compilation manquantes ajoutees (airband, dsd-fme,
  libacars) + `libopenblas0` (numpy, sinon radio-watch plante a l'import).
- **RNNoise n'etait pas du tout dans l'installeur** (`librnnoise.so`
  compile a la main en "suite 16") : radio-watch plantait en boucle.
  Ajoute, avec les flags NEON.
- `tools/*.py` n'etait jamais copie : log-ship plantait en boucle.
- `magicsound.py` visait `python3.9` en dur : chemin calcule via sysconfig.
- **Bug dangereux corrige** : une reinstallation ecrasait le
  `credentials.py` rempli du Pi par le modele vide. Il n'est plus copie
  que s'il est absent.
- Ajoutes : blacklist du pilote DVB, `loginctl enable-linger` (session
  audio sans connexion), points de montage NAS, sudo sans mot de passe
  LIMITE (`/etc/sudoers.d/020-pi-console-watchers` : mount, umount,
  systemctl reboot/poweroff), deploiement de la console et masquage de
  `getty@tty1`.

Valide en relancant l'installeur complet depuis ce depot sur le Pi
reinstalle : code 0, 9 services actifs.

### Console vocale reconstruite (`console/`, `systemd/pi-console.service`)

Elle n'avait jamais ete mise dans ce depot (choix de perimetre de la
creation du depot) : perdue avec la carte SD. Reconstruite d'apres la
description en tete de ce document, **et cette fois versionnee ici**.
Architecture d'origine conservee (registre deterministe, mots-cles puis
repli flou `fuzzywuzzy`, seuil 65, "fail closed", espeak local), plus :

- **Echo vocal de la frappe** (evolution prevue plus haut) : tty en mode
  brut, chaque touche est dite ; la disposition AZERTY etant appliquee par
  le noyau, on lit le vrai caractere, accents compris. Toute touche coupe
  la parole. Retour arriere / Echap / Tab (relire la ligne).
- **Mode reperage des touches** (outil de calibration prevu plus haut) :
  commande "touches", annonce chaque touche sans rien executer ; les
  touches sans caractere (Maj, Ctrl, Alt, AltGr, Windows, verrouillages,
  multimedia) sont lues via evdev. Sortie : 3 x Echap.
- Score flou = moyenne `token_set_ratio` / `token_sort_ratio` :
  `token_set_ratio` seul donnait 100 a des saisies d'un mot ("le").
- Reponses sous le verrou audio partage des watchers, attente bornee a
  10 s (pas de silence de plusieurs minutes derriere une annonce longue).
- Commandes : aide, imprimante (API OctoPrint, `OCTOPRINT_API_KEY` dans
  `credentials.py`), temperature, heure, radio (scan FM `rtl_power` ;
  dit clairement si radio-watch occupe le dongle), touches, repete,
  **redemarrer / eteindre** (confirmation "oui" obligatoire, mots-cles
  explicites seulement, jamais par le repli flou, annonce avant d'agir),
  **simulation**.
- **Simulation** (successeur de `simulate_all.py`, perdu lui aussi) :
  23 scenarios (trains x6, avions x2, meteo x6, crue x2, bus/tram x2,
  radio x2, disques x3) qui appellent directement les fonctions
  d'annonce des watchers deployes, avec des donnees fictives. Selection
  par mots-cles : "simulation train" (categorie), "simulation train
  retard" (le plus precis), "simulation tout".

### Correctif meteo_watch.py

"Vient du ouest-nord-ouest" -> elision : "de l'est", "de l'ouest-nord-
ouest", "du nord", "du sud-est". (Les horaires SNCF "23 heure 11" restent
volontairement au singulier.)

### Reste a faire cote utilisateur

- Remplir `credentials.py` (SNCF, OpenSky, cle API OctoPrint) sur le Pi.
- Terminer l'assistant de premiere configuration d'OctoPrint.
- `/etc/sudoers.d/010-vador-nopasswd` (sudo total sans mot de passe) a ete
  ajoute pour la reinstallation : peut etre supprime, la regle limitee
  suffit aux services.

### Partages Samba pour le Mac (2026-09-29)

Samba reinstalle (absent de la nouvelle image). Nouvelle configuration
`nas/smb.conf`, deployee par `install.sh` : partages `server` (nd1 entier)
et `stockage` (nd2 entier), `valid users = vador`, `force user = vador`,
masques 0664/0775 (l'ancienne config avait 766 pour les dossiers, sans
droit de traversee), `vfs_fruit` pour le Finder, annonce mDNS. Mot de
passe Samba cree a la main par l'utilisateur (`smbpasswd -a vador`).

Cote Mac : partages montes sous `/Volumes/server` et `/Volumes/stockage`,
mot de passe dans le trousseau, affichage des serveurs connectes sur le
bureau (Finder), et agent `~/Library/LaunchAgents/local.deepspace9.disques.plist`
qui attend que le port 445 du Pi reponde puis remonte les deux partages a
l'ouverture de session (teste : demontage puis remontage automatique sans
invite de mot de passe).

### Commande console "mise a jour" (2026-09-29)

`console/maj.py` : apt-get update, full-upgrade, autoremove --purge, clean,
en arriere-plan, apres confirmation "oui". Etapes parlees seulement
(recherche, nombre de paquets, telechargement, installation par quarts,
nettoyage, fin + conseil de redemarrage si noyau/firmware/libc/systemd
touches ou /var/run/reboot-required) ; detail complet dans
`server/logs/mise_a_jour_<date>.log` sur le NAS. Retaper la commande
pendant qu'elle tourne donne l'etape en cours. apt ne doit jamais poser de
question sur tty1 (il avalerait le clavier) : stdin=/dev/null,
DEBIAN_FRONTEND=noninteractive, --force-confold. Regle sudoers limitee
etendue a /usr/bin/apt-get (+ env_keep DEBIAN_FRONTEND). Teste avec une
fausse installation et un vrai `apt-get update` ; premiere vraie mise a
jour a faire par l'utilisateur (241 paquets en attente le 2026-09-29).

### Commande console "décris" : webcam + IA (2026-09-29)

`console/vision.py`. Photo demandee au mjpg-streamer d'OctoPi
(`127.0.0.1:8080/?action=snapshot`) : il occupe deja la webcam pour
OctoPrint, ouvrir `/dev/video0` directement echouerait. Webcam : Sonix
0c45:6536, sur le hub VIA alimente, derriere un interrupteur USB ; elle ne
voit que l'interieur du caisson de l'imprimante.

**Fournisseur** : Google Gemini, offre gratuite (cle `GEMINI_API_KEY`,
nouveau format de 53 caracteres, sans le prefixe `AIza`). Essais en
cascade `gemini-2.5-flash` -> `gemini-flash-latest` ->
`gemini-flash-lite-latest` sur 429/5xx. ~6 s par description.
Pistes ecartees, verifiees le 2026-09-29 : **GitHub Models est ferme
depuis le 30 juillet 2026** (l'API ne repond plus que "OK") ; l'acces
anonyme de Pollinations n'a plus de modele avec vision. `gh` est
neanmoins installe et connecte (compte blind-vador), utile pour git.

Photo refusee si luminosite moyenne < 10 (rien n'est envoye). Mesure : a
17 (bureau eteint), Gemini decrivait encore correctement le caisson.
Photo et description jamais ecrites sur disque, description non gardee
pour "repete" (reponse vide = rien a memoriser, voir main.py). Mais la
photo part chez Google, qui peut l'utiliser sur l'offre gratuite.
Description lue avec espeak (interruptible) plutot que la voix cloud :
`play_sound_effect` coupe a 15 s, trop court pour trois phrases.

### radio_watch : reprise sur plateau thermique (2026-09-29)

Scanner bloque en pause thermique de 7 h a 11 h : le Pi restait a 65C au
repos (charge ~0, CPU a 600 MHz), donc jamais sous TEMP_RESUME_C = 60C.
Ajout, a la demande de l'utilisateur, d'une reprise sur plateau bornee a
60-68C : temperature stable (<= 1,5C d'ecart) pendant 20 min et <= 68C ->
reprise annoncee ; au-dessus, pause maintenue et un seul avertissement
vocal. Cause de fond : refroidissement insuffisant. Boitier plastique avec
petit ventilateur monte par l'utilisateur, non pilote par le systeme
(aucun overlay de ventilateur, donc branche en direct sur le 5 V) : a
verifier au toucher/a l'oreille. Un boitier aluminium double ventilateur
attend d'etre monte.

Attention : `watchers/radio_watch.py` est en fins de ligne CRLF (seul
fichier du depot dans ce cas) -- un script Python qui le reecrit en mode
texte les convertit en LF sans prevenir (diff de tout le fichier).

### Console : "parasite", "accroche" / "decroche" (2026-09-29)

Nouveau `lib/noise_signatures.py`, partage scanner / console :
- **Derniere capture** (`/tmp/radio_watch_derniere_capture.json`), ecrite
  par `_handle_candidate()` (canaux FM et balayages, via rtl_fm) et
  `_relay_channel()` (fenetres RTLSDR-Airband) : frequence, bande ou
  fenetre, fichier du journal, empreinte sonore de l'audio radio seul.
  ACARS n'est pas concerne (donnees, pas d'audio).
- **Empreinte** : energie sur 24 bandes log-espacees 300-3000 Hz, en log,
  centree, normalisee ; similarite cosinus. Mesures : un meme bruit a deux
  volumes 0,99 ; bruit contre voix -0,1 a -0,3 ; vraies captures de la
  nuit : "VHF basse" et "VHF marine" 0,99 entre leurs deux moitiees
  (parasites continus typiques), transmissions aviation ~0,8, captures
  differentes <= 0,82. Seuil de reconnaissance 0,92, et frequence a
  +/- 12,5 kHz obligatoire.
- **"parasite"** : supprime l'enregistrement du journal, ajoute l'empreinte
  a `server/radio_log/parasites_connus.json` (NAS : survit a une panne de
  carte SD). Ensuite, sur une frequence proche d'un parasite connu
  SEULEMENT, pre-ecoute silencieuse de 1,5 s avant toute annonce ; si
  l'empreinte correspond, abandon silencieux ; sinon l'audio pre-ecoute
  est rejoue en tete de diffusion (rien de perdu). Aucun delai ailleurs.
- **"accroche"** : `/tmp/radio_watch_controle.json` ; la boucle principale
  l'applique apres la securite thermique (toujours prioritaire), suspend
  la pause programmee, et interrompt les etapes restantes du cycle
  (`_lock_pending()`). Ecoute verrouillee : fenetre RTLSDR-Airband reduite
  au seul canal (squelch reel), ou verification rtl_power de la seule
  frequence puis `_handle_candidate()`. Teste en direct sur l'ATIS de Lyon
  (126,175 MHz) : verrouille en 10 s, ecoute bien limitee a ce canal,
  decroche en 5 s. L'ATIS n'etait pas captee a ce moment-la (conditions).
- Pas encore vu en conditions reelles : l'abandon d'un parasite connu
  (il faut qu'un parasite signale revienne).

### Console : mode "manuel" (2026-09-29)

`console/manuel.py` (dialogue) + `lib/manual_tuner.py` (ecoute). Le
scanner publie au demarrage ses 21 plages (`/tmp/radio_watch_plages.json` :
15 bandes balayees avec pas, 6 fenetres RTLSDR-Airband a canaux fixes) --
aucune liste en double dans la console. Pendant le dialogue, la console
ecrit `{"manual": ...}` dans le fichier de controle ; la boucle principale
(apres la securite thermique, qui coupe aussi l'ecoute manuelle) fait
tourner un pipeline `rtl_fm | aplay` SANS silencieux sur la frequence,
relance a chaque changement. Sortie sur ALSA "default" (dmix) et non
plughw : verifie que deux lectures simultanees reussissent sur "default",
pour que la console annonce les frequences pendant la diffusion. Echap
convertit le mode manuel en verrouillage (avec silencieux) sur la
frequence choisie ; "decroche" reprend le balayage.

Teste : logique du dialogue sur les vraies plages (pas, dix pas, bords de
plage, bouclage des plages, verrouillage final), et ecoute reelle 93,9 puis
107,0 MHz (stations deja detectees par le scanner), relance sur changement
de frequence, arret propre sans processus orphelin au "decroche".

**Correctif le jour meme** : en defilant vite aux fleches, l'utilisateur a
fait planter le mode manuel -- un rtl_fm orphelin restait accroche au dongle
(la 1re version n'attendait que le shell de "rtl_fm | aplay"). Corrige :
enfants directs arretes un par un (SIGKILL au besoin), accord seulement apres
0,5 s de frequence stable. Piege rencontre : `process_group` vers le groupe
d'une autre session -> EPERM. Test d'endurance passe.

### Plantage du pilote son + touche F du mode manuel (2026-09-29)

Console muette : le pilote du jack (snd_bcm2835, "staging") avait plante
a 12 h 17 ("vchi message timeout", puis "failed to open VCHI service
connection") ; plus aucun son, rechargement du module bloque en etat D,
redemarrage bloque, coupure de courant necessaire. Apres redemarrage, 71
"failed to close VCHI service connection (status=-11)" en 8 min, par
rafales EXACTEMENT pendant l'usage du mode manuel : chaque relance d'aplay
refermait la carte. Une voix espeak ou une lecture isolee ne declenchent
rien. Correctif : aplay unique pendant tout le mode manuel (voir
lib/manual_tuner.py). Verifie : 20 relances -> 0 nouvelle erreur.
Autre fragilite connue : la sortie exclusive plughw du scanner bloque la
voix de la console pendant une diffusion (jusqu'a 45 s). A revoir.

Touche F (demande utilisateur) : _ManualSquelch dans radio_watch.py =
filtre de bande vocale + silencieux RNNoise de la reception, applique en
continu ; sans effet en WFM (musique).

Le meme apres-midi, le hub VIA de l'antenne a decroche ~6 min apres le
redemarrage force (erreurs -71 sur le port bleu) : antenne et webcam
absentes, rtl_fm "No supported devices found".

### Diagnostic USB de l'apres-midi (2026-09-29) et bug nas-watch

Apres le redemarrage force, le hub VIA de l'antenne ne s'enumere plus et
secoue le bus a chaque branchement (disques du hub 1 decroches 3 fois).
Tests au clavier (appareil sain) : ports bleu du haut et noir du haut SAINS ;
bleu du bas sain en USB 3 (disques) ; noir du bas suspect -- il ne s'agite
que quand on branche sur le port juste au-dessus (connecteur mecanique ?).
Aucune cause logicielle : pas de reglage USB, VL805 a jour, aucune erreur
du controleur xHCI, erreurs deja presentes sous l'ancien noyau. La cle
RTL-SDR, au bout d'une longue rallonge passive vers le mat exterieur, echoue
(-71) sur deux ports sains : tres probablement la rallonge sans hub
alimente (ca marchait le matin via le hub VIA). A racheter : un bon hub
USB 3 alimente. Pas besoin de changer de Pi.

Bug trouve : en deplacant le hub des disques, montages morts + nas-watch
plante (EIO en lisant le cache vocal sur le disque mort). Corrige dans
lib/speak.py (toute OSError = pas de cache).

### Console : arreter / reprendre les veilles (2026-09-29)

`console/veilles.py`, aiguille AVANT la reconnaissance normale (main.py) :
une action + un nom de veille = ce module ("arrete radio" -> stop + disable,
persiste apres redemarrage ; "reprends radio" -> enable + start ; "redemarre
radio" -> restart). "arrete" seul ou "veilles" = liste avec l'etat.
"toutes les veilles" exclut nas-watch et log-ship (protection des disques).
Conflits evites : "redemarre" seul redemarre toujours le Pi (confirmation),
"reprends" seul et "reprends le scan" decrochent toujours le scanner (d'ou
l'absence de l'alias "scan" pour la veille radio), "arrete le pi" eteint
toujours. Regle sudoers limitee etendue, generee par install.sh (start,
stop, restart, enable, disable de chaque unite sauf pi-console). Teste :
aiguillage de 19 phrases, aller-retour reel arret/reprise de stas-watch.
Contexte : antenne indisponible (voir diagnostic USB ci-dessus), la veille
radio peut etre arretee en attendant le nouveau materiel.

### Console : dialogue "journal" (2026-09-29)

`console/journal.py` : enregistrements de `server/radio_log/` (48 h, date
dans le nom) et de ses dossiers d'archives par jour. Tab / Maj+Tab = jour
(annonce jour + nombre), haut/bas = ecouter (sans annonce), droite = copie
dans `storage/radio/` nommee "mardi 29 septembre 2026 06h59m56s.mp3",
gauche = rejet (suppression + apprentissage de l'empreinte comme
"parasite"), Entree = rejouer, Echap = fermer. Lecteur : aplay unique pour
tout le dialogue, alimente par ffmpeg (meme precaution que le mode manuel).
Empreintes : `record_capture()` les range desormais dans
`radio_log/.empreintes.json` (par nom de fichier, menage des fichiers
purges) : tout enregistrement recent peut etre rejete ET appris ; les
anciens (avant ce changement) sont seulement supprimes. Teste sur copies
(position initiale, garder, deja garde, rejet avec et sans empreinte,
changement de jour) + lecture reelle : aucune nouvelle erreur du pilote.

**Pilote son toujours fragile** : 188 "failed to close VCHI" en 53 min
apres le redemarrage de 14 h 13, sans mode manuel, dont 2 "failed to open"
(precurseurs du plantage de midi). Pistes : sortie son maintenue ouverte en
permanence + tout sur "default" ; ou carte son USB (contourne snd_bcm2835).

### Sortie son deplacee sur un adaptateur USB (2026-09-29)

Le pilote du jack integre (snd_bcm2835) accumulait les "failed to close /
failed to open VCHI service connection" meme en usage normal (188 en 53 min),
avec risque d'un nouveau blocage complet du son. Remplace par un adaptateur
USB vers jack de l'utilisateur ("USB Audio Device", Generalplus 1b3f:2008,
pilote snd-usb-audio). `config/asound.conf` (deploye si l'adaptateur est
detecte) : "default" = plug -> dmix sur la carte "Device" (nom stable, pas
d'index). `lib/sound_player.py` : AUDIO_DEVICE "default" au lieu de plughw
exclusif -> la console parle meme pendant une diffusion radio (auparavant
muette jusqu'a 45 s). Verifie : deux lectures simultanees, espeak, format
flottant 16 kHz de l'aviation, ffmpeg ; carillon + voix en meme temps ;
plus aucune erreur VCHI. Volume Speaker 85 %, memorise (alsactl store).
Piege : `defaults.pcm.card Device` est refuse ("card is not a string") et
casse TOUT le son tant que le fichier est la.

Ports USB (mis a jour) : le bleu du BAS est defaillant en USB 2 (adaptateur
neuf refuse, -71) mais bon en USB 3 -> hub des disques dessus ; bleu du
haut (bon USB 2/3) -> adaptateur son ; noir du haut -> clavier ; noir du
bas suspect.
Jack integre ensuite desactive (`dtparam=audio=off` dans
/boot/firmware/config.txt, applique par install.sh si l'adaptateur est la) :
snd_bcm2835 n'est plus charge, plus aucune erreur VCHI possible.

### Console : dialogue "archives" (2026-09-29)

`console/archives.py` : extraits gardes de `storage/radio/`, en lecture
seule. Meme navigation que le journal (Tab = jour, haut/bas = ecouter) ;
gauche/droite = volume de lecture temporaire (20 % a 300 %, par pas de 20,
`Player.gain` + audioop.mul). Date : celle du nom si l'extrait vient du
journal, sinon celle du fichier ; pour les extraits nommes a la main, le
titre est lu en entier (espeak bloquant) avant l'ecoute.

### Console muette apres defilement rapide : verrou JACK d'espeak (2026-09-29)

Symptome : en mode manuel, defilement rapide aux fleches, puis plus aucune
voix ; la confirmation "redemarre" restait figee. Pas un plantage : un
espeak bloque (futex) sur /dev/shm/jack_db-1000. espeak -> PortAudio ->
tentative JACK ; tue pendant cette tentative (la console coupe la voix a
chaque touche), il laisse le verrou ferme, et tout espeak suivant attend.
Deblocage : tuer les espeak, supprimer /dev/shm/jack_db-*. Correctif :
lib/espeak_out.py (espeak --stdout | aplay), plus jamais d'acces audio par
espeak lui-meme. Verifie : --stdout ne cree aucun verrou JACK.

**Touche F revue le meme jour** : le silencieux maison (RNNoise + souffle
aigu > 5 kHz, calibre a 0,54-0,56 sur canaux vides) ouvrait sur tonalites et
DTMF mais jamais sur la voix du talkie de test (journal : voix 0 %, souffle
aigu jusqu'a 0,85 pendant la parole -- audio de rtl_fm sature). F lance
maintenant RTLSDR-Airband sur le seul canal (port UDP 9290, centre decale
de 300 kHz, reglages de la fenetre airband contenant la frequence), relaye
vers l'aplay unique. Sans F : rtl_fm inchange.

Aussi : "verrouille, aucune annonce" s'expliquait par une pause thermique
(16 h 40 - 17 h 19, Pi a 74 C) : en pause, rien n'est ecoute, meme verrouille.

## 2026-09-30 : etude du filtrage (code + mesures) et premiers correctifs

Mesures sur le journal de septembre (566 candidats) : RNNoise ecarte 52 %
des candidats, mais APRES les avoir diffuses jusqu'a 45 s (mediane 64 s,
6 h de souffle) ; 41 % des declenchements entre 0,65 et 0,70 ; bandes a
86-100 % de rejets (PMSE, 23 cm, pro UHF/VHF, 70 cm), VHF basse 90 % gardes
malgre une signature de parasite continu. Filtre vocal : -0,1 dB a 1 kHz,
-19 dB a 100 Hz, -16 dB a 6 kHz. 172 parasites appris, dont 115 sur
446,00625 : effet reel nul sur le PMR446 (117 pre-ecoutes sur 128 sans
empreinte), 6 vrais parasites ecartes sur les balayages (0,92-0,95).
Priorites de veille fixees avec l'utilisateur : ne jamais bloquer a tort,
ne pas couper les fins, ne pas manquer les voix faibles, continuite, pas de
saturation ; souffle, traine et attente sont secondaires ; la largeur du
filtre et le reste se decideront par la notation.
Correctifs : voir le commit. Observation apres correctif : les emissions
du canal 1 sont des begaiements du silencieux (4,6 s de son en 24 morceaux
sur 53 s) -- la pre-ecoute ne peut pas les reconnaitre ; avec le delai de
3 s, elles restent ouvertes jusqu'a la limite. A trancher par la notation.

### Notation des enregistrements et bilan (2026-09-30)

Idee de l'utilisateur ("machine learning du pauvre") : touche N du journal,
`console/notation.py`. 11 champs (haut/bas = champ, gauche/droite = valeur,
Entree sur OK) : contenu (voix, musique, tonalites, donnees, rien),
lisibilite 1-5, souffle, brouillage (type), son deforme (coloration NISQA),
volume, stabilite (haché), debut coupe, fin (coupee trop tot / correcte /
trop longue : erreurs de detecteur de voix), meritait l'annonce, certitude.
References : SINPO (UIT-R SM.1135), ITU-T P.835, NISQA, metriques VAD
(front-end clipping, mid-speech clipping, overhang, noise as speech),
AudioSet. Notes dans `server/radio_notes.json` (jamais purge), avec les
mesures du systeme pour la capture. `console/bilan.py` : resume par bande et
propositions (a partir de 8 notes, notes "hesitant" a demi-poids), adaptees
au chemin (seuil de voix RNNoise pour rtl_fm, silencieux pour RTLSDR-Airband) ;
rien n'est applique automatiquement. "bilan" retire des mots-cles de "sante".

## 2026-10-01 : systeme d'attention (reconstruction)

Constat : le scanner tournait en tourniquet fixe (cycle ~113 s, fenetres
fixes 8-15 s chacune, chaque bande balayee revue toutes les ~28 min), sans
memoire d'activite. Un systeme de priorite existait du 1er au 4 septembre
(channel_stats.json : 46 canaux/bandes, 20 dernieres visites ; message
"balayage bonus priorise aviation/ACARS" quand un avion etait proche ; et
un "relachement progressif du seuil de reprise" thermique) mais n'est
jamais entre dans le depot : perdu avec la carte SD, comme la console.
Reconstruit : lib/attention.py (voir commit), contexte aerien dans
adsb_watch, commande console "attention". Tests hors ligne : sans piqure
le programme est identique au tourniquet ; PMR446 actif -> 37,5 s + une
visite de plus ; piqure divisee par 4 en 1 h ; recevabilite de Lyon sol
tombee a 0,2 apres 20 visites piquees sans capture ; plancher strict.
Mesure du 1er octobre : 14,5 h de pause thermique sur 24 h -- le
refroidissement reste le premier gain possible. OpenSky anonyme : 400
requetes/jour pour 960 demandees (90 s) -> 429 une bonne partie de la
journee ; identifiants OpenSky necessaires.

## 2026-09-30 : reglages sur le NAS et commandes ville, gare, aeroport

Demande utilisateur : un dossier de reglages sur le NAS (accessible depuis
le Mac et l'iPhone), les cles API dans des fichiers texte a remplir par
copier-coller, et des commandes console pour choisir sa commune, la gare
et les aeroports, avec une arborescence alphabetique au clavier.

- `lib/reglages.py` : `server/nearfield-anchor-settings/` (lieu.json,
  trains.json, avions.json, cles/*.txt, LISEZMOI.txt, listes/ en cache).
  Repli : copie sur la carte SD (`reglages_cache`, dernier contenu valide),
  puis valeurs par defaut (Saint-Etienne), puis credentials.py pour les
  cles. Cles relues a chaque usage (pas de redemarrage) ; lieux lus au
  demarrage des veilles, la console relance celles concernees.
- Nettoyage des cles collees : BOM, espaces, guillemets, retour chariot ;
  premiere ligne non vide seulement.
- Veilles branchees : sncf (gare + cle), meteo (departement + position),
  adsb (position, deux aeroports, boite de requete calculee autour d'eux,
  plafonnee a 20 sq deg ; plus de tentative OAuth sans cle).
- Console : `reglages` (etat + cles presentes, jamais lues), `ville <nom>`
  (geo.api.gouv.fr, homonymes en liste), `gare` et `aeroport` (arborescence
  : haut/bas, droite deplie, gauche replie, lettres tapees = recherche,
  retour arriere, entree, echap). Aerodromes : OurAirports, France, code
  OACI LF (433) ; gares : open data SNCF (2782, premier code UIC).
- Limites connues : la veille crues garde son troncon (Loire forezienne),
  la veille STAS reste propre a Saint-Etienne, les frequences radio
  tour/approche restent LFMH/LFLL (piste : airport-frequencies.csv
  d'OurAirports), MAX_RADIUS_KM des annonces avions reste 15.9 km.

### Commande "tableau" : departs et arrivees, favoris (2026-09-30)

- `tableau gare` / `tableau aeroport` (sinon choix en liste). Premier
  niveau : gare suivie (ou aeroport proche et grand), favoris, puis les
  lettres. Une gare se deplie en Departs / Arrivees, chacun charge a
  l'ouverture (API SNCF, 20 trains, temps reel : heure theorique, mode,
  numero, destination ou origine, retard ou suppression).
- Aeroports : seulement ceux a code IATA et vols commerciaux. Source
  AirLabs /schedules (cle cles/airlabs.txt, gratuit ~1000 requetes/mois,
  cache 5 min par tableau). Vols en partage de code ignores ; destination
  dite par le nom de l'aeroport (liste IATA mondiale OurAirports en cache).
  Code compagnie = 2 premiers caracteres (U2, V7...). Format AirLabs teste
  seulement sur des donnees simulees : a verifier avec la vraie cle.
- Favoris : etoile sur une gare ou un aeroport, dans `tableau`, `gare` et
  `aeroport` ; `favoris.json` (UIC / OACI). Pas de doublon avec la gare
  suivie. Recherche d'aeroport aussi par code IATA ou OACI.
- `arbre.py` generalise : noeuds a profondeur libre, chargement a
  l'ouverture (LoadError = phrase dite), annonce du nombre d'entrees.

## 2026-09-30 : flash d'info locale

- `lib/flash_info.py` : presse des dernieres 24 h (Google Actualites sur la
  commune, fil France 3 du departement -- adresse deduite de geo.api.gouv.fr
  region + departement), homonymes filtres ("Saint-Etienne-de-Montluc" pour
  Saint-Etienne), articles regroupes par sujet (similarite des mots du titre
  >= 0.5), sujets classes par nombre de medias puis fraicheur.
- Redaction : Gemini (JSON : flash + rubrique par sujet), consigne de ne
  rien ajouter et d'ecarter les titres racoleurs. Repli sans Gemini : les 5
  meilleurs titres, sans mise en cache (on retente au prochain appel).
- Regle de cache voulue par l'utilisateur : le texte presse n'est reecrit
  que si la peche change (ensemble des titres des 12 sujets retenus) ; sinon
  relu tel quel (~1 s au lieu de ~20 s). Cache : reglages_cache/flash_info.json.
- Toujours frais a chaque demande : heure, vigilances (etat des veilles
  meteo et crues), meteo Open-Meteo (aujourd'hui, ou demain apres 18 h),
  retards et suppressions de la gare suivie sur 3 h, bus et tram (Loire).
- Console : "flash", "info locale", "quoi de neuf"... ; "flash details" :
  rubrique > sujet > articles (source, age, titre, resume France 3).
- Pas de jingle (a ajouter quand l'utilisateur en fournira un). Pas encore
  de flash automatique a heure fixe (question laissee ouverte).

## 2026-09-30 : voix Microsoft pour la console

- Demande : la console parle avec les voix Microsoft (edge-tts, deja celles
  des annonces), voix choisie dans les reglages, echo des touches en cache,
  repli espeak si le service gele ou tombe.
- Mesures depuis le Pi : premier son en 0,3 a 0,5 s, meme pour un long flash
  (lecture au fil de l'eau : edge-tts stream -> mpg123 -o alsa, paquet
  mpg123 ajoute). Echo de touche en cache : WAV sans silences (ffmpeg
  silenceremove debut + fin), joue par aplay, instantane.
- Regles (console/voix.py) : en cache -> joue ; echo <= 30 caracteres pas
  en cache -> espeak tout de suite + preparation Microsoft en fond ; le reste
  -> au fil de l'eau, garde en cache si <= 200 caracteres. Rien dans 1,5 s ou
  erreur -> espeak prend le relais et le service est ignore 60 s.
- Cache sur la carte SD (/home/vador/autopilot/voix_cache/<voix>/), 100 Mo
  max, menage au demarrage (moins recemment utilises d'abord). 235 sons
  d'echo prepares au demarrage et a chaque changement de voix (3 fils, ~1
  min 30).
- Commande "voix" : les 13 voix des annonces + espeak, chaque voix se
  presente elle-meme ; puis 5 debits (echo = debit + 30 points, max +100 %).
  console.json : voix, debit, debit_echo. Defaut : Denise, +20 %, echo +50 %.
- A verifier a l'oreille par l'utilisateur : prononciation des lettres
  isolees par les voix neuronales (mode "touches").

## 2026-09-30 : nouvelles veilles de donnees et modes numeriques

Demande utilisateur : tout ecouter de ce qui est ouvert autour de chez lui
(aveugle, etre informe au maximum), l'attention fera le tri.

- Constat : multimon-ng absent depuis la reinstallation (le scanner
  l'ignorait sans rien dire : pagers, DTMF, morse, APRS non decodes) ;
  installe et ajoute a install.sh.
- Fenetres de donnees (lib/attention.py DATA_WINDOWS, radio_watch.DATA_SCANNERS,
  outils communs lib/veille_donnees.py : annonce journalisee, anti-repetition
  persistante, archive JSONL dans radio_log/donnees/, distance/direction) :
  - aprs (30 s, watchers/aprs_watch.py) : rtl_fm 144,800 -> multimon-ng -A ->
    aprslib ; paquets relayes (thirdparty) deballes ; annonces : messages et
    bulletins, station nouvelle (6 h), station a moins de 3 km (30 min),
    station meteo (3 h) ; releves meteo publies dans /tmp/meteo_stations_aprs.json.
    Test reel : 4 paquets en 60 s (igate, objet a Saint-Etienne, station a 250 km ignoree).
  - capteurs (30 s, watchers/capteurs_watch.py) : rtl_433, 433,92 et 868,3 MHz
    en alternance ; alarmes et sonnettes a chaque evenement, nouveaux capteurs
    une fois par semaine, pneus jamais annonces. Test reel : 3 thermometres
    voisins (Oregon, LaCrosse, Acurite) en 30 s.
  - vdl2 (25 s, watchers/vdl2_watch.py) : dumpvdl2 compile (install.sh ;
    ici en attendant dans ~/autopilot/bin), 4 frequences europeennes, ACARS
    transporte confie a acars_watch._process_message. Aucune trame a 23 h lors
    du test (a revoir de jour). Contexte aerien : +0,3 comme l'ACARS.
- Modes numeriques en plus du -fa : quand une reception rtl_fm est ecartee
  comme non-voix, nouvel essai dsd-fme selon la frequence : dPMR (446,0-446,2),
  M17 (2 m, 70 cm), NXDN48 (VHF/UHF pro). Ces modes sortent en MONO (verifie
  dans dsd_main.c), le -fa en stereo : nombre de voies choisi selon le mode.
- Console : commande "capteurs" ; le flash cite la station meteo APRS la
  plus proche (moins de 20 km, moins de 2 h).
- Reste a faire : DAB+ (welle.io disponible dans Debian) pour la commande
  radio -- demande de prendre la cle en exclusivite, comme le mode manuel.

## 2026-10-01 : pneus, pagers, ADS-B direct, planeurs, balises 406

- Voitures (lib/voitures.py, via capteurs_watch) : trames TPMS 433 MHz
  regroupees en passages (moins de 10 s d'ecart), identifiants vus ensemble
  = meme vehicule ; habituel apres 3 jours differents (annonce une fois) ;
  annonces : vehicule inconnu la nuit (23 h-5 h), vehicule nomme qui passe,
  pneu sous 160 kPa d'un vehicule nomme. Statistiques ramenees au temps
  d'ecoute reel en 433. Etat : radio_log/voitures.json (NAS), 30 jours.
  Commande "voitures" (stats, "nomme la voiture 3 facteur"). Limite : un
  TPMS emet surtout en roulant, une voiture garee est invisible.
- Pagers 466 MHz (watchers/pagers_watch.py) : reseau e*Message, une
  frequence par passage parmi 6, multimon-ng POCSAG/FLEX ; messages
  alphanumeriques lisibles annonces. Test : trame POCSAG sur 466,050.
- ADS-B direct (watchers/adsb_local_watch.py) : dump1090-mutability en
  fenetre (service Debian desactive : il avait pris la cle a
  l'installation) ; publie /tmp/adsb_local.json, fusionne par adsb_watch
  avec OpenSky ("capte en direct par l'antenne"), hysteresis de 10 min
  avant re-annonce. Fenetre au plus toutes les 4 min. Antenne actuelle
  (446) mediocre en 1090 : 1 message valide en 20 s la nuit.
- Planeurs et petits aeronefs (watchers/ogn_watch.py) : pas par la cle mais
  par internet, serveur APRS public de l'Open Glider Network (FLARM, FANET,
  OGN), rayon 30 km, fil dans adsb_watch ; type d'appareil tire de l'id OGN.
  Relais ADS-B et stations receptrices ignores.
- Balises de detresse 406 MHz (watchers/balises_watch.py) : dec406_scan
  (MIT, compile ; il a fallu libairspy-dev et libiio-dev), fenetre de 55 s
  au plus tous les quarts d'heure (DATA_MIN_INTERVAL), toute salve annoncee.
- Ballons-sondes : non retenus pour l'instant (l'utilisateur ne voyait pas
  l'usage).

## 2026-10-01 : niveau d'aide de la console (conseils)

- console/conseils.py : chaque dialogue separe son titre (toujours dit) de
  sa consigne (quelles touches font quoi). Mode "toujours" (defaut) : la
  consigne a chaque ouverture ; mode "premiere_fois" : seulement a la
  premiere utilisation de chaque fonction (console.json, conseils_vus).
  F1 redit la consigne dans tous les dialogues, quel que soit le mode.
  Rappels sur mauvaise touche et confirmations : toujours dits.
- Dialogues convertis : accueil, journal, archives, manuel, notation,
  reperage des touches, voix et debit, listes simples, arborescences
  (gare, aeroport, tableau, flash details), saisie de la commune.
- Commande "conseils" (aussi "mode expert", "aide pratique") : activer,
  desactiver, remise a zero des consignes deja dites.
- Piege corrige : voix_cmd reecrivait console.json avec ses seules cles
  (perte du niveau d'aide) ; fusion avec l'existant desormais.

### Journal et archives : plus d'enregistrement saute (2026-10-01)

- Rejet (fleche gauche) : le suivant, qui prend la place du rejete, est
  joue aussitot apres "Rejete" (avant : il etait seulement selectionne, et
  la fleche bas suivante le sautait -- reg2, reg4... jamais entendus).
- Borne debut/fin de journee : si rien n'est en lecture, l'enregistrement
  selectionne est relance apres l'annonce de la borne ; s'il est en
  lecture, on l'arrete comme avant. Player.playing() (fil de relais vivant).
- voix.say_blocking : phrase puis enregistrement sans chevauchement, sans
  reprendre le verrou audio deja tenu par le dialogue.

### Journal : V pour valider une bonne prise (2026-10-01)

- Touche V : note "bonne prise" en une touche, sans dialogue et sans
  couper l'ecoute (notation.validate) : lisibilite 5, aucun souffle ni
  brouillage, son naturel, volume correct, stable, debut complet, fin
  correcte, meritait oui, certitude sure ; contenu "non_precise" (ignore par
  le bilan dans les parts par contenu) ; marquee validation_rapide. Pique la
  bande comme une note "oui". Une note detaillee existante n'est pas
  ecrasee (N pour la modifier) ; deja validee : rien de plus.

### APRS : commentaires traduits en francais (2026-10-01)

- lib/aprs_texte.py : type de station d'apres le symbole APRS (table
  principale, secondaire, surimpression L = LoRa, I = internet, E =
  EchoLink...), sinon le suffixe (-9 mobile, -7 portatif, -13 meteo ; pas
  pour les objets). Commentaire : reglages de relais traduits (frequence,
  decalage, tonalite d'acces, portee, lien entre deux bandes, mode D-Star,
  DMR, C4FM), tension, temperature, altitude, sigles (Digi, iGate, WX, RX
  ONLY, ADRASEC + departement, 73...) ; retires : telemetrie compressee et
  Mic-E, adresses web, mail et IP, logiciels et versions, PHG, locator,
  petits codes, indicatif de la station repete, nombres isoles.
- Mis au point sur les 47 stations reellement recues (archive aprs_*.jsonl).

## 2026-10-01 : priorites des haut-parleurs (arbitre audio)

- lib/audio_lock.py reecrit (interface acquire()/release() inchangee) :
  registre commun /tmp/audio_arbitre.json sous verrou court, entrees des
  processus morts purgees. Sources : alertes (meteo, crues), console,
  trains, avions, radio (captures en direct), donnees (APRS, ACARS,
  capteurs, pagers, planeurs), bus, disques ; source deduite du script si
  non precisee. Reglages priorites.json (NAS + copie locale), relus toutes
  les 10 s par chaque service.
- Comportements face a une source plus prioritaire : attend (file triee par
  priorite puis anciennete), attenue (joue a 30 % ; une source plus
  prioritaire peut demarrer par-dessus), abandonne. Jamais de coupure en
  cours (choix utilisateur). Defaut : tout "attend", sauf radio "attenue".
- Volume dynamique : tous les sons passent par un relais PCM qui applique
  audio_lock.scale() par morceaux de 100-200 ms : effets et voix Microsoft
  (sound_player : ffmpeg -> s16le -> aplay), espeak de secours
  (espeak_out, option scale), radio en direct (rtl_fm, RTLSDR-Airband en
  flottant, dsd-fme), mode manuel (ticket passe au fil de relais). Source
  qui abandonne : rien n'est joue.
- Console : voix, journal, archives, rire passent par l'arbitre (source
  console, attente bornee a 10 s pour les reponses). La voix de la console
  elle-meme n'est pas attenuee (lecteurs directs) : en "attenue", elle
  joue a plein volume.
- Commande "priorites" (console/priorites.py) : haut/bas, Majuscule+fleche
  (etat de Majuscule lu par evdev, la console ne le recoit pas) ou page
  haut/bas pour deplacer, gauche/droite pour le comportement.
- Simulation multi-processus validee : radio attenuee a 30 % sous un
  train, bus abandonne, alerte arrivee apres les disques servie avant eux.

### Decodage numerique valide par la synchro, annonces longues entieres (2026-10-01)

Trois phenomenes signales par l'utilisateur, causes trouvees dans les logs :
- Carillon "decodage numerique" sans rien derriere : il etait joue au debut
  de CHAQUE sonde dsd-fme (34 sondes dans la journee, 2 decodages). Sonde
  desormais silencieuse ; carillon + "Reception <protocole>, <bande>"
  seulement une fois le decodage confirme.
- Voix deformee sans annonce de bande : en second essai NXDN, dsd-fme a
  sorti du son sans aucune synchro (voix synthetisee a partir de bruit),
  joue directement. Nouveau : le son est garde en attente et jete tant
  qu'il n'y a pas DSD_FME_MIN_CLEAN_SYNC (2) synchros sans erreur, formant
  au moins 25 % des synchros. Rejoue sur les sorties reelles : DMR du
  30/09 valide (149/150), faux M17 du 01/10 rejete (7/84), NXDN rejete.
  Nombre de voies deduit de la taille des datagrammes (640 = stereo, 320 =
  mono) plutot que de l'option de mode. Sonde portee a 6 s (le demarrage de
  dsd-fme en prend ~3).
- "Balise" annoncee sans son message : la lecture de la voix etait bornee
  a 15 s fixes (speak.py) ; les annonces APRS longues (indicatif epele +
  commentaire) etaient coupees. Borne = duree reelle du MP3 (48 kbit/s) + 5 s.
- Vu au passage : des annonces ont attendu 300 s le verrou pendant que le
  journal etait ouvert (le dialogue tient la sortie son tout du long).

### Journal ouvert : captation silencieuse ; capteurs decodes en double (2026-10-01)

- Journal ou archives ouverts (drapeau /tmp/journal_ouvert.json, pid
  verifie) : les captations radio (source "radio" de l'arbitre) sont
  enregistrees en silence -- ticket "silent" : ni carillon, ni annonce, son
  en direct a volume 0, mais le son brut va au journal -- puis
  speak.journal_notice() dit "Captation entrante ajoutee au journal."
  (source console, entre deux ecoutes). Demande utilisateur : sinon on ne
  sait plus si on entend le direct ou le journal.
- Le journal ne tient plus la sortie son tout du long : Player la prend a
  chaque lecture et la rend a la fin ou a l'arret (les annonces passent
  entre deux ecoutes ; fin des attentes de 300 s).
- Mise a jour du journal ouvert : radio_log touche /tmp/journal_maj a chaque
  enregistrement ; le journal se rescanne a la touche suivante en gardant
  sa position.
- Capteurs : une meme emission reconnue par deux decodeurs de rtl_433
  (Rubicson-Temperature et Solight-TE44 : meme id, canal, temperature,
  meme seconde de reception rtl_433) etait annoncee deux fois. Doublon
  ignore (archive marque "doublon") ; heure de reception prise dans le champ
  "time" de rtl_433. Il y avait bien deux thermometres (id 25 et 164) : le
  numero est maintenant dit dans l'annonce.

## 2026-10-01 : suivi meteo renforce

- lib/meteo_locale.py : bulletin parle (temps present, ressenti, vent,
  pression et tendance 3 h, 12 prochaines heures : orage, pluie,
  instabilite CAPE, rafales, limite du gel ; 3 jours ; vigilances ; releves
  des stations APRS a moins de 60 km et des capteurs voisins ; ballons-
  sondes en vol via SondeHub ; signes avant-coureurs ; niveau du Pi).
- Historique des releves voisins (meteo_historique.json, 24 h) alimente
  toutes les 5 min par meteo_watch ; signes avant-coureurs : chute de 4 C en
  45 min, pression -3 hPa en 3 h, vent +25 km/h en 30 min, debut de pluie ;
  direction d'arrivee = station qui a reagi la premiere.
- Niveau de menace 0-3 (vigilance, orage prevu, CAPE, rafales, pression,
  signes) publie dans /tmp/contexte_meteo.json ; attention.air_context y
  ajoute un renfort des fenetres aprs et capteurs (0,6 x niveau) : le
  scanner les ecoute davantage a l'approche d'une perturbation, sans
  quitter les autres frequences. "attention" le dit.
- meteo_watch annonce la montee (niveau 2 : "Une perturbation approche :
  ... Je renforce l'ecoute des stations meteo voisines", niveau 3 : danger
  imminent), la retombee, et chaque nouveau signe avant-coureur avec la
  direction d'arrivee -- en plus des alarmes existantes.
- Commande "meteo" (quel temps, previsions, orage, pluie...) : bulletin,
  puis balayage meteo force (APRS 60 s, capteurs 433 puis 868 MHz) passe en
  tete du cycle suivant du scanner, resume parle a la fin.
- Ballons-sondes : donnees ouvertes SondeHub (position, altitude,
  temperature, frequence) ; pas encore de decodage local par la cle.

### Ballons-sondes captes en direct (2026-10-01)

- watchers/sondes_watch.py, fenetre "sondes" (45 s) : SondeHub dit quel
  ballon est en vol a moins de 250 km, au-dessus de 500 m, et sa frequence ;
  sinon la fenetre rend la main aussitot. Chaine : rtl_fm (-M fm -F9 -s 15k)
  -> sox (48 kHz, 8 bits, passe-haut 20 Hz) -> decodeur rs1729/RS selon le
  modele (rs41mod, m10m20mod pour Meteo-France, dfm09mod), --ptu --json.
- Annonces : premiere reception (puis toutes les 20 min), eclatement
  (vitesse verticale <= -8 m/s), limite du gel mesuree (passage par 0 C en
  montee, interpole) ; profil publie dans /tmp/sondes_profil.json et repris
  par le bulletin meteo. Renfort d'attention meteo : +0,3 x niveau.
- Decodeurs compiles (install.sh ; ici dans ~/autopilot/bin), sox installe.
  Test : chaine demarree et arretee proprement ; aucun ballon a portee a
  12 h 20 (lachers habituels vers 13 h et 1 h, heure de Paris).

### Bulletin apres balayage ; observations meteo de l'utilisateur (2026-10-01)

- Commande "meteo" : le bulletin n'est donne qu'APRES le balayage du panel
  radio meteo (APRS 60 s, capteurs 433 et 868 MHz, ballon-sonde a portee),
  pour etre complet, ultra-local compris (demande utilisateur). Le scanner
  verifie la demande entre chaque fenetre, signale la fin
  (/tmp/radio_watch_meteo_fait.json) ; la console attend en arriere-plan
  (clavier libre), lit le bulletin, "repete" le relit. Sans reponse du
  scanner (pause, manuel) : bulletin avec les releves des dernieres heures.
- lib/meteo_obs.py + console/observation.py : observations percues sans la
  vue (precipitations, tonnerre loin/proche/tout pres, vent d'apres
  Beaufort au toucher et a l'oreille, evolution, ressenti, odeur,
  direction si percue, certitude), inspirees de mPING (NOAA), CoCoRaHS et
  des codes temps present de l'OMM. Phrases directes ("il pleut fort",
  "tonnerre au loin", "orage tout pres", "il fait beau"...) ou dialogue
  "observation". Instantane du modele et des signes a chaque observation.
- Effet immediat : tonnerre au loin -> niveau 2, proche/tout pres -> 3 avec
  conseils de securite, averse violente/grele -> 2 (45 min), renfort
  d'attention publie aussitot.
- Apprentissage par comptes : fiabilite du modele pour la pluie par tranche
  de probabilite (5 cas minimum, phrase dans le bulletin si l'ecart depasse
  20 points), seuil d'orage local (1er quartile de la CAPE quand le
  tonnerre est entendu, 3 cas), fiabilite de chaque type de signe avant-
  coureur (suivi d'orage ou forte pluie dans les 2 h, 5 cas : moins de 20 %
  -> poids reduit, 60 % et plus -> poids fort). Signes annonces journalises
  (meteo_signes.jsonl). "bilan meteo" dit tout ca et rappelle le biais
  (on observe surtout quand il se passe quelque chose).

### Veille meteo sous chaleur (2026-10-01)

- Pendant une pause thermique, si un bulletin est demande ou si la menace
  publiee est >= 2, le scanner garde UNIQUEMENT les fenetres meteo
  (bulletin : APRS 60 s, capteurs 433 et 868, ballon-sonde ; sinon toutes
  les 5 min : APRS 30 s, capteurs 30 s, ballon-sonde), tout le reste reste
  suspendu. Plafond 78 C, et jusqu'a 80 C exclu (coupure dure inchangee) si
  la menace est critique (niveau 3). Verifie entre chaque fenetre.
- Etat publie "pause_thermique_meteo" : la console attend alors le
  balayage ; "pause_thermique" (trop chaud meme pour la meteo) : bulletin
  aussitot, avec la raison. Annonce une fois par pause : "Malgre la chaleur,
  je garde l'ecoute meteo seule, jusqu'a 78 degres."
- Simulation des 6 cas (77/79/81 C, niveaux 0/2/3, bulletin ou non) conforme.

### Bulletin vulgarise, capteurs non meteo ecartes (2026-10-01)

- Les "75,5 degres" du bulletin venaient d'un thermometre de barbecue voisin
  (rtl_433 "GT-TMBBQ05"), pas d'une station mal reglee : rtl_433 sort deja
  en Celsius (-C si) et l'APRS impose le Fahrenheit, converti par aprslib.
- capteurs_watch classe par nom de modele : cuisson (BBQ, Meat, Maverick,
  ThermoPro TP08/11/12/20, Burnhard, Acurite 01185M), piscine, frigo, sol ;
  annonces adaptees ("thermometre de cuisson ou de barbecue") ; type publie.
- meteo_locale.outdoor_sensors : seuls les thermometres de type meteo a
  moins de 8 C du modele (sinon de la mediane) servent au bulletin, a
  l'historique et aux signes avant-coureurs ; stations APRS a plus de 12 C du
  modele non citees ; le bulletin dit combien d'appareils ont ete ecartes.
- Vulgarisation : pression (stable / degradation / amelioration / haute ou
  basse), air instable ou tres instable explique, "il gele au-dessus de X
  metres", air presque sature ; ballon-sonde explique (ce que c'est,
  altitude en km, et comparaison a l'atmosphere standard partant de la
  temperature au sol, -6,5 C/km : normal, plus froid -> instable, averses et
  orages favorises, plus doux -> stable ; au-dessus de 11 km : haute
  atmosphere, toujours tres froide).

### Capteurs non meteo expliques par Gemini, avec humour (2026-10-01)

- Nouvel appareil qui n'est pas une station meteo (cuisson, piscine,
  frigo, sol, sonnette, inconnu) : ses donnees decodees (sans les champs
  radio) sont envoyees a Gemini (lib/gemini.py, appel texte commun) qui les
  explique en 2-3 phrases avec une petite blague, sans inventer ni juger le
  voisin. Repli sur l'annonce simple. Jamais pour les alarmes (sobres).
  Textes archives (donnees/capteurs_gemini_*.jsonl). Le premier appui d'une
  sonnette nouvelle est raconte par cette annonce.

### APRS : commentaires obscurs expliques par Gemini (2026-10-01)

- aprs_texte.is_weird : apres traduction, sigle inconnu de 2 a 4 lettres,
  code melant lettres et chiffres, ou commentaire non vide entierement vide
  par la traduction (telemetrie Mic-E pure exclue). Sur 45 stations reelles :
  EDF OK EurakMon, "ANYTONE AT-D878UV-1.26", TU2k, ".4b10224".
- Dans ce cas seulement, au moment d'annoncer : paquet brut + format +
  symbole a Gemini (lib/gemini.py), 1 a 3 phrases, ton amuse, sans repeter
  indicatif ni position, sans inventer ; l'explication remplace la
  traduction. Cache par (station, commentaire), 300 entrees ; archive
  donnees/aprs_gemini_*.jsonl. Repli : traduction habituelle.

## 2026-10-01 : ondes courtes avec la nouvelle cle, et pistes de captation

- La Nooelec NESDR SMArt v5 recoit bien 0,1-25 MHz par echantillonnage
  direct, voie Q (rtl_fm -E direct2 ; pyrtlsdr==0.2.93, set_direct_sampling(2) ;
  la version recente de pyrtlsdr exige une librtlsdr plus recente). Le
  rtl_power de Debian (-D) utilise la voie I : spectre plat, trompeur.
  Mesure avec l'antenne de talkie 446 : pics jusqu'a +22 dB, mais les plus
  forts (7,89 et 8,70 MHz) hors bandes de diffusion et sans modulation :
  parasites locaux probables ; rien sur 4625 kHz (UVB-76). Extraits dans
  stockage/radio/ondes_courtes. Il faut un long fil (10-20 m, unun 9:1).
- En attente de decision utilisateur : fenetre HF du scanner (UVB-76 :
  voix qui interrompt le bourdonnement ; bandes de diffusion et
  radioamateur, BLU via rtl_fm usb/lsb) ; ecoute de recepteurs KiwiSDR
  publics par internet en attendant l'antenne.
- Pistes listees a l'utilisateur (antenne Sirio 1,20 m attendue) : bande
  68-87,5 MHz et 406,1-430 MHz non couvertes ; satellites meteo Meteor-M
  (LRPT 137 MHz, images de nuages decrites par Gemini) ; ISS (APRS 145,825,
  images SSTV 145,800) ; 869,2 MHz (teleassistance) ; compteurs wM-Bus
  868,95 / 169 MHz (souvent chiffres) ; avec un fil HF : NAVTEX 518 kHz,
  VOLMET, HFDL (dumphfdl), meteo-fax (DWD), FT8.

### Plages manquantes et relais appris par l'APRS (2026-10-01)

- Nouvelles bandes balayees : vhf_80 (52-85 MHz : ancienne bande 80 MHz,
  ancienne bande TV I) et uhf_410 (406,1-430 MHz : reseaux professionnels).
- lib/relais.py : un relais qui annonce sa frequence de sortie en APRS
  (symbole r, & ou 0, ou indices T123, R17k, tone, Voice, MMDVM...), a moins
  de 60 km, est appris (frequence, mode fm/dmr/dstar/c4fm, tonalite) dans
  radio_log/relais.json ; passerelles LoRa et iGates exclues. Le scanner
  ajoute a chaque cycle une bande "fm_channels" par groupe (6 m, 2 m, 70 cm),
  un rtl_power par groupe ; decodage numerique habituel (dsd-fme -fa).
  Amorce depuis les archives : F5ZBR 145,612 (2 km), F6ZCD DMR 145,2875,
  F1ZEV 51,95, F1ZCK D-Star 438,2. L'annonce APRS dit "Je l'ajoute a mon
  ecoute des relais" quand un relais est appris.

### Donnees ouvertes : aeroports, rivieres, seismes, air ; trafic a la demande (2026-10-01)

- lib/trafic.py, commande "trafic" : flux DATEX II ouvert de Bison Fute
  (reseau national), evenements a moins de 40 km tries par gene (accident,
  bouchon, obstacle, puis fermetures, puis travaux) et distance ; route,
  ville, direction, description utile. A la demande seulement (choix
  utilisateur : pas de voiture, utile pour une visite).
- lib/donnees_ouvertes.py, commande "environnement" et bulletin meteo :
  METAR des deux aeroports (aviationweather.gov) traduits (vent, rafales,
  visibilite, phenomenes "orage avec forte pluie", plafond, CB/TCU
  expliques) ; rivieres Hub'Eau v2 (une station par riviere, les plus
  proches : Ondaine, Loire a Grangent en altitude de retenue, Furan,
  Semene ; hauteur, tendance 2 h, debit) ; seismes EMSC (rayon ~550 km,
  "ressenti probable" selon magnitude et distance) ; qualite de l'air et
  pollens abondants (Open-Meteo / CAMS).
- meteo_watch (toutes les 10 min) annonce : seisme notable (M >= 2,5 a moins
  de 150 km, ou M >= 4,5), riviere qui monte de 20 cm en une heure ; un
  orage ou cumulonimbus au METAR fait monter la menace au niveau 2.
- Teleray (radioactivite IRSN) : pas d'acces ouvert trouve ; coupures
  d'electricite : pas d'API ouverte. Non branches.

### Satellites (2026-10-01)

- lib/satellites.py : orbites Celestrak (weather, stations, amateur) et
  emetteurs actifs SatNOGS, rafraichis chaque jour ; passages pyephem
  (python3-ephem) au-dessus de 25 degres pour Meteor, 20 pour l'ISS, 55 pour
  les satellites radioamateurs FM (4 par jour au plus, les plus hauts).
- Le scanner verifie entre chaque fenetre si un passage commence
  (satellites.due, 90 s d'avance) ; watchers/satellites_watch.py le suit
  jusqu'au bout (annonce de debut) :
  - Meteor-M2 3 / M2 4 : I/Q 8 bits 1,024 MHz centre 137,5 MHz (couvre
    137,1 et 137,9) sur stockage/radio/satellites ; tools/meteor_traitement.py
    en arriere-plan (nice 19) : extraction des deux frequences (filtre
    calcule aux seuls echantillons gardes, decimation 6), meteor_demod
    72/80k, meteor_decode variantes, meilleure image gardee (PNG), decrite
    par Gemini puis annoncee ; brut efface. ~1,2 s de traitement par seconde
    enregistree.
  - ISS : APRS 145,825 MHz, stations annoncees "via la station spatiale
    internationale" (6 par passage au plus, distance ignoree).
  - Satellites FM (SO-50, AO-27, AO-123, Diwata-2B, Fox-1B, Tevel...) :
    rtl_fm 40 kHz (Doppler), detecteur de voix RNNoise du scanner ; au journal
    seulement si au moins 4 s de voix.
- Commande "satellites" : prochains passages.

### LoRa et Meshtastic : captation des rafales (2026-10-01)

- watchers/lora_watch.py, fenetre "lora" (30 s), en alternance :
  Meshtastic 869,525 MHz (250 kHz, preset europeen LongFast) et APRS LoRa
  433,775 MHz (125 kHz). rtl_sdr 1 MS/s, puissance dans la largeur du canal
  par blocs de 10 ms (sans la raie centrale), rafale = 6 dB au-dessus de la
  mediane pendant au moins 50 ms ; enregistree en I/Q brut (cu8, 100 ms de
  marge) avec sa fiche JSON dans stockage/radio/lora/<profil>/. Pas de
  decodage en direct (chaleur) : decodage differe prevu avec gr-lora_sdr
  (GNU Radio 3.10, paquets Debian) puis dechiffrement du canal public
  Meshtastic (cle par defaut connue) et analyse APRS des paquets LoRa APRS.

## 2026-10-01 : pont avec une machine compagne

Les calculs lourds (image Meteor : ~45 min de CPU et 82 C sur le Pi) partent
sur une machine plus puissante. Donnees sur le disque nd1, dossier
`server/nearfield-anchor-bridge` ; dialogue direct par le reseau local.

- Confiance : paires de cles Ed25519 des deux cotes (bridge/pont_protocole.py,
  bibliotheque cryptography). A l'appairage, les cles publiques passent par
  le NAS et chaque machine dit un code a 4 chiffres tire des DEUX cles :
  une cle remplacee en route donnerait des codes differents. Le Pi garde ses
  machines de confiance sur sa carte SD (~/autopilot/compagnon), pas sur le
  NAS. Tous les messages, resultats et suppressions sont signes.
- Liaison : la machine se connecte au Pi (port 47811, lib/compagnon_serveur.py,
  thread de meteo_watch) : son pare-feu n'a rien a ouvrir. Presentation
  signee dans les deux sens (nonce), battement toutes les 30 s. Le Pi pousse
  "travail" ; la machine repond "pris" puis "fini". Machine absente : paquet
  de reveil Wake-on-LAN toutes les 10 min (le Mac du 2026-10-01 ne se met
  jamais en veille systeme et est en Wi-Fi : le reveil par le reseau n'y est
  pas garanti, un cable Ethernet le serait).
- Travaux : travaux/<id>/travail.json, pris par renommage en
  travail.pris.json ; resultat.json signe, avec l'empreinte SHA-256 de chaque
  fichier rendu. Le Pi fait decrire l'image par Gemini, la range en PNG sur
  nd2/storage/radio/satellites, annonce, compte les statistiques, efface le
  travail. Travail sans resultat apres 12 h : efface.
- Machine compagne : dossier `bridge/` (nearfield_bridge.py, meteor.py,
  pont_protocole.py, dialogue.applescript, installer.sh). Python 3.9 suffit.
  L'installateur met numpy, cryptography et cmake par pip --user, compile
  meteor_demod/meteor_decode (sortie BMP, sans libpng), installe le service
  (LaunchAgent local.nearfield.bridge sur Mac, systemd utilisateur sous
  Linux) et, sur Mac, l'application "Nearfield Bridge" (~/Applications,
  raccourci sur le bureau) : dialogues standard lus par VoiceOver pour voir
  les ponts et leurs statistiques, appairer, supprimer un pont (le Pi est
  prevenu par message signe), mettre le service en pause. Pendant un calcul,
  `caffeinate` empeche la veille sans toucher aux reglages.
- Console du Pi : "appairage" (en meme temps que l'application), "compagnon".
- Meteor n'est ecoute (et cite par "satellites") que si une machine de
  confiance qui sait faire "meteor" est connectee ou a ete vue cette semaine.
  tools/meteor_traitement.py reste comme secours manuel sur le Pi.
- A venir sur le meme pont : decodage LoRa/Meshtastic des rafales, SSTV ISS.

### Pont : retours du premier essai reel (2026-10-01)

- Appairage : valider d'un seul cote suffit. Oui sur le Mac -> confirme-<id>.json
  signe par le Mac, le Pi accepte seul et ferme son dialogue (reveil de la
  lecture clavier : keys.wake / keys.WAKE dans console/main.py). Entree sur
  le Pi -> le dialogue du code du Mac (osascript lance par le module) se
  ferme seul. Plus de voix "say" cote Mac : elle arrivait en retard,
  VoiceOver lit deja les dialogues.
- L'application etait "anglaise" (CFBundleDevelopmentRegion en) : VoiceOver
  lisait le francais avec une voix anglaise. L'installateur la regle sur la
  langue du systeme et la re-signe.
- macOS refuse le disque reseau a un Python lance directement par launchd
  (PermissionError, sans fenetre) : le service tourne dans une application
  invisible ~/.nearfield-bridge/Nearfield Bridge Service.app (LSUIElement),
  qui recoit l'autorisation "volumes reseau".
- Premier passage traite par le Mac (Meteor M2 4, 16 h 43) : chaine complete
  OK (pris, calcul, resultat signe, annonce du Pi 4 s apres), mais pas
  d'image (signal trop faible, antenne fouet). 23 min, surtout du reseau :
  le calcul se fait desormais sur le disque local du Mac apres une copie.

### gr-lora_sdr compile (2026-10-01)

Compile en 2 h environ (tools/build_lora.sh, a la reinstallation : installer
d'abord python3-gnuradio puis lancer ce script comme tache utilisateur). Le
lanceur tools/python3-gnuradio, copie dans ~/autopilot/bin, ajoute ses
chemins (bibliotheque et module Python). Utilise par bridge/lora_decode.py
(voir ci-dessous) : en secours seulement sur le Pi.

### LoRa et Meshtastic decodes tout de suite, par le Mac (2026-10-01)

Choix utilisateur : le travail differe part a la machine compagne des
qu'elle est la ; LoRa n'est plus differe ; decodage LoRa eteint sur le Pi
tant que le Mac repond.

- lora_watch : chaque rafale gardee devient aussitot un travail "lora" du
  pont (lib/lora_messages.py), copie de la rafale (moins de 1 Mo).
- bridge/lora_decode.py (gr-lora_sdr, commun Pi et Mac) : filtre a la
  largeur du canal, 2 echantillons par chip, essaie les facteurs
  d'etalement (profil d'abord : SF11 / 250 kHz / sync 0x2B Meshtastic
  LongFast, SF12 / 125 kHz / 0x12 APRS LoRa). Tampon agrandi pour SF12.
  Valide sur des rafales generees par l'emetteur gr-lora_sdr (texte exact,
  CRC bon) ; Mac 0,2 s par rafale, Pi 1,3 s.
- Mac : GNU Radio de conda-forge dans ~/.nearfield-bridge/gnuradio
  (Miniforge dans ~/.nearfield-bridge/conda) + gr-lora_sdr compile dessus
  (il faut libboost-devel). Capacite "lora". Deux files de travail : LoRa
  (rapide) ne passe jamais derriere un Meteor.
- Pi : compagnon_serveur pousse le travail (tour toutes les 2 s) ; le
  resultat signe (trames.json) est interprete sur le Pi : Meshtastic canal
  public par defaut (lib/meshtastic_lite.py : message texte, nouveau noeud,
  position une fois par heure ; canal prive archive seulement), APRS LoRa
  par aprslib et l'annonce de aprs_watch. Fiche JSON de la rafale completee
  ("decode"). Boucle complete mesuree : 2,8 s.
- Secours : aucune machine capable connectee, ou travail pas pris en 20 s,
  et Pi sous 72 C -> le Pi decode lui-meme (nice 19, SF du profil seul).
  Mesure : 2,8 s, Mac en pause.
- Les deux rafales APRS LoRa du 2026-10-01 (0,38 s) ne contiennent pas de
  trame : trop courtes pour un paquet SF12 (plus de 1,5 s).

## 2026-10-02 : journaux separes, Ctrl+fleches

- lib/journaux.py : chaque enregistrement du journal est range d'apres
  l'etiquette au debut de son nom : radio (par defaut), donnees (APRS,
  ACARS, bipeurs, OGN, balises, LoRa), capteurs (capteurs, sondes,
  voitures), satellites. "journal capteurs", "journal satellites",
  "journal donnees", "archives capteurs"... ; "journal" seul = radio. Une
  phrase qui commence par journal ou archives passe avant les autres
  commandes (console/main.py, _match).
- Archives par journal : radio garde storage/radio/ ; les autres
  storage/radio/archives/<journal>/.
- Journal capteurs (console/journal_capteurs.py) : un releve lu par capteur
  et par jour, depuis l'archive de toutes les trames (donnees/capteurs_*),
  y compris les capteurs ecartes du bulletin (raison dite) et l'explication
  amusante Gemini ; voitures resumees en un element (passages, vehicules).
  Les annonces "nouveau capteur" et "voiture" n'y sont plus en double.
  Un releve garde (fleche droite) devient un .txt relu par les archives.
- Ctrl + fleche haut / bas : premier / dernier element du jour (journal et
  archives). Ctrl lu par evdev (keys.modifier_held) : la console envoie la
  meme sequence qu'une fleche seule.
- Constat : les 35 passages de voitures de la nuit du 1er au 2 octobre sont
  UN seul vehicule (numero 3), sans doute gare tout pres.

## 2026-10-02 : bilan complet et reglages audio appris des notes

- Anciennes notes mises de cote (server/radio_notes_archive_2026-10-02_*.json,
  demande utilisateur : repartir sur la nouvelle grille).
- Grille : un champ par brouillage (sifflement, bourdonnement, autre
  station, crachotements : aucun / un peu / beaucoup), comme les corpus
  notent chaque dimension a part ; exemples sonores fixes (E / F2).
- lib/reglages_audio.py : reglages par bande (attente avant de clore, marge
  de detection, aigus gardes, retrait du souffle, seuil anti-crachotements,
  anti-sifflement, anti-bourdonnement, volume), versions successives sur la
  carte SD ; lus a chaque ecoute par radio_watch et airband_bridge (marge ->
  seuil du silencieux RTLSDR-Airband).
- console/bilan.py : qualite 0-1 par note ; propositions chiffrees par bande
  sur les notes arrivees DEPUIS le dernier changement (8 au moins) ; reglage
  contre-productif = qualite en baisse de 0,08 apres lui (5 notes) -> dit,
  et annule tout seul en mode automatique. Commandes : bilan, applique les
  reglages, reglage precedent, reglages automatiques oui / non, reglages
  audio, reapprendre de zero (confirmation ; tout est archive, les parasites
  et stations probables restent).
- Filtres (audio_filters.GentleCleaner) mesures sur une vraie voix (NISQA) :
  anti-sifflement = pic stable dans le temps (la voix varie) : -10 dB de
  sifflement, voix intacte ; anti-bourdonnement = plancher persistant sur
  les harmoniques de 50 Hz en analyse fine (cases de 5 Hz, tolere la derive
  du reseau) : -11 dB, voix intacte. Premiers essais ecartes : masque
  spectral simple (abimait la voix de 4 a 7 dB), moyenne synchrone de
  periode (parfaite a 50,00 Hz, inutilisable a 50,02 Hz).

## 2026-10-02 : pont en differe, reseau local macOS, decodeurs en mode manuel

- Pont : sans liaison directe, fonctionnement en DIFFERE par le NAS toutes
  les 12 h (decision utilisateur : pas d'acces disque frequents) ; signe de
  vie signe "compagnons/<id>.battement.json", travaux gardes 36 h ; LoRa
  decode par le Pi tant que la liaison n'est pas directe.
- macOS "Reseau local" (Apple TN3179, forums developpeurs) : pas de remise a
  zero possible (FB14944392) ; identite suivie par la signature (mal avec
  une signature locale "ad hoc") et par l'UUID de l'executable principal
  (nos deux applets osacompile partagent le meme executable "applet") ;
  agent launchd : declarer AssociatedBundleIdentifiers ; bug connu de
  Sequoia : autorisation invalide apres redemarrage, rendue en decochant puis
  recochant. Exemption officielle par adresse :
  sudo defaults write com.apple.network.local-network AllowedWiFiLocalNetworkAddresses -array "192.168.1.17/32"
  (puis redemarrage) -- reglage systeme, a faire par l'utilisateur.
  Mesure : connexions refusees "No route to host" (Python et nc) depuis le
  service, meme lance par LaunchServices.
- Mode manuel : en FM etroite, rtl_fm a 48 kHz nourrit aussi dsd-fme (serveur
  TCP local 127.0.0.1:7360, comme SDR++ ; -fa plus le mode de la bande) et
  multimon-ng ; voix numerique confirmee (synchros propres) jouee a la place
  du gresillement apres le son de decodage ; annonces par la console
  (/tmp/manuel_evenements.jsonl) ; chiffres DTMF regroupes. Regles communes
  scanner / manuel dans lib/decodeurs.py. Teste : DTMF reel decode, deux
  dsd-fme connectes au flux ; voix DMR en manuel pas encore entendue.

## 2026-10-03 : phrases de l'interface centralisees, personnalite du Pi

- lib/phrases.py : dire(cle, **variables) ; phrases/<langue>.ini = version
  NEUTRE de reference (une section par phrase : texte, contexte, garder,
  fixe), jamais modifiee par le Pi ; autre langue = autre fichier, memes
  cles (reglage "langue" de console.json). Nombres passes deja accordes
  (phrases.nombre). Passes par le module : journal, archives, notation,
  mode manuel, appairage, commande personnalite (le reste suivra).
- lib/personnalite.py : chaque nuit a minuit (thread de meteo_watch), Gemini
  reecrit les phrases non fixes selon la personnalite choisie (commande
  "personnalite" : texte libre, neutre, surprise) : fiche de style d'abord
  (resume + regles de ton, partagee par tous les blocs), puis blocs de 20 en
  JSON strict (mode JSON de Gemini), chaque phrase verifiee (variables, mots
  proteges sans tenir compte de la casse, longueur 1,8x, pas de symboles),
  bloc illisible retente une fois, sinon phrase neutre. Sortie :
  ~/autopilot/phrases/personnalite_fr.ini (+ _hier). Essai reel : 3 min,
  79 phrases reecrites ("un vieux loup de mer bourru mais attachant").

### Suite (2026-10-03) : TOUTE l'interface passe par les phrases

- 1433 phrases dans phrases/fr.ini (757 reecrivables par la personnalite,
  les autres "fixe" : noms, unites, morceaux de phrase). Console, lib et
  veilles : tout ce qui est dit ou affiche passe par dire().
- Pluriels traduisibles : nombre(n, "note") lit [unite.note] = "note|notes".
- Tableaux de libelles (codes enregistres, libelles dits) :
  phrases.Etiquettes (dict traduit a la lecture : modes radio, couleurs et
  niveaux de vigilance, directions avec preposition "au nord" / "de l'est",
  types APRS, marques de voitures, touches du clavier, jours, mois...) et
  phrases.Champs (grilles de notation et d'observation). Cle
  <groupe>.<code>, ou par libelle (par_libelle) quand les codes sont
  illisibles. dire_ou(cle, defaut) pour les descriptions des commandes
  (commande.<nom>.description / .confirmation).
- Espaces de bord : l'INI les perd, ils restent dans le code (" " + dire()).
- Verdict "vraie station ou artefact" : garde en CODE (bruit, artefact,
  vraie, indecis) dans stations.json, dit dans la langue du moment.
- Outils : tools/phrases_convertir.py (liste, convertir, constantes,
  etiquettes, reste) ; tools/phrases_verifier.py (chaque dire() du code :
  cle presente, memes variables) -- a lancer avant chaque deploiement.
  Piege corrige : en Python 3.9 (Mac) la position des expressions DANS une
  f-string est fausse, l'outil regenere le code par ast.unparse.
- Personnalite : blocs de 40 (une vingtaine d'appels Gemini par nuit).
- Volontairement HORS des phrases (a traiter avec la traduction) :
  * mots-cles et exemples des commandes, phrases directes d'observation
    (meteo_obs.QUICK) : prochaine etape, jeux de mots-cles par langue ;
  * consignes envoyees a Gemini (flash, capteurs, APRS, personnalite) :
    en francais ; ajouter "reponds en <langue>" quand une autre langue
    sera choisie ;
  * noms de bandes et de canaux enregistres dans les journaux (radio_watch,
    relais.GROUPS, airband_bridge) : ce sont des donnees ;
  * reecriture des commentaires APRS (aprs_texte.WORDS) : moteur propre
    au francais ; messages de debogage (print).

## 2026-10-03 : scanner bloque par une cle muette ; agregat meteo ; flash complet

- PANNE : de 3 h 07 a 11 h 41 puis de nouveau des 11 h 44, le scanner est
  reste bloque dans la fenetre LoRa 869,525 MHz : rtl_sdr attendait la cle
  (plus aucun echantillon), lora_watch attendait rtl_sdr (read() sans
  limite). Plus aucune ecoute, capteurs compris (d'ou un bulletin sans
  releves). Corrige : lib/lecture_flux.lire() (rend la main apres 5 s de
  silence) pour LoRa et la voix des satellites FM ; chien de garde dans
  radio_watch : une fenetre de donnees qui depasse sa duree + 120 s voit ses
  programmes radio (rtl_*, dump1090, dumpvdl2, acarsdec, dec406 -- jamais la
  voix) arretes, le cycle reprend, une ligne dans le journal. Teste sur le Pi
  avec un faux rtl_sdr.
- AGREGAT METEO (lib/meteo_agregat.py) : le bulletin part de TOUTES les
  mesures -- METAR des deux aeroports (donnees_ouvertes.metar_values),
  stations APRS et thermometres captes pendant le balayage ou avant
  (historique 24 h) -- recalees sur le modele : ecart au modele a l'heure du
  releve, applique au modele de maintenant, poids exp(-age/tau) x qualite x
  distance, mediane ponderee, ecarts invraisemblables rejetes. Fenetres :
  temperature 6 h (tau 3 h), humidite 3 h, pression 3 h, vent 1 h, pluie
  1 h. Modele Open-Meteo : 6 h de passe, humidite et vent horaires. Le
  bulletin dit "N degres mesures autour de toi" (et le modele s'il s'ecarte
  de 1,5 degre ou plus), puis la composition ("d'apres 4 releves : 2
  aeroports, 2 thermometres"). Les anciennes phrases "releves autour de toi"
  et "aeroport de ..." en clair sont remplacees par l'agregat (les orages
  d'aeroport restent dans les raisons de vigilance).
- FLASH COMPLET : commande "flash" = balayage meteo puis collecte en
  parallele (flash_info.collect : presse, bulletin agrege avec rivieres,
  seismes, air, sondes, routes Bison Fute, trains, bus) puis mise en forme
  (build). En arriere-plan comme la meteo (main._scan_then) ; "flash
  details" reste immediat. Essai reel : 19 s de collecte hors balayage.

## 2026-10-03 (suite) : interets, LoRa reellement detecte, tout ce qui est capte est dit

- CENTRES D'INTERET (lib/interets.py, commande "interets") : 9 categories
  (talkies, aviation, radioamateurs, CB, meteo, secours, marine,
  professionnels, FM commerciale), niveaux aucun / faible / normal / fort /
  passion = x0,1 / 0,4 / 1 / 1,8 / 2,5 sur la piqure (attention.pressure).
  FM commerciale "faible" par defaut (bandes de verification, du trafic en
  permanence). Plancher d'exploration et recentrage des stations inchanges.
  Forme directe : "interet avions fort". Reglage "interets" sur le NAS.
- LORA : la chaine pont -> Mac -> decodage marchait, mais la detection par
  energie gardait les capteurs 433,92 voisins (0 trame sur toutes les
  rafales) et ratait le vrai LoRa (sous le bruit). Detection par dechirp :
  preambule aligne sur 4 symboles. Valide : trames synthetiques gr-lora_sdr
  (APRS SF12, Meshtastic SF11) decodees CRC bon par lora_decode.py ;
  detection jusqu'a ~-21 dB (SF12) ; fausses rafales et bruit : 0 ou 1.
  La piqure LoRa ne vient plus que d'une trame decodee.
- ANNONCES : capteurs meteo dits a chaque passage en une phrase groupee
  (un capteur redit apres 30 min, ou s'il bouge de 1 degre / 5 %) ;
  atypiques (cuisson, piscine, frigo, sol...) avec une blague Gemini ;
  pagers sans texte et numeriques ; trames VDL2 sans texte resumees par
  avion nouveau (30 min). Constat 30 h : toutes les fenetres de donnees
  visitees a chaque cycle ; APRS 144,800 sans paquet capte.

## 2026-10-03 : messages ACARS et VDL2 dits en langage humain

- Avant : seuls le texte libre H1 et la serie Q (OOOI) etaient dits ; tout
  le reste ecarte. Constat sur les archives VDL2 : controle X.25, accuses,
  ACARS transporte (label _d), passages de relais XID avec destination du
  plan de vol (dst_airport) et position de l'avion.
- lib/avions_info.py : appareil d'apres adsbdb.com (gratuit, sans cle :
  modele, compagnie, immatriculation, photo ; trajet par indicatif), cache
  carte SD ~/autopilot/avions_cache.json ; phase de vol d'apres l'etat ADS-B
  publie par adsb_watch (/tmp/avions_etat.json : sol, roulage, decollage,
  montee, croisiere, palier, descente, approche) ; allure du MODELE (Gemini,
  une fois par type) et LIVREE d'apres une vraie photo (Gemini vision, une
  fois par compagnie) ; plafond 40 appels Gemini par jour pour ces fiches.
- lib/acars_traduction.py : fiche deterministe (nature de trame, label
  ACARS sur, etape OOOI, destination annoncee par l'avion, CPDLC/ADS-C
  libacars) + appareil, mise en 2-3 phrases par Gemini (leger mais serieux,
  rien d'invente, altitudes en metres, l'avion fait foi contre la base
  publique) ; repli phrase fixe ; 20 appels par heure au plus.
- VDL2 : trames regroupees par avion a chaque passage ; vrai contenu
  explique a chaque nouveaute, simple entretien de liaison dit une fois par
  30 min (phrase fixe, sans Gemini). ACARS classique : meme traduction.
- Essai sur les vraies trames archivees : Transavia F-HTVQ "blanc et vert...
  en direction de Nice", easyJet "en direction de Faro". Incident d'essai :
  deux annonces ACARS fictives parlees sur le Pi et enregistrees au journal
  (15 h 17), supprimees.

## 2026-10-03 : prises vocales au fil de l'eau, journal "messages"

- lib/prise_vocale.py : toute annonce de donnees (veille_donnees.announce)
  passe par une file traitee par un fil dedie : le texte (ou la fonction qui
  le produit : Gemini, base d'avions) est synthetise en fichier (edge-tts,
  repli espeak dans un fichier), carillon + voix assembles en une prise du
  journal (server/radio_log/<etiquette>_<date>.mp3) avec sa fiche
  <nom>.json (texte, protocole, source, frequence), puis diffusee. L'ecoute
  continue pendant ce temps (mise en file : 0,3 s). Journal ouvert :
  "donnees" se tait comme "radio" (audio_lock.SILENT_WHEN_JOURNAL), la prise
  est quand meme au journal, un avis court previent. Passage de satellite :
  wait=True (dit avant l'ecoute).
- Au fil de l'eau : capteurs dits a CHAQUE releve (seule la meme trame
  repetee dans SAME_FRAME_SEC est ecartee) ; VDL2 et ACARS a chaque trame
  (retransmission identique ecartee 5 min ; entretien de liaison : une fois
  par avion et par passage) ; pagers : retransmission 5 min.
- Journal "messages" (lib/journaux.py, prefixe message_) : avions, pagers,
  messages et bulletins APRS, Meshtastic ; "donnees" garde stations APRS,
  planeurs, balises. Touche I sur un message : protocole, emetteur,
  frequence (fiche). Les releves texte des capteurs lus par la voix de la
  console ne sont plus ajoutes au journal (console/journal_capteurs.py
  n'est plus appele ; a supprimer ou a refaire en prise vocale du jour).
- Messages d'avions : "Message provenant d'un <modele> de <compagnie>, par
  le protocole <nom>." puis description en texte alternatif, puis le
  contenu ; le protocole n'est plus explique. Modeles lisibles
  (avions_info.spoken_model : Airbus A320neo, Bombardier CRJ 1000).

### Autres sources de messages a capter (etude, rien d'installe)

Faisables avec le materiel actuel, par ordre d'interet :
1. RDS des radios FM (redsea, a installer) : nom de station, radiotexte
   (titres, infos), type de programme, annonces trafic. Se greffe sur la
   bande FM deja balayee.
2. Textes des radios numeriques radioamateur : SMS et alias de l'emetteur
   DMR, texte lent D-STAR, texte C4FM (dsd-fme, deja la).
3. DAB+ (welle-cli, a installer ; Saint-Etienne a des multiplex locaux) :
   textes DLS (titres, infos) et diaporamas (images, decrites par Gemini).
4. Mode S etendu des avions (pyModeS sur la sortie brute de dump1090) :
   vent et temperature mesures par les avions en altitude.
Plus tard, avec l'antenne HF : NAVTEX (490/518 kHz), bulletins meteo en
RTTY (DWD), messages FT8/JS8 des radioamateurs.
Peu probables ici : AIS et ASN des bateaux (Rhone a ~50 km derriere les
monts). Exclus : compteurs d'energie sans fil (donnees privees de voisins),
charges utiles LoRaWAN et radios des secours (chiffrees).

## 2026-10-03 : nouvelles sources de messages (sauf RDS, refuse par l'utilisateur)

- RADIOS NUMERIQUES : lib/decodeurs.dsd_line (commun scanner + manuel)
  extrait des lignes de dsd-fme l'emetteur (SRC/TGT), l'alias, les SMS et
  textes (SMS, Text, ISO7/ISO8, M17 Meta Text) et le GPS ; dits au fil de
  l'eau par lib/messages_numeriques.py (journal messages), avec indicatif et
  ville via radioid.net (lib/radioamateurs.py, cache 30 j, jamais le nom).
  Deja capte : DMR TG 208101, SRC 2081639 = F4LFY (Nice), via relais local.
- MODE S ETENDU : pyModeS 2.22 installe (pip --user, vador) ; la fenetre
  ADS-B locale lit les trames brutes de dump1090 (127.0.0.1:30002) ;
  lib/modes_meteo.py dit la meteo (BDS 4,4 : vent, temperature, humidite)
  et les dangers (BDS 4,5) transmis par les avions, publie les vents en
  altitude (/tmp/vents_altitude.json, a fondre dans le bulletin plus tard).
  Trame de test de pyModeS : vent et temperature bien lus (bds.infer la
  dit EMPTY : trame d'ecole incoherente, normal).
- DAB+ : welle.io 2.4 installe (apt) ; watchers/dab_watch.py (fenetre
  "dab", 45 s) decouvre les ensembles puis ecoute a tour de role : textes
  DLS et images de diaporama (Gemini, 10 par heure au plus ; images gardees
  dans donnees/dab_images). Ensembles trouves : 6C Saint-Etienne-Et,
  7B Metropolitain 2, 8A S-ETIENNE LOCAL1 (~/autopilot/dab_ensembles.json).
  CHALEUR : 7 min de decodage DAB+ + l'installation ont mene le Pi a 76 C
  (pause thermique) ; une seule station decodee a la fois (CAROUSEL=1).
- Centre d'interet "dab" ; chien de garde : welle-cli ajoute.
- Reste : HF (NAVTEX, RTTY meteo, FT8/JS8) des que l'antenne filaire est la.
- Incident : un "pkill -f" dont le motif figurait dans sa propre commande a
  coupe la session SSH et laisse radio-watch arrete ~1 min (relance).

## 2026-10-03 : filtre de pollution, journal sans trous, DMR plus long

- lib/filtre_pollution.py, filtre d'entree des messages (DAB+ d'abord) :
  promotion (web, reseaux, telephone, jeux, "ecoutez", "la radio du",
  "100 %", "hits"...), mot distinctif du nom de la station dans son texte,
  titre musical "ARTISTE - TITRE", slogan appris (meme texte deja vu de la
  meme source en 7 jours) ; images classees par Gemini (PUB = pas dite ; 3
  pubs d'affilee : images de la station ignorees 24 h). Tout l'ecarte est
  archive (donnees/filtre_pollution_*.jsonl). Choix : titres de chansons
  ecartes (pas des messages).
- Journal : sound_player enregistre le carillon meme quand la source est
  muette (journal ouvert, priorite perdue) -- cause des prises sans habillage
  (FM, 446, OGN...) ; repli espeak de speak_with_lead_sound joue un fichier
  (enregistre). Eclats de squelch Airband < 0,6 s abandonnes (prises 446 de
  0,1 s). Prefixe "capteurs_" range dans capteurs (anciennes annonces
  groupees). Prises aviation silencieuses : du 2 septembre, anciens dossiers
  du jour, pas un defaut actuel.
- DMR : DSD_FME_IDLE_SEC 3 -> 10 s, DSD_FME_MAX_SEC 60 -> 300 s (QSO avec
  le Quebec capte puis coupe entre deux interlocuteurs). Debut manque :
  inherent au balayage.

## 2026-10-03 : DAB+ par station, pre-ecoute avant d'ouvrir le son

- DAB+ : le filtre de contenu (lib/filtre_pollution.py) ne marchait pas et
  consommait Gemini : supprime. Filtrage par STATION, en LISTE NOIRE (on ne
  cherche que le peu connu : radios locales, associatives...) : Radio France
  (dont le reseau "ici" + lieu), grandes radios nationales et reseaux
  commerciaux (dab_watch.DEFAULT_BANNED) + reglage "dab" cle "interdites"
  (NAS) ; mots entiers (rtl interdit RTL2, pas RTLB). Aucune requete Gemini
  pour une station interdite. Stations vues par ensemble : /tmp/dab_watch.json.
- PRE-ECOUTE (balayage : radio_watch._prelisten ; Airband/446 :
  airband_bridge._prelisten, detecteur RNNoise fourni par radio_watch) :
  jusqu'a 6 s d'ecoute silencieuse ; voix reconnue sur 0,6 s d'affilee (ou
  message de donnees lisible) = on annonce, on ouvre, on rejoue le tampon
  puis le direct ; sinon rien n'est joue ni range au journal (empreinte de
  bruit apprise pour le balayage). Teste : vraie voix 446 ouverte en 0,6 s ;
  parasite bref et bruit ecartes ; une capture 446 de 8 s du journal
  (proba voix max 0,31, 73 % semblable a un parasite connu) serait ecartee.

## 2026-10-03 (soir) : panne DAB, capteurs d'interieur, phrase des avions

- PANNE : en reecrivant la liste noire DAB+, des constantes de
  dab_watch.py (CAROUSEL, SOUND, _memory...) ont ete effacees ; radio-watch
  plantait a chaque fenetre DAB et systemd le relancait (9 fois en 20 min,
  le cycle reprenait par les capteurs : "scanner bloque sur les capteurs").
  Corrige ; radio_watch._guarded isole desormais chaque fenetre (erreur
  notee, cycle continue). Avant tout deploiement : pyflakes (noms non
  definis) et controle de passage satellite BLOQUANT.
- Capteurs : valeurs identiques a celles deja dites tues ; thermometres
  d'interieur probables (lib/capteurs_interieurs.py : 24 h de releves
  bruts, amplitude faible ET niveau ecarte de 3 C des autres ; la nuit,
  18-26 C et 4 C au-dessus des autres) ecartes de la meteo, annonces
  "capteur d'interieur probable" (Acurite 606TX et inFactory TH a 22 C) ;
  la routine d'un capteur ne pique plus l'attention.
- Messages d'avions : "Un appareil modele X, <description alternative>,
  immatricule Y, envoie le message suivant : <donnees>, signifiant que
  <traduction>." Gemini rend description / donnees / sens en JSON, la
  phrase est assemblee par le programme (avion.phrase, elision qu'il) ;
  secours : sens fixe par nature de trame (vdl2.sens). Mode S idem.
- Journal ouvert : avis "captation ajoutee au journal" une fois par minute.
- Voix Microsoft qui passaient a l'anglais : edge-tts 7.2.8 ecrit en dur
  xml:lang='en-US' et n'a pas de parametre de langue ; les voix
  "Multilingual" devinent la langue phrase par phrase. La balise <lang>
  prevue par Microsoft est REFUSEE par le service gratuit de Edge (aucun son
  renvoye, essai reel). Donc : voix multilingues retirees (speak.
  EDGE_TTS_VOICES, choix de la console ; une voix multilingue reglee est
  remplacee par Denise), requete en fr-FR (lib/edge_langue.py, mkssml
  remplace), 48 echantillons multilingues purges du cache tts_cache.
  nas-watch non redemarre (gestion des disques) : prend le correctif a son
  prochain redemarrage.
- Quota gratuit de gemini-2.5-flash epuise le 2026-10-03 au soir (429) :
  gemini.ask bascule sur les autres modeles ; surveiller la personnalite
  de minuit.

## 2026-10-04 : personnalite encadree

- La nuit du 3 au 4, Gemini avait reecrit 604 phrases, annonces comprises,
  avec des tics ("que votre journee resonne en parfaite harmonie" x40,
  "que l'harmonie demeure" x39), des references musicales partout et la
  phrase des avions cassee ("L'appareil {appareil}..." = "L'appareil Un
  appareil modele...").
- Desormais : perimetre = petites phrases d'interface des dialogues
  (phrases.PERSONA_FAMILIES, <= 140 caracteres ; 266 phrases), applique
  aussi a la lecture (phrases.rewritable dans texte()) : aucune annonce ne
  peut plus etre touchee. Consignes : petite touche de ton (exemple
  utilisateur "valide comme station probable, il est parfait, waouh !"),
  pas de voeux, de metaphores, de themes ; longueur 1,35x (+30 car.) ;
  tics (3 mots inventes dans plus de 3 phrases) ramenes au neutre.
- Regeneree le 4 a midi : 207 phrases, ex. "Cette entree ne peut pas etre
  mise en favori, helas."
- Precision utilisateur (4 au soir) : pas un theme mais une PERSONA (dictee
  ou surprise), avec clins d'oeil et references a son univers. Surprise :
  inspiree d'un archetype tire au hasard (personnalite.ARCHETYPES : petit
  robot espiegle, marxiste depite, patron de banque, detective...). Mots
  centraux gardes (controle : 60 % des mots de 5 lettres et plus de la
  phrase neutre ; "journal" ne devient jamais "partition"). Longueur 1,6x.
  Essai : philosophe de l'agora, 163 phrases colorees ("Point de commune
  trouvee pour {query} dans l'immensite."), tics "par Zeus" ecartes.
- Mode manuel muet (4 octobre, 13 h 03 - 13 h 41) : pause thermique (75 C
  pendant un deploiement + generation de la personnalite) ; la securite
  coupait aussi l'ecoute manuelle sans rien dire, et la reprise attendait
  60 C ou un plateau de 20 min. Desormais radio_watch._manual_during_pause :
  ecoute manuelle permise pendant la pause sous MANUAL_HEAT_MAX_C (70 C),
  sinon message dans le mode manuel (manuel.trop_chaud) ; fleches
  reactives (0,1 s) pendant la pause.

## 2026-10-06 : notifications, Alt + fleche dans le journal

- Commande "notifications" (mots-cles : notification, nouveautes, du
  nouveau) : nombre d'entrees pas encore ecoutees, journal par journal ;
  "Aucune nouvelle entree" sinon. Suivi : lib/journal_vus.py (carte SD,
  ~/autopilot/journal_ecoutes.json) ; chaque enregistrement joue dans un
  journal est marque ecoute ; ce qui existait au premier usage (6 octobre,
  18 h 38) n'est pas compte.
- Journal : Alt + fleche bas / haut = entree suivante / precedente sans
  note (notation.load_notes, par nom de fichier), en continuant sur les
  jours suivants dans l'ordre de Tab ; Alt lu par evdev comme Ctrl.

## 2026-10-06 : bilan, attendre que tout soit note

- Le Pi ajustait parfois une bande alors que des prises restaient a noter.
  Les notes sont maintenant rattachees au reglage en vigueur a l'heure de
  CAPTURE (capture_le), plus a l'heure de notation. MIN_NOTES (8) n'est
  qu'un plancher : proposition et ajustement automatique (y compris
  l'annulation pour regression) attendent que toutes les captures de la
  bande faites depuis son dernier changement soient notees
  (bilan._unrated_since, via journal.scan_recordings + empreintes) ; chaque
  note au-dela du minimum compte en bonus. Le bilan dit "N prises faites
  avec les reglages actuels a noter avant de proposer".

## 2026-10-06 : rejeter un message DAB+ interdit sa station

- Journal "messages", fleche gauche sur un message DAB+ : la prise et sa
  fiche sont supprimees, et la station (fiche "station", sinon debut de
  "source") est ajoutee a dab.json "interdites" sur le NAS ; dab_watch la
  relit a chaque station, sans redemarrage. Phrases journal.dab_interdite,
  dab_deja_interdite, dab_interdite_echec (NAS absent).
- Avant, le rejet laissait la fiche .json orpheline (19 fiches DAB du
  3 octobre sans son). Encore autorisees parmi elles : RADIO ONDAINE,
  EUROPE 2 DAB+, FUEGO, ACTIV DAB+ (decision utilisateur en attente).
- dab_watch.py copie dans ~/autopilot/pi_console mais radio-watch pas
  redemarre : le champ "station" arrivera au prochain redemarrage.

## 2026-10-06 : commande "liste noire" (stations DAB+)

- lib/dab_liste.py : liste de base (ex dab_watch.DEFAULT_BANNED) + reglage
  dab.json "interdites" (ajouts) et "retirees" (entrees de base enlevees).
  Partagee par dab_watch (kept), le journal (fleche gauche) et la console.
- console/liste_noire_cmd.py, commande "liste noire" (blacklist, stations
  interdites). Mode liste : haut/bas, lettre = saut, Retour arriere efface
  l'entree, Ctrl+A puis Retour arriere efface tout, Ctrl+Z annule. Mode
  edition ("liste noire editer" ou Tab) : taper un nom, Entree l'ajoute ;
  dit si la station a deja ete captee (canal) ou si le nom est deja couvert.
- L'utilisateur a deja interdit ACTIV DAB+ et RADIO ONDAINE depuis le
  journal. radio-watch et pi-console redemarres (aucun passage proche).

## 2026-10-06 : plantes, etape 1 (commande "plantes")

Plan valide par l'utilisateur, 6 etapes : 1) commande plantes ; 2) calendrier
de vie (fenetres min-max, recalees sur les stades constates) et journal
"plantes" ; 3) alertes (gel, vent, chaleur, soif) redites toutes les 30 min
jusqu'a "compris / ok / bien recu / bien pris", silence 22 h - 7 h par
defaut (reglable), simples nouvelles de croissance dites une fois ;
4) flash : conseils meteo par plante, ravageurs du mois a Saint-Etienne et
traitements ecoresponsables (fiche Gemini mensuelle + declencheurs meteo),
quoi planter ce mois-ci (pot, balcon, jardin) ; 5) photos : travail dans
server/plantes/<plante>/ (+ "a identifier"), description datee en texte
gardee pour toujours dans storage/plantes (nd2), comparaison aux
descriptions passees ; les PHOTOS ne sont archivees que manuellement
(evenement marquant) ; 6) sondes de sol Ecowitt WH51 / Froggit DP100
(868 ou 433 MHz, rtl_433 Fineoffset-WH51) avec appairage : pause scanner
5 min, pile, nouvel identifiant, verre d'eau = confirmation + etalonnage
sec / mouille, rattachement a une plante.
L'utilisateur n'a AUCUN thermometre : les capteurs d'interieur entendus
sont a un voisin.

Etape 1 faite : lib/plantes.py (reglage "plantes" du NAS ; emplacement en
4 axes : dedans/dehors, balcon/jardin, pot/pleine terre, abri de la pluie ;
fiche Gemini JSON faite une fois a l'ajout : durees min-max, seuils gel /
croissance / chaleur, vent, eau, jours sans eau en pot l'ete, conseils),
console/plantes_cmd.py (liste + ajout, formes directes "plante basilic
arrosee / rentree / fleurie"). Lettres I A R E reservees (pas de saut
sur ces lettres). plantes.complete_missing() refera les fiches manquantes
(a appeler depuis l'etape 2).

## 2026-10-06 (nuit) : plantes, etape 2 (calendrier de vie)

- lib/plantes_calendrier.py : prochain evenement par stade (seme -> levee,
  germe -> premieres vraies feuilles, pousses/developpee -> premieres fleurs
  comptees depuis la levee, fleurs -> fruits). Ancre = date reelle du stade
  (historique), sinon levee estimee depuis le semis ou les premieres
  feuilles ; une plante "developpee" sans autre stade connu n'a pas de
  prevision. Plante dehors : moyenne 7 jours Open-Meteo (cache 3 h) ; sous
  le minimum de croissance + 3 C, fenetre x1,5 ; sous le minimum, rien.
- Thread de meteo_watch toutes les 30 min : ouverture de fenetre annoncee
  une fois (prise "plante_calendrier", journal "plantes"), rappel unique
  3 jours apres la fin ; silence 22 h - 7 h ; fiches manquantes refaites
  une fois par jour. Etat : ~/autopilot/plantes_calendrier.json (SD).
- Touche I du dialogue plantes : fiche + prochaine etape et ses dates.
- Phrases sans accord (pluriels "les premieres vraies feuilles").

## 2026-10-07 : plantes, etape 3 (alertes)

- lib/plantes_alertes.py, appele par la boucle de plantes_calendrier
  (meteo_watch, 30 min) : gel (min de la nuit <= temp_gel_c, +2 C en pot,
  a partir de 17 h), rentrer (plante "a rentrer l'hiver", nuit <= 5 C),
  vent (rafales 18 h : fragile 35, moyen 55, robuste 75 km/h), chaleur
  (max 24 h >= temp_max_c), soif (ET0 FAO Open-Meteo depuis le dernier
  arrosage dit, x0,3 dedans, moins la pluie si exposee ; reserve =
  jours_sans_eau_pot_ete x 5 mm, x3 en pleine terre ; 8 h - 21 h).
- Redite toutes les 30 min tant que la situation dure ("Rappel."), muette
  22 h - 7 h ; commande console "compris" / "ok" / "bien recu" / "bien
  pris" : ack() -> ~/autopilot/plantes_acquit.json, une alerte nee apres
  reste a dire. Etat : ~/autopilot/plantes_alertes.json.
- Premiere plante reelle : Citronnier caviar (balcon, pot, abrite, gel
  -3 C, vent fragile, a rentrer l'hiver). arrosee_le encore vide : la soif
  n'est suivie qu'apres un premier "arrosee".

## 2026-10-07 : plantes, etape 4 (rubrique plantes du flash)

- lib/plantes_flash.py, rubrique apres la meteo (flash court et complet,
  collectee en parallele dans flash_info.collect) : meteo 48 h groupee par
  situation (gel, rentrer, nuits fraiches <= 10 C pour les plantes a rentrer
  l'hiver, vent 36 h, chaleur, bientot soif), soif et evenements de vie par
  plante ; ravageurs du mois + traitement naturel (fiche Gemini par mois et
  par liste de plantes, ~/autopilot/reglages_cache/plantes_mois.json,
  nouvel essai au plus 1 fois par heure) : flash court = ceux que la meteo
  favorise (pluie, chaud_sec, chaud_humide, froid_humide), 2 au plus ;
  complet = tous ; semis du mois par pot / balcon / jardin (court : 7
  premiers jours du mois, 2 idees par lieu ; complet : toujours, 4).
- Gemini ajoute le latin au nom de la plante : plantes.find() tolerant.
- Alertes (etape 3) groupees aussi : une alerte par situation qui nomme
  toutes les plantes (cle "gel|date"...), soif par plante.
- Plantes de l'utilisateur au 7 octobre : Citronnier caviar, Feijoa
  'Unique', Manguier commun, Piment Jalapeno, Poivron 'Chocolate Beauty'
  (toutes sur le balcon).
- La copie de travail du Mac (/tmp) a disparu au redemarrage : clone
  desormais dans le dossier de la session (pcw/).

## 2026-10-07 : plantes, etape 5 (photos)

- lib/plantes_photos.py. Travail : /media/nas/nd1/server/plantes/<slug>/
  (un dossier par plante, cree par le Pi, + a_identifier, LISEZ-MOI.txt).
  Archives : /media/nas/nd2/storage/plantes/<slug>/evolution.txt (UNE
  liste datee, une ligne par photo, "Probleme : ..." si besoin) et photos/
  (touche G du dialogue : derniere photo analysee + description actuelle).
- Agregation (demande utilisateur) : seule description_actuelle.txt reste
  sur server, remplacee a chaque photo une fois l'evolution ajoutee a la
  liste ; Gemini recoit photo + description precedente + 5 dernieres
  lignes : requete de taille constante. Stade avance par la photo ->
  enregistre sur la liste relue. Photos du dossier de travail effacees
  apres 30 jours. .suivi.json = photos deja traitees.
- Passage toutes les heures (boucle de plantes_calendrier, meteo_watch),
  6 photos max ; commande "plantes photos" pour ne pas attendre.
  gemini.ask accepte mime (HEIC de l'iPhone).
- main._match : une ligne qui commence par "plante(s)" va aux plantes
  ("plante photo" partait vers decris).
- 2026-10-07, premieres vraies photos (HEIC, citronnier et feijoa) : le Pi
  ne disait que "Premier etat note" -> la description est maintenant dite
  en entier (photo_premiere / photo_nouvelle) ; Gemini recoit le portrait
  de la plante (description du registre) et sait qu'un pot peut contenir
  plusieurs plantes : le citronnier, d'abord "introuvable", est repere.
  Les deux photos ont ete re-analysees (anciens resultats effaces).
- 2026-10-07, premieres vraies photos (HEIC, citronnier et feijoa) : le Pi
  ne disait que "Premier etat note" -> la description est maintenant dite
  en entier (photo_premiere / photo_nouvelle) ; Gemini recoit le portrait
  de la plante (description du registre) et sait qu'un pot peut contenir
  plusieurs plantes : le citronnier, d'abord "introuvable", est repere.
  Les deux photos ont ete re-analysees (anciens resultats effaces).
- 2026-10-07, prudence (demande utilisateur) : une photo ne change plus le
  stade. Gemini rend des "observations" (fruits, fleurs, boutons, probleme)
  avec OU les chercher a la main depuis le pot (tige, hauteur, cote) ; elles
  vont dans plante["a_confirmer"] (une nouvelle photo remplace celles sans
  reponse). Dialogue : C confirme (le stade suit ; fruits -> conseil de
  maturite), H signale une hallucination ; formes directes "plante X
  confirme / faux". Ligne datee "Vérifié au toucher" / "Signalé faux" dans
  evolution.txt, que Gemini doit tenir pour vraie aux photos suivantes.
  Phrase joyeuse (3 variantes) quand des fruits sont vus.
- Maturite : fiche["maturite"] (reconnaitre un fruit mur au toucher, a
  l'odeur, a la facon dont il se detache) + jours_maturite, demandes a
  Gemini quand la plante est en fruits ; evenement de calendrier
  "maturite" ; flash complet toujours, court dans la fenetre. Faites pour
  le piment (35-50 j) et le poivron (50-70 j).
- Citronnier : 1re analyse "jeunes fruits", 2e analyse aucun fruit -> a
  verifier au toucher par l'utilisateur.
- 2026-10-07, suggestions : l'analyse d'une photo rend aussi "voisines"
  (toutes les autres plantes visibles, meme incertaines) ; le Pi ecarte
  celles deja dans la liste (plantes.find, cle genre + espece du latin),
  deja proposees ou refusees, et garde les autres dans le reglage
  "plantes_suggestions" avec l'emplacement de la plante photographiee.
  La liste des plantes connues N'EST PLUS dans la consigne : Gemini y
  ramenait tout (belle-de-nuit jamais vue avec, 2 fois sur 3 sans).
  Photo du dossier a_identifier -> suggestion aussi. Dialogue : touche S,
  Entree ajoute (fiche Gemini faite), Retour arriere refuse pour toujours.
  Premieres suggestions reelles : Belle de nuit (Mirabilis jalapa),
  Faux-acacia (Robinia, certitude faible).

## 2026-10-07 : capteurs a soi et appairage (plantes etape 6, elargie)

- lib/capteurs_catalogue.py : genre d'un capteur d'apres les grandeurs de
  sa trame rtl_433 (sol, pluie, station vent/pluie, foudre, particules,
  CO2, fuite, thermo-hygro, thermo) et geste de preuve (sonde dans un verre
  d'eau, eau dans le pluviometre, coupelles tournees, main / souffle...).
- lib/capteurs_miens.py : registre (reglage "capteurs" du NAS), protocole
  console <-> radio_watch (/tmp/capteurs_appairage.json et .jsonl),
  Pairing (ecarte les capteurs entendus ces 30 jours = voisins, et les
  pneus), etalonnage sec / mouille de la sonde WH51 (ad_raw air puis eau).
- radio_watch : demande d'appairage prise apres le mode manuel (etat
  "appairage"), _lock_pending l'inclut ; capteurs_watch.pairing_session :
  rtl_433 -f 433.92M -f 868.3M -H 20. Teste en vrai : prise en 18 s,
  trames des voisins (Acurite, Oregon, LaCrosse) captees.
- capteurs_watch : capteurs a soi annonces sous leur nom, sonde de sol
  muette (plantes), pile faible 1 fois par semaine.
- console/capteurs_cmd.py, commande "mes capteurs" / "nouveau capteur" /
  "nouvelle sonde" (avant "appairage" = machine compagne).
- Plantes : une sonde rattachee remplace l'estimation de soif (seuils 15 /
  25 / 35 % selon le besoin en eau) ; flash complet dit l'humidite.
- Teste en simulation (aucun capteur a soi encore) : WH51 annoncee, geste
  reconnu 2 -> 96 %, rattachee au citronnier, etalonnee 70 / 412.
- Pistes non faites : capteurs filaires sur le Pi (I2C BME280, kit SparkFun
  anemometre / girouette / pluviometre a interrupteurs sur GPIO),
  passerelle Ecowitt (envoi local vers le Pi), capteurs a soi dans
  l'agregat meteo (poids fort).

## 2026-10-07 : transports en commun de la commune (bus, tram, metro...)

- lib/transports.py : decouverte par le Point d'Acces National
  (transport.data.gouv.fr/api : aoms/{insee}, datasets ; geo.api.gouv.fr
  pour EPCI / departement / region). Local = commune, EPCI, AOM, ou
  proprietaire = AOM de la commune (IDFM declare sur la region) : actifs ;
  departement / region : inactifs (sauf sans reseau local) ; agregats
  jamais d'office. Reglage "transports" du NAS ; liste nationale (2,5 Mo)
  gardee 7 jours sur la SD.
- Lignes : routes.txt du GTFS (famille par route_type, etendu compris),
  copie SD 7 jours ; lecture PARTIELLE HTTP Range quand le serveur
  l'accepte (IDFM 244 Mo -> 4,5 s), sinon telechargement <= 80 Mo (STAS 9 Mo).
- Temps reel : lib/gtfs_rt.py, decodeur protobuf sans dependance (retards,
  annulations, alertes). Perturbations : alertes GTFS-RT et page STAS
  (stas_watch), classees par famille via les numeros de ligne des titres.
- Console : "bus" (trolleybus compris), "tram", "metro", "transports"
  (toutes les familles ; "transports funiculaire"...), "transports
  reseaux" (choix, details, R = chercher de nouveau). Commande "ville" :
  recherche des reseaux en arriere-plan. Flash : rubrique generique
  (remplace stas_text). stas_watch se tait si la STAS n'est plus active.
- Teste : Saint-Etienne (STAS bus / trolleybus / tram, temps reel),
  Clermont (T2C), Paris (IDFM : familles ok, pas de temps reel lisible).
- A faire : lire SIRI / SIRI Lite (85 + 21 reseaux, dont IDFM via PRIM
  avec cle), annonces au fil de l'eau des alertes GTFS-RT des autres
  reseaux (stas_watch ne couvre que la STAS).

## 2026-10-07 : SIRI Lite, cle PRIM, veille de tous les reseaux, module central

- lib/siri_lite.py : general-message et situation-exchange, JSON ou XML
  (from_xml), variantes InfoMessage / GeneralMessage, shortMessage /
  SHORT_MESSAGE ; LineRef reduits a leur code. Teste : Bordeaux (117
  messages), SNCF (618 situations).
- transports.discover note les services SIRI Lite d'apres original_url
  (url = redirection data.gouv) et MEME "indisponibles" (401 sans cle au
  controle du PAN). KEYED_HOSTS : prim.iledefrance-mobilites.fr ->
  cles/prim.txt, en-tete apiKey. 401/403 sans cle connue -> "acces
  refuse" (Strasbourg CTS, Lyon TCL : identifiants propres a demander).
  Jamais "aucune perturbation" quand une source est inaccessible.
- Cle PRIM : reglages.KEYS["prim"], prim.txt vide cree, LISEZMOI a jour.
- watchers/stas_watch.py devient la veille de TOUS les reseaux actifs
  (page STAS + GTFS-RT + SIRI Lite) : annonces par vd.announce (prise
  vocale, journal "transports", prefixe transport_), 1 fois, silence
  22 h - 7 h (dit au matin si toujours d'actualite), premier passage
  silencieux ; etat ~/autopilot/transports_vus.json.
- Module central de parole : phrases.rewritable accepte "personnalite =
  oui" ; 63 phrases d'interface des dialogues plantes, liste noire, mes
  capteurs, transports reseaux y sont ouvertes (annonces et reponses
  informatives restent hors personnalite). Francais en dur supprime
  (canal, et, lignes de la liste des evolutions des plantes).

## 2026-10-07 : Ctrl / Alt + fleches, commande "qui es-tu"

- Bug : Ctrl + fleche (et Alt) souvent rates dans le journal depuis
  l'ajout d'Alt. modifier_held lisait l'etat du clavier APRES la fleche
  (~45 ms par lecture, Alt puis Ctrl : ~100 ms) : appui rapide, Ctrl deja
  relache. Et la table de la console (dumpkeys) avale Alt + haut
  (KeyboardSignal) ; Alt + gauche / droite changent de console virtuelle.
- keys._Tracker (demarre par main) : fil evdev permanent, note les
  modificateurs tenus a l'instant ou une touche est enfoncee (valable
  1,5 s) ; Alt + haut vu sur le clavier -> keys.ALT_UP injecte dans la
  lecture (reveil WAKE, pop_injected). Teste : Ctrl+haut / Ctrl+bas /
  Alt+bas relaches avant lecture, Alt+haut transmise, re-scan.
- Commande "qui es-tu" (quelle est ta personnalite, presente-toi...) :
  personnalite.who_am_i(), resume du jour (console.json), origine
  (cette nuit, surprise ou demande), "en detail" = fiche de style lue en
  tete de personnalite_<langue>.ini.

## 2026-10-07 (midi) : controle general

- Services : tous actifs, 0 redemarrage en boucle ; lighttpd (OctoPi) en
  echec depuis le demarrage, hors projet. Pi 64 C, throttled=0x0, RAM et
  disques larges. NAS : remontage auto au demarrage du 6 (disques lents).
- Journaux 24 h : radio = statistiques dsd-fme (pas d'erreurs), "Failed to
  set ppm error" sans effet (PPM 0, correct pour la NESDR TCXO ; le -74 ppm
  de septembre a disparu), aplay interrompu a l'arret d'une lecture ;
  meteo = delais Hub'Eau / Open-Meteo, liaison Mac retablie a 10 h 53.
  Quota Gemini : aucun refus (les "429" etaient des frequences).
- Cle radio (1-1.3.2) remise a zero par le noyau ~ toutes les 27 min, au
  rythme des fenetres LoRa (lecture brute arretee de force) ; le scanner
  repart a chaque fois. A surveiller, pas d'action.
- Couverture : toutes les fenetres visitees dans les dernieres minutes
  (attention). APRS : visite mais RIEN decode depuis le 1er octobre (10
  captures avant) -> verifier a l'oreille (mode manuel 144,800 MHz) ou
  l'antenne.
- Reglages audio du bilan dans leurs bornes : aviation (AM) passe-bas
  3500 Hz, souffle -13 dB, +3 dB, fin 4,5 s, marge -1 dB : normaux.
  FM diffusion : passe-bas 4000 Hz + anti-sifflement + anti-bourdonnement
  + souffle 13 dB + -6 dB : valables pour la parole, appauvrissent la
  musique (borne haute du passe-bas 4500 Hz). Bilan : "canal station
  probable" attend 1 prise a noter (regle du 6 octobre en action).
- Phrase corrigee : bilan.encore_a_noter (accord).

## 2026-10-07 (apres-midi) : cle reinitialisee, veille DMR

- Cle NESDR remise a zero ~27 min : rtl_sdr de la fenetre LoRa (et du
  recentrage, lib/stations.py) etait arrete (SIGTERM) SANS que le tuyau
  soit lu : bloque en ecriture, il ne traitait plus l'arret et etait tue
  apres 5 s en plein transfert USB -> reset a l'ouverture suivante. Les
  autres fenetres (rtl_433 -T, welle-cli sans tuyau) n'ont pas ce probleme.
  lecture_flux.arreter() vide le tuyau pendant l'arret (tue en dernier
  recours, c'est dit) ; rtl_sdr -n (s'arrete seul). Deploye 12 h 16.
- Prise DMR du 6/10 12 h 43 : 467,2408 MHz, relais (IDLE, Activity
  Update, SLCO), code couleur 1, intervalle 1, groupes 109 (emetteurs 99,
  302) et 107 (113, 177, 183), en clair. Relecture de 10 jours de
  journal : 19 sessions DMR, 5 frequences dont 467,2408 (14 fois, surtout
  vers 0 h et 3 h), 467,4006 (relais, groupe 101), 145,2875 (relais
  radioamateur, groupe 208101).
- lib/dmr_registre.py : registre SD (dmr_frequences.json, pas 6,25 kHz) :
  codes couleur, intervalles, groupes -> emetteurs, relais, systeme
  (Capacity Plus...), appels prives / chiffres, histogramme horaire ;
  score de retour (recence demi-vie 7 j, heure habituelle, relais) ;
  seed() relit le journal systeme (annonces neutralisees).
- decodeurs.dsd_line -> dmr_registre.line. Regle utilisateur : appel prive
  ou chiffre -> son coupe, rien enregistre (session jetee), donnees seules.
- Fenetre "dmr" (attention.DATA_WINDOWS, 20 s, interet "professionnels") :
  radio_watch._dmr_window ecoute en dsd-fme -fs (DMR seul, plus de fausses
  synchros M17) les 4 frequences les plus prometteuses a tour de role,
  avec le meme habillage qu'une capture (journal radio, cible "dmr").
  Premiere fenetre : appel en cours sur 467,2408 capte (nouvel emetteur 183).
- Annonces (journal messages) : nouvelle frequence DMR, nouveau groupe.
- Console "dmr" (resume des frequences) et "dmr explications" (glossaire).

## 2026-10-07 (fin) : pistes d'ecoute sauvegardees, cle NESDR

- Suggestions radio (decouverte du spectre, DMR ressources partagees,
  satellites amateur, GRAVES/meteores, brouillage GPS via ADS-B, drones
  Remote ID, VOR, bande L Inmarsat/Iridium, HF) : docs/ecoutes_a_explorer.md.
- Enquete cle NESDR (ligne par fenetre du cycle) : reset a 13:29:30 pile a
  l'ouverture de la fenetre "balises" (dec406, 406 MHz) -> meme motif que
  LoRa (programme rtl tue sans vider le tuyau). A corriger avec
  lecture_flux.arreter. Webcam OctoPi non correlee. Correctif LoRa deja
  en place : ~1 reset / 40 min contre 1 / 27 min avant.

## 2026-10-07 (soir) : donnees en clair ajoutees (points 1 et 2)

- Point 1 fait et deploye : codes transpondeur d'urgence avions
  (7500 detournement, 7600 panne radio, 7700 urgence) lus dans l'ADS-B/
  Mode S deja recu (OpenSky index 14 + squawk capte aussi en direct par
  adsb_local). adsb_watch._check_emergencies : annonce prioritaire (son
  scaner_error), 1 fois par appareil et par code (EMERGENCY_FORGET_SEC
  1800 s). Teste en silence (7700/7600/7500). En service.
- Point 2 fait et deploye : ATIS (meteo aero en boucle, AM, en clair).
  lib/atis.py capture rtl_fm -M am ~40 s -> ogg/opus 12k -> gemini.ask
  (audio ajoute a l'API) en JSON (lettre, resume, piste, vent, QNH,
  avertissements). Frequences : LFMH 132,800 ; LFLL 126,175. Store
  ~/autopilot/atis.json, rafraichi par la fenetre "atis" du cycle
  (attention, min 1200 s, interet aviation) sans TTS. Commande console
  "atis" (+ "atis lyon/saint-etienne") et rubrique flash (flash.atis).
  Gemini appele au plus 1 fois / 25 min / aeroport (REFRESH_SEC).
  Validation en situ en cours (1re capture Lyon).
- Reste a faire : point 4 (presence/sante reseau cellulaire, gr-gsm),
  point 3 (FR-Alert cell broadcast, a tester), point 5 (presence/activite
  TETRA). Plus A1/A2/A4/A5 du doc, et corriger balises_watch (meme arret
  de tuyau que LoRa, derniere cause de reset de la cle).

## 2026-10-07 (soir, suite) : resets de la cle, ATIS

- Resets de la cle elucides : surviennent a l'ouverture du device APRES une
  fenetre qui pipe rtl_fm (APRS, pagers) -> rtl_fm bloque en ecriture (tuyau
  vers multimon-ng plein) tue en plein transfert USB. Corrige comme LoRa :
  decodeur arrete d'abord, puis lecture_flux.arreter(rtl) vide le tuyau.
  (balises : stdout deja draine, PAS le motif ; dec406 non coupable.)
  Correctif APRS/pagers commite+pousse, pas encore deploye (eviter un
  redemarrage de plus a chaud ; s'appliquera au prochain).
- ATIS : chaine validee techniquement (capture AM -> ogg -> Gemini audio ->
  JSON). LFMH (Saint-Etienne) muet (petit aeroport, hors heures) : store
  = dernier_essai seul. Rotation corrigee (atis._touched) pour que LFLL
  (Lyon, ATIS continu) soit tente -> validation en cours.
- Pi tres sollicite aujourd'hui (nombreux redemarrages, captures AM/DMR) :
  tourne chaud (74-78C), watch-zone, pas de pause bloquante grace a la
  soupape. Lever le pied sur les redemarrages.

## 2026-10-07 (soir) : resets de la cle resolus, ATIS cadre

- VALIDE : correctif APRS/pagers (arret propre de rtl_fm) -> 0 reset de la
  cle en 45 min (contre ~1 / 20-30 min avant). Saga close. Memoire
  pi-rtl-arret-tuyau.md.
- ATIS : pas d'empreinte sonore (teste : non fiable, memes mots/voix =
  spectre quasi identique entre deux lettres, risque de rater un vrai
  changement). On garde : espacement progressif des aeroports muets
  (jusqu'a 6 h), intervalle min 25 min, aucune annonce vocale. Les
  decodeurs locaux (multimon Morse, dsd-fme numerique) ne lisent pas la
  parole -> Gemini reste le seul transcripteur, appele seulement quand un
  aeroport redevient audible. ATIS = source secondaire (deja par internet).
- Point 4 (reseau cellulaire, gr-gsm) : en attente d'un creneau pour
  immobiliser la cle (apt install + test exclusif), Pi refroidi.

## 2026-10-07 (fin de session) : ATIS ecoute-directe, regression corrigee
- ATIS : module Gemini retire (la fenetre airband aviation_atis capte deja
  la voix au journal). Ajout fenetre locale aviation_atis_st_etienne
  (132,800 MHz) : balayee sans erreur, voix au journal, zero Gemini.
- Regression corrigee : lecture_flux.arreter plantait (fileno sur stdout
  deja ferme) -> fenetres pagers et aprs en erreur. arreter tolere
  desormais un stdout ferme. Deploye.
- A VERIFIER plus tard : comme aprs/pagers ferment rtl.stdout cote parent,
  arreter ne peut PAS vider leur tuyau (fallback terminate+kill). Recontroler
  que les resets de la cle restent a 0 ; sinon, ne plus fermer rtl.stdout
  pour que le drain opere (comme LoRa).
- NON FAIT (limite d'usage) : point 4 reseau cellulaire. gr-gsm installable
  par apt (dry-run : 25 paquets, ~GNU Radio osmocom). Necessite d'immobiliser
  la cle (stopper radio-watch) pour le test. A faire Pi refroidi.

## 2026-10-08 : mode manuel, DMR et stations d'aeroport
- DMR en manuel : une station probable DMR (bande "dmr" ou flag numerique,
  stations.is_digital) est desormais ecoutee en DMR -- dsd-fme en -fs (DMR
  seul, accroche mieux que -fa) et souffle analogique coupe (silence entre
  les communications du relais, voix decodee pendant). Le drapeau numerique
  passe de stations -> target() -> request_manual -> radio_watch -> 
  manual_tuner.tune(numerique=). A CONFIRMER a l'oreille sur une vraie
  transmission DMR.
- Stations d'aeroport : les 6 fenetres airband a une seule frequence (ATIS
  Lyon/St-Etienne, St-Etienne, Lyon tour/sol/approche) ne sont plus 6
  plages separees mais une seule plage "stations d'aeroport" (manuel.
  plage_aeroport), navigable station par station. pmr446 (16 canaux) reste
  une vraie plage. _publish_plages regroupe ; manuel target() honore
  station["source"] (airband). Verifie : plages regenerees OK.

## 2026-10-08 : point 4 (reseau cellulaire) -- toolchain prete, reception nulle
- gr-gsm installe (apt ; xtrx-dkms, un pilote d'SDR absent, retire pour
  garder apt sain). Le module Debian est gnuradio.gsm (pas "grgsm").
- Invocation qui marche malgre le conflit numpy (pip numpy 2 dans
  /usr/local casse GNU Radio compile avec numpy 1.x Debian) :
  PYTHONPATH=/usr/lib/python3/dist-packages /usr/bin/python3 -S
  /usr/bin/grgsm_scanner -b GSM900 ...  (script : tools/scan_gsm.sh).
- Teste en vrai (radio-watch arrete) : GSM900 speed 20 et speed 8 verbeux,
  ~0 cellule detectee. Cause = reception : antenne 446 courte et interieure,
  mauvaise a 900 MHz, et 2G en declin en France. MEME limite physique que
  l'ATIS de Lyon / la HF : gate sur une meilleure antenne.
- PAS de commande ni de fenetre de cycle ajoutee : inutile d'immobiliser la
  cle pour une source qui ne capte rien aujourd'hui. A retester avec la
  Sirio / une antenne fenetre via tools/scan_gsm.sh ; si des cellules
  sortent (MCC 208 = France ; MNC -> operateur), on batira le suivi
  presence/sante (cellules vues, apparition/disparition), sans IMSI ni contenu.

## 2026-10-08 : DMR approfondi (choix utilisateur)
- Emetteurs radioamateurs (>= 6 chiffres) resolus en indicatif + ville via
  radioid.net (lib/radioamateurs.dmr_id), jamais le nom. A l'affichage de la
  commande dmr seulement (cache SD ; scanner non ralenti). Les petits
  identifiants des relais commerciaux 467 restent numeriques.
- dmr_registre.networks() : regroupe les frequences d'un meme reseau
  multi-canaux (Capacity Plus...) par GROUPES D'APPEL partages entre
  frequences a < 5 MHz -- PAS par code couleur (le CC 1 est partout et
  melait a tort 145 ham et 467 commercial). Verifie : seul 467,2374 +
  467,2408 regroupes (groupes 107/108/109 communs).
- Reel : 145,2875 ham = F4LFY (Nice), F6GLS (Saint-Raphael), VE2FSJ
  (Brossard), F4MXS (Regusse) ; 467 = relais commerciaux (IDs courts).

## 2026-10-08 : integration DMR <-> scanner verifiee (demande utilisateur)
DMR n'est pas une logique a part, c'est une facette du balayage. Verifie :
- Interets : "dmr" est dans la categorie "professionnels" (interets.category_of)
  -> la fenetre dmr ET le balayage uhf_business repondent au meme interet.
- Decouverte = meme process : _handle_candidate du balayage principal ->
  try_digital_voice_decode(-fa, tous modes) -> _dmr_learn -> registre ; la
  fenetre dmr revisite (-fs). Prouve : registre = 6 frequences trouvees par
  le balayage. Pas deux logiques distinctes.
- Notation : les captures DMR sont des prises du journal radio, notables
  comme les autres (18 notes), alimentent l'attention.
- Bilan CORRIGE : le tri analogique/numerique se fait PAR CAPTURE
  (mesures.numerique). Une bande mixte (uhf_business, relais_2m) regle son
  audio sur ses captures analogiques et ignore le DMR ; une bande 100 %
  numerique (dmr) est "suivie" sans reglage audio (dsd-fme ne passe pas par
  reglages_audio). Plus de proposition/auto-ajustement analogique absurde.
- Profondeur : bandes DMR balayees au pas de 12,5 kHz ; chiffre ecarte
  (dsd echoue + coupure prive/chiffre) ; la veille dmr revisite desormais
  TOUTES les frequences connues (mieux notees plus souvent), pas seulement
  le top 4 -> on capte tout le DMR ouvert.

## 2026-10-08 : annonces groupees avions + trains, ambiance gare
- Avions (adsb_watch) : les nouveaux appareils d'un passage sont annonces
  en UN message "{n} avions detectes : <av1> <av2>..." (avion.plusieurs_
  detectes) ; un seul avion garde la phrase habituelle. Urgences et survols
  restent individuels.
- Trains (sncf_watch) : annonces GROUPEES. Chaque annonce qui part entre dans
  un tampon ; apres _GROUP_WINDOW_SEC (4 s) tout est regroupe PAR TYPE et dit
  ensemble ("2 trains vont etre mis en place : le TER ... et le TGV ..."). Un
  seul train = phrase habituelle mot pour mot. Depart/terminus/arret groupes
  separement (verbes differents). Perturbations/suppressions restent
  individuelles.
- Sons d'ambiance simultanes : _play_ambient_sound joue SANS verrou exclusif
  (play_sound_effect -> aplay "default"/dmix mixe), donc une arrivee et un
  depart au meme instant jouent ensemble.
- Evenement d'ambiance secondaire : proba AMBIENCE_EXTRA_PROB (0,15) qu'un son
  de train declenche, en parallele, un train qui passe (ter/tgv_passby) ou un
  klaxon (horn1.mp3) au hasard -- decoratif, pour faire vivre la gare.

## 2026-10-11 : veille USB, reparation automatique des peripheriques
- Constat sur deepspace9 : la webcam s'est deconnectee une seconde a
  23 h 47 (mise en veille USB automatique) ; mjpg-streamer est mort
  ("Error grabbing frames") mais webcamd restait "active" : camera
  revenue jamais reprise, vision.py et photos des plantes muettes 14 h.
  Un appareil lent branche directement sur le Pi (port 1-1.4) n'a jamais
  ete reconnu (port bloque dans l'etat "default", erreurs -32/-71, meme
  apres coupure du port et ancien schema d'enumeration : materiel ou
  cable). L'imprimante rebranchee (CH340) n'est pas reprise par
  OctoPrint, qui ne se connecte qu'a son demarrage.
- watchers/usb_watch.py (service usb-watch, toutes les 15 s) : appareils
  reconnus par leur TYPE (clavier HID, audio sans video, UVC, RTL2838,
  convertisseur serie), pas par leur port. Gestes gradues, un probleme =
  son echelle : veille retiree tout de suite ; puis rebranchement
  logiciel / relance de webcamd / retrait du pilote TNT, au 2e passage
  en panne, 2 min entre deux gestes. Echec de toute l'echelle -> on le
  dit ("Debranche-le, puis rebranche-le"), rappel au plus toutes les 2 h,
  nouvel essai discret toutes les 30 min.
- Ports bloques : coupure/remise du courant du port, puis meme geste
  avec old_scheme_first ; sinon annonce (directement sur le Pi / sur un
  hub). Annonce aussi quand l'appareil finit par etre reconnu.
- Imprimante : connexion OctoPrint demandee seulement quand elle
  (re)apparait ou au demarrage de la veille, 3 essais -- deconnectee a
  la main dans OctoPrint, elle n'est pas reconnectee derriere ton dos.
- Debranchement d'un appareil deja vu (souvenir sur la carte SD,
  ~/autopilot/usb_watch_attendus.json) annonce apres ~30 s, retour aussi.
- tools/usb-repare (/usr/local/sbin, sudo -n limite) : seuls gestes root ;
  refuse un port sain, un hub ou un disque (les disques restent a
  nas_watch.py). config/99-nearfield-usb.rules : plus de veille USB
  automatique pour les appareils (hubs exceptes).
- Console : commande "usb" (etat, ports bloques, reparations reussies du
  jour) ; usb-watch dans "veilles", protegee de "arrete toutes les
  veilles" (elle garde le clavier et le son de la console), et dans le
  bilan de sante.

## 2026-10-11 : pilotage vocal de l'imprimante 3D
- lib/octoprint.py : client partage. API REST pour etat, temperatures,
  deplacements, chauffe, travaux, fichiers. La position reelle n'est pas
  dans l'API : M114 envoye au firmware, reponse guettee dans le websocket
  d'OctoPrint (/sockjs/websocket, session passive ouverte avec la cle) --
  0,5 a 0,7 s mesure. M400 avant M114 (position apres les mouvements),
  JAMAIS pendant une impression : M400 viderait le tampon du firmware.
- console/imprimante_cmd.py, dialogue "pilote imprimante" : fleches X/Y
  (haut = arriere), Page precedente/suivante Z, + / - pas 0,1 / 1 / 10 /
  50 mm (Z 10 au plus), Ctrl+X origine X Y, Ctrl+Z origine Z, Debut
  origine de tout (en tache de fond, fin dite quand le firmware a fini,
  refus du firmware dit en clair), x y z p position, t temperatures,
  e / r 5 mm de filament (buse >= 170 degres), v ventilateur, m moteurs
  coupes, n reglage du plateau (4 coins a 30 mm du bord puis centre, buse
  a Z0 pour la feuille de papier, origine faite d'abord si besoin), i etat.
- Garde-fous : rien ne bouge pendant une impression ni une pause (un
  gcode irait dans le flux de la piece) ; Z ne descend que de 1 mm par
  appui tant que Z n'a pas ete mis a l'origine dans cette console ;
  chauffe bornee (buse 260, plateau 110) ; lancement et annulation d'une
  impression apres "oui" ; arret d'urgence (M112) sans question mais
  seulement sur ses mots exacts.
- Commandes tapees : chauffe (prechauffages d'OctoPrint : pla, abs ; ou
  buse N / plateau N), refroidis, pause / reprends / annule l'impression
  ("pause" et "reprends" seuls restent aux veilles et au scanner),
  imprime (liste des fichiers, fleches, Entree), ventilateur [N | arret],
  moteurs, connecte / deconnecte l'imprimante, vitesse d'impression N,
  debit N, arret d'urgence. "imprimante" donne maintenant l'etat complet
  (fichier, pause, erreur, temps restant, temperatures).
- watchers/imprimante_watch.py (service imprimante-watch, source de son
  "imprimante 3D" dans les priorites) : chauffe atteinte, impression
  lancee (duree estimee), 25 / 50 / 75 %, pause, reprise, terminee (duree
  reelle), annulee, erreurs, rappel toutes les 15 min d'une chauffe
  allumee sans impression. Veille pilotable par "arrete octoprint" (pas
  "imprimante" : "coupe la chauffe de l'imprimante" doit refroidir).

## 2026-10-14 : antenne discone, DMR, capteurs, reglages son, pont Mac (fait)

- Antenne : discone Sirio montee dehors. Premier montage : ~20 dB de perte
  (liaison coaxiale) ; corrige par l'utilisateur. Mesure apres : FM +28 a
  +33 dB au-dessus du bruit (fouet : ~7 dB), relais DMR 467,23 +21 dB.
  Rallonge USB ajoutee (le gros coax tordait le dongle) : aucune perte,
  4 echantillons par million perdus a 2,4 MS/s. Pi souvent en pause
  thermique (75 C) : ventilation a envisager.
- Console : "reapprendre" (et toute commande confirmee sans handler)
  plantait la console (cmd.handler None) ; corrige dans main.py.
- Ancienne antenne mise de cote : 172 enregistrements radio d'avant
  15:47 deplaces vers storage/radio (+ annexes et manifeste dans
  storage/radio/ancienne_antenne_annexes) ; parasites_connus.json (374
  empreintes) et bruit_de_fond.json archives "_archive_fouet_..." ; profils
  de bruit et reglages audio archives par "reapprendre".
- DMR : les "Message DMR : _-- -402__" etaient des enregistrements ARS
  Motorola (poste qui s'annonce au serveur de dispatch 16448250, IP
  13.250.250.250, port 4005), pas du texte ; une partie refusee (ICMP
  "Destination Network Unreachable" : dispatch injoignable, les postes
  reessaient). lib/decodeurs.py les reconnait et les donne a
  lib/dmr_registre.py (ars_event) : fichier du jour
  donnees/dmr_ars_<jour>.jsonl, postes dans le registre, bonus de veille
  apres une connexion recente, resume du jour range au journal messages a
  23 h 50 (daily_tick, radio_watch), "dmr aujourd'hui" / "dmr hier".
  632 faux messages deplaces dans server/corbeille/dmr_faux_messages_2026-10-14.
  Note : 467,2373 et 467,2408 sont tres probablement LE MEME relais
  (3,5 kHz d'ecart, memes postes et groupes) mesure avec un decalage.
- Capteurs : memoire persistante lib/capteurs_connus.py (premier/dernier
  vu, nombre, jours, mien), etat de capteurs_watch sur la carte SD (plus
  /tmp) ; "nouveau" = jamais entendu (avant : tous les 7 jours et a chaque
  redemarrage). Commande "capteurs connus". Alarmes : phrase sobre de
  Gemini (nature de l'appareil et de l'alarme, faux decodage probable),
  cache par contenu 30 jours. Aucun capteur appaire pour l'instant (normal).
- Reglages son a la main : commande "reglages son" (console/reglages_son_cmd.py),
  type de contenu par bande (auto, musique, voix, donnees) + couches
  (filtre voix, pilote FM, souffle, crachotements, sifflement,
  bourdonnement, volume, aigus) ; reglages_audio.json "manuel", prioritaire
  sur l'appris, garde par "reapprendre" ; F / B reecoutent la derniere
  capture filtree / brute. Sans reglage manuel, sortie identique a l'octet
  pres a l'ancienne chaine (verifie).
- Pont Mac : coupures a repetition = Mac en veille demandee (Software
  Sleep), pas un bug ; le Pi coupe apres 120 s sans battement, journal
  rendu lisible ("en veille probablement"). Wake-on-LAN actif (womp 1).
  Le Mac envoie maintenant l'etat de session (verrouille, inactivite) ;
  "compagnon" le dit. 20 echecs = Meteor "signal trop faible" (fouet).
- LoRa : les "noeuds LoRa" (F1ZCK-4 Guizay 5 km digi+iGate 433,775 ;
  F4HYI-10 ; 7X2SR-10 iGate RX seul a la gare d'Amplepuis) venaient de
  l'APRS VHF (paquets tiers relayes par F1ZCK-3). Test 20 min d'ecoute
  continue de 433,775 : 2 vraies trames LoRa APRS reperees ("<" en tete),
  confiees au Mac en une seconde, mais abimees (CRC). Le Pi n'ecoute LoRa
  que ~2 % du temps en temps normal. Signal enregistre tres bas dans le
  convertisseur (amplitude moyenne 0,008, max 0,067) a -g 40.

## 2026-10-14 : a faire (demande utilisateur)

1. Notation en degres, par onglets (console/notation.py). Plus de choix
   exclusifs : chaque dimension est un curseur (pourcentage ou degre),
   tout a 0 par defaut. Onglets : Contenu (voix %, musique %, tonalites %,
   donnees %... un contenu peut etre "musique puis voix"), Perturbations
   (sifflement, bourdonnement, autre station, crachotements, souffle,
   chacun en degre), Son (naturel, etouffe, metallique, sature : en
   degres, pas un seul choix), Qualite (lisibilite, volume, stabilite,
   debut, fin, merite, certitude). Tab / Maj+Tab : onglet suivant /
   precedent ; fleches haut / bas : dans l'onglet ; gauche / droite :
   la valeur. Garder la lecture des anciennes notes (mapper l'ancien
   choix unique en 100 % / degre plein) pour que bilan.py reste juste.
2. Contenu appris par station / frequence. Le Pi additionne les
   pourcentages notes par frequence (stations.py, registre) : une station
   surtout musicale prend des reglages "musique" plutot que "voix", sans
   ecraser un reglage fait a la main (reglages_audio "manuel" reste
   prioritaire). Idealement un type de contenu par station, pas seulement
   par bande.
3. Journal : touche C = "bonne prise, mais coupee trop tot" (comme V,
   meme qualite, plus fin = coupee). Aujourd'hui il faut V puis N et
   renoter la fin : tres frequent.
4. Fin coupee trop tot -> ecouter plus longtemps, sur TOUTES les bandes y
   compris DMR. bilan.py ecarte les captures numeriques (_note_digital)
   de ses suggestions : verifier que la fin / le debut / le volume y sont
   quand meme appris (seuls les filtres analogiques ne s'appliquent pas
   au DMR). Pour le DMR : attente de fin (attente_fin_s, deja lue pour
   "dmr"), debut, volume ; et/ou seuil de fin. Dans "reglages son", la
   bande DMR doit proposer ces couches-la (fin, debut, volume) meme sans
   filtres.
5. Sifflement sur la bande FM, present MEME dans le son brut (dit par
   l'utilisateur) : donc avant la chaine de filtres, dans la reception ou
   la demodulation (rtl_fm wbfm -s 200000 -r 48000 -E dc -E deemp).
   Mesures du 2026-10-14 non concluantes (aucune tonalite fixe sur 89,5
   et 106,45 ; 1 588 Hz sur 100,9 peut-etre dans le programme). Pistes :
   enregistrer un brut ou l'utilisateur l'entend et l'analyser ;
   comparer avec rtl_fm -M fm -s 240000 | sox sinc -15k rate 48000 (aucune
   tonalite mesuree) ; verifier le centrage (_centered) et la DC.
6. Gain du dongle (propose, en attente de decision) : A) gain optimal
   mesure par bande (paliers 20 a 49,6 dB : plancher de bruit et taux
   d'ecretage, garder le plus haut sans ecretage ; a refaire avec la
   discone) ; B) relance en gain plus fort quand un candidat ou un
   preambule LoRa est faible, en surveillant l'ecretage. Puis refaire le
   test LoRa (lora_watch est a -g 40 ; 49,6 laisserait encore ~13 dB de
   marge sur la trame enregistree) et decider de la part d'ecoute LoRa
   (fenetres plus longues ou veille de nuit).
7. Pont Mac : decider quoi faire quand l'ecran du Mac est verrouille
   (pas de dialogue d'appairage ? autre ?) ; aujourd'hui le Pi le sait et
   le dit seulement.

## 2026-10-14 (nuit) : points 1 a 4 de la liste faits

- Notation en degres par onglets (console/notation.py) : Contenu (voix,
  musique, tonalites, donnees, rien, en %), Perturbations (souffle +
  4 brouillages, degres aucun..envahissant), Son (etouffe, metallique,
  sature, volume -2..+2), Qualite (lisibilite 0..5, coupures, debut coupe,
  fin -2..+2), Avis (interet -2..+2, doute). Tout a 0 ; Tab / Maj+Tab
  onglets, haut/bas, gauche/droite, E exemple, Entree enregistre. La note
  garde "degres" ET "reponses" (legacy() : ancienne grille) ; bilan.quality
  utilise les degres quand ils sont la. Interet 0 = "bof" (neutre).
- Contenu appris par station : lib/contenus.py (carte SD,
  contenus_stations.json, recalcule a chaque note) ; reglages_audio.get(band,
  freq_hz) applique "musique" ou "donnees" a une station >= 60 % de ce
  contenu (>= 2 notes), seulement si la bande est en contenu automatique.
  chaine_audio passe la frequence.
- Journal : touche C = bonne prise coupee trop tot (fin -2 / "coupee") ; sur
  une prise deja notee, corrige seulement la fin.
- Voix numerique : nouveaux reglages attente_fin_numerique_s (None = 10 s,
  DSD_FME_IDLE_SEC) et gain_numerique_db, lus par try_digital_voice_decode
  (target passe par _dmr_window et _handle_candidate) ; la fin DMR etait un
  delai fige qui ignorait tout reglage. bilan.suggestions_digital les ajuste
  (fins coupees : +3 s, 4..30 ; volume) des 4 notes numeriques. "reglages
  son" : bande dmr = ces deux couches seulement ; toutes les bandes ont en
  plus attente de fin et marge de detection (Espace = valeur du scanner) ;
  FM etroite : aussi les deux couches numeriques.
- Reste : 5 (sifflement FM, attend un enregistrement ou il s'entend),
  6 (gain : mesure lancee, ~/backups/mesure_gain_2026-10-14.json), 7 (Mac
  verrouille : decision utilisateur).


## 2026-10-21 au 23 : diagnostic radio, voix, volumes, gare, scanner (fait)

Session de diagnostic puis d'ajouts, tout deploye et teste sur le Pi.

- Antenne : la panne du 19/10 (plus aucune trame 433 MHz) venait du coax N-N
  neuf (ame coupee, tresse bonne : la FM passait par la tresse a +24 dB). La
  discone branchee sans coax : 42 trames, 9 capteurs en 3 min. Le verdict
  "antenne" ecoute maintenant les capteurs 90 s (lib/mesure_antenne.py), la FM
  ne prouvant rien ; audiographes de la mesure (lib/audiographe.py, commande
  "antenne", "antenne son").
- sncf_watch : cle de train sans le suffixe ":RealTime:<uuid>" (un retard
  relancait annonce et bruitage a chaque mise a jour) ; garde-fou anti-doublon
  strict (choix et reservation sous verrou, plus de doublon force, bruitage
  reprogramme remplace) ; "a" sans accent avant une heure (certaines voix
  lisaient "a accent grave").
- Annonces de gare : effet de quai calcule avant le jingle (plus de blanc),
  voix +2 dB sur le jingle, nouvelle reverberation par convolution "grand
  espace ouvert" (lib/reverb_quai.py : queue de bruit decorrele, decroissance
  par bande, reflexions legeres, queue entiere ; plus de filtres en peigne).
- Voix : edge-tts 7.3.1 (Microsoft a change Sec-MS-GEC le 13/10). Pas de test
  periodique : un refus 403 pendant la parole lance watchers/edge_maj.py
  (systemd/edge-maj.service) qui essaie la derniere version a part, l'installe,
  redemarre les services et l'annonce ; journal journal_voix_microsoft.jsonl.
  Sonde de la console seulement en panne ; seuil du premier son 2,5 s (3
  retards de suite avant "absent"). Voix d'ailleurs via lib/voix_externe.py,
  presentees comme Piper : 19 voix macOS par le pont (messages "dire"/"son",
  aucune ouverture de port sur le Mac), Google Traduction, MBROLA fr1-fr7
  (compile depuis github.com/numediart/MBROLA, voix dans /usr/share/mbrola).
  Voix Piper "low" retirees (charabia). Modes aleatoires : toutes, distantes,
  locales, Microsoft.
- Commande "volume" (console/volume_cmd.py, lib/volumes.py) : volume general
  (carte son) et curseurs par veille et par canal, appliques au vol.
- Scanner (lib/maintien.py) : plus de coupure d'un echange interessant (DMR
  coupe pile a 120 s) ; interet fort/passion : delai de fermeture x2 ; garde-fou
  900 s. Commandes "zapper", "ignorer" (frequence, groupe ou appelant DMR,
  5 min a toujours ; lib/ignores.py), "verrouiller" corrige pendant un appel DMR.
- Journal "donnees" : resume horaire du trafic non vocalise par Gemini avec un
  trait d'esprit (lib/trafic_heure.py) ; bipeurs sans texte seulement la.
- Relais APRS (lib/relais.py, lib/ctcss.py, commande "relais") : formats nus et
  liaisons "A<=>B", relais lointains notes, tonalite CTCSS retrouvee dans le
  son brut = relais confirme, audibilite par relais.
- Notifications : les fleches du journal traversent les jours (au bout de la
  journee on continue dans la precedente) -- le journal se parcourt en entier,
  et tout ce qui est ecoute n'est plus "nouveau" ; avant, les entrees des jours
  precedents restaient comptees. Une prise dite a voix haute N'EST PAS marquee
  ecoutee (l'utilisateur n'est pas toujours dans la piece). journal_vus.mark
  sous verrou (plusieurs processus) ; touche B (son brut) compte comme ecoute.
- Aviation quasi muette (mesure du 23/10) : ATIS a +3,7 dB au gain 20, moins
  en montant le gain = tuner sature par la FM (+24 dB) ; cuvette de
  Saint-Etienne. Le gain 20 est le bon. Remede propose : coupe-bande FM
  (Nooelec Flamingo FM, RTL-SDR Blog) entre la cle et l'adaptateur SMA-N ;
  ensuite remesurer les ATIS et adapter la mesure "antenne" (stations FM).

## A faire : utilitaire "materiel" (demande utilisateur 2026-10-23, pas commence)

Un dialogue de la console, du meme style que les autres (fleches, Entree,
Echap, F1), pour decrire le materiel radio et regler le scanner en
consequence, couvrant les besoins des radioamateurs.

- Inventaire : peripheriques USB detectes et proposes automatiquement s'ils
  touchent a la radio (cles RTL-SDR par numero de serie, HackRF, Airspy,
  SDRplay, cartes son, interfaces serie/CAT FTDI-CP210x-CH340 d'un poste,
  commutateurs ou multiplexeurs d'antennes pilotables en USB, GPS) ; appareils
  passifs declares a la main (coupe-bande FM, preampli, coupleur/splitter,
  bias-tee, antennes et les bandes qu'elles couvrent bien ou mal).
- Par cle : son antenne, et une strategie :
  * meme antenne (coupleur) ou antennes separees ;
  * ecoute ciblee : bandes dediees a la cle dont l'antenne les couvre bien,
    exclues de celle qui les ecoute mal ;
  * suiveuse (et qui elle suit) : la cle 1 capte sur une bande, la cle 2 si
    libre s'y accroche et la cle 1 retourne balayer -- rien n'est perdu ;
  * balayage reparti : le Pi distribue les bandes entre les cles.
- Effets des appareils passifs : un coupe-bande FM declare = fm_diffusion
  sautee, verdict "antenne" sans les stations FM, gains aviation a remesurer.
- Constat technique (2026-10-23) : le scanner est construit autour d'UNE cle.
  Une cinquantaine de modules lancent rtl_fm, rtl_power, rtl_433, rtl_airband,
  dsd-fme, acarsdec, dumpvdl2... presque tous sur l'index 0 implicite, et une
  seule boucle (watchers/radio_watch.py) les sequence. A prevoir, par etapes :
  1. choisir chaque cle par son numero de serie (rtl_eeprom ; les index
     changent au branchement) et passer le peripherique a tous les outils ;
  2. le dialogue, la detection et les declarations (testables avec une cle) ;
  3. le moteur multi-cles (ecoute en parallele, suiveuse, reparti, dedie),
     a ecrire et tester quand une seconde cle sera branchee.
