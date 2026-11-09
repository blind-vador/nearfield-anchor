"""Notation "tonalites" ou "donnees" : la prise repart a tous les juges (2026-11-10)."""
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import chemins  # noqa: E402
import notation  # noqa: E402
import propositions  # noqa: E402

tmp = tempfile.mkdtemp()
prise = os.path.join(tmp, "canal_station_probable_lundi_9_novembre_2026_15h46m50s.mp3")
subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=1000:d=3", "-ar", "16000", prise],
               check=True)
brut = os.path.join(tmp, "brut_dsd")
notes = []
with mock.patch.object(chemins, "base", lambda nom: brut), \
        mock.patch.object(propositions, "_juge_courant", return_value=("nfm", 160_650_000, "canal")), \
        mock.patch("frequences_actives.noter", side_effect=lambda *a, **k: notes.append((a, k))):
    voix = notation.empty_degrees()
    assert notation._examen_contenu(prise, {"degres": voix, "freq_hz": 160_650_000}) == ""
    assert not os.path.isdir(brut)
    tons = notation.empty_degrees()
    tons["contenu"]["tonalites"] = 60
    phrase = notation._examen_contenu(prise, {"degres": tons, "freq_hz": 160_650_000, "label": "canal"})
    assert phrase.strip(), phrase
    wavs = [f for f in os.listdir(brut) if f.endswith(".wav")]
    assert len(wavs) == 1 and wavs[0].startswith("renvoi_"), wavs
    fiche = json.load(open(os.path.join(brut, wavs[0][:-4] + ".json")))
    assert fiche["etiquette"] == "renvoi" and fiche["freq_hz"] == 160_650_000
    assert notes and notes[0][0][:2] == (160_650_000, "donnees"), notes
    # deja en file : on le dit, sans doublon
    assert notation._examen_contenu(prise, {"degres": tons, "freq_hz": 160_650_000}).strip()
    assert len([f for f in os.listdir(brut) if f.endswith(".wav")]) == 1
print("notation : tonalites et donnees repartent aux juges")
