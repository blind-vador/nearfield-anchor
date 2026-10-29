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

echo "=== 1/5  venv Python + dépendances ==="
mkdir -p "$APP"
[ -d "$VENV" ] || /usr/bin/python3 -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip
"$VENV/bin/pip" -q install -r "$HERE/requirements.txt"

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
# Passe complète la nuit (02:30).
plist local.nearfield.analyse \
  "<string>$VENV/bin/python</string><string>$APP/analyse.py</string><string>--hours</string><string>24</string>" \
  "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>30</integer></dict><key>RunAtLoad</key><false/>"
# File d'examen à la demande (touche "e" du journal), toutes les 15 min.
plist local.nearfield.examen \
  "<string>$VENV/bin/python</string><string>$APP/analyse.py</string><string>--queue</string>" \
  "<key>StartInterval</key><integer>900</integer><key>RunAtLoad</key><true/>"
U=$(id -u)
for a in disques analyse examen; do
  launchctl bootout "gui/$U" "$AGENTS/local.nearfield.$a.plist" 2>/dev/null || true
  launchctl bootstrap "gui/$U" "$AGENTS/local.nearfield.$a.plist"
done

echo "=== 5/5  terminé ==="
cat <<FIN

Installé : venv $VENV, modèles $PANNS, script $APP/analyse.py, 3 agents launchd.

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
