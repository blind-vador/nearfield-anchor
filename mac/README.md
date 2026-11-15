# Passe d'analyse IA (Mac mini)

**Installation clef en main : `bash mac/install-mac.sh`** (depuis le dépôt cloné) — crée le venv, installe les dépendances, télécharge les modèles, pose le script et l'agent de montage du NAS. La passe est lancée par l'agent du pont (`bridge/examen_mac.py`). Détails ci-dessous.

`analyse.py` tourne sur le Mac mini (M4), PAS sur le Pi. Il lit les clips du NAS
(partage Samba monté), score voix/musique via **PANNs CNN14**, nettoie les cas
ambigus (noisereduce + **DeepFilterNet**), et dépose des propositions dans
`radio_log/propositions.json` (console Pi : commande « journal proposition »).

## Reproduire l'installation
Python **3.11** (conda-forge, par le conda de nearfield), venv dédié hors dépôt :

```
~/.nearfield-bridge/conda/bin/mamba create -y -p ~/pcw-analyse/venv python=3.11 pip
~/pcw-analyse/venv/bin/python -m pip install -r requirements.txt
cp analyse.py ~/pcw-analyse/analyse.py
```

Pourquoi 3.11 (2026-11-16) : le Python 3.9 des outils macOS ne reçoit plus les correctifs
de sécurité de pillow, torch, urllib3, requests… (119 failles connues dans l'ancien venv,
aucune dans celui-ci selon `pip-audit -r requirements.txt`) ; DeepFilterLib n'a pas encore
de roue 3.12. `requirements.txt` épingle tout ; **numpy < 2** et **packaging < 24** viennent
de DeepFilterNet, dont l'import de `torchaudio.backend` (retiré de torchaudio 2.1+) est
remplacé par un petit module dans `analyse.py`.

### Modèles (téléchargés une fois)
- PANNs CNN14 : `~/panns_data/Cnn14_mAP=0.431.pth`
  (https://zenodo.org/record/3987831/files/Cnn14_mAP%3D0.431.pth?download=1)
- Labels AudioSet : `~/panns_data/class_labels_indices.csv`
  (https://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv)
- DeepFilterNet3 : auto-téléchargé dans `~/Library/Caches/DeepFilterNet/`.

### Lancement (2026-11-11)
- `local.nearfield.disques` (launchd) : monte le NAS Samba au boot + re-vérif 5 min.
- La passe ne tourne plus par launchd : macOS refuse l'accès aux volumes réseau aux tâches
  d'arrière-plan (« Operation not permitted », passe à vide du 11 octobre au 11 novembre).
  Le Pi dépose des travaux au pont : `examen` (touche E du journal radio, un clip) et
  `examen_lot` (passe de la nuit, `watchers/analyse_nuit.py`, minuteur 02:30). L'agent du pont
  lance `analyse.py` ; le script n'écrit que dans le dossier du travail et le Pi range les
  propositions (`lib/examen.py`).

Base NAS côté Mac : `/Volumes/server/server/radio_log` (résolue dynamiquement
par `resolve_base()`). Usage : `analyse.py --clip SRC --sortie DIR [--nom NOM]` ou `analyse.py --lot [--base B] --sortie DIR [--hours N] [--max N]`.
