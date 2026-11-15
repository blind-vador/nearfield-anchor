#!/usr/bin/env python3
"""Lance toute la suite, chaque fichier de test dans SON processus (2026-11-15).

Les tests de nearfield remplacent, a l'import ou dans setUp, des chemins et des
fonctions de modules partages (phrases.BASE_DIR, gemini.ask, audio_lock,
juges...). Ils sont ecrits pour etre lances un par un ("python3 tests/test_x.py").
"python3 -m unittest discover" les charge tous dans un seul processus : ils se
marchent dessus et 16 tests echouaient sans aucun defaut du code.

Usage : python3 tests/lancer_tous.py [motif...]     (motif : partie du nom de fichier)
Code de sortie 0 si tout passe ; le resume dit chaque fichier en echec.
"""
import glob
import os
import subprocess
import sys
import time

ICI = os.path.dirname(os.path.abspath(__file__))


def main(motifs):
    fichiers = sorted(glob.glob(os.path.join(ICI, "test_*.py")))
    if motifs:
        fichiers = [f for f in fichiers if any(m in os.path.basename(f) for m in motifs)]
    echecs, debut = [], time.time()
    for f in fichiers:
        nom = os.path.basename(f)
        t = time.time()
        r = subprocess.run([sys.executable, "-W", "ignore", f], capture_output=True, text=True,
                           cwd=os.path.dirname(ICI))
        bilan = next((l for l in reversed(r.stderr.splitlines() + r.stdout.splitlines())
                      if l.startswith(("OK", "FAILED"))), "")
        etat = "ok" if r.returncode == 0 else "ECHEC"
        print(f"{etat:5} {nom} ({time.time() - t:.1f} s) {bilan}", flush=True)
        if r.returncode:
            echecs.append(nom)
            print("\n".join(("      " + l) for l in r.stderr.splitlines()[-30:]), flush=True)
    print(f"\n{len(fichiers)} fichiers en {time.time() - debut:.0f} s : "
          + (f"{len(echecs)} en echec ({', '.join(echecs)})" if echecs else "tout passe"))
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
