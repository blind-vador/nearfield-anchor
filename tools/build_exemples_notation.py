#!/usr/bin/env python3
"""Fabrique le referentiel FIXE d'exemples sonores de la grille de notation
(console/notation.py, touches E et F2), a partir de sources reelles et
documentees (demande utilisateur 2026-10-02 : pas les notes de
l'utilisateur, un referentiel stable). Reproductible :

  build_exemples_notation.py <dossier des sources> <dossier de sortie>

Sources (licences et auteurs dans SOURCES dans le dossier de sortie) :
- conditions de reference P.835 publiees par Microsoft (depot P.808, MIT) :
  voix ITU-T P.501 a niveaux de bruit (SNR) et de deformation (MNRU)
  connus, notees par des auditeurs -> lisibilite, souffle ;
- corpus NISQA (TU Berlin), extraits a UNE seule degradation, voix
  Librivox (domaine public), notes d'auditeurs -> son, stabilite, volume ;
- Wikimedia Commons : ronflement 50 Hz du secteur, numerotation DTMF,
  paquets radio AFSK 1200, Gymnopedie de Satie, reception sur 10 MHz avec
  brouillage, ondes courtes ; NASA (Cassini) : crepitements radio d'eclairs.
Les brouillages sont d'abord donnes seuls (2 s), puis sous une voix propre.
"""
import json
import os
import subprocess
import sys

import numpy as np

RATE = 16000
LEVEL_DBOV = -26.0  # niveau nominal de la parole (P.56)


def load(path, start=0.0, dur=None):
    cmd = ["ffmpeg", "-v", "quiet", "-ss", str(start)] + (["-t", str(dur)] if dur else []) + \
          ["-i", path, "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, dtype=np.float32).astype(np.float64)


def active_level(x):
    """Niveau (dBov) des passages actifs, approximation de P.56."""
    frames = x[: len(x) // 160 * 160].reshape(-1, 160)
    e = (frames ** 2).mean(axis=1)
    active = e > (e.max() * 10 ** (-4.5))
    return 10 * np.log10(e[active].mean() + 1e-12)


def at_level(x, dbov=LEVEL_DBOV):
    return x * 10 ** ((dbov - active_level(x)) / 20)


def mix(voice, noise, rel_db):
    """Bruit sous la voix, rel_db sous le niveau de la voix."""
    noise = np.resize(noise, len(voice))
    return voice + at_level(noise, active_level(voice) + rel_db)


def save(x, path):
    x = np.clip(x, -1, 1).astype(np.float32)
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "f32le", "-ar", str(RATE), "-ac", "1", "-i", "-",
                    "-b:a", "64k", path], input=x.tobytes(), check=True)


def silence(sec):
    return np.zeros(int(RATE * sec))


def speech_bounds(x):
    frames = x[: len(x) // 160 * 160].reshape(-1, 160)
    e = (frames ** 2).mean(axis=1)
    on = np.flatnonzero(e > e.max() * 1e-3)
    return on[0] * 160, (on[-1] + 1) * 160


def steadiest_whistle(path, win=6.0):
    """Passage ou une tonalite de 400 a 3000 Hz domine le plus, et reste
    sur la meme frequence : (score, debut en s, frequence en Hz)."""
    full = load(path).astype(np.float32)  # decode une seule fois
    n = int(RATE * win) // 2048 * 2048
    f = np.fft.rfftfreq(2048, 1 / RATE)
    band = (f > 400) & (f < 3000)
    best = (-1.0, 0.0, 0.0)
    for start in range(0, len(full) - n, n):
        frames = full[start:start + n].reshape(-1, 2048)
        spec = np.abs(np.fft.rfft(frames * np.hanning(2048), axis=1))[:, band]
        peaks = spec.argmax(axis=1)
        mode = np.bincount(peaks).argmax()
        steady = np.mean(np.abs(peaks - mode) <= 1)
        ratio = (spec.max(axis=1) / (np.median(spec, axis=1) + 1e-9)).mean()
        if ratio * steady > best[0]:
            best = (float(ratio * steady), start / RATE, float(f[band][mode]))
    return best


def main(src, out):
    os.makedirs(out, exist_ok=True)
    S = lambda name: os.path.join(src, name)
    voice = at_level(load(S("nisqa_naturel.wav")))
    other = load(S("nisqa_autre_voix.wav"))
    made = {}

    def put(field, value, x, source):
        name = f"{field}_{value}.mp3"
        save(x, os.path.join(out, name))
        made[f"{field}/{value}"] = {"fichier": name, "source": source}

    # Lisibilite : echelle MNRU P.835 (SNR 40 dB), notes moyennes des auditeurs.
    for value, cond, mos in [(1, "c1", 1.85), (2, "c2", 2.78), (3, "c3", 3.54), (4, "c4", 4.32), (5, "c0", 4.69)]:
        put("lisibilite", value, at_level(load(S(f"p835_{cond}_f1.wav"))),
            f"P.835 condition {cond}, voix notee {mos} sur 5 par les auditeurs")
    # Souffle : echelle de bruit P.835 (MNRU 40), note du bruit de fond.
    for value, cond, desc in [("aucun", "c0", "reference, fond note 4,69"), ("discret", "c9", "SNR 30 dB, fond note 3,33"),
                              ("moyen", "c8", "SNR 20 dB, fond note 2,5"), ("fort", "c7", "SNR 10 dB, fond note 2,03"),
                              ("envahissant", "c6", "SNR 0 dB, fond note 1,4")]:
        put("souffle", value, at_level(load(S(f"p835_{cond}_f1.wav"))), f"P.835 condition {cond}, {desc}")
    # Son, stabilite, volume : NISQA, une seule degradation (niveaux conserves pour le volume).
    nisqa = json.load(open(S("nisqa_choix.json")))
    for field, value, key in [("son", "naturel", "naturel"), ("son", "etouffe", "etouffe"), ("son", "metallique", "metallique"),
                              ("son", "sature", "sature"), ("stabilite", "stable", "naturel"),
                              ("stabilite", "coupures", "coupures"), ("stabilite", "hache", "hache")]:
        put(field, value, at_level(load(S(f"nisqa_{key}.wav"))),
            f"NISQA {nisqa[key]['filename_deg']} (notes : globale {nisqa[key]['mos']}, coloration "
            f"{float(nisqa[key]['col']):.2f}, coupures {float(nisqa[key]['dis']):.2f})")
    for value, key in [("faible", "volume_faible"), ("correct", "naturel"), ("fort", "volume_fort")]:
        x = load(S(f"nisqa_{key}.wav"))
        level = active_level(x)
        x = at_level(x) if value == "correct" else x  # le niveau d'origine EST l'exemple
        put("volume", value, x, f"NISQA {nisqa[key]['filename_deg']}, niveau {level:.0f} dBov (normal : -26)")
    # Debut / fin : la voix de reference, coupee dans le premier / dernier mot.
    a, b = speech_bounds(voice)
    put("debut", "complet", voice, "NISQA naturel")
    put("debut", "coupe", voice[a + int(0.35 * RATE):], "NISQA naturel, coupe 0,35 s apres le debut de la parole")
    put("fin", "correcte", voice, "NISQA naturel")
    put("fin", "coupee", voice[: b - int(0.4 * RATE)], "NISQA naturel, coupe 0,4 s avant la fin de la parole")
    tail = at_level(load(S("shortwave.ogg"), 0, 3), LEVEL_DBOV - 12)
    put("fin", "longue", np.concatenate([voice, tail]), "NISQA naturel suivi de 3 s de bruit d'ondes courtes")
    # Brouillage : le bruit seul (2 s), puis sous la voix propre.
    hum = load(S("mains_hum.ogg"))
    crackle = load(S("saturn_lightning.wav"))
    score, t, freq = steadiest_whistle(S("interference_10mhz.ogg"))
    whistle = load(S("interference_10mhz.ogg"), t, 6)
    put("brouillage", "aucun", voice, "NISQA naturel")
    for value, noise, rel, source in [
            ("sifflement", whistle, -10, f"reception sur 10 MHz (Commons, Pracchia-78), passage a {t:.0f} s, "
                                         f"tonalite vers {freq:.0f} Hz"),
            ("bourdonnement", hum, -15, "ronflement 50 Hz du secteur (Commons, domaine public)"),
            ("autre_station", at_level(other), -8, "deuxieme voix Librivox (NISQA) sous la premiere"),
            ("crachotements", crackle, -8, "crepitements radio d'eclairs, Cassini (NASA, domaine public)")]:
        alone = at_level(noise[: 2 * RATE], LEVEL_DBOV + rel)
        put("brouillage", value, np.concatenate([alone, silence(0.4), mix(voice, noise, rel)]), source)
    # Contenu.
    put("contenu", "voix", voice, "NISQA naturel (Librivox)")
    put("contenu", "musique", at_level(load(S("satie.ogg"), 10, 8)), "Satie, Gymnopedie no 1 (Commons, domaine public)")
    put("contenu", "tonalites", at_level(load(S("dtmf.ogg"), 0, 6)), "numerotation DTMF (Commons, domaine public)")
    put("contenu", "donnees", at_level(load(S("afsk1200.ogg"), 0, 6)), "packet radio AFSK 1200 bauds (Commons, Denis Apel, CC BY-SA 2.0 de)")
    put("contenu", "rien", at_level(load(S("shortwave.ogg"), 3, 6)), "bruit d'ondes courtes (Commons, domaine public)")
    json.dump(made, open(os.path.join(out, "SOURCES.json"), "w"), ensure_ascii=False, indent=1)
    print(f"{len(made)} exemples dans {out} ; sifflement : passage a {t:.0f} s, {freq:.0f} Hz (score {score:.1f})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
