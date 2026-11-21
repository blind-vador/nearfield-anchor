#!/usr/bin/env python3
"""Voix hors Piper qui se presentent comme Piper (2026-10-22) : memes options
de ligne de commande, meme sortie (PCM 16 bits mono a TAUX Hz), pour que
toute la chaine des voix (lib/piper_out.py : console, annonces, cache,
preparation d'avance, choix "aleatoire") les utilise sans rien changer.

Voix (identifiant complet "piper:<moteur>#<nom>", voir piper_out.VIRTUELLES) :
  mac#<voix>     voix macOS de la machine compagne, par la liaison du pont
                 (compagnon_serveur, prise /tmp/voix_mac.sock) ; rien a
                 calculer sur le Pi
  google#fr      Google Traduction (gratuit, sans compte, non officiel) :
                 texte decoupe en morceaux de 180 caracteres au plus
  mbrola#frN     MBROLA fr1 a fr7 (diphones, local, ~80 fois le temps reel
                 sur le Pi : quasiment rien pour le processeur)

Options reprises de Piper : --voix ID, --output_raw (son sur la sortie),
-f FICHIER (WAV), --json-input (une ligne {"text", "output_file"} par son),
--length_scale X (plus petit = plus vite). Les autres (-m, -s, -q,
--noise_scale...) sont acceptees et ignorees. Code de sortie 1 si rien n'a
pu etre dit : l'appelant passe a la voix suivante.
"""
import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
import wave
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

TAUX = 22050
MAC_SOCKET = "/tmp/voix_mac.sock"
MAC_DELAI_SEC = 20
GOOGLE_URL = "https://translate.google.com/translate_tts"
GOOGLE_MAX = 180
GOOGLE_DELAI_SEC = 15
MBROLA = "/usr/local/bin/mbrola"
MBROLA_DIR = "/usr/share/mbrola"
MBROLA_FEMMES = {"fr2", "fr4"}   # prosodie espeak mb-fr4 (voix de femme), mb-fr1 sinon
UA = "Mozilla/5.0"


def _debit(length_scale):
    """length_scale Piper -> debit "+20%" (macOS)."""
    return f"{round((1 / max(0.3, length_scale) - 1) * 100):+d}%"


# --- moteurs : chacun rend un generateur de blocs PCM (TAUX Hz, mono, 16 bits) -----

def _mac(voix, texte, length_scale):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(MAC_DELAI_SEC)
    s.connect(MAC_SOCKET)
    s.sendall((json.dumps({"texte": texte, "voix": voix, "debit": _debit(length_scale)}) + "\n").encode())
    try:
        while True:
            bloc = s.recv(65536)
            if not bloc:
                return
            yield bloc
    finally:
        s.close()


def _morceaux(texte, taille=GOOGLE_MAX):
    """Aux fins de phrase, puis a la ponctuation, puis avant un mot qui ouvre la suite, jamais apres un
    petit mot (lib/decoupe_voix.py) : un morceau coupe au dernier espace venu sonnait comme une phrase
    finie au milieu de la phrase."""
    import decoupe_voix
    return decoupe_voix.morceaux(texte, taille)


def _decode(donnees_mp3, length_scale):
    filtre = ["-af", f"atempo={min(2.0, max(0.5, 1 / length_scale)):.3f}"] if abs(length_scale - 1) > 0.01 else []
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", "pipe:0"] + filtre +
                       ["-f", "s16le", "-ar", str(TAUX), "-ac", "1", "pipe:1"],
                       input=donnees_mp3, capture_output=True, timeout=30)
    return r.stdout


def _google(langue, texte, length_scale):
    for morceau in _morceaux(texte):
        url = GOOGLE_URL + "?" + urllib.parse.urlencode(
            {"ie": "UTF-8", "client": "tw-ob", "tl": langue, "q": morceau})
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=GOOGLE_DELAI_SEC) as r:
            mp3 = r.read()
        pcm = _decode(mp3, length_scale)
        if not pcm:
            raise RuntimeError("son Google illisible")
        yield pcm


def _mbrola(nom, texte, length_scale):
    base = os.path.join(MBROLA_DIR, nom, nom)
    pho = subprocess.run(["espeak", "-v", "mb-fr4" if nom in MBROLA_FEMMES else "mb-fr1", "-q", "--pho"],
                         input=texte.encode("utf-8"), capture_output=True, timeout=30).stdout
    if not pho:
        raise RuntimeError("espeak n'a rien rendu")
    tmp = f"/tmp/mbrola_{os.getpid()}.wav"
    try:
        subprocess.run([MBROLA, "-e", "-t", f"{length_scale:.3f}", base, "-", tmp], input=pho,
                       capture_output=True, timeout=30)
        with wave.open(tmp) as w:
            taux, pcm = w.getframerate(), w.readframes(w.getnframes())
        if taux != TAUX:
            pcm, _ = audioop.ratecv(pcm, 2, 1, taux, TAUX, None)
        yield pcm
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


MOTEURS = {"mac": _mac, "google": _google, "mbrola": _mbrola}


def blocs(voix_id, texte, length_scale=1.0):
    moteur, _, nom = voix_id.split(":", 1)[-1].partition("#")
    return MOTEURS[moteur](nom, texte, length_scale)


# --- sorties a la maniere de Piper --------------------------------------------------

def _vers_fichier(voix_id, texte, chemin, length_scale):
    pcm = b"".join(blocs(voix_id, texte, length_scale))
    if len(pcm) < 1000:
        return False
    with wave.open(chemin, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAUX)
        w.writeframes(pcm)
    return True


def main(argv):
    voix_id, length_scale, fichier, brut, json_in = None, 1.0, None, False, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--voix":
            voix_id, i = argv[i + 1], i + 1
        elif a == "--length_scale":
            length_scale, i = float(argv[i + 1]), i + 1
        elif a in ("-f", "--output_file"):
            fichier, i = argv[i + 1], i + 1
        elif a == "--output_raw":
            brut = True
        elif a == "--json-input":
            json_in = True
        elif a in ("-m", "-s", "--noise_scale", "--noise_w", "--model", "--speaker"):
            i += 1
        i += 1
    if not voix_id:
        print("voix_externe : --voix manquant", file=sys.stderr)
        return 2
    entree = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        if json_in:
            faits = 0
            for ligne in entree.splitlines():
                try:
                    d = json.loads(ligne)
                    faits += _vers_fichier(voix_id, d["text"], d["output_file"], length_scale)
                except Exception as exc:
                    print(f"voix_externe : {exc!r}", file=sys.stderr)
            return 0 if faits else 1
        if fichier:
            return 0 if _vers_fichier(voix_id, entree, fichier, length_scale) else 1
        dit = 0
        sortie = sys.stdout.buffer
        for bloc in blocs(voix_id, entree, length_scale):
            sortie.write(bloc)
            sortie.flush()
            dit += len(bloc)
        return 0 if dit > 1000 else 1
    except BrokenPipeError:
        return 0
    except Exception as exc:
        print(f"voix_externe : {voix_id} : {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
