#!/bin/bash
# Installe le module compagnon de nearfield anchor sur une machine plus
# puissante que le Pi (Mac ou Linux), sans droits administrateur :
#   - numpy, cryptography (cles du pont) et cmake via pip --user ;
#   - piper-venv (piper-tts, voix calculees pour le Pi) et, sur Apple
#     Silicon, vlm-venv (mlx-vlm, description d'images), via uv ;
#   - meteor_demod / meteor_decode (dbdexter-dev, libres) compiles dans
#     ~/.nearfield-bridge/bin ;
#   - GNU Radio (conda-forge, dans ~/.nearfield-bridge/gnuradio) et
#     gr-lora_sdr compile dessus : decodage LoRa / Meshtastic des rafales ;
#   - le service, lance a l'ouverture de session (LaunchAgent sur Mac,
#     unite systemd utilisateur sur Linux), a basse priorite ;
#   - sur Mac, l'application "Nearfield Bridge" (dialogues lus par
#     VoiceOver) dans ~/Applications, avec un raccourci sur le bureau :
#     ponts, statistiques, appairage, suppression, pause du service.
# Ailleurs : python3 ~/.nearfield-bridge/nearfield_bridge.py appairage, en
# meme temps que la commande "appairage" sur la console du Pi.
# Desinstaller : bash installer.sh --retirer
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.nearfield-bridge"
BUILD="$DEST/build"
PY="$(command -v python3)"
LABEL="local.nearfield.bridge"

if [ "$1" = "--retirer" ]; then
    if [ "$(uname)" = "Darwin" ]; then
        launchctl unload "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
        rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
    else
        systemctl --user disable --now nearfield-bridge.service 2>/dev/null || true
        rm -f "$HOME/.config/systemd/user/nearfield-bridge.service"
    fi
    for pi in $("$PY" "$DEST/nearfield_bridge.py" ponts 2>/dev/null | cut -f1); do
        "$PY" "$DEST/nearfield_bridge.py" oublier "$pi" || true
    done
    rm -rf "$DEST" "$HOME/Applications/Nearfield Bridge.app" "$HOME/Desktop/Nearfield Bridge"
    echo "Module compagnon retiré."
    exit 0
fi

echo "-- module compagnon : fichiers --"
mkdir -p "$DEST/bin" "$BUILD"
cp "$HERE/nearfield_bridge.py" "$HERE/meteor.py" "$HERE/pont_protocole.py" "$HERE/puissance.py" "$HERE/lora_decode.py" "$HERE/decrire_images.py" \
    "$HERE/voix_piper.py" "$HERE/voix_piper_worker.py" "$DEST/"

echo "-- numpy, cryptography, cmake --"
"$PY" -m pip install --user --quiet --upgrade numpy cryptography cmake
CMAKE="$(command -v cmake || true)"
[ -z "$CMAKE" ] && CMAKE="$("$PY" -c 'import site,os;print(os.path.join(site.USER_BASE,"bin","cmake"))')"

echo "-- meteor_demod / meteor_decode --"
EXTRA="-DCMAKE_BUILD_TYPE=Release"
# Sur Mac, le kit systeme par defaut (celui du macOS installe) : cmake prend
# sinon le plus recent present, parfois trop neuf pour l'editeur de liens
# des outils de developpement (constate avec le kit macOS 27, 2026-10-01).
if [ "$(uname)" = "Darwin" ]; then
    SDK="$(xcode-select -p)/SDKs/MacOSX$(sw_vers -productVersion | cut -d. -f1).sdk"
    [ -d "$SDK" ] || SDK="$(xcode-select -p)/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
    [ -d "$SDK" ] || SDK="$(xcrun --show-sdk-path)"
    # Pas le mode Release : ses options visent les processeurs ARM du Pi
    # (-mfpu), refusees par clang sur Apple Silicon.
    EXTRA="-DCMAKE_OSX_SYSROOT=$SDK -DCMAKE_BUILD_TYPE=Mac -DCMAKE_C_FLAGS=-O3"
fi
for d in meteor_demod meteor_decode; do
    if [ ! -x "$DEST/bin/$d" ]; then
        rm -rf "$BUILD/$d"
        git clone --quiet --depth 1 "https://github.com/dbdexter-dev/$d.git" "$BUILD/$d"
        mkdir -p "$BUILD/$d/build"
        (cd "$BUILD/$d/build" && "$CMAKE" .. $EXTRA -DENABLE_TUI=OFF -DUSE_PNG=OFF >/dev/null \
            && make -j4 >/dev/null && cp "$d" "$DEST/bin/")
    fi
done

echo "-- voix Piper (travaux \"voix\" du Pi : piper-venv) --"
# Environnement Python a part (piper-tts) : le calcul des sons que le Pi
# prepare d'avance (voix_piper.py). Python 3.11 via uv (roues piper-tts).
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    UV="$HOME/.local/bin/uv"
fi
if ! "$DEST/piper-venv/bin/python" -c "import piper" 2>/dev/null; then
    "$UV" venv -q --python 3.11 "$DEST/piper-venv"
    "$UV" pip install -q --python "$DEST/piper-venv/bin/python" piper-tts
fi
"$DEST/piper-venv/bin/python" -c "import piper; print('piper-tts : pret')"

# Description d'images (decrire_images.py) : modeles MLX, Apple Silicon seulement.
if [ "$(uname)-$(uname -m)" = "Darwin-arm64" ]; then
    echo "-- description d'images (vlm-venv : mlx-vlm, argostranslate) --"
    if ! "$DEST/vlm-venv/bin/python" -c "import mlx_vlm, argostranslate" 2>/dev/null; then
        "$UV" venv -q --python 3.12 "$DEST/vlm-venv"
        "$UV" pip install -q --python "$DEST/vlm-venv/bin/python" mlx-vlm argostranslate transformers torch
    fi
    "$DEST/vlm-venv/bin/python" -c "import mlx_vlm; print('mlx-vlm : pret')" || echo "!! vlm-venv incomplet"
fi

echo "-- GNU Radio et gr-lora_sdr (decodage LoRa) --"
GR="$DEST/gnuradio"
if ! "$GR/bin/python" -c "import gnuradio.lora_sdr" 2>/dev/null; then
    if [ ! -x "$DEST/conda/bin/conda" ]; then
        case "$(uname)-$(uname -m)" in
            Darwin-arm64) MF=Miniforge3-MacOSX-arm64.sh ;;
            Darwin-x86_64) MF=Miniforge3-MacOSX-x86_64.sh ;;
            Linux-aarch64) MF=Miniforge3-Linux-aarch64.sh ;;
            *) MF=Miniforge3-Linux-x86_64.sh ;;
        esac
        curl -fsSL -o "$BUILD/miniforge.sh" "https://github.com/conda-forge/miniforge/releases/latest/download/$MF"
        bash "$BUILD/miniforge.sh" -b -p "$DEST/conda" >/dev/null
    fi
    [ -d "$GR" ] || "$DEST/conda/bin/conda" create -y -q -p "$GR" >/dev/null
    "$DEST/conda/bin/conda" install -y -q -p "$GR" -c conda-forge \
        gnuradio-core volk pybind11 numpy cmake make pkg-config cxx-compiler libboost-devel >/dev/null
    rm -rf "$BUILD/gr-lora_sdr"
    git clone --quiet --depth 1 https://github.com/tapparelj/gr-lora_sdr.git "$BUILD/gr-lora_sdr"
    mkdir -p "$BUILD/gr-lora_sdr/build"
    GR_EXTRA=""
    [ "$(uname)" = "Darwin" ] && GR_EXTRA="-DCMAKE_OSX_SYSROOT=$SDK"
    (cd "$BUILD/gr-lora_sdr/build" && PATH="$GR/bin:$PATH" "$GR/bin/cmake" .. $GR_EXTRA \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$GR" -DCMAKE_INSTALL_PREFIX="$GR" \
        -DPYTHON_EXECUTABLE="$GR/bin/python" >/dev/null && make -j4 >/dev/null && make install >/dev/null)
fi
"$GR/bin/python" -c "import gnuradio.lora_sdr; print('gr-lora_sdr : pret')"
rm -rf "$BUILD"

echo "-- service --"
if [ "$(uname)" = "Darwin" ]; then
    # Le service tourne dans une application invisible, "Nearfield Bridge
    # Agent" : macOS n'accorde le disque reseau et le reseau local qu'a une
    # application. Construite d'apres Apple TN3179 (2026-10-02) : vrai
    # programme compile (UUID d'executable unique ; les applets AppleScript
    # partagent tous le meme), Python lance en enfant (connexions attribuees a
    # l'application), application responsable declaree au LaunchAgent, et
    # reconstruite / re-signee SEULEMENT si le lanceur change : macOS garde
    # ainsi l'autorisation "Reseau local" d'une installation a l'autre.
    AGENT_ID="local.nearfield.bridge.agent"
    AGENT_APP="$DEST/Nearfield Bridge Agent.app"
    AGENT_BIN="$AGENT_APP/Contents/MacOS/nearfield-bridge-agent"
    STAMP="$(shasum "$HERE/agent_launcher.c" | cut -d' ' -f1)"
    if [ ! -x "$AGENT_BIN" ] || [ "$(cat "$AGENT_APP/Contents/Resources/source.sha1" 2>/dev/null)" != "$STAMP" ]; then
        echo "   construction de Nearfield Bridge Agent"
        rm -rf "$AGENT_APP"
        mkdir -p "$AGENT_APP/Contents/MacOS" "$AGENT_APP/Contents/Resources"
        clang -O2 -Wall -isysroot "$SDK" -o "$AGENT_BIN" "$HERE/agent_launcher.c"
        cat > "$AGENT_APP/Contents/Info.plist" <<INFO
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>$AGENT_ID</string>
  <key>CFBundleName</key><string>Nearfield Bridge Agent</string>
  <key>CFBundleDisplayName</key><string>Nearfield Bridge Agent</string>
  <key>CFBundleExecutable</key><string>nearfield-bridge-agent</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleDevelopmentRegion</key><string>fr</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSLocalNetworkUsageDescription</key><string>Se connecter au Raspberry Pi deepspace9 pour faire les calculs lourds qu'il lui confie (images de satellites météo, décodage LoRa).</string>
</dict>
</plist>
INFO
        echo "$STAMP" > "$AGENT_APP/Contents/Resources/source.sha1"
        codesign --force -s - --identifier "$AGENT_ID" "$AGENT_APP"
    fi
    # Ancienne enveloppe AppleScript du service (executable partage) : retiree.
    rm -rf "$DEST/Nearfield Bridge Service.app"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$AGENT_BIN</string></array>
  <key>AssociatedBundleIdentifiers</key>
  <array><string>$AGENT_ID</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>Nice</key><integer>10</integer>
</dict>
</plist>
PLIST
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
else
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/nearfield-bridge.service" <<UNIT
[Unit]
Description=Module compagnon nearfield anchor

[Service]
ExecStart=$PY $DEST/nearfield_bridge.py service
Restart=always
RestartSec=30
Nice=10

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable --now nearfield-bridge.service
fi

if [ "$(uname)" = "Darwin" ]; then
    echo "-- application Nearfield Bridge --"
    mkdir -p "$HOME/Applications"
    APP="$HOME/Applications/Nearfield Bridge.app"
    # Reconstruite seulement si le script change : macOS garde ainsi l'acces
    # au disque reseau accorde a l'application (identite stable).
    APP_STAMP="$(shasum "$HERE/dialogue.applescript" | cut -d' ' -f1)"
    if [ "$(cat "$APP/Contents/Resources/source.sha1" 2>/dev/null)" != "$APP_STAMP" ]; then
    rm -rf "$APP"
    osacompile -o "$APP" "$HERE/dialogue.applescript"
    # Langue de l'application = langue du systeme : sinon l'applet est
    # "anglaise" et VoiceOver lit le francais avec une voix anglaise.
    LANG_SYS="$(defaults read -g AppleLanguages 2>/dev/null | sed -n 2p | tr -d ' ",' | cut -d- -f1)"
    LANG_SYS="${LANG_SYS:-fr}"
    /usr/libexec/PlistBuddy -c "Set :CFBundleDevelopmentRegion $LANG_SYS" "$APP/Contents/Info.plist"
    /usr/libexec/PlistBuddy -c "Delete :CFBundleLocalizations" "$APP/Contents/Info.plist" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :CFBundleLocalizations array" -c "Add :CFBundleLocalizations:0 string $LANG_SYS" \
        "$APP/Contents/Info.plist"
    mkdir -p "$APP/Contents/Resources/$LANG_SYS.lproj"
    /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string local.nearfield.bridge.app" "$APP/Contents/Info.plist" 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier local.nearfield.bridge.app" "$APP/Contents/Info.plist"
    echo "$APP_STAMP" > "$APP/Contents/Resources/source.sha1"
    codesign --force --deep -s - "$APP" 2>/dev/null || true  # signature locale refaite apres modification
    fi
    ln -sfn "$HOME/Applications/Nearfield Bridge.app" "$HOME/Desktop/Nearfield Bridge"
    echo "Ouvre « Nearfield Bridge » sur le bureau pour créer un pont."
else
    echo "Pour l'appairage : python3 $DEST/nearfield_bridge.py appairage"
fi
"$PY" "$DEST/nearfield_bridge.py" etat
