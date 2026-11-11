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

Flux (2026-11-11 : lance par l'agent du pont, plus par launchd) :
  macOS refuse l'acces aux volumes reseau aux taches launchd d'arriere-plan
  ("Operation not permitted" : 32 nuits a 0 clip du 11 octobre au 11 novembre).
  L'agent du pont (bridge/examen_mac.py), qui a cet acces, lance ce script en
  sous-processus pour un travail "examen" du Pi :
  - --clip SRC --sortie DIR : un clip demande (touche E du journal radio) ;
  - --lot --base RADIO_LOG --sortie DIR : la passe de la nuit (bruts + rejets).
  Le script n'ecrit JAMAIS sur le NAS hors de DIR : propositions et sons
  nettoyes y sont deposes, le Pi les range lui-meme (un seul ecrivain).
  1. decode en wav (afconvert), score chaque clip (presence de voix, analyse
     spectrale fine) ;
  2. cas ambigu (voix possible mais peu claire) -> nettoie a plusieurs niveaux,
     re-score ; si un niveau fait ressortir la voix -> PROPOSITION avec les
     niveaux (brut + traitements). Cas "rejet mais voix probable" -> proposition
     de faux rejet.

Dependances (venv ~/pcw-analyse/venv) : numpy, scipy, soundfile, noisereduce.
Usage : analyse.py --clip SRC --sortie DIR [--nom NOM] | --lot [--base B] --sortie DIR [--hours 24] [--max 300]
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
import unicodedata

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
# Classes AudioSet : voix (somme des probas) et musique (meilleure proba de la
# famille musicale -- 'music' attrape Music + genres, + le chant).
SPEECH_WORDS = ("speech", "speaking", "conversation", "narration")
MUSIC_WORDS = ("music", "singing")
# Niveaux de nettoyage : (nom, methode, parametre). methode "nr" = noisereduce
# (soustraction spectrale, parametre = prop_decrease) ; "df" = DeepFilterNet
# (debruitage NEURONAL, discriminatif -> pas d'hallucination). VOIX : agressif
# (reveler une voix noyee). MUSIQUE : DOUX, pour oter le souffle/sifflement SANS
# defigurer la musique. Le niveau "neuronal" (DeepFilterNet) ferme chaque liste.
NIVEAUX_VOIX = [("léger", "nr", 0.6), ("moyen", "nr", 0.85), ("fort", "nr", 1.0), ("neuronal", "df", None)]
NIVEAUX_MUSIQUE = [("léger", "nr", 0.3), ("moyen", "nr", 0.5), ("fort", "nr", 0.7), ("neuronal", "df", None)]
# Seuils (score PANNs), calibres sur clips reels (2026-10-10).
AMBIG_HIGH = 0.50       # base voix au-dessus : deja claire/audible, inutile de proposer
REVEAL_MIN = 0.50       # apres nettoyage, la proba de voix doit atteindre ca
GAIN_MIN = 0.25         # ET le nettoyage doit avoir fait gagner au moins ca
MUSIC_HIGH = 0.50       # au-dessus : clip considere MUSIQUE (pas une piste voix)
HISS_MIN = 0.40         # platitude spectrale HF : au-dessus, souffle/sifflement a nettoyer
MAX_CLIP_SEC = 20.0

# PANNs CNN14 charge une seule fois (lazy). Repli sur le score spectral si
# indisponible (modele absent, import casse...).
_model = {"at": None, "speech_idx": None, "music_idx": None}


def _panns():
    if _model["at"] is None:
        try:
            from panns_inference import AudioTagging, labels
            _model["at"] = AudioTagging(checkpoint_path=None, device="cpu")
            _model["speech_idx"] = [i for i, l in enumerate(labels)
                                    if any(w in l.lower() for w in SPEECH_WORDS)]
            _model["music_idx"] = [i for i, l in enumerate(labels)
                                   if any(w in l.lower() for w in MUSIC_WORDS)]
            print(f"[analyse] PANNs CNN14 chargé ({len(_model['speech_idx'])} classes voix, "
                  f"{len(_model['music_idx'])} musique)")
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


def classify(y, sr):
    """(voix, musique) en 0..1, UNE seule inference PANNs. voix = somme des
    classes parole ; musique = meilleure proba de la famille musicale. Repli
    spectral (voix seule, musique 0) si PANNs indisponible."""
    if y.size < sr // 4:
        return 0.0, 0.0
    at = _panns()
    if at:
        try:
            yf = y.astype(np.float32)
            if sr != PANNS_SR:  # securite (l'audio nettoye garde sr)
                m = int(len(yf) * PANNS_SR / sr)
                yf = np.interp(np.linspace(0, len(yf), m, endpoint=False),
                               np.arange(len(yf)), yf).astype(np.float32)
            clip, _ = at.inference(yf[None, :])
            voix = min(1.0, sum(clip[0][i] for i in _model["speech_idx"]))
            musique = min(1.0, max(clip[0][i] for i in _model["music_idx"]))
            return float(voix), float(musique)
        except Exception as exc:
            print(f"[analyse] inférence PANNs : {exc}")
    return _spectral_score(y, sr), 0.0


def voice_score(y, sr):  # compat / diagnostics
    return classify(y, sr)[0]


def _hf_hiss(y, sr):
    """Platitude spectrale dans le haut du spectre (5-12 kHz) : eleve = souffle
    large bande / sifflement a nettoyer ; faible = aigus musicaux propres."""
    y = y.astype(np.float64)
    win = min(len(y), 1 << 15)
    if win < 1024:
        return 0.0
    spec = np.abs(np.fft.rfft(y[:win] * np.hanning(win))) ** 2
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    hf = spec[(freqs >= 5000) & (freqs <= min(12000, sr / 2 - 1))] + 1e-12
    if hf.size < 4:
        return 0.0
    return float(np.exp(np.mean(np.log(hf))) / np.mean(hf))


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


_df = {"model": None, "state": None}  # DeepFilterNet chargé une seule fois


def clean(y, sr, method, param):
    """Nettoie un clip ; retourne (audio, sr_sortie). methode "nr" =
    noisereduce (param = prop_decrease) ; "df" = DeepFilterNet (neuronal, 48 kHz)."""
    if method == "df":
        return clean_df(y, sr)
    if nr is None:
        return y, sr
    try:
        return nr.reduce_noise(y=y, sr=sr, prop_decrease=param, stationary=True), sr
    except Exception:
        return y, sr


def clean_df(y, sr):
    """Debruitage NEURONAL DeepFilterNet (discriminatif, pas de son genere).
    Fonctionne a 48 kHz : on resample vers 48k, on debruite, on retourne a 48k.
    Repli : audio inchange si DeepFilterNet indisponible."""
    try:
        import torch
        from df.enhance import enhance, init_df
        if _df["model"] is None:
            _df["model"], _df["state"], _ = init_df()
        target = int(_df["state"].sr())  # 48000
        if sr != target:
            from math import gcd
            from scipy.signal import resample_poly
            g = gcd(target, int(sr))
            yy = resample_poly(y, target // g, int(sr) // g).astype(np.float32)
        else:
            yy = y.astype(np.float32)
        enh = enhance(_df["model"], _df["state"], torch.from_numpy(yy).unsqueeze(0))
        return enh.squeeze(0).detach().cpu().numpy().astype(np.float32), target
    except Exception as exc:
        print(f"[analyse] DeepFilterNet indisponible : {exc}")
        return y, sr


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


def process_clip(src, out_root, tmpdir, audio_rel_dir, dry, force=False, origine="ia", nom=None):
    """out_root : dossier ou poser les sons nettoyes (le dossier du travail).
    nom : nom du clip tel que le journal le connait (src peut etre une copie)."""
    name = nom or os.path.basename(src)
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
    v0, m0 = classify(y, sr)   # voix, musique a l'etat brut

    # Classification du contenu (demande utilisateur) : un clip ou la MUSIQUE
    # domine n'est pas une piste voix -- ne pas y "reveler" une voix (la musique
    # faussait les nettoyages trop forts). On lui propose a la place un nettoyage
    # DOUX du souffle/sifflement, qui preserve la musique, et seulement s'il y a
    # vraiment du souffle (platitude HF).
    is_music = m0 >= MUSIC_HIGH and m0 >= v0
    # force=True : clip demande explicitement a l'examen (touche "e" du journal)
    # -> on produit TOUJOURS une proposition pour comparer, meme si deja clair.
    if not force:
        if not is_music and not is_rejet and v0 >= AMBIG_HIGH:
            return None  # voix deja claire : audible, rien a proposer
        if is_music and not is_rejet and _hf_hiss(y, sr) < HISS_MIN:
            return None  # musique deja propre : pas de souffle a enlever

    niveaux = NIVEAUX_MUSIQUE if is_music else NIVEAUX_VOIX
    band = band_from_name(name)
    pid = f"{int(time.time()*1000)}_{abs(hash(name)) % 10000}"
    niveaux_calc, best_v = [], v0
    for niveau, method, param in niveaux:
        yc, sc = clean(y, sr, method, param)
        if not is_music:
            best_v = max(best_v, classify(yc, sc)[0])
        niveaux_calc.append((niveau, method, param, yc, sc))

    if is_music:
        # Nettoyage musique : on propose des que la musique est presente et
        # qu'il y a du souffle (deja verifie). L'utilisateur juge l'amelioration.
        resume = (f"{band} : musique (proba {m0:.2f}) avec du souffle/sifflement — "
                  f"nettoyage doux pour l'écoute. À confirmer.")
        confiance = round(m0, 2)
    else:
        gain = best_v - v0
        propose = force or (best_v >= REVEAL_MIN and gain >= GAIN_MIN) or (is_rejet and v0 >= AMBIG_HIGH)
        if not propose:
            return None
        musique = f" (musique {m0:.2f} présente)" if m0 >= 0.3 else ""
        if is_rejet:
            resume = f"{band} : écarté comme bruit, mais une voix semble présente (proba {v0:.2f}){musique}. À confirmer."
        else:
            resume = (f"{band} : voix de {v0:.2f} à {best_v:.2f} après nettoyage{musique}. "
                      f"À confirmer à l'oreille.")
        # Si de la musique traine, la "voix revelee" est plus douteuse : on pondere.
        confiance = round(best_v * (1.0 - 0.4 * m0), 2)
    if origine == "demande":
        resume = "Examen demandé — " + resume

    traitements = []
    for niveau, method, param, yc, sc in niveaux_calc:
        # Nom ASCII (2026-11-11) : "léger" ecrit par macOS (NFD) sur le partage Linux ne
        # correspondait plus a l'empreinte signee par le pont, le Pi ignorait le resultat.
        ascii_niveau = unicodedata.normalize("NFKD", niveau).encode("ascii", "ignore").decode()
        rel = os.path.join(audio_rel_dir, f"{pid}_{ascii_niveau}.wav")
        if not dry:
            out = os.path.join(out_root, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            sf.write(out, np.clip(yc, -1, 1).astype(np.float32), sc)
        params = {"methode": method, "musique": is_music}
        if method == "nr":
            params["prop_decrease"] = param
        traitements.append({"niveau": niveau, "audio": rel,
                            "action": {"kind": "filtre", "target": band, "params": params}})
    brut_rel = os.path.join("rejets", name) if is_rejet else name
    return {"id": pid, "le": time.time(), "origine": origine, "confiance": confiance,
            "resume": resume, "brut": brut_rel, "traitements": traitements,
            "action": {}, "etat": "attente"}


def _ecrire(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# --- Examen d'un clip : Whisper d'abord (2026-11-11, plan phase 4) ------------
# Whisper sur le brut decide de la suite. Bon et propre : on ne cherche pas a
# ameliorer ce qui est deja bien. Inaudible : toute l'echelle de nettoyage, chaque
# niveau repasse par Whisper. Entre les deux : leger et neuronal seulement. Un
# niveau n'est retenu que s'il ameliore nettement la comprehension de Whisper.
WHISPER_BON = 0.70          # confiance moyenne des mots au-dessus : rien a ameliorer
WHISPER_INAUDIBLE = 0.40    # en dessous : inaudible, toute l'echelle
WHISPER_INTELLIGIBLE = 0.50  # texte rendu au Pi (nommage) seulement au-dessus
WHISPER_GAIN = 0.08         # gain minimal pour retenir un niveau de nettoyage
WHISPER_SEC = 120


def transcrire(chemin, whisper, tmpdir, nom):
    """{"texte", "confiance"} (confiance = probabilite moyenne des mots) ou None."""
    if not whisper:
        return None
    cli, modele, prompt = whisper
    wav16 = os.path.join(tmpdir, nom + ".16k.wav")
    base = os.path.join(tmpdir, nom + ".whisper")
    try:
        subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", chemin, wav16],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([cli, "-m", modele, "-l", "fr", "-t", "4", "-nt", "-np", "-f", wav16, "-of", base, "-ojf",
                        "--prompt", prompt], capture_output=True, timeout=WHISPER_SEC, check=True)
        with open(base + ".json", encoding="utf-8") as f:
            segs = json.load(f).get("transcription") or []
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"  [whisper] {nom} : {exc}")
        return None
    probas = [t.get("p", 0.0) for seg in segs for t in seg.get("tokens") or []
              if not str(t.get("text", "")).startswith("[_")]
    texte = " ".join(" ".join((seg.get("text") or "").split()) for seg in segs).strip()
    raison = hallucination(texte, prompt)
    if raison:
        print(f"  [whisper] {nom} : texte invente ({raison}) : {texte[:80]!r}")
        return {"texte": "", "confiance": 0.0, "invente": raison}
    return {"texte": texte, "confiance": round(float(np.mean(probas)), 3) if probas and texte else 0.0}


HALLUCINATIONS = ("sous-titre", "sous titre", "amara.org", "merci d'avoir regard", "abonnez-vous", "abonne-toi",
                  "thanks for watching", "générique", "generique", "musique")


def _mots(texte):
    return re.findall(r"[0-9a-zà-ÿ']+", (texte or "").lower())


def hallucination(texte, prompt=""):
    """Raison pour laquelle ce texte est invente par Whisper sur du bruit, ou None
    (calibre le 2026-11-11 : l'invite recitee a 0,97, « *Générique* » a 0,72)."""
    propre = re.sub(r"\*[^*]*\*|\[[^\]]*\]|\([^)]*\)", " ", texte or "")
    mots = _mots(propre)
    if not mots:
        return "aucun mot" if texte else None
    bas = propre.lower()
    if any(h in bas for h in HALLUCINATIONS):
        return "formule connue"
    vocab = set(_mots(prompt))
    if vocab and sum(m in vocab for m in mots) >= 0.5 * len(mots):
        return "consigne recitee"
    if len(mots) >= 8 and len(set(mots)) < 0.3 * len(mots):
        return "repetition"
    return None


def examiner_clip(src, sortie, tmpdir, nom, whisper, origine):
    """(proposition ou None, transcription) d'un clip demande."""
    name = nom or os.path.basename(src)
    wav = os.path.join(tmpdir, "brut.wav")
    decode_wav(src, wav)
    y, sr = sf.read(wav)
    if y.ndim > 1:
        y = y.mean(axis=1)
    y = y[: int(sr * MAX_CLIP_SEC)]
    v0, m0 = classify(y, sr)
    if m0 >= MUSIC_HIGH and m0 >= v0:   # musique : nettoyage doux, pas de parole a comprendre
        prop = process_clip(src, sortie, tmpdir, "propositions_audio", False, force=True, origine=origine, nom=nom)
        return prop, {"statut": "musique", "confiance": 0.0, "musique": round(m0, 2), "voix": round(v0, 2)}
    t0 = transcrire(wav, whisper, tmpdir, "brut") or {"texte": "", "confiance": 0.0}
    p0 = t0["confiance"]
    hiss = _hf_hiss(y, sr)
    if whisper is None:
        niveaux = NIVEAUX_VOIX
    elif p0 >= WHISPER_BON:
        niveaux = [n for n in NIVEAUX_VOIX if n[0] == "léger"] if hiss >= HISS_MIN else []
    elif p0 < WHISPER_INAUDIBLE:
        niveaux = NIVEAUX_VOIX
    else:
        niveaux = [n for n in NIVEAUX_VOIX if n[0] in ("léger", "neuronal")]
    essais = [{"niveau": "brut", "confiance": p0}]
    meilleur = {"niveau": "brut", "confiance": p0, "texte": t0["texte"]}
    pid = f"{int(time.time()*1000)}_{abs(hash(name)) % 10000}"
    traitements = []
    for niveau, method, param in niveaux:
        yc, sc = clean(y, sr, method, param)
        ascii_niveau = unicodedata.normalize("NFKD", niveau).encode("ascii", "ignore").decode()
        rel = os.path.join("propositions_audio", f"{pid}_{ascii_niveau}.wav")
        out = os.path.join(sortie, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        sf.write(out, np.clip(yc, -1, 1).astype(np.float32), sc)
        t = transcrire(out, whisper, tmpdir, ascii_niveau) or {"texte": "", "confiance": 0.0}
        essais.append({"niveau": niveau, "confiance": t["confiance"]})
        params = {"methode": method, "musique": False}
        if method == "nr":
            params["prop_decrease"] = param
        traitements.append({"niveau": niveau, "audio": rel, "whisper": t["confiance"],
                            "action": {"kind": "filtre", "target": band_from_name(name), "params": params}})
        if t["confiance"] >= meilleur["confiance"] + WHISPER_GAIN:
            meilleur = {"niveau": niveau, "confiance": t["confiance"], "texte": t["texte"]}
    if whisper is None:
        statut = "sans_whisper"
    elif meilleur["confiance"] < WHISPER_INTELLIGIBLE:
        statut = "inaudible"
    elif meilleur["niveau"] != "brut":
        statut = "ameliore"
    else:
        statut = "bon"
    transcription = {"statut": statut, "niveau": meilleur["niveau"], "confiance": meilleur["confiance"],
                     "texte": meilleur["texte"] if statut in ("bon", "ameliore") else "",
                     "essais": essais, "voix": round(v0, 2), "musique": round(m0, 2), "souffle": round(hiss, 2)}
    if not traitements:
        return None, transcription
    band = band_from_name(name)
    pc = lambda x: f"{round(x * 100)} %"   # noqa: E731
    if statut == "ameliore":
        resume = (f"{band} : parole comprise à {pc(p0)} au brut, {pc(meilleur['confiance'])} "
                  f"après nettoyage {meilleur['niveau']}.")
    elif statut == "inaudible":
        resume = f"{band} : parole peu compréhensible, même nettoyée ({pc(meilleur['confiance'])} au mieux)."
    elif statut == "bon":
        resume = f"{band} : déjà bien comprise ({pc(p0)}), le nettoyage n'apporte rien de net."
    else:
        resume = f"{band} : voix {v0:.2f}, nettoyages à comparer."
    if origine == "demande":
        resume = "Examen demandé — " + resume
    # du meilleur au moins bon pour Whisper : T passe d'un niveau a l'autre dans cet ordre
    traitements.sort(key=lambda t: -t.get("whisper", 0))
    prop = {"id": pid, "le": time.time(), "origine": origine, "confiance": meilleur["confiance"],
            "resume": resume, "brut": name, "traitements": traitements, "action": {}, "etat": "attente"}
    return prop, transcription


def run_clip(src, sortie, nom, whisper=None, origine="demande"):
    """Clip demande a l'examen (touche E) ou choisi le soir : propositions.json et
    transcription.json dans le dossier du travail."""
    tmpdir = tempfile.mkdtemp(prefix="pcw_examen_", dir=os.path.expanduser("~/pcw-analyse"))
    try:
        prop, transcription = examiner_clip(src, sortie, tmpdir, nom, whisper, origine)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    _ecrire(os.path.join(sortie, "propositions.json"), [prop] if prop else [])
    _ecrire(os.path.join(sortie, "transcription.json"), transcription)
    print(f"[analyse] examen {nom or os.path.basename(src)} : {transcription['statut']} "
          f"({transcription['confiance']}) ; {prop['resume'] if prop else 'aucun nettoyage a proposer'}")
    return 0


def run_lot(base, sortie, hours, limit):
    """Passe de la nuit : bruts et rejets des dernieres heures."""
    clips = collect(base, hours, limit)
    print(f"[analyse] {len(clips)} clip(s) a examiner (noisereduce {'OK' if nr else 'ABSENT'})")
    tmpdir = tempfile.mkdtemp(prefix="pcw_analyse_", dir=os.path.expanduser("~/pcw-analyse"))
    props = []
    try:
        for src in clips:
            prop = process_clip(src, sortie, tmpdir, "propositions_audio", False)
            if prop:
                props.append(prop)
                print(f"  [proposition] {prop['resume']}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    _ecrire(os.path.join(sortie, "propositions.json"), props)
    print(f"[analyse] termine : {len(props)} proposition(s).")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", help="un clip demande a l'examen (touche E du journal)")
    ap.add_argument("--nom", help="nom du clip dans le journal")
    ap.add_argument("--lot", action="store_true", help="passe de la nuit sur les bruts et rejets")
    ap.add_argument("--base", help="radio_log du NAS (defaut : partage monte)")
    ap.add_argument("--sortie", required=True, help="dossier du travail : propositions et sons y sont deposes")
    ap.add_argument("--whisper-cli", help="whisper.cpp (whisper-cli) : la parole comprise guide l'examen")
    ap.add_argument("--whisper-modele")
    ap.add_argument("--whisper-prompt", default="")
    ap.add_argument("--origine", default="demande")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--max", type=int, default=300)
    args = ap.parse_args()
    os.makedirs(args.sortie, exist_ok=True)
    if args.clip:
        whisper = ((args.whisper_cli, args.whisper_modele, args.whisper_prompt)
                   if args.whisper_cli and args.whisper_modele else None)
        return run_clip(args.clip, args.sortie, args.nom, whisper, args.origine)
    if not args.lot:
        ap.error("--clip ou --lot")
    base = args.base or resolve_base()
    try:
        os.listdir(base)
    except OSError as exc:
        print(f"[analyse] NAS illisible ({base}) : {exc}")
        return 1
    print(f"[analyse] base : {base}")
    return run_lot(base, args.sortie, args.hours, args.max)


if __name__ == "__main__":
    raise SystemExit(main())
