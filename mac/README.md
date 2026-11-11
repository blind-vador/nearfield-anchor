# Passe d'analyse IA (Mac mini)

**Installation clef en main : `bash mac/install-mac.sh`** (depuis le dépôt cloné) — crée le venv, installe les dépendances, télécharge les modèles, pose le script et l'agent de montage du NAS. La passe est lancée par l'agent du pont (`bridge/examen_mac.py`). Détails ci-dessous.

`analyse.py` tourne sur le Mac mini (M4), PAS sur le Pi. Il lit les clips du NAS
(partage Samba monté), score voix/musique via **PANNs CNN14**, nettoie les cas
ambigus (noisereduce + **DeepFilterNet**), et dépose des propositions dans
`radio_log/propositions.json` (console Pi : commande « journal proposition »).

## Reproduire l'installation
Python 3.9 (CLT macOS), venv dédié hors dépôt :

```
python3 -m venv ~/pcw-analyse/venv
~/pcw-analyse/venv/bin/pip install -r requirements.txt
cp analyse.py ~/pcw-analyse/analyse.py
```

`requirements.txt` épingle **numpy < 2** (compat torch 2.2.2) + torch/torchaudio
2.2.2 + panns-inference + deepfilternet + noisereduce + soundfile/scipy.

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
