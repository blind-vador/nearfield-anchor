#!/bin/bash
# dsd-neo sur le Pi lui-meme (decodage TETRA natif, 2026-11-08) : le Pi n'a plus
# besoin d'une machine compagne pour le TETRA. Meme version et meme correctif que
# le Mac (construire_dsd_neo.sh). Installe dans ~/autopilot/dsd-neo, sans sudo.
# Compile lentement (nice, 2 coeurs) : le Pi chauffe vite.
#   bash construire_dsd_neo_pi.sh            (relancable)
#   bash construire_dsd_neo_pi.sh --refaire
set -e
ICI="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/autopilot/dsd-neo"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
if [ -x "$DEST/bin/dsd-neo" ] && [ "$1" != "--refaire" ]; then
    echo "-- dsd-neo deja compile ($DEST/bin/dsd-neo) --"; exit 0
fi
for p in cmake libsndfile1-dev librtlsdr-dev libssl-dev libncurses-dev; do
    dpkg -s "$p" >/dev/null 2>&1 || { echo "paquet manquant : $p (sudo apt install $p)"; exit 1; }
done
COMMUN="-DCMAKE_PREFIX_PATH=$DEST -DCMAKE_INSTALL_PREFIX=$DEST -DCMAKE_INSTALL_RPATH=$DEST/lib -DCMAKE_BUILD_TYPE=Release"
LENT="nice -n 19"

echo "-- mbelib-neo --"
git clone --quiet https://github.com/arancormonk/mbelib-neo.git "$BUILD/mbelib-neo"
cmake -S "$BUILD/mbelib-neo" -B "$BUILD/mbelib-neo/build" $COMMUN >/dev/null
$LENT cmake --build "$BUILD/mbelib-neo/build" -j2 >/dev/null
cmake --install "$BUILD/mbelib-neo/build" >/dev/null

echo "-- dsd-neo (sans son, sans interface ; entree rtl_tcp et dongle) --"
git clone --quiet https://github.com/arancormonk/dsd-neo.git "$BUILD/dsd-neo"
git -C "$BUILD/dsd-neo" checkout --quiet eb1a3409
git -C "$BUILD/dsd-neo" apply "$ICI/dsd-neo-nearfield-sds.patch"
cmake -S "$BUILD/dsd-neo" -B "$BUILD/dsd-neo/build" $COMMUN \
    -DDSD_AUDIO_BACKEND=none \
    -DDSD_ENABLE_RTLSDR=ON -DDSD_ENABLE_AIRSPY=OFF -DDSD_ENABLE_SOAPYSDR=OFF \
    -DDSD_ENABLE_TERMINAL_UI=OFF -DDSD_WARNINGS_AS_ERRORS=OFF -DBUILD_TESTING=OFF >/dev/null
$LENT cmake --build "$BUILD/dsd-neo/build" -j2 >/dev/null
cmake --install "$BUILD/dsd-neo/build" >/dev/null

"$DEST/bin/dsd-neo" --help 2>&1 | grep -m1 -i "tetra\|usage\|version" || true
echo "dsd-neo : pret ($DEST/bin/dsd-neo)"
