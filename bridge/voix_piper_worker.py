"""Tourne dans piper-venv (voir voix_piper.py) : entrees.json -> sons.tar."""
import json
import os
import sys
import tarfile
import tempfile
import wave

from piper import PiperVoice, SynthesisConfig

folder, modeles = sys.argv[1], sys.argv[2]
entrees = json.load(open(os.path.join(folder, "entrees.json"), encoding="utf-8"))
voix, faits, sautes = {}, 0, 0
with tempfile.TemporaryDirectory(prefix="voix_piper_") as tmp:
    names = []
    for e in sorted(entrees, key=lambda e: e["modele"]):
        v = voix.get(e["modele"])
        if v is None:
            v = voix[e["modele"]] = PiperVoice.load(os.path.join(modeles, e["modele"] + ".onnx"))
        syn = SynthesisConfig(speaker_id=e.get("locuteur"), length_scale=e["length_scale"],
                              noise_scale=e["noise_scale"], noise_w_scale=e["noise_w"])
        name = f"{e['n']}.wav"
        try:
            with wave.open(os.path.join(tmp, name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(v.config.sample_rate)
                v.synthesize_wav(e["texte"], w, syn_config=syn, set_wav_format=False)
            names.append(name)
            faits += 1
        except Exception as exc:  # texte sans rien a prononcer
            sautes += 1
            print(f"saute {e['texte']!r} : {exc!r}", file=sys.stderr)
    with tarfile.open(os.path.join(folder, "sons.tar.tmp"), "w") as tar:
        for name in names:
            tar.add(os.path.join(tmp, name), arcname=name)
os.replace(os.path.join(folder, "sons.tar.tmp"), os.path.join(folder, "sons.tar"))
print(f"{faits} sons calcules" + (f", {sautes} sautes" if sautes else ""))
