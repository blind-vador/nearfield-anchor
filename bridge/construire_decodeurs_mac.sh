#!/bin/bash
# Decodeurs du Pi sur le Mac compagnon (2026-11-01), AUX MEMES VERSIONS que le
# Pi (verifiees le 2026-11-01), pour que Pi et compagne rendent les memes
# verdicts sur les memes sons (travail partage, tickets) :
#   multimon-ng 1.2.0, direwolf 1.6, rtl_433 22.11, redsea 7555c9f,
#   libacars 2.2.1 + acarsdec 3.7 + dumpvdl2 2.7.0, RNNoise (pyrnnoise),
#   Python 3.11 + numpy + Pillow + sstv 0.2.0 (decodeurs Python du depot),
#   freedv_rx (codec2, juges de la BLU), whisper.cpp (transcription du soir).
# Conda-forge n'a aucun d'eux pour Mac arm64 : compiles depuis les sources
# officielles dans l'environnement conda "radio" (comme dsd-neo).
#   bash construire_decodeurs_mac.sh            (relancable, saute ce qui est fait)
#   bash construire_decodeurs_mac.sh --refaire
set -e
DEST="$HOME/.nearfield-bridge"
ENV="$DEST/radio"
PY="$DEST/py311"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
REFAIRE="$1"
CONDA="$DEST/conda/bin/conda"
[ -x "$CONDA" ] || { echo "conda absent : lancer installer.sh d'abord"; exit 1; }
[ -d "$ENV" ] || "$CONDA" create -y -q -p "$ENV" >/dev/null

fait() { [ -x "$ENV/bin/$1" ] && [ "$REFAIRE" != "--refaire" ]; }

echo "-- dependances (conda-forge) --"
"$CONDA" install -y -q -p "$ENV" -c conda-forge cmake make pkg-config rtl-sdr libsndfile \
    portaudio liquid-dsp nlohmann_json meson ninja glib libxml2 zlib codec2 >/dev/null

SDK="$(xcrun --show-sdk-path)"
export PATH="$ENV/bin:$PATH"
export PKG_CONFIG_PATH="$ENV/lib/pkgconfig:$ENV/share/pkgconfig"
# CMAKE_POLICY_VERSION_MINIMUM : les versions du Pi (2016-2022) declarent un CMake
# trop ancien pour CMake 4 ; on garde leur code tel quel.
COMMUN="-DCMAKE_OSX_SYSROOT=$SDK -DCMAKE_PREFIX_PATH=$ENV -DCMAKE_INSTALL_PREFIX=$ENV -DCMAKE_INSTALL_RPATH=$ENV/lib -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5"

cloner() {  # depot, version (etiquette ou commit), dossier
    git clone --quiet "$1" "$BUILD/$3"
    git -C "$BUILD/$3" checkout --quiet "$2"
}
cmake_installer() {  # dossier, options supplementaires
    cmake -S "$BUILD/$1" -B "$BUILD/$1/build" $COMMUN $2 > "$BUILD/$1.cmake.log" 2>&1 || { tail -20 "$BUILD/$1.cmake.log"; return 1; }
    cmake --build "$BUILD/$1/build" -j4 >/dev/null
    cmake --install "$BUILD/$1/build" >/dev/null
}

if ! fait multimon-ng; then
    echo "-- multimon-ng 1.2.0 --"
    cloner https://github.com/EliasOenal/multimon-ng.git 1.2.0 multimon-ng
    cmake_installer multimon-ng "-DCMAKE_DISABLE_FIND_PACKAGE_X11=ON -DCMAKE_DISABLE_FIND_PACKAGE_PulseAudio=ON"
fi

if ! fait direwolf; then
    echo "-- direwolf 1.6 --"
    cloner https://github.com/wb2osz/direwolf.git 1.6 direwolf
    cmake_installer direwolf "-DUNITTEST=OFF"
fi

if ! fait rtl_433; then
    echo "-- rtl_433 22.11 --"
    cloner https://github.com/merbanan/rtl_433.git 22.11 rtl_433
    cmake_installer rtl_433 "-DENABLE_RTLSDR=ON -DENABLE_SOAPYSDR=OFF -DENABLE_OPENSSL=OFF"
fi

if ! fait redsea; then
    echo "-- redsea 7555c9f --"
    cloner https://github.com/windytan/redsea.git 7555c9f redsea
    # liquid-dsp vient de conda : meson ne le trouve qu'avec ces chemins
    (cd "$BUILD/redsea" && CXXFLAGS="-I$ENV/include" LDFLAGS="-L$ENV/lib -Wl,-rpath,$ENV/lib" \
        meson setup -Dbuildtype=release --prefix="$ENV" build >/dev/null \
        && meson compile -C build >/dev/null && meson install -C build >/dev/null)
fi

if ! fait dumpvdl2 || ! fait acarsdec; then
    echo "-- libacars 2.2.1, acarsdec 3.7, dumpvdl2 2.7.0 --"
    cloner https://github.com/szpajder/libacars.git v2.2.1 libacars
    cmake_installer libacars ""
    cloner https://github.com/TLeconte/acarsdec.git acarsdec-3.7 acarsdec
    # HOST_NAME_MAX n'existe que sous Linux : valeur Linux (255) donnee a la
    # compilation ; acarsdec ne cherche ses en-tetes que dans /usr/include. Sans
    # le module dongle (pthread_tryjoin_np, propre a la libc GNU) : le Mac n'a
    # pas de dongle, il decode des FICHIERS son (option -f), ce qui sert au
    # travail partage.
    CFLAGS="-DHOST_NAME_MAX=255 -I$ENV/include" LDFLAGS="-L$ENV/lib" \
        cmake_installer acarsdec "-Drtl=OFF"
    cloner https://github.com/szpajder/dumpvdl2.git v2.7.0 dumpvdl2
    CFLAGS="-I$ENV/include" LDFLAGS="-L$ENV/lib" \
        cmake_installer dumpvdl2 "-DSOAPYSDR=OFF -DSDRPLAY=OFF -DSDRPLAY3=OFF -DMIRISDR=OFF -DRTLSDR=ON"
fi

RN="$DEST/decodeurs/rnnoise_lib"
if [ ! -f "$RN/librnnoise.dylib" ] || [ "$REFAIRE" = "--refaire" ]; then
    echo "-- RNNoise (pyrnnoise, comme le Pi) --"
    git clone --quiet --recursive https://github.com/pengzhendong/pyrnnoise.git "$BUILD/pyrnnoise"
    git -C "$BUILD/pyrnnoise" checkout --quiet fe603b2   # celle du Pi (compile le 2026-10-29)
    git -C "$BUILD/pyrnnoise" submodule update --quiet --init --recursive
    # Poids du modele (rnnoise_data.c) : comme rnnoise/download_model.sh, mais sans
    # wget ni sha256sum (absents du Mac) -- empreinte SHA-256 verifiee.
    (cd "$BUILD/pyrnnoise/rnnoise" && empreinte="$(cat model_version)" \
        && curl -fsSL -o modele.tar.gz "https://media.xiph.org/rnnoise/models/rnnoise_data-$empreinte.tar.gz" \
        && [ "$(shasum -a 256 modele.tar.gz | cut -d' ' -f1)" = "$empreinte" ] \
        && tar xomf modele.tar.gz) || { echo "modele RNNoise : telechargement ou empreinte invalide"; exit 1; }
    cmake -S "$BUILD/pyrnnoise" -B "$BUILD/pyrnnoise/build" -DCMAKE_OSX_SYSROOT="$SDK" \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS="-O3" -DCMAKE_POLICY_VERSION_MINIMUM=3.5 >/dev/null
    make -C "$BUILD/pyrnnoise/build" -j4 >/dev/null
    mkdir -p "$RN"
    cp "$BUILD"/pyrnnoise/build/librnnoise.dylib "$RN/"
fi

if [ ! -x "$PY/bin/python" ] || [ "$REFAIRE" = "--refaire" ]; then
    echo "-- Python 3.11 des decodeurs (numpy, Pillow, sstv 0.2.0) --"
    "$CONDA" create -y -q -p "$PY" -c conda-forge python=3.11 numpy pillow pip >/dev/null
    "$PY/bin/pip" install -q sstv==0.2.0
fi

WH="$DEST/whisper.cpp"
if [ ! -x "$WH/build/bin/whisper-cli" ] || [ "$REFAIRE" = "--refaire" ]; then
    echo "-- whisper.cpp v1.8.2 + modele small (transcription du soir) --"
    [ -d "$WH/.git" ] || git clone --quiet https://github.com/ggml-org/whisper.cpp.git "$WH"
    git -C "$WH" checkout --quiet v1.8.2
    cmake -S "$WH" -B "$WH/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_SYSROOT="$SDK" >/dev/null
    cmake --build "$WH/build" -j4 --target whisper-cli >/dev/null
    (cd "$WH" && bash models/download-ggml-model.sh small >/dev/null)
fi

echo "-- verification --"
for b in multimon-ng direwolf rtl_433 redsea acarsdec dumpvdl2 freedv_rx; do
    [ -x "$ENV/bin/$b" ] && echo "  $b : ok" || echo "  $b : ABSENT"
done
[ -f "$RN/librnnoise.dylib" ] && echo "  RNNoise : ok" || echo "  RNNoise : ABSENT"
"$PY/bin/python" -c "import sstv, numpy; print('  Python decodeurs : ok (numpy', numpy.__version__, ')')"
