#!/bin/bash
# Installe le module compagnon de nearfield anchor sur une machine plus
# puissante que le Pi (Mac ou Linux), sans droits administrateur :
#   - numpy, cryptography (cles du pont) et cmake via pip --user ;
#   - meteor_demod / meteor_decode (dbdexter-dev, libres) compiles dans
#     ~/.nearfield-bridge/bin ;
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
cp "$HERE/nearfield_bridge.py" "$HERE/meteor.py" "$HERE/pont_protocole.py" "$DEST/"

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
rm -rf "$BUILD"

echo "-- service --"
if [ "$(uname)" = "Darwin" ]; then
    # Le service tourne dans une application invisible : macOS n'autorise le
    # disque reseau qu'a une application (une fois, par la fenetre systeme
    # "souhaite acceder aux fichiers d'un volume reseau"), pas a un Python
    # lance directement par launchd (constate le 2026-10-01).
    SERVICE_APP="$DEST/Nearfield Bridge Service.app"
    rm -rf "$SERVICE_APP"
    osacompile -o "$SERVICE_APP" "$HERE/service.applescript"
    /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$SERVICE_APP/Contents/Info.plist"
    /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier local.nearfield.bridge.service" "$SERVICE_APP/Contents/Info.plist" 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string local.nearfield.bridge.service" "$SERVICE_APP/Contents/Info.plist"
    codesign --force --deep -s - "$SERVICE_APP" 2>/dev/null || true
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$SERVICE_APP/Contents/MacOS/applet</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
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
    rm -rf "$HOME/Applications/Nearfield Bridge.app"
    APP="$HOME/Applications/Nearfield Bridge.app"
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
    codesign --force --deep -s - "$APP" 2>/dev/null || true  # signature locale refaite apres modification
    ln -sfn "$HOME/Applications/Nearfield Bridge.app" "$HOME/Desktop/Nearfield Bridge"
    echo "Ouvre « Nearfield Bridge » sur le bureau pour créer un pont."
else
    echo "Pour l'appairage : python3 $DEST/nearfield_bridge.py appairage"
fi
"$PY" "$DEST/nearfield_bridge.py" etat
