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
#   3) installe les dependances Python (pip)
#   4) aplatit watchers/ + lib/ + config/ + tools/ dans un seul repertoire
#      cible (preserve le style d'imports plats des scripts -- aucune
#      modification de code necessaire), deploie la console
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
BUILD_DIR="${BUILD_DIR:-/tmp/pi-console-watchers-build}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
    espeak ffmpeg rtl-sdr librtlsdr-dev libjansson-dev \
    build-essential cmake git python3-pip pkg-config wget \
    libconfig++-dev libmp3lame-dev libshout3-dev libfftw3-dev libpulse-dev \
    libsndfile1-dev libitpp-dev libncurses-dev libcodec2-dev portaudio19-dev \
    zlib1g-dev libxml2-dev \
    libopenblas0 samba \
    python3-evdev python3-fuzzywuzzy python3-levenshtein

echo "=== 2/6 : compilation des bibliotheques externes (idempotent) ==="
mkdir -p "$BUILD_DIR"

if [ ! -f "$TARGET_DIR/rtl_airband/src/build/src/rtl_airband" ]; then
    echo "-- RTLSDR-Airband --"
    mkdir -p "$TARGET_DIR"
    if [ ! -d "$TARGET_DIR/rtl_airband/.git" ]; then
        rm -rf "$TARGET_DIR/rtl_airband"
        git clone https://github.com/szpajder/RTLSDR-Airband.git "$TARGET_DIR/rtl_airband"
    fi
    # Depuis 2024 le CMakeLists principal est a la racine du depot (plus
    # dans src/). On construit quand meme dans src/build pour garder le
    # chemin attendu par airband_bridge.py (AIRBAND_BIN). cmake doit tourner
    # DEPUIS le depot : scripts/find_version appelle "git describe" dans le
    # repertoire courant et echoue ailleurs ("not a git repository").
    rm -rf "$TARGET_DIR/rtl_airband/src/build"
    (cd "$TARGET_DIR/rtl_airband" && cmake -S . -B src/build -DNFM=ON && make -C src/build -j4)
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

if [ ! -f "$TARGET_DIR/rnnoise_lib/librnnoise.so" ]; then
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
    mkdir -p "$TARGET_DIR/rnnoise_lib"
    cp "$BUILD_DIR/pyrnnoise/build/librnnoise.so" "$TARGET_DIR/rnnoise_lib/"
else
    echo "-- RNNoise deja compile, saute --"
fi

echo "=== 3/6 : dependances Python (pip) ==="
pip3 install -r "$REPO_DIR/requirements.txt"

echo "=== 4/6 : deploiement des fichiers (aplati dans $TARGET_DIR) ==="

mkdir -p "$TARGET_DIR" "$TARGET_DIR/rnnoise_lib" "$RADIONO_TARGET_DIR" "$NAS_TARGET_DIR" "$CONSOLE_TARGET_DIR"
cp "$REPO_DIR"/watchers/*.py "$TARGET_DIR/"
cp "$REPO_DIR"/lib/*.py "$TARGET_DIR/"
cp "$REPO_DIR"/lib/rnnoise_lib/*.py "$TARGET_DIR/rnnoise_lib/"
cp "$REPO_DIR"/tools/*.py "$TARGET_DIR/"
cp "$REPO_DIR"/config/sound_paths.py "$TARGET_DIR/"
# Jamais ecraser un credentials.py deja rempli sur le Pi : une reinstallation
# le remplacerait sinon par le modele vide.
if [ ! -f "$TARGET_DIR/credentials.py" ]; then
    echo "!! credentials.py absent -- copie du modele vide (credentials.example.py)."
    echo "!! sncf_watch.py/adsb_watch.py et la commande imprimante de la console"
    echo "!! ne fonctionneront PAS tant que $TARGET_DIR/credentials.py n'est pas rempli."
    cp "$REPO_DIR/config/credentials.example.py" "$TARGET_DIR/credentials.py"
fi
cp "$REPO_DIR"/radiono/radiono.py "$RADIONO_TARGET_DIR/"
cp "$REPO_DIR"/config/sound_paths.py "$RADIONO_TARGET_DIR/"
PYTHON_SITE="$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
mkdir -p "$PYTHON_SITE"
cp "$REPO_DIR"/radiono/magicsound.py "$PYTHON_SITE/magicsound.py"
cp "$REPO_DIR"/nas/nas_watch.py "$NAS_TARGET_DIR/"
cp "$REPO_DIR"/console/*.py "$CONSOLE_TARGET_DIR/"
chown -R "$RUN_USER:$RUN_USER" "$RADIONO_TARGET_DIR"

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
# nas_watch.py (mount/umount) et la console (redemarrer/eteindre).
SUDOERS_FILE=/etc/sudoers.d/020-pi-console-watchers
cat > "$SUDOERS_FILE.tmp" <<EOF
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/mount, /usr/bin/umount, /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff
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
# tty1 appartient a la console vocale : plus de login classique sur l'ecran
# (SSH reste le moyen normal d'administration).
systemctl stop getty@tty1.service 2>/dev/null || true
systemctl mask getty@tty1.service

echo "=== 6/6 : unites systemd ==="
cp "$REPO_DIR"/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
for unit in "$REPO_DIR"/systemd/*.service; do
    name="$(basename "$unit")"
    systemctl enable "$name"
    systemctl restart "$name"
done

cat <<EOF

=== Installation terminee ===

Points a verifier manuellement :
- $TARGET_DIR/credentials.py (JAMAIS commite, voir .gitignore) contient
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
