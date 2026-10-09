#!/usr/bin/env python3
"""Passe d'analyse du Mac mini (2026-10-09) : second avis plus costaud que le
RNNoise du Pi, le soir quand l'activite baisse.

Idee (demande utilisateur) : le Mac vérifie les captures du jour, apprend des
notes humaines, et pour les cas SUSPECTS avec voix possible il a le droit de
"creuser fort" (debruitage agressif, plusieurs niveaux) pour faire ressortir la
voix. GARDE-FOU : il ne remplace jamais l'audio par du son genere ; le
nettoyage ici est DETERMINISTE (soustraction spectrale, noisereduce), donc pas
d'hallucination de mots. Il ne fait que PROPOSER ; l'utilisateur tranche a
l'oreille dans la console (commande "journal proposition", touches b/t/c/r).

Flux :
  1. retrouve le partage NAS monte (filet de securite : point de montage
     dynamique), lit radio_log (+ rejets).
  2. copie les clips du jour dans un TEMP local, decode en wav (afconvert).
  3. score chaque clip (presence de voix, analyse spectrale fine).
  4. cas ambigu (voix possible mais peu claire) -> nettoie a 3 niveaux
     (noisereduce), re-score ; si un niveau fait ressortir la voix -> depose
     une PROPOSITION avec les 3 niveaux (brut + traitements) sur le NAS.
     cas "rejet mais voix probable" -> proposition de faux rejet.
  5. verse les resultats sur le NAS, PUIS purge le TEMP (pas de poussiere).

Dependances (venv ~/pcw-analyse/venv) : numpy, scipy, soundfile, noisereduce.
Usage : analyse.py [--hours 24] [--max 300] [--dry]
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import numpy as np
import soundfile as sf

try:
    import noisereduce as nr
except Exception:
    nr = None

# --- Emplacements ------------------------------------------------------------
DEFAULT_BASE = "/Volumes/server/server/radio_log"
VOICE_LO, VOICE_HI = 300.0, 3400.0     # bande de parole (repli spectral)
PANNS_SR = 32000                        # PANNs CNN14 attend du 32 kHz mono
# Classes AudioSet considerees comme de la voix (somme de leurs probas = score).
SPEECH_WORDS = ("speech", "speaking", "conversation", "narration")
# Niveaux de nettoyage, du plus leger au plus agressif (prop_decrease noisereduce)
NIVEAUX = [("léger", 0.6), ("moyen", 0.85), ("fort", 1.0)]
# Seuils sur le score PANNs (proba de voix), calibres sur des clips reels
# (2026-10-10) : une voix noyee dans le souffle a une base TRES basse (0.03-0.1)
# qui bondit apres nettoyage -> on ne doit PAS ecarter sur base basse, c'est le
# cas le plus interessant. On creuse tout ce qui n'est pas deja clairement de la
# voix, et on propose quand le nettoyage fait FRANCHIR le seuil de voix.
AMBIG_HIGH = 0.50       # base au-dessus : voix deja claire/audible, inutile de proposer
REVEAL_MIN = 0.50       # apres nettoyage, la proba de voix doit atteindre ca
GAIN_MIN = 0.25         # ET le nettoyage doit avoir fait gagner au moins ca
MAX_CLIP_SEC = 20.0

# PANNs CNN14 charge une seule fois (lazy). Repli sur le score spectral si
# indisponible (modele absent, import casse...).
_model = {"at": None, "speech_idx": None}


def _panns():
    if _model["at"] is None:
        try:
            from panns_inference import AudioTagging, labels
            _model["at"] = AudioTagging(checkpoint_path=None, device="cpu")
            _model["speech_idx"] = [i for i, l in enumerate(labels)
                                    if any(w in l.lower() for w in SPEECH_WORDS)]
            print(f"[analyse] PANNs CNN14 chargé ({len(_model['speech_idx'])} classes voix)")
        except Exception as exc:
            print(f"[analyse] PANNs indisponible ({exc}) -> score spectral")
            _model["at"] = False
    return _model["at"]


def resolve_base():
    """Point de montage du partage 'server' (dynamique : encaisse un server-1
    eventuel), puis .../server/radio_log. Repli sur DEFAULT_BASE."""
    try:
        out = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if "/server " in line and "smbfs" in line and " on " in line:
                mp = line.split(" on ", 1)[1].split(" (", 1)[0]
                cand = os.path.join(mp, "server", "radio_log")
                if os.path.isdir(cand):
                    return cand
    except Exception:
        pass
    return DEFAULT_BASE


# --- Audio -------------------------------------------------------------------
def decode_wav(src, dst):
    """mp3/m4a... -> wav mono 32 kHz (taux attendu par PANNs) via afconvert."""
    subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", f"LEI16@{PANNS_SR}",
                    "-c", "1", src, dst], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def voice_score(y, sr):
    """Probabilite de voix 0..1. PANNs CNN14 (somme des classes parole) si
    disponible, sinon repli spectral deterministe."""
    if y.size < sr // 4:
        return 0.0
    at = _panns()
    if at:
        try:
            yf = y.astype(np.float32)
            if sr != PANNS_SR:  # securite (l'audio nettoye garde sr)
                m = int(len(yf) * PANNS_SR / sr)
                yf = np.interp(np.linspace(0, len(yf), m, endpoint=False),
                               np.arange(len(yf)), yf).astype(np.float32)
            clip, _ = at.inference(yf[None, :])
            return float(min(1.0, sum(clip[0][i] for i in _model["speech_idx"])))
        except Exception as exc:
            print(f"[analyse] inférence PANNs : {exc}")
    return _spectral_score(y, sr)


def _spectral_score(y, sr):
    """Repli : part d'energie dans la bande de parole x (1 - platitude spectrale)."""
    y = y.astype(np.float64)
    n = 1 << 15
    win = min(len(y), n)
    spec = np.abs(np.fft.rfft(y[:win] * np.hanning(win))) ** 2
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    total = spec.sum() + 1e-12
    band_ratio = spec[(freqs >= VOICE_LO) & (freqs <= VOICE_HI)].sum() / total
    sub = spec[(freqs >= 100) & (freqs <= 6000)] + 1e-12
    flatness = np.exp(np.mean(np.log(sub))) / np.mean(sub)
    return float(max(0.0, min(1.0, band_ratio * (1.0 - flatness))))


def clean(y, sr, prop):
    if nr is None:
        return y
    try:
        return nr.reduce_noise(y=y, sr=sr, prop_decrease=prop, stationary=True)
    except Exception:
        return y


# --- Propositions (meme schema que lib/propositions.py du Pi) ----------------
def write_proposition(base, prop):
    path = os.path.join(base, "propositions.json")
    tmp = path + f".mac{os.getpid()}.tmp"
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = []
    data.append(prop)
    del data[:-300]
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def band_from_name(name):
    """Libelle de bande lisible depuis le nom de fichier (prefixe avant la date)."""
    base = re.split(r"_(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)_", name)[0]
    return base.replace("_", " ").strip() or "capture"


# --- Passe -------------------------------------------------------------------
def collect(base, hours, limit):
    """Clips du jour à analyser : UNIQUEMENT l'audio radio BRUT (*.brut.mp3,
    le signal capté avant habillage) et les rejets. Surtout PAS les
    enregistrements du journal (*.mp3 « propres »), qui contiennent l'annonce
    TTS + le carillon -- PANNs y détecterait notre propre voix de synthèse et
    saturerait (constat 2026-10-10)."""
    cutoff = time.time() - hours * 3600
    files = glob.glob(os.path.join(base, "*.brut.mp3")) + glob.glob(os.path.join(base, "rejets", "*.mp3"))
    recent = [f for f in files if os.path.getmtime(f) >= cutoff]
    recent.sort(key=os.path.getmtime, reverse=True)
    return recent[:limit]


def process_clip(src, base, tmpdir, audio_rel_dir, dry):
    name = os.path.basename(src)
    is_rejet = os.path.basename(os.path.dirname(src)) == "rejets"
    wav = os.path.join(tmpdir, name + ".wav")
    try:
        decode_wav(src, wav)
        y, sr = sf.read(wav)
    except Exception as exc:
        print(f"  [skip] {name} : {exc}")
        return None
    if y.ndim > 1:
        y = y.mean(axis=1)
    y = y[: int(sr * MAX_CLIP_SEC)]
    base_score = voice_score(y, sr)

    # Voix deja claire (hors rejet) : audible telle quelle, rien a proposer.
    if base_score >= AMBIG_HIGH and not is_rejet:
        return None

    # On creuse : 3 niveaux de nettoyage, re-score EN MEMOIRE (on n'ecrit les
    # wav que si on propose, pour ne pas laisser de clips orphelins sur le NAS).
    band = band_from_name(name)
    pid = f"{int(time.time()*1000)}_{abs(hash(name)) % 10000}"
    niveaux_calc, best_cleaned = [], base_score
    for niveau, prop in NIVEAUX:
        yc = clean(y, sr, prop)
        best_cleaned = max(best_cleaned, voice_score(yc, sr))
        niveaux_calc.append((niveau, prop, yc))
    gain = best_cleaned - base_score

    # On propose si le nettoyage REVELE de la voix (franchit REVEAL_MIN avec un
    # gain net), ou si un rejet sonne deja comme de la voix. L'utilisateur
    # tranche a l'oreille (b/t/c/r) -- garde-fou anti-artefact.
    propose = (best_cleaned >= REVEAL_MIN and gain >= GAIN_MIN) or (is_rejet and base_score >= AMBIG_HIGH)
    if not propose:
        return None

    traitements = []
    for niveau, prop, yc in niveaux_calc:
        rel = os.path.join(audio_rel_dir, f"{pid}_{niveau}.wav")
        if not dry:
            out = os.path.join(base, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            sf.write(out, np.clip(yc, -1, 1).astype(np.float32), sr)
        traitements.append({"niveau": niveau, "audio": rel,
                            "action": {"kind": "filtre", "target": band,
                                       "params": {"prop_decrease": prop}}})
    if is_rejet:
        resume = f"{band} : écarté comme bruit, mais une voix semble présente (proba {base_score:.2f}). À confirmer."
    else:
        resume = (f"{band} : voix faible révélée par le nettoyage, de {base_score:.2f} à {best_cleaned:.2f}. "
                  f"À confirmer à l'oreille.")
    brut_rel = os.path.join("rejets", name) if is_rejet else name
    return {"id": pid, "le": time.time(), "origine": "ia",
            "confiance": round(best_cleaned, 2),
            "resume": resume, "brut": brut_rel, "traitements": traitements,
            "action": {}, "etat": "attente"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--max", type=int, default=300)
    ap.add_argument("--dry", action="store_true", help="n'écrit rien sur le NAS")
    args = ap.parse_args()

    base = resolve_base()
    if not os.path.isdir(base):
        print(f"[analyse] NAS indisponible ({base}) -- partage non monté ? Abandon.")
        return 1
    print(f"[analyse] base : {base}")
    clips = collect(base, args.hours, args.max)
    print(f"[analyse] {len(clips)} clip(s) du jour à examiner (noisereduce {'OK' if nr else 'ABSENT'})")

    tmpdir = tempfile.mkdtemp(prefix="pcw_analyse_", dir=os.path.expanduser("~/pcw-analyse"))
    audio_rel_dir = "propositions_audio"
    deposees = 0
    try:
        for src in clips:
            prop = process_clip(src, base, tmpdir, audio_rel_dir, args.dry)
            if prop:
                if not args.dry:
                    write_proposition(base, prop)
                deposees += 1
                print(f"  [proposition] {prop['resume']}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)  # on traite puis on nettoie
    print(f"[analyse] terminé : {deposees} proposition(s) déposée(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
