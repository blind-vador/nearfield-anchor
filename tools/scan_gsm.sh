#!/bin/sh
# Balayage des cellules GSM (2026-10-08, point 4 : presence/sante du reseau
# cellulaire -- cellules diffusees publiquement, JAMAIS d'identifiant d'abonne
# ni de contenu). gr-gsm est compile avec le numpy Debian 1.x ; le numpy 2 de
# pip dans /usr/local casse GNU Radio, d'ou python3 -S + le seul dist-packages
# Debian. Immobilise la cle : arrete radio-watch le temps du balayage.
#   usage : tools/scan_gsm.sh [GSM900|DCS1800] [duree_s]
BAND="${1:-GSM900}"; DUREE="${2:-150}"
echo "Arret de radio-watch (cle liberee)..."
sudo systemctl stop radio-watch
echo "Balayage $BAND pendant ${DUREE}s (Ctrl-C pour arreter)..."
timeout "$DUREE" env PYTHONPATH=/usr/lib/python3/dist-packages /usr/bin/python3 -S \
    /usr/bin/grgsm_scanner -b "$BAND" --speed 8 -v
echo "Relance de radio-watch..."
sudo systemctl start radio-watch
