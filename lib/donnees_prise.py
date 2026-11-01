"""Decodeurs de donnees sur le flux de la prise unique (2026-11-01).

Doctrine : tout ce qui passe devant le scanner passe devant TOUS les decodeurs,
sans mur logique. La prise unique (lib/prise_unique.py) donnait deja son flux
FM 48 kHz a dsd-fme (voix numerique) et a multimon-ng (donnees jusqu'a 9600
bauds, via la chaine analogique a 22 050 Hz). Il manquait :
  - l'AIS (navires, GMSK 9600 bauds : multimon-ng ne le decode pas),
  - l'EAS/SAME (alertes, AFSK 520 bauds),
que Dire Wolf decode (paquet Debian "direwolf"). Un Dire Wolf par mode, nourri
par la prise comme un dsd-fme ; sans le programme, rien ne change.

Le scanner est distribue : ce qui est rare ici (navires loin de la mer) ne
l'est pas pour un utilisateur au bord de l'eau."""
import os
import re
import shutil
import subprocess
import threading

import prise_unique

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Entree stdin, sortie nulle, pas de serveurs AGW/KISS (plusieurs instances).
CONFIG = os.path.join(RACINE, "config", "direwolf_donnees.conf")
MODES = [("AIS", "AIS"), ("EAS", "EAS")]   # (option -B, nom dit)
# Lignes de trame decodee ; le reste (bandeau de demarrage, statistiques) est ignore.
_TRAME = re.compile(r"(!AIVD[MO],|^AIS \d|ZCZC-|NNNN|^EAS)")


def disponible():
    return shutil.which("direwolf") is not None and os.path.exists(CONFIG)


class Ecoute:
    """Les Dire Wolf d'une prise ; messages() rend les trames decodees."""

    def __init__(self, prise):
        self._procs, self._lignes = [], []
        self._verrou = threading.Lock()
        if not disponible():
            return
        for option, nom in MODES:
            try:
                proc = subprocess.Popen(
                    ["direwolf", "-c", CONFIG, "-B", option, "-r", str(prise_unique.TAUX), "-n", "1",
                     "-b", "16", "-t", "0", "-a", "0", "-q", "hd", "-"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except OSError as exc:
                print(f"[donnees_prise] direwolf {nom} impossible : {exc!r}")
                continue
            prise.vers_processus(proc.stdin)
            threading.Thread(target=self._lire, args=(proc, nom), daemon=True).start()
            self._procs.append(proc)

    def _lire(self, proc, nom):
        try:
            for brut in proc.stdout:
                ligne = brut.decode(errors="replace").strip()
                if ligne and _TRAME.search(ligne):
                    print(f"[donnees_prise] {nom} : {ligne}")
                    with self._verrou:
                        self._lignes.append((nom, ligne))
        except Exception:
            pass

    def arreter(self):
        """A appeler une fois la prise fermee : Dire Wolf finit sa derniere
        trame a la fin du flux, on ne le coupe qu'ensuite."""
        for proc in self._procs:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def messages(self):
        """[(nom, texte)] : la description lisible d'une trame quand Dire Wolf
        en donne une, sinon la trame brute."""
        with self._verrou:
            lignes = list(self._lignes)
        lisibles = [(n, l) for n, l in lignes if not l.startswith("[") and "!AIVD" not in l]
        return lisibles or lignes
