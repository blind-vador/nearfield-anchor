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
import threading

import audio_lock

WAV_HEADER = 44


def duree_max(text, minimum=45):
    """Temps maximal pour dire ce texte (2026-10-19 : les delais fixes de 30,
    45 ou 120 s coupaient les textes longs, comme l'aide) : ~9 caracteres par
    seconde au debit le plus lent, plus une marge."""
    return max(minimum, len(text or "") / 9 + 20)
ESPEAK_RATE = 22050


class Speech:
    """Une phrase en cours : espeak (fabrique) | aplay (joue)."""

    def __init__(self, text, voice="fr", rate=None, scale=None):
        """scale(pcm) -> pcm : volume applique au vol (priorites, audio_lock) ;
        sans scale, espeak alimente aplay directement (console)."""
        self._text = text
        args = ["espeak", "-v", voice, "-b", "1", "--stdout"]
        if rate:
            args += ["-s", str(rate)]
        # Le PCM est relaye dans un autre fil : il doit connaitre le ticket
        # du fil appelant, sinon le volume decide par l'arbitre (attenue si une
        # source plus prioritaire parle) etait ignore (fuite du 2026-10-16).
        self._ticket = audio_lock.ticket_courant() if scale is not None else None
        self._espeak = subprocess.Popen(args + [text], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._relay = None
        if scale is None:
            self._aplay = subprocess.Popen(["aplay", "-q", "-D", "default"],
                                           stdin=self._espeak.stdout, stderr=subprocess.DEVNULL)
            self._espeak.stdout.close()  # aplay est le seul lecteur du tube
        else:
            self._aplay = subprocess.Popen(["aplay", "-q", "-D", "default", "-r", str(ESPEAK_RATE),
                                            "-f", "S16_LE", "-c", "1"],
                                           stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self._relay = threading.Thread(target=self._pump, args=(scale,), daemon=True)
            self._relay.start()

    def _pump(self, scale):
        try:
            self._espeak.stdout.read(WAV_HEADER)
            while True:
                data = self._espeak.stdout.read(ESPEAK_RATE // 5 * 2)
                if not data:
                    break
                self._aplay.stdin.write(scale(data, self._ticket))
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            try:
                self._aplay.stdin.close()
            except OSError:
                pass

    def poll(self):
        return self._aplay.poll()

    def wait(self, timeout=None):
        # timeout=None : borne a 60 s pour ne jamais bloquer la console si
        # aplay reste ouvert sur une carte USB/dmix plantee.
        if timeout is None:
            timeout = duree_max(self._text, 60)
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


def say(text, voice="fr", rate=None, timeout=None, scale=None):
    """Parle et attend la fin de la phrase (au plus timeout secondes)."""
    return Speech(text, voice, rate, scale).wait(timeout=timeout)
