# Passe d'analyse IA (Mac mini)

**Installation clef en main : `bash mac/install-mac.sh`** (depuis le dépôt cloné) — crée le venv, installe les dépendances, télécharge les modèles, pose le script et les 3 agents launchd. Détails ci-dessous.

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

### Tâches planifiées (launchd/, à copier dans ~/Library/LaunchAgents/)
- `local.deepspace9.disques` : monte le NAS Samba au boot + re-vérif 5 min.
- `local.deepspace9.analyse` : passe complète la nuit (02:30).
- `local.deepspace9.examen` : draine la file d'examen (touche « e » du journal) toutes les 15 min.

Base NAS côté Mac : `/Volumes/server/server/radio_log` (résolue dynamiquement
par `resolve_base()`). Usage : `analyse.py [--hours N] [--max N] [--dry] [--queue]`.
