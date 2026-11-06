"""Decodage leger des sons (2026-11-06). ffmpeg charge 229 bibliotheques : sur le
Pi, 0,6 a 2 s avant le premier son, a chaque fleche du journal et chaque mot d'une
annonce. mpg123 puis sox rendent le meme nombre d'octets en 0,02 s. ffmpeg reste
pour les autres formats, et en secours si la chaine legere ne rend rien."""
import os
import shutil
import subprocess
import threading

_LEGERS = bool(shutil.which("mpg123") and shutil.which("sox"))


def _sortie(taux):
    return ["-t", "raw", "-r", str(taux), "-c", "1", "-b", "16", "-e", "signed-integer", "-"]


class Flux:
    """Le son d'un fichier en PCM 16 bits mono. arreter() vient de n'importe quel
    fil sans attendre ; fermer() ramasse les processus, a la fin de la lecture."""

    def __init__(self, chemin, taux):
        self.chemin, self.taux = chemin, taux
        self._procs = []
        self._arrete = False
        self._a_rendu = False
        self._secours = False
        self._verrou = threading.Lock()
        ext = os.path.splitext(chemin)[1].lower()
        if _LEGERS and ext == ".mp3":
            self._mp3()
        elif _LEGERS and ext == ".wav":
            self._lancer(["sox", "-D", "-q", chemin] + _sortie(taux))
        else:
            self._ffmpeg()

    def _lancer(self, cmd, stdin=None):
        proc = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._procs.append(proc)
        self.stdout = proc.stdout
        return proc

    def _mp3(self):
        dec = self._lancer(["mpg123", "-q", "-m", "-w", "-", self.chemin])
        # L'en-tete ecrit dans un tuyau n'a pas de longueur : sox doit l'ignorer.
        self._lancer(["sox", "-D", "-q", "--ignore-length", "-t", "wav", "-"] + _sortie(self.taux), stdin=dec.stdout)
        dec.stdout.close()

    def _ffmpeg(self):
        self._lancer(["ffmpeg", "-v", "quiet", "-nostdin", "-i", self.chemin,
                      "-f", "s16le", "-ac", "1", "-ar", str(self.taux), "-"])

    def lire(self, n):
        data = self.stdout.read(n)
        if data:
            self._a_rendu = True
            return data
        with self._verrou:
            if self._a_rendu or self._secours or self._arrete or not _LEGERS:
                return b""
            self._secours = True
            self._fermer_procs()
            self._ffmpeg()
        return self.stdout.read(n)

    def arreter(self):
        with self._verrou:
            self._arrete = True
            for p in self._procs:
                if p.poll() is None:
                    p.terminate()

    def _fermer_procs(self):
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            if p.stdout and not p.stdout.closed:
                p.stdout.close()
        self._procs = []

    def fermer(self):
        with self._verrou:
            self._arrete = True
            self._fermer_procs()


def pcm(chemin, taux, timeout=30):
    """Tout le son d'un fichier ; b"" s'il est illisible."""
    try:
        flux = Flux(chemin, taux)
    except OSError:
        return b""
    garde = threading.Timer(timeout, flux.arreter)
    garde.daemon = True
    garde.start()
    morceaux = []
    try:
        while True:
            bout = flux.lire(65536)
            if not bout:
                return b"".join(morceaux)
            morceaux.append(bout)
    except (OSError, ValueError):
        return b"".join(morceaux)
    finally:
        garde.cancel()
        flux.fermer()
