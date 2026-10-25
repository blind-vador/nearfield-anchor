#!/bin/bash
# dsd-fme sur une machine compagne (capacite "radio", 2026-10-25) : les juges
# du scanner (rejeu des sons bruts devant chaque decodeur) tournent ici quand
# cette machine est plus libre que le Pi.
#
# Sans Homebrew ni droits administrateur : environnement conda dedie
# ($DEST/radio : cmake, libsndfile, ncurses, codec2), mbelib compile dedans,
# et un PulseAudio "muet" (pulse_muet/) a la place du vrai -- dsd-fme l'exige
# pour se compiler, mais la machine compagne ne joue jamais de son (-o null).
# Meme version de dsd-fme que le Pi (DSD_COMMIT) : memes verdicts des deux cotes.
#
#   bash construire_dsd.sh            (appele par installer.sh, relancable)
#   bash construire_dsd.sh --refaire  (recompile meme si dsd-fme est la)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.nearfield-bridge"
ENV="$DEST/radio"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
DSD_COMMIT=4fe32db      # celui du Pi (dsd-fme -h : "AW 2026-52-g4fe32db")
MBE_COMMIT=master

if [ -x "$ENV/bin/dsd-fme" ] && [ "$1" != "--refaire" ]; then
    echo "-- dsd-fme deja compile ($ENV/bin/dsd-fme) --"
    exit 0
fi

echo "-- environnement radio (conda-forge) --"
CONDA="$DEST/conda/bin/conda"
[ -x "$CONDA" ] || { echo "conda absent ($CONDA) : lancer installer.sh d'abord"; exit 1; }
[ -d "$ENV" ] || "$CONDA" create -y -q -p "$ENV" >/dev/null
"$CONDA" install -y -q -p "$ENV" -c conda-forge cmake make pkg-config libsndfile ncurses codec2 >/dev/null

EXTRA=""
if [ "$(uname)" = "Darwin" ]; then
    SDK="$(xcode-select -p)/SDKs/MacOSX$(sw_vers -productVersion | cut -d. -f1).sdk"
    [ -d "$SDK" ] || SDK="$(xcrun --show-sdk-path)"
    EXTRA="-DCMAKE_OSX_SYSROOT=$SDK"
    LIBEXT=dylib
else
    LIBEXT=so
fi
export PATH="$ENV/bin:$PATH"
CMAKE_COMMUN="$EXTRA -DCMAKE_PREFIX_PATH=$ENV -DCMAKE_INSTALL_PREFIX=$ENV -DCMAKE_INSTALL_RPATH=$ENV/lib -DCMAKE_POLICY_VERSION_MINIMUM=3.5"  # mbelib date de cmake 2.x

echo "-- PulseAudio muet --"
mkdir -p "$ENV/include/pulse" "$ENV/lib"
cp "$HERE"/pulse_muet/pulse/*.h "$ENV/include/pulse/"
for lib in pulse pulse-simple pulse-mainloop; do
    if [ "$LIBEXT" = dylib ]; then
        cc -O2 -dynamiclib -I"$HERE/pulse_muet" -install_name "$ENV/lib/lib$lib.dylib" \
            -o "$ENV/lib/lib$lib.dylib" "$HERE/pulse_muet/pulse_muet.c"
    else
        cc -O2 -shared -fPIC -I"$HERE/pulse_muet" -o "$ENV/lib/lib$lib.so" "$HERE/pulse_muet/pulse_muet.c"
    fi
done

echo "-- mbelib --"
git clone --quiet https://github.com/szechyjs/mbelib.git "$BUILD/mbelib"
(cd "$BUILD/mbelib" && git checkout --quiet "$MBE_COMMIT" && mkdir build && cd build \
    && cmake .. $CMAKE_COMMUN >/dev/null && make -j4 >/dev/null && make install >/dev/null)

echo "-- dsd-fme ($DSD_COMMIT) --"
git clone --quiet https://github.com/lwvmobile/dsd-fme.git "$BUILD/dsd-fme"
(cd "$BUILD/dsd-fme" && git checkout --quiet "$DSD_COMMIT" && mkdir build && cd build \
    && cmake .. $CMAKE_COMMUN -DCURSES_NEED_NCURSES=TRUE >/dev/null && make -j4 >/dev/null && make install >/dev/null)

"$ENV/bin/dsd-fme" -h 2>&1 | grep -m1 "Build Version" || true
echo "dsd-fme : pret ($ENV/bin/dsd-fme)"
