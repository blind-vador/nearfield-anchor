# Écoutes et captations radio à explorer (nearfield anchor)

Idées validées par l'utilisateur le 2026-10-07. Centrées **radio** : modes de
transmission captables autour de la machine (communications, échanges
numériques), pas d'indicateurs socio-économiques (suivis à la main).
Règles du projet : signaux ouverts, aucun déchiffrement, pas d'audio privé,
aucun suivi d'individus.

Matériel : clé Nooelec NESDR SMArt v5 (24–1700 MHz), antenne 446 actuelle,
Bluetooth + Wi-Fi intégrés au Pi 4. Antennes à acheter : bande L (~30 € +
préampli), fil HF + unun 9:1 (Sirio 1,20 m attendue).

## A. Sans matériel supplémentaire

1. **Découverte méthodique du spectre** (moteur du reste) : balayage large
   24–1700 MHz la nuit ; chaque émission inconnue classée par forme
   (largeur, rafales/continu, numérique/analogique) ; le Pi propose les
   nouvelles cibles. Aurait trouvé 467,24 MHz sans le hasard.
2. **DMR à ressources partagées** (Capacity Plus / Tier III) : suivre la
   signalisation du relais 467 MHz, apprendre la carte des canaux, suivre
   un appel d'une fréquence à l'autre (dsd-fme, une seule clé). Tenter
   aussi NXDN, dPMR sur les fréquences trouvées.
3. **Satellites radioamateurs** : télémesures 145/435 MHz via gr-satellites
   (paquet Debian, GNU Radio déjà là) ; SSTV de l'ISS décrite par Gemini.
4. **Échos du radar GRAVES** (143,050 MHz) : comptage de météores par heure,
   détection de passages satellites/débris au Doppler.
5. **Brouillage GPS** : qualité de position GNSS dans les trames ADS-B déjà
   reçues ; chute simultanée sur une zone = brouillage/leurrage.
6. **Drones Remote ID** (France, >800 g) : position du drone en Wi-Fi/BT
   (puces du Pi) ; jamais la position du pilote.
7. **Balises VOR** : indicatif Morse + cap, identification et direction.
8. **Étalonnage de la clé** sur un émetteur de référence : peut réparer le
   décodage LoRa, améliore le DMR.

## B. Avec antenne bande L

- **Inmarsat** (satellite SE) : ACARS océanique ; messages de sécurité
  maritime en clair (tempêtes, zones de tir, avis de navigation).
- **Iridium** : ACARS et données système (pas les messages privés).

## C. Avec le fil HF

- HFDL (ACARS HF mondial), NAVTEX + RTTY météo marine, ALE (signalisation +
  indicatifs), radios d'État internationales + DRM (transcrites/résumées),
  HFGCS (US Air Force, en clair), UVB-76, stations de nombres, FT8/WSPR.

## D. Écarté (hors règles du projet)

Police/pompiers chiffrés, GSM-R, DECT/babyphones, compteurs d'eau/gaz des
voisins, clés de voiture, BT/Wi-Fi des passants (= suivi de personnes).

## État / priorités

Ordre proposé : A1 → A2 → A5 → A4 → satellites → drones. Puis B, puis C
quand l'antenne HF sera posée.

### LoRa (bug ouvert)
22 trames LoRa APRS vraies détectées en 10 j (433,775 MHz), gardées sur le
stockage, mais **0 décodée** (toujours « 0 valide, 1 abîmée », CRC faux) :
ni carillon ni annonce — d'où le silence côté utilisateur. Causes probables :
signal faible ou décalage de fréquence. Pistes : redécoder les trames gardées
avec recherche de décalage ; étalonner la clé (A8).

### Clé NESDR réinitialisée par le noyau
Après le correctif LoRa (lecture_flux.arreter, rtl_sdr -n), passé de ~1 reset
/ 27 min à ~1 / 40 min. Reste **balises_watch** (dec406, 406 MHz) : reset à
13:29:30 pile à l'ouverture de la fenêtre balises → même motif (programme
rtl tué sans vider le tuyau). À corriger comme LoRa. Webcam OctoPi
(mjpg_streamer) boucle toutes les 2 min mais N'est PAS corrélée aux resets.
