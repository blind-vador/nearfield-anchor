#!/bin/bash
# dsd-neo sur le Mac compagnon (decodage TETRA, 2026-10-26) : fork de dsd-fme
# qui decode le TETRA (-fT) depuis un flux I/Q. Le Mac n'a pas de dongle : il
# recoit le flux brut du Pi par rtl_tcp (-i rtltcp:PI:1234:...). Version allegee,
# sans son local (DSD_AUDIO_BACKEND=none, la voix sort par UDP) ni interface.
#
# Reutilise l'environnement conda "radio" deja cree pour dsd-fme (libsndfile,
# ncurses, codec2, cmake). Ajoute openssl. Compile mbelib-neo puis dsd-neo.
#   bash construire_dsd_neo.sh            (relancable)
#   bash construire_dsd_neo.sh --refaire
set -e
ICI="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.nearfield-bridge"
ENV="$DEST/radio"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

if [ -x "$ENV/bin/dsd-neo" ] && [ "$1" != "--refaire" ]; then
    echo "-- dsd-neo deja compile ($ENV/bin/dsd-neo) --"; exit 0
fi
CONDA="$DEST/conda/bin/conda"
[ -x "$CONDA" ] || { echo "conda absent : lancer installer.sh d'abord"; exit 1; }
[ -d "$ENV" ] || "$CONDA" create -y -q -p "$ENV" >/dev/null

echo "-- dependances (openssl, libsndfile, ncurses, cmake) --"
# rtl-sdr : fournit librtlsdr ; le pipeline radio I/Q de dsd-neo (partage avec
# l'entree rtltcp) n'est compile que si le support RTL-SDR est actif, meme sans
# dongle local sur le Mac.
"$CONDA" install -y -q -p "$ENV" -c conda-forge openssl libsndfile ncurses cmake make pkg-config rtl-sdr >/dev/null

SDK="$(xcrun --show-sdk-path)"
export PATH="$ENV/bin:$PATH"
COMMUN="-DCMAKE_OSX_SYSROOT=$SDK -DCMAKE_PREFIX_PATH=$ENV -DCMAKE_INSTALL_PREFIX=$ENV -DCMAKE_INSTALL_RPATH=$ENV/lib -DCMAKE_BUILD_TYPE=Release"

echo "-- mbelib-neo --"
git clone --quiet https://github.com/arancormonk/mbelib-neo.git "$BUILD/mbelib-neo"
cmake -S "$BUILD/mbelib-neo" -B "$BUILD/mbelib-neo/build" $COMMUN >/dev/null
cmake --build "$BUILD/mbelib-neo/build" -j4 >/dev/null
cmake --install "$BUILD/mbelib-neo/build" >/dev/null

echo "-- dsd-neo (allege : sans dongle local, sans son, sans interface) --"
git clone --quiet https://github.com/arancormonk/dsd-neo.git "$BUILD/dsd-neo"
# Version eprouvee + correctif nearfield (2026-10-27) : imprime le contenu des
# messages courts TETRA ("[TETRA SDS PAYLOAD]"), lu par bridge/tetra_neo.py.
git -C "$BUILD/dsd-neo" checkout --quiet eb1a3409
git -C "$BUILD/dsd-neo" apply "$ICI/dsd-neo-nearfield-sds.patch"
cmake -S "$BUILD/dsd-neo" -B "$BUILD/dsd-neo/build" $COMMUN \
    -DOPENSSL_ROOT_DIR="$ENV" \
    -DDSD_AUDIO_BACKEND=none \
    -DDSD_ENABLE_RTLSDR=ON -DDSD_ENABLE_AIRSPY=OFF -DDSD_ENABLE_SOAPYSDR=OFF \
    -DDSD_ENABLE_TERMINAL_UI=OFF -DDSD_WARNINGS_AS_ERRORS=OFF -DBUILD_TESTING=OFF >/dev/null
cmake --build "$BUILD/dsd-neo/build" -j4 >/dev/null
cmake --install "$BUILD/dsd-neo/build" >/dev/null

"$ENV/bin/dsd-neo" --help 2>&1 | grep -m1 -i "tetra\|usage\|version" || true
echo "dsd-neo : pret ($ENV/bin/dsd-neo)"
