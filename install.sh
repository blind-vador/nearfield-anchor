#!/usr/bin/env bash
# Installeur "clef en main" pour l'ecosysteme watchers (radio/train/meteo/
# avion/crue/ACARS + gestion NAS + radiono) et la console vocale sur tty1.
# Pense pour un Raspberry Pi OS / OctoPi (Debian 12 bookworm, armhf),
# teste sur le Pi de reference de ce projet (reinstallation complete du
# 2026-09-28 apres la perte de la carte SD).
#
# Ce script :
#   1) installe les paquets systeme (apt)
#   2) compile RTLSDR-Airband, mbelib+dsd-fme, libacars+acarsdec et RNNoise
#      depuis les sources (aucun n'est empaquete Debian) -- saute une etape
#      si le binaire attendu existe deja (idempotent, relancable sans risque)
#      et le moteur de voix locales Piper
#   3) installe les dependances Python (pip)
#   4) aplatit watchers/ + lib/ + config/ + tools/ dans un seul repertoire
#      cible (preserve le style d'imports plats des scripts -- aucune
#      modification de code necessaire), deploie la console, les sons de
#      notification (branche assets) et les voix Piper francaises
#   5) reglages systeme (pilote DVB, session audio, points de montage,
#      droits sudo limites, tty1 pour la console)
#   6) deploie les unites systemd et les active
#
# Usage : sudo bash install.sh
set -euo pipefail

RUN_USER="${RUN_USER:-vador}"
TARGET_DIR="${TARGET_DIR:-/home/vador/autopilot/pi_console}"
RADIONO_TARGET_DIR="${RADIONO_TARGET_DIR:-/home/vador/autopilot}"
NAS_TARGET_DIR="${NAS_TARGET_DIR:-/home/vador/autopilot/nas_manager}"
CONSOLE_TARGET_DIR="${CONSOLE_TARGET_DIR:-/home/vador/autopilot/console}"
BUILD_DIR="${BUILD_DIR:-/tmp/nearfield_anchor-build}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Axe 4 (2026-10-29) : le depot est la source unique, plus de copie a plat.
# Les services tournent depuis $REPO_DIR via les drop-ins systemd (PYTHONPATH).
AIRBAND_DIR="/home/vador/autopilot/rtl_airband"   # build RTLSDR-Airband (artefact, hors depot)
PP_DEPOT="$REPO_DIR/lib:$REPO_DIR/watchers:$REPO_DIR/tools:$REPO_DIR/config:$REPO_DIR/bridge"

# OctoPi installe /root/bin/git, un garde-fou qui refuse git en root (pour
# proteger l'installation d'OctoPrint). Les depots clones ici n'ont rien a
# voir avec OctoPrint : on le contourne pour ce script seulement, sans le
# supprimer, en retirant /root/bin du PATH.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Debian 12 refuse pip au niveau systeme (PEP 668, "externally-managed-
# environment") : les services tournent avec /usr/bin/python3, on assume.
export PIP_BREAK_SYSTEM_PACKAGES=1
export DEBIAN_FRONTEND=noninteractive

echo "=== 1/6 : paquets systeme (apt) ==="
apt-get update
apt-get install -y \
    espeak ffmpeg mpg123 sox python3-ephem multimon-ng rtl-433 dump1090-mutability libglib2.0-dev libairspy-dev libiio-dev rtl-sdr librtlsdr-dev libjansson-dev \
    build-essential cmake git python3-pip pkg-config wget \
    libconfig++-dev libmp3lame-dev libshout3-dev libfftw3-dev libpulse-dev \
    libsndfile1-dev libitpp-dev libncurses-dev libcodec2-dev portaudio19-dev \
    zlib1g-dev libxml2-dev \
    libopenblas0 samba \
    python3-evdev python3-fuzzywuzzy python3-levenshtein python3-pil \
    python3-cryptography python3-serial

echo "=== 2/6 : compilation des bibliotheques externes (idempotent) ==="
mkdir -p "$BUILD_DIR"

if [ ! -f "$AIRBAND_DIR/src/build/src/rtl_airband" ]; then
    echo "-- RTLSDR-Airband --"
    mkdir -p "$(dirname "$AIRBAND_DIR")"
    if [ ! -d "$AIRBAND_DIR/.git" ]; then
        rm -rf "$AIRBAND_DIR"
        git clone https://github.com/szpajder/RTLSDR-Airband.git "$AIRBAND_DIR"
    fi
    # Depuis 2024 le CMakeLists principal est a la racine du depot (plus
    # dans src/). On construit quand meme dans src/build pour garder le
    # chemin attendu par airband_bridge.py (AIRBAND_BIN). cmake doit tourner
    # DEPUIS le depot : scripts/find_version appelle "git describe" dans le
    # repertoire courant et echoue ailleurs ("not a git repository").
    rm -rf "$AIRBAND_DIR/src/build"
    (cd "$AIRBAND_DIR" && cmake -S . -B src/build -DNFM=ON && make -C src/build -j4)
else
    echo "-- RTLSDR-Airband deja compile, saute --"
fi

if [ ! -x /usr/local/bin/dsd-fme ]; then
    echo "-- mbelib + dsd-fme --"
    rm -rf "$BUILD_DIR/mbelib" "$BUILD_DIR/dsd-fme"
    git clone https://github.com/szechyjs/mbelib.git "$BUILD_DIR/mbelib"
    mkdir -p "$BUILD_DIR/mbelib/build"
    (cd "$BUILD_DIR/mbelib/build" && cmake .. && make -j4 && make install && ldconfig)
    git clone https://github.com/lwvmobile/dsd-fme.git "$BUILD_DIR/dsd-fme"
    mkdir -p "$BUILD_DIR/dsd-fme/build"
    (cd "$BUILD_DIR/dsd-fme/build" && cmake .. && make -j4 && make install)
else
    echo "-- dsd-fme deja installe, saute --"
fi

if [ ! -x /usr/local/bin/acarsdec ]; then
    echo "-- libacars + acarsdec --"
    rm -rf "$BUILD_DIR/libacars" "$BUILD_DIR/acarsdec"
    git clone https://github.com/szpajder/libacars.git "$BUILD_DIR/libacars"
    mkdir -p "$BUILD_DIR/libacars/build"
    (cd "$BUILD_DIR/libacars/build" && cmake .. && make -j4 && make install && ldconfig)
    git clone https://github.com/TLeconte/acarsdec.git "$BUILD_DIR/acarsdec"
    mkdir -p "$BUILD_DIR/acarsdec/build"
    (cd "$BUILD_DIR/acarsdec/build" && cmake -Drtl=1 .. && make -j4 && make install)
else
    echo "-- acarsdec deja installe, saute --"
fi

# dump1090-mutability (ADS-B direct, watchers/adsb_local_watch.py) installe
# un service qui s'accapare la cle en permanence : le scanner le lance
# lui-meme par fenetres, le service doit rester arrete.
systemctl disable --now dump1090-mutability 2>/dev/null || true

# Balises de detresse 406 MHz (watchers/balises_watch.py) : dec406, MIT.
if [ ! -x /usr/local/bin/dec406_scan ]; then
    echo "-- dec406 --"
    rm -rf "$BUILD_DIR/dec406"
    git clone https://github.com/Yverr0y/Decode_sarsat_406_v1g_v2g.git "$BUILD_DIR/dec406"
    # dec406 fait un reset USB "dur" du dongle a CHAQUE ouverture : toutes les
    # ~25 min (fenetre balises), le dongle a fini par se bloquer pour de bon
    # (2026-10-20, erreur -71). Reset desormais seulement si DEC406_USB_RESET.
    perl -0pi -e 's/(static void reset_rtl_usb\(void\) \{\n)/$1  if (!getenv("DEC406_USB_RESET")) return;  \/* nearfield-anchor *\/\n/' \
        "$BUILD_DIR/dec406/src/backend_rtlsdr.c" "$BUILD_DIR/dec406/src/main_scan.c"
    grep -q DEC406_USB_RESET "$BUILD_DIR/dec406/src/backend_rtlsdr.c" || { echo "!! dec406 : correctif du reset USB non applique"; exit 1; }
    (cd "$BUILD_DIR/dec406" && make build/dec406_scan && install -m 755 build/dec406_scan /usr/local/bin/)
fi

# Ballons-sondes (watchers/sondes_watch.py) : decodeurs libres rs1729/RS
# (RS41, M10/M20, DFM), alimentes par rtl_fm puis sox.
if [ ! -x /usr/local/bin/rs41mod ]; then
    echo "-- decodeurs de ballons-sondes --"
    rm -rf "$BUILD_DIR/RS"
    git clone --depth 1 https://github.com/rs1729/RS.git "$BUILD_DIR/RS"
    (cd "$BUILD_DIR/RS/demod/mod" && gcc -c demod_mod.c -O2 -w && gcc -c bch_ecc_mod.c -O2 -w \
        && gcc rs41mod.c demod_mod.o bch_ecc_mod.o -lm -O2 -w -o rs41mod \
        && gcc m10m20mod.c demod_mod.o -lm -O2 -w -o m10m20mod \
        && gcc dfm09mod.c demod_mod.o -lm -O2 -w -o dfm09mod \
        && install -m 755 rs41mod m10m20mod dfm09mod /usr/local/bin/)
fi

# Satellites Meteor-M (watchers/satellites_watch.py, tools/meteor_traitement.py) :
# demodulateur et decodeur LRPT libres de dbdexter-dev.
if [ ! -x /usr/local/bin/meteor_decode ]; then
    echo "-- meteor_demod / meteor_decode --"
    for d in meteor_demod meteor_decode; do
        rm -rf "$BUILD_DIR/$d"
        git clone --depth 1 "https://github.com/dbdexter-dev/$d.git" "$BUILD_DIR/$d"
        mkdir -p "$BUILD_DIR/$d/build"
        (cd "$BUILD_DIR/$d/build" && cmake .. && make -j4 && install -m 755 "$d" /usr/local/bin/)
    done
fi

# VDL2 (successeur de l'ACARS, watchers/vdl2_watch.py) : a compiler, comme
# acarsdec, sur libacars deja installee.
if [ ! -x /usr/local/bin/dumpvdl2 ]; then
    echo "-- dumpvdl2 --"
    rm -rf "$BUILD_DIR/dumpvdl2"
    git clone https://github.com/szpajder/dumpvdl2.git "$BUILD_DIR/dumpvdl2"
    mkdir -p "$BUILD_DIR/dumpvdl2/build"
    (cd "$BUILD_DIR/dumpvdl2/build" && cmake .. && make -j4 && make install)
fi

if [ ! -f "$REPO_DIR/lib/rnnoise_lib/librnnoise.so" ]; then
    echo "-- RNNoise (librnnoise.so pour radio_watch.py) --"
    # Pas de roue pip pour l'ARM 32 bits : compilation du module natif de
    # pyrnnoise (voir ROADMAP, "suite 16"). Sans les flags NEON, le code
    # tourne a 0,6x le temps reel, inutilisable en flux continu ; avec, 2,3x.
    if [ "$(dpkg --print-architecture)" = "armhf" ]; then
        RNNOISE_CFLAGS="-march=armv8-a -mfpu=neon-fp-armv8 -O3"
    else
        RNNOISE_CFLAGS="-O3"
    fi
    rm -rf "$BUILD_DIR/pyrnnoise"
    git clone --recursive --depth 1 https://github.com/pengzhendong/pyrnnoise.git "$BUILD_DIR/pyrnnoise"
    (cd "$BUILD_DIR/pyrnnoise" \
        && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS="$RNNOISE_CFLAGS" \
        && make -C build -j4)
    mkdir -p "$REPO_DIR/lib/rnnoise_lib"
    cp "$BUILD_DIR/pyrnnoise/build/librnnoise.so" "$REPO_DIR/lib/rnnoise_lib/"
else
    echo "-- RNNoise deja compile, saute --"
fi

# Voix locales Piper (lib/piper_out.py) : binaire officiel armv7 dans
# /home/vador/piper, voix francaises du depot officiel rhasspy/piper-voices
# (installees plus bas, etape 4, par lib/piper_voix.py qui verifie les MD5).
PIPER_DIR=/home/$RUN_USER/piper
if [ ! -x "$PIPER_DIR/piper" ]; then
    echo "-- Piper --"
    case "$(uname -m)" in
        aarch64) PIPER_TGZ=piper_linux_aarch64.tar.gz ;;
        x86_64) PIPER_TGZ=piper_linux_x86_64.tar.gz ;;
        *) PIPER_TGZ=piper_linux_armv7l.tar.gz ;;
    esac
    wget -q -O "$BUILD_DIR/piper.tar.gz" "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/$PIPER_TGZ"
    mkdir -p "$PIPER_DIR/voix"
    tar -xzf "$BUILD_DIR/piper.tar.gz" -C "$PIPER_DIR" --strip-components=1
    chown -R "$RUN_USER:$RUN_USER" "$PIPER_DIR"
else
    echo "-- Piper deja installe, saute --"
fi

# MBROLA (voix locales tres legeres, lib/voix_externe.py, 2026-10-22) : Raspbian
# n'a pas de binaire mbrola pour armhf, seulement les donnees -> compile depuis
# les sources officielles (libres depuis 2018), voix fr1 a fr7.
if [ ! -x /usr/local/bin/mbrola ]; then
    echo "-- MBROLA --"
    rm -rf "$BUILD_DIR/mbrola"
    git clone -q --depth 1 https://github.com/numediart/MBROLA.git "$BUILD_DIR/mbrola"
    make -s -C "$BUILD_DIR/mbrola"
    install -m 755 "$BUILD_DIR/mbrola/Bin/mbrola" /usr/local/bin/mbrola
fi
for v in fr1 fr2 fr3 fr4 fr5 fr6 fr7; do
    if [ ! -f "/usr/share/mbrola/$v/$v" ]; then
        mkdir -p "/usr/share/mbrola/$v"
        wget -q -O "/usr/share/mbrola/$v/$v" "https://github.com/numediart/MBROLA-voices/raw/master/data/$v/$v"
    fi
done

echo "=== 3/6 : dependances Python (pip) ==="
pip3 install -r "$REPO_DIR/requirements.txt"

echo "=== 4/6 : deploiement (depot source unique : artefacts, secret, runtime) ==="

mkdir -p "$RADIONO_TARGET_DIR"
# Plus d'aplatissement dans pi_console (axe 4) : watchers/lib/tools/bridge
# restent dans le depot, les services les atteignent par PYTHONPATH (drop-ins,
# etape 6). On ne deploie hors du depot que ce qui n'y vit pas.
install -D -o vador -g vador -m 755 "$REPO_DIR"/tools/python3-gnuradio /home/vador/autopilot/bin/python3-gnuradio
# Phrases de l'interface, version neutre par langue (lib/phrases.py).
install -d -o vador -g vador /home/vador/autopilot/phrases
install -o vador -g vador -m 644 "$REPO_DIR"/phrases/*.ini /home/vador/autopilot/phrases/
# Exemples de reference de la grille de notation (touches E / F2), referentiel fixe.
install -d -o vador -g vador /home/vador/autopilot/exemples_notation
install -o vador -g vador -m 644 "$REPO_DIR"/assets/exemples_notation/* /home/vador/autopilot/exemples_notation/
# Secret (jamais commite) : dans le depot, gitignore (config/credentials.py).
# Jamais ecraser un credentials.py deja rempli, sinon une reinstallation le
# remplacerait par le modele vide.
if [ ! -f "$REPO_DIR/config/credentials.py" ]; then
    echo "!! credentials.py absent -- copie du modele vide (credentials.example.py)."
    echo "!! sncf_watch.py/adsb_watch.py et la commande imprimante de la console"
    echo "!! ne fonctionneront PAS tant que $REPO_DIR/config/credentials.py n'est pas rempli."
    cp "$REPO_DIR/config/credentials.example.py" "$REPO_DIR/config/credentials.py"
fi
# Dossier de reglages sur le NAS (lib/reglages.py). Depuis le depot (PYTHONPATH),
# ne touche a rien d'existant.
sudo -u "$RUN_USER" env PYTHONPATH="$PP_DEPOT" python3 -c "import reglages; reglages.ensure()" \
    || echo "!! dossier de reglages non cree (NAS absent ?) -- relancer la commande reglages de la console"
# radiono (outil HF) et magicsound (greffon audio) tournent hors depot.
cp "$REPO_DIR"/radiono/radiono.py "$RADIONO_TARGET_DIR/"
cp "$REPO_DIR"/config/sound_paths.py "$RADIONO_TARGET_DIR/"
PYTHON_SITE="$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
mkdir -p "$PYTHON_SITE"
cp "$REPO_DIR"/radiono/magicsound.py "$PYTHON_SITE/magicsound.py"
# Reparations USB (usb_watch.py) : seul geste root, appele via sudo -n.
install -m 755 "$REPO_DIR"/tools/usb-repare /usr/local/sbin/usb-repare
chown -R "$RUN_USER:$RUN_USER" "$RADIONO_TARGET_DIR"

# Sons de notification (jingles, bips, boucles de l'imprimante...) : branche
# "assets" du depot, deposee dans le dossier "server" du NAS
# (config/sound_paths.py). Rien d'existant n'est ecrase (tar -k). Les sons
# Epidemic Sound sont sous licence : depot prive uniquement.
SONS_DIR="${SONS_DIR:-/media/nas/nd1/server}"
# git en tant que $RUN_USER : le depot lui appartient (git refuse sinon en root).
GIT_U="sudo -u $RUN_USER git -C $REPO_DIR"
$GIT_U fetch -q origin assets:refs/remotes/origin/assets 2>/dev/null || true
if [ -d "$SONS_DIR" ] && $GIT_U rev-parse -q --verify origin/assets >/dev/null; then
    echo "-- sons de notification -> $SONS_DIR --"
    $GIT_U archive origin/assets | sudo -u "$RUN_USER" tar -xk -C "$SONS_DIR" 2>/dev/null || true
else
    echo "!! sons non deployes (NAS non monte ou branche assets absente) : relancer l'installeur ensuite"
fi

# Voix Piper francaises (verifiees, voir lib/piper_voix.py) ; une voix deja la est sautee.
for v in fr_FR-siwis-medium fr_FR-siwis-low fr_FR-upmc-medium fr_FR-tom-medium fr_FR-mls-medium fr_FR-mls_1840-low fr_FR-gilles-low; do
    [ -f "$PIPER_DIR/voix/$v.onnx" ] && continue
    sudo -u "$RUN_USER" env PYTHONPATH="$PP_DEPOT" python3 -c "import piper_voix; ok, why = piper_voix.install('$v'); print('$v', 'installee' if ok else why)" \
        || echo "!! voix $v non installee (reseau ?)"
done

echo "=== 5/6 : reglages systeme ==="
# Le noyau capture le dongle RTL-SDR comme tuner TNT : rtl_power/rtl_fm ne
# peuvent plus l'ouvrir. Effet complet au prochain redemarrage.
cat > /etc/modprobe.d/blacklist-rtl-sdr-dvb.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
# Session utilisateur active des le demarrage, sans connexion : les unites
# utilisent XDG_RUNTIME_DIR=/run/user/<uid>.
loginctl enable-linger "$RUN_USER"
# Points de montage de nas_watch.py (pas d'entree fstab, volontairement).
mkdir -p /media/nas/nd1 /media/nas/nd2
# sudo sans mot de passe limite a ce dont les services ont besoin :
# nas_watch.py (mount/umount), usb_watch.py (usb-repare) et la console
# (redemarrer/eteindre, mise a jour).
SUDOERS_FILE=/etc/sudoers.d/020-nearfield_anchor
# systemctl start/stop/restart/enable/disable des veilles : commandes
# "arrete"/"reprends" de la console (console/veilles.py).
VEILLE_CMDS=""
for unit in "$REPO_DIR"/systemd/*.service; do
    name="$(basename "$unit" .service)"
    [ "$name" = "pi-console" ] && continue
    for action in start stop restart enable disable; do
        VEILLE_CMDS="$VEILLE_CMDS, /usr/bin/systemctl $action $name"
    done
done
# apt-get : commande "mise a jour" de la console (env_keep pour qu elle
# reste non interactive : apt ne doit jamais poser de question sur tty1).
cat > "$SUDOERS_FILE.tmp" <<EOF
Defaults!/usr/bin/apt-get env_keep += "DEBIAN_FRONTEND LC_ALL"
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/mount, /usr/bin/umount, /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff, /usr/bin/apt-get, /usr/local/sbin/usb-repare$VEILLE_CMDS
EOF
visudo -cf "$SUDOERS_FILE.tmp" && chmod 440 "$SUDOERS_FILE.tmp" && mv "$SUDOERS_FILE.tmp" "$SUDOERS_FILE"
# Partages reseau "server" (nd1) et "stockage" (nd2) pour les autres
# machines de la maison (Mac, Windows). Le mot de passe Samba n'est pas
# gere ici : a creer une fois a la main (sudo smbpasswd -a $RUN_USER).
cp /etc/samba/smb.conf /etc/samba/smb.conf.avant-install 2>/dev/null || true
cp "$REPO_DIR/nas/smb.conf" /etc/samba/smb.conf
testparm -s >/dev/null
systemctl enable smbd
systemctl restart smbd
# Sortie son : adaptateur USB (config/asound.conf) plutot que le jack
# integre, dont le pilote snd_bcm2835 plantait (2026-09-29). Seulement si
# l'adaptateur est branche : sinon "default" viserait une carte absente et
# plus rien ne sortirait.
# Carte "boucle" (snd-aloop) : la sortie par defaut y envoie aussi tout le
# son, que la commande "capture" enregistre (lib/capture_son.py). Chargee a
# chaque demarrage : sans elle, la sortie par defaut ne marcherait plus.
echo "snd-aloop" > /etc/modules-load.d/snd-aloop.conf
echo "options snd-aloop index=5 id=Loopback pcm_substreams=2" > /etc/modprobe.d/snd-aloop.conf
modprobe snd-aloop index=5 id=Loopback pcm_substreams=2 || true
if aplay -l 2>/dev/null | grep -q "USB Audio"; then
    cp "$REPO_DIR/config/asound.conf" /etc/asound.conf
    amixer -q -c Device sset Speaker 85% || true
    alsactl store Device || true
    # Et le jack integre n'est plus charge du tout (effet au redemarrage).
    sed -i "s/^dtparam=audio=on/dtparam=audio=off/" /boot/firmware/config.txt
else
    echo "!! Pas d'adaptateur son USB detecte : sortie sur le jack integre (pilote fragile)."
fi
# Pas de mise en veille USB automatique des appareils (usb_watch.py, 2026-10-11).
cp "$REPO_DIR/config/99-nearfield-usb.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --action=add || true
# tty1 appartient a la console vocale : plus de login classique sur l'ecran
# (SSH reste le moyen normal d'administration).
systemctl stop getty@tty1.service 2>/dev/null || true
systemctl mask getty@tty1.service

echo "=== 6/6 : unites systemd ==="
cp "$REPO_DIR"/systemd/*.service /etc/systemd/system/
# Drop-ins (axe 4) : chaque service tourne depuis le depot (ExecStart + PYTHONPATH),
# et non la copie a plat. Un <svc>.service.d/override.conf par service.
cp -r "$REPO_DIR"/systemd/dropins/* /etc/systemd/system/
cp "$REPO_DIR"/systemd/*.timer /etc/systemd/system/  # securite-audit (2026-10-23)
systemctl daemon-reload
for timer in "$REPO_DIR"/systemd/*.timer; do
    systemctl enable --now "$(basename "$timer")"
done
for unit in "$REPO_DIR"/systemd/*.service; do
    name="$(basename "$unit")"
    systemctl enable "$name"
    systemctl restart "$name"
done

cat <<EOF

=== Installation terminee ===

Points a verifier manuellement :
- $REPO_DIR/config/credentials.py (JAMAIS commite, voir .gitignore) contient
  SNCF_TOKEN, OPENSKY_CLIENT_ID/SECRET et OCTOPRINT_API_KEY -- s'il vient
  d'etre cree a partir du modele vide, edite-le avec tes identifiants puis :
  systemctl restart sncf-watch adsb-watch pi-console
- config/sound_paths.py suppose un NAS monte sur /media/nas/nd1/server --
  adapte NAS_BASE si ton organisation differe.
- Partages Samba "server" et "stockage" : si ce Pi est neuf, cree le
  mot de passe de partage : sudo smbpasswd -a $RUN_USER
- Le pilote TNT du dongle radio n'est completement decharge qu'apres un
  redemarrage : sudo reboot
- nas/legacy/ (nas.py, smbv1.conf, smbv2.conf) n'est PAS deploye
  automatiquement -- conserve pour reference historique uniquement,
  remplace par nas_watch.py (montage par UUID, plus robuste).
- Verifie chaque service : systemctl status <nom>.service

EOF
