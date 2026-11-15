#!/usr/bin/env bash
# Installeur clef en main de la MACHINE COMPAGNE (Mac mini) de nearfield-anchor.
# Met en place la passe d'analyse IA (PANNs + DeepFilterNet) qui examine les
# clips du NAS et dépose des propositions dans le "journal proposition" du Pi.
#
# Usage : bash mac/install-mac.sh      (depuis le dépôt cloné)
# Idempotent : relançable sans risque.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../mac
HOME_DIR="$HOME"
APP="$HOME_DIR/pcw-analyse"            # venv + script stable + logs
VENV="$APP/venv"
PANNS="$HOME_DIR/panns_data"
AGENTS="$HOME_DIR/Library/LaunchAgents"
NAS_USER="${NFA_NAS_USER:-pi}"                       # compte Samba du NAS (pas l'utilisateur du Mac)
NAS_HOST="${NFA_NAS_HOST:-raspberrypi.local}"

echo "=== 1/5  venv Python 3.11 + dépendances ==="
# 2026-11-16 : Python 3.11 (les correctifs de sécurité de pillow, torch, urllib3… exigent
# 3.10 ou plus ; DeepFilterLib s'arrête à 3.11). Le Python 3.9 des outils macOS ne suffit
# plus. Source, dans l'ordre : le conda de nearfield (~/.nearfield-bridge/conda, conda-forge),
# un python3.11 déjà installé, sinon on s'arrête en disant quoi faire.
mkdir -p "$APP"
CONDA="${NFA_CONDA:-$HOME_DIR/.nearfield-bridge/conda}"
if [ -x "$VENV/bin/python" ] && ! "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 11))'; then
  ANCIEN="$APP/venv-ancien-$(date +%Y%m%d)"
  echo "-- ancien venv ($("$VENV/bin/python" --version 2>&1)) gardé dans $ANCIEN --"
  mv "$VENV" "$ANCIEN"
fi
if [ ! -x "$VENV/bin/python" ]; then
  if [ -x "$CONDA/bin/mamba" ] || [ -x "$CONDA/bin/conda" ]; then
    OUTIL="$CONDA/bin/mamba"; [ -x "$OUTIL" ] || OUTIL="$CONDA/bin/conda"
    "$OUTIL" create -y -q -p "$VENV" python=3.11 pip
  elif command -v python3.11 >/dev/null 2>&1; then
    python3.11 -m venv "$VENV"
  else
    echo "!! Python 3.11 introuvable : installe le conda de nearfield (Nearfield Bridge) ou python.org 3.11, puis relance."
    exit 1
  fi
fi
"$VENV/bin/python" -m pip -q install --upgrade pip
"$VENV/bin/python" -m pip -q install -r "$HERE/requirements.txt"

echo "=== 2/5  modèles (PANNs CNN14 + labels) ==="
mkdir -p "$PANNS"
[ -f "$PANNS/class_labels_indices.csv" ] || curl -fsSL -o "$PANNS/class_labels_indices.csv" \
  "https://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv"
[ -f "$PANNS/Cnn14_mAP=0.431.pth" ] || curl -fL --retry 2 -o "$PANNS/Cnn14_mAP=0.431.pth" \
  "https://zenodo.org/record/3987831/files/Cnn14_mAP%3D0.431.pth?download=1"
# DeepFilterNet3 se télécharge seul au 1er usage (~/Library/Caches/DeepFilterNet).

echo "=== 3/5  script stable ==="
cp "$HERE/analyse.py" "$APP/analyse.py"

echo "=== 4/5  agents launchd (générés avec vos chemins) ==="
mkdir -p "$AGENTS"
plist() {  # $1=label  $2=ProgramArguments(xml)  $3=extra(xml)
  cat > "$AGENTS/$1.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array>$2</array>
  $3
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$APP/$1.log</string>
  <key>StandardErrorPath</key><string>$APP/$1.log</string>
</dict></plist>
PL
}
# Montage NAS Samba (identifiants dans le trousseau), re-vérif toutes les 5 min.
plist local.nearfield.disques \
  "<string>/bin/sh</string><string>-c</string><string>for i in \$(seq 1 60); do /usr/bin/nc -z -G 2 $NAS_HOST 445 2>/dev/null &amp;&amp; break; sleep 5; done; for s in server stockage; do /sbin/mount | /usr/bin/grep -q \"on /Volumes/\$s \" || /usr/bin/osascript -e \"mount volume \\\"smb://$NAS_USER@$NAS_HOST/\$s\\\"\" 2>/dev/null; done</string>" \
  "<key>RunAtLoad</key><true/><key>StartInterval</key><integer>300</integer>"
# 2026-11-11 : plus d'agent "analyse" ni "examen". macOS refuse l'acces aux
# volumes reseau aux taches launchd d'arriere-plan ; la passe est lancee par
# l'agent du pont (bridge/examen_mac.py, travaux "examen" et "examen_lot" du Pi).
U=$(id -u)
launchctl bootout "gui/$U" "$AGENTS/local.nearfield.disques.plist" 2>/dev/null || true
launchctl bootstrap "gui/$U" "$AGENTS/local.nearfield.disques.plist"
for a in analyse examen; do   # anciens agents : retires s'ils sont encore la
  launchctl bootout "gui/$U" "$AGENTS/local.nearfield.$a.plist" 2>/dev/null || true
  rm -f "$AGENTS/local.nearfield.$a.plist"
done

echo "=== 5/5  terminé ==="
cat <<FIN

Installé : venv $VENV, modèles $PANNS, script $APP/analyse.py, agent launchd de montage du NAS.\nLa passe elle-même est lancée par le pont (bridge/installer.sh).

À FAIRE MANUELLEMENT (une fois) :
  1. Monter le NAS : Finder > Aller > Se connecter au serveur >
       smb://$NAS_HOST/server   (coche "mémoriser dans le trousseau")
     (l'agent 'disques' le remontera ensuite tout seul).
  2. Pour que le Mac POUSSE le backup sur GitHub, ajouter un jeton (fine-grained,
     Contents read/write) dans ~/.git-credentials puis :
       git config --global --add credential.helper store
       printf 'https://VOTRE_USER:VOTRE_JETON@github.com\n' >> ~/.git-credentials
       chmod 600 ~/.git-credentials
  3. Récupérer les sons de la gare (branche assets) et les poser sur le NAS :
       git fetch origin assets && git checkout assets   # ou cloner la branche
       cp -R train_scaner/. /Volumes/server/server/train_scaner/   # via le NAS monté
FIN
