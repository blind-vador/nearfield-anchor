"""espeak -> aplay : la seule facon de faire parler espeak dans ce projet.

espeak ne joue JAMAIS le son lui-meme (2026-09-29) : il passe par PortAudio,
qui tente d'abord de joindre un serveur JACK (absent ici) via un verrou en
memoire partagee, /dev/shm/jack_db-<uid>. Un espeak tue pendant cette
tentative -- ce que fait la console a chaque touche pour couper la parole --
laisse ce verrou ferme : TOUS les espeak suivants attendent indefiniment
(console muette, confirmation de redemarrage figee, constate apres un
defilement rapide aux fleches en mode manuel). Avec --stdout, espeak se
contente de fabriquer le WAV, sans aucun acces audio ni verrou JACK ; aplay
le joue sur "default" (mixeur de l'adaptateur USB).
"""
import subprocess


class Speech:
    """Une phrase en cours : espeak (fabrique) | aplay (joue)."""

    def __init__(self, text, voice="fr", rate=None):
        args = ["espeak", "-v", voice, "-b", "1", "--stdout"]
        if rate:
            args += ["-s", str(rate)]
        self._espeak = subprocess.Popen(args + [text], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._aplay = subprocess.Popen(["aplay", "-q", "-D", "default"],
                                       stdin=self._espeak.stdout, stderr=subprocess.DEVNULL)
        self._espeak.stdout.close()  # aplay est le seul lecteur du tube

    def poll(self):
        return self._aplay.poll()

    def wait(self, timeout=None):
        try:
            code = self._aplay.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            return None
        self._espeak.wait(timeout=5)
        return code

    def kill(self):
        """Coupe la parole et ramasse les deux processus (pas de fantomes)."""
        for proc in (self._aplay, self._espeak):
            if proc.poll() is None:
                proc.kill()
        for proc in (self._aplay, self._espeak):
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def say(text, voice="fr", rate=None, timeout=30):
    """Parle et attend la fin de la phrase (au plus timeout secondes)."""
    return Speech(text, voice, rate).wait(timeout=timeout)
