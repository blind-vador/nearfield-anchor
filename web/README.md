# Point d'entrée web de ce Pi

Sauvegarde/référence des fichiers du point d'entrée unique (2026-10-10).
`ce Pi.local` (80/443, http+https) mène à une page de liens, OctoPrint
passe sous `/octoprint`.

## Pièces
- `index.php` + `registre.exemple.txt` : vivent sur le NAS dans
  `server/webserver/`. `index.php` lit `registre.txt` (lignes `nom::chemin`) et
  liste les liens (HTML sémantique/accessible). Ajouter un site : dossier dans
  `webserver/<nom>/` + ligne `Nom::/<nom>/` au registre.
- `nginx-webserver.conf` : site nginx (`/etc/nginx/sites-available/webserver`)
  servant `webserver/` en PHP (php8.2-fpm) sur `127.0.0.1:8088`.
- `haproxy.cfg.reference` : config haproxy de référence. Clés du re-routage :
  `default_backend webserver` ; `/octoprint/` → backend octoprint avec
  `http-request replace-path /octoprint/(.*) /\1` + `add-header X-Script-Name
  /octoprint` (montage sous-dossier OctoPrint). Backups vivants sur le Pi :
  `/etc/haproxy/haproxy.cfg.bak-*`.

## Reproduire (sur le Pi)
`sudo apt-get install -y php-fpm` ; poser `nginx-webserver.conf` dans
sites-available + lien dans sites-enabled ; appliquer le re-routage haproxy
(voir `haproxy.cfg.reference`) ; `nginx -t` / `haproxy -c` avant reload.

Contenu éditable (sites web, sources) : sur le NAS `server/webserver/` et
`server/dev/` (git-able, montés depuis le Mac).
