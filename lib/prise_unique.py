"""Prise unique du dongle (2026-10-25) : un seul rtl_fm a 48 kHz sur la
frequence, et chaque lecteur en recoit une copie, EN MEME TEMPS :
  - un dsd-fme par mode numerique utile sur la bande ("-i -"),
  - la chaine analogique de radio_watch (voix FM, multimon-ng), a 22 050 Hz,
  - le son brut garde pour les juges (juges_watch).
Avant : un decodeur apres l'autre, chacun rouvrant le dongle pour 6 s ;
l'emission etait souvent finie avant que le bon decodeur arrive.

L'arbitre dit qui a la parole sur les haut-parleurs : le premier decodeur
numerique VALIDE la prend, meme a une voix FM deja ouverte (une synchro
validee est une preuve, la voix FM une estimation) ; un autre decodeur
numerique ne la lui reprend pas, sauf s'il la cede ("juge suivant").

Verifie le 2026-10-25 sur le relais DMR 467,2375 MHz : "rtl_fm -s 48000 |
dsd-fme -i -" donne autant de synchros propres que l'entree rtl de dsd-fme
(276/280 contre 276/278) ; "-s 12500 -r 48000" ne donne rien.

Interrupteur : le fichier ARRET (touch) remet l'ancien enchainement."""
import chemins
import audioop
import os
import queue
import threading
import time
import wave

import numpy as np

TAUX = 48000
MORCEAU = 4800 * 2          # 0,1 s de son 16 bits
ARRET = chemins.base("prise_unique_off")
RTL_FM = ["rtl_fm", "-M", "fm", "-s", str(TAUX), "-l", "0", "-E", "dc"]


def active():
    return not os.path.exists(ARRET)


def commande(freq_hz):
    return RTL_FM[:1] + ["-f", str(int(freq_hz))] + RTL_FM[1:]


class Prise:
    """Le flux d'un rtl_fm deja ouvert (radio_watch._open_rtl_fm), distribue a
    chaque abonne par sa propre file (un lecteur lent ne retient pas les autres)."""

    def __init__(self, proc):
        self.proc = proc
        self.ouverte = True
        self._abonnes = []
        self._verrou = threading.Lock()
        threading.Thread(target=self._lire, daemon=True).start()

    def _lire(self):
        try:
            while True:
                data = self.proc.stdout.read(MORCEAU)
                if not data:
                    break
                with self._verrou:
                    for q in self._abonnes:
                        q.put(data)
        except Exception:
            pass
        finally:
            self.ouverte = False
            with self._verrou:
                for q in self._abonnes:
                    q.put(None)

    def abonner(self):
        q = queue.Queue()
        with self._verrou:
            if self.ouverte:
                self._abonnes.append(q)
            else:
                q.put(None)
        return q

    def desabonner(self, q):
        with self._verrou:
            if q in self._abonnes:
                self._abonnes.remove(q)
        q.put(None)

    def vers_processus(self, stdin):
        """Nourrit l'entree d'un processus (dsd-fme -i -) jusqu'a la fin du flux."""
        q = self.abonner()

        def nourrir():
            try:
                while True:
                    data = q.get()
                    if data is None:
                        break
                    stdin.write(data)
                    stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                self.desabonner(q)
                try:
                    stdin.close()
                except OSError:
                    pass
        threading.Thread(target=nourrir, daemon=True).start()

    def flux(self, taux):
        """Faux processus rtl_fm pour la chaine analogique : .stdout donne le son
        au taux demande, .terminate() le detache (sans fermer la prise)."""
        return _Flux(self, taux)

    def enregistrer(self, chemin):
        """Son brut 48 kHz pour les juges ; rend la fonction qui arrete et ferme."""
        q = self.abonner()
        w = wave.open(chemin, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAUX)
        fini = threading.Event()

        def ecrire():
            try:
                while True:
                    data = q.get()
                    if data is None:
                        break
                    w.writeframes(data)
            finally:
                w.close()
                fini.set()
        threading.Thread(target=ecrire, daemon=True).start()

        def arreter():
            self.desabonner(q)
            fini.wait(5)
        return arreter

    def fermer(self):
        for p in (self.proc,):
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


class _Taux:
    """48 kHz -> taux plus bas, avec un vrai filtre anti-repliement : le bruit
    du discriminateur FM monte avec la frequence, sans filtre il retomberait
    dans la bande vocale."""

    def __init__(self, sortie, entree=TAUX, prises=63):
        self.entree, self.sortie = entree, sortie
        coupure = 0.45 * sortie
        n = np.arange(prises) - (prises - 1) / 2
        h = np.sinc(2 * coupure / entree * n) * np.hamming(prises)
        self.h = (h / h.sum()).astype(np.float32)
        self.reste = np.zeros(prises - 1, dtype=np.float32)
        self.etat = None

    def convertir(self, pcm):
        if self.sortie == self.entree:
            return pcm
        x = np.concatenate([self.reste, np.frombuffer(pcm, dtype=np.int16).astype(np.float32)])
        self.reste = x[-(len(self.h) - 1):]
        y = np.clip(np.convolve(x, self.h, "valid"), -32768, 32767).astype(np.int16).tobytes()
        out, self.etat = audioop.ratecv(y, 2, 1, self.entree, self.sortie, self.etat)
        return out


class _Flux:
    def __init__(self, prise, taux):
        self._prise = prise
        self._q = prise.abonner()
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "rb")
        self._w = os.fdopen(w, "wb")
        self._fil = threading.Thread(target=self._pomper, args=(_Taux(taux),), daemon=True)
        self._fil.start()

    def _pomper(self, taux):
        try:
            while True:
                data = self._q.get()
                if data is None:
                    break
                self._w.write(taux.convertir(data))
                self._w.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                self._w.close()
            except OSError:
                pass

    def terminate(self):
        self._prise.desabonner(self._q)

    kill = terminate

    def wait(self, timeout=None):
        self._fil.join(timeout)
        return 0

    def poll(self):
        return None if self._fil.is_alive() else 0


class Arbitre:
    """Qui a la parole sur la frequence (voir en tete)."""

    def __init__(self):
        self._verrou = threading.Lock()
        self.gagnant = None          # mode dsd-fme ("-fa"...), "analogique", ou None
        self.protocole = None
        self.stats = {}              # mode -> stats de dsd-fme (decodeurs.dsd_line)
        self.cedes = set()           # modes ecartes par "juge suivant"
        self.termine = threading.Event()  # l'ecoute est finie : les decodeurs en attente s'arretent
        self.pref = None             # juge le mieux note sur cette frequence
        self.pref_jusqua = 0.0       # il a la parole en premier pendant ce delai

    def prioritaire(self, qui):
        """Un juge deja confirme ici n'a pas encore parle : les autres attendent."""
        with self._verrou:
            return bool(self.pref and qui != self.pref and self.gagnant is None
                        and time.time() < self.pref_jusqua and self.pref not in self.cedes)

    def prendre(self, qui, protocole=None):
        with self._verrou:
            if qui in self.cedes:
                return False
            if qui == "analogique":
                ok = self.gagnant is None
            else:
                if (self.pref and qui != self.pref and self.gagnant is None
                        and time.time() < self.pref_jusqua and self.pref not in self.cedes):
                    return False
                ok = self.gagnant in (None, "analogique", qui)
            if ok:
                self.gagnant, self.protocole = qui, protocole if qui != "analogique" else None
            return ok

    def ceder(self, qui):
        with self._verrou:
            self.cedes.add(qui)
            if self.gagnant == qui:
                self.gagnant, self.protocole = None, None

    def numerique_gagne(self):
        return self.gagnant not in (None, "analogique")

    def numerique_en_vue(self):
        """Un decodeur a deja vu au moins une synchro propre : peut-etre du numerique."""
        return any(s.get("clean", 0) >= 1 for s in list(self.stats.values()))

    def attendre_numerique(self, secondes):
        fin = time.time() + secondes
        while time.time() < fin and not self.numerique_gagne() and not self.termine.is_set():
            time.sleep(0.1)
        return self.numerique_gagne()
