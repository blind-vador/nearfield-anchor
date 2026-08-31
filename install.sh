#!/usr/bin/env bash
# Installeur "clef en main" pour l'ecosysteme watchers (radio/train/meteo/
# avion/crue/ACARS + gestion NAS + radiono). Pense pour un Raspberry Pi OS
# (Debian armhf), teste sur le Pi de reference de ce projet.
#
# Ce script :
#   1) installe les paquets systeme (apt)
#   2) compile RTLSDR-Airband, mbelib+dsd-fme, libacars+acarsdec depuis les
#      sources (aucun n'est empaquete Debian) -- saute une etape si le
#      binaire attendu existe deja (idempotent, relancable sans risque)
#   3) installe les dependances Python (pip)
#   4) aplatit watchers/ + lib/ + config/ dans un seul repertoire cible
#      (preserve le style d'imports plats des scripts -- aucune
#      modification de code necessaire)
#   5) deploie les unites systemd et les active
#
# Usage : sudo bash install.sh
set -euo pipefail

TARGET_DIR="${TARGET_DIR:-/home/vador/autopilot/pi_console}"
RADIONO_TARGET_DIR="${RADIONO_TARGET_DIR:-/home/vador/autopilot}"
NAS_TARGET_DIR="${NAS_TARGET_DIR:-/home/vador/autopilot/nas_manager}"
BUILD_DIR="${BUILD_DIR:-/tmp/pi-console-watchers-build}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== 1/5 : paquets systeme (apt) ==="
apt-get update
apt-get install -y \
    espeak ffmpeg rtl-sdr librtlsdr-dev libjansson-dev \
    build-essential cmake git python3-pip

echo "=== 2/5 : compilation des bibliotheques externes (idempotent) ==="
mkdir -p "$BUILD_DIR"

if [ ! -f "$TARGET_DIR/rtl_airband/src/build/src/rtl_airband" ]; then
    echo "-- RTLSDR-Airband --"
    mkdir -p "$TARGET_DIR"
    git clone https://github.com/szpajder/RTLSDR-Airband.git "$TARGET_DIR/rtl_airband" || true
    mkdir -p "$TARGET_DIR/rtl_airband/src/build"
    (cd "$TARGET_DIR/rtl_airband/src/build" && cmake -DNFM=ON .. && make -j4)
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

echo "=== 3/5 : dependances Python (pip) ==="
pip3 install -r "$REPO_DIR/requirements.txt"

echo "=== 4/5 : deploiement des fichiers (aplati dans $TARGET_DIR) ==="

if [ ! -f "$REPO_DIR/config/credentials.py" ]; then
    echo "!! config/credentials.py absent -- copie du modele vide (credentials.example.py)."
    echo "!! Les watchers sncf_watch.py/adsb_watch.py ne fonctionneront PAS tant que"
    echo "!! ce fichier n'est pas rempli avec de vrais identifiants (voir README.md)."
    cp "$REPO_DIR/config/credentials.example.py" "$REPO_DIR/config/credentials.py"
fi

mkdir -p "$TARGET_DIR" "$TARGET_DIR/rnnoise_lib" "$RADIONO_TARGET_DIR" "$NAS_TARGET_DIR"
cp "$REPO_DIR"/watchers/*.py "$TARGET_DIR/"
cp "$REPO_DIR"/lib/*.py "$TARGET_DIR/"
cp "$REPO_DIR"/lib/rnnoise_lib/*.py "$TARGET_DIR/rnnoise_lib/"
cp "$REPO_DIR"/config/sound_paths.py "$TARGET_DIR/"
cp "$REPO_DIR"/config/credentials.py "$TARGET_DIR/"
cp "$REPO_DIR"/radiono/radiono.py "$RADIONO_TARGET_DIR/"
cp "$REPO_DIR"/config/sound_paths.py "$RADIONO_TARGET_DIR/"
cp "$REPO_DIR"/radiono/magicsound.py /usr/local/lib/python3.9/dist-packages/magicsound.py 2>/dev/null || \
    echo "!! adapte le chemin dist-packages a ta version de python3 si besoin (radiono/magicsound.py)"
cp "$REPO_DIR"/nas/nas_watch.py "$NAS_TARGET_DIR/"

echo "=== 5/5 : unites systemd ==="
cp "$REPO_DIR"/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
for unit in "$REPO_DIR"/systemd/*.service; do
    name="$(basename "$unit")"
    systemctl enable "$name"
    systemctl restart "$name"
done

cat <<'EOF'

=== Installation terminee ===

Points a verifier manuellement :
- config/credentials.py (JAMAIS commite, voir .gitignore) contient les
  jetons SNCF_TOKEN et OPENSKY_CLIENT_ID/SECRET -- si ce fichier vient
  d'etre cree a partir du modele vide (credentials.example.py), edite-le
  avec tes propres identifiants puis redemarre sncf-watch/adsb-watch :
  systemctl restart sncf-watch adsb-watch
- config/sound_paths.py suppose un NAS monte sur /media/nas/nd1/server --
  adapte NAS_BASE si ton organisation differe.
- nas/legacy/ (nas.py, smbv1.conf, smbv2.conf) n'est PAS deploye
  automatiquement -- conserve pour reference historique uniquement,
  remplace par nas_watch.py (montage par UUID, plus robuste).
- Verifie chaque service : systemctl status <nom>.service

EOF
