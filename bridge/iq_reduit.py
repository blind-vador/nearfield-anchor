"""IQ reduit entre le Pi et la machine compagne (2026-11-01).

Pourquoi : dsd-neo demande toujours au dongle au moins 1 MS/s (1,536 MS/s pour
48 kHz de bande DSP, 1,152 MS/s pour le TETRA a 72 kHz), puis decime lui-meme.
Servi par le rtl_tcp du Pi, c'est 18 a 25 Mbit/s d'emission Wi-Fi continue --
ce qui a fait caler la puce Wi-Fi du Pi (30/10 23 h 10, 01/11 13 h 01), moins
d'une minute apres l'ouverture du flux a chaque fois.

Comment, sans toucher ni au Pi ni a dsd-neo : le relais (iq_fanout) intercepte
les commandes rtl_tcp de dsd-neo.
  - taux demande R : on demande au Pi un taux P que le dongle sait faire
    nativement (225-300 kS/s) avec R = L x P, L entier (1,536 M = 6 x 256 k ;
    1,152 M = 4 x 288 k) ;
  - frequence demandee F : dsd-neo accorde a "cible + R/4" (decalage fs/4,
    sauf accord decale 0x0a) ; on accorde le Pi a "cible + P/4" (la cible reste
    hors du pic DC du dongle) ;
  - flux recu a P : interpole par L (filtre passe-bas polyphase, +-0,45 P garde)
    puis decale en frequence pour remettre la cible exactement la ou dsd-neo
    l'attend dans son flux a R.
Le Wi-Fi du Pi passe de ~25 a ~4 Mbit/s ; hors de +-0,45 P autour de la cible,
dsd-neo ne voit plus rien (il n'en a pas besoin : sa bande utile fait 48 ou
72 kHz). Si aucun P entier ne convient, on laisse passer tel quel."""
import struct

import numpy as np

TAUX_MIN, TAUX_MAX = 225_001, 300_000     # plage basse native des dongles RTL
GARDE = 0.45                               # fraction de P gardee de chaque cote
PRISES_PAR_PHASE = 16

CMD_FREQ, CMD_TAUX, CMD_DECALE = 0x01, 0x02, 0x0A


def taux_reduit(taux_demande):
    """(P, L) avec taux_demande = L x P et P dans la plage native, ou None."""
    for l in range(2, 16):
        if taux_demande % l:
            continue
        p = taux_demande // l
        if p < TAUX_MIN:
            break
        if p <= TAUX_MAX:
            return p, l
    return None


def _filtre(l):
    n = l * PRISES_PAR_PHASE
    t = np.arange(n) - (n - 1) / 2
    coupure = GARDE / l                    # en cycles par echantillon de sortie
    h = np.sinc(2 * coupure * t) * np.hamming(n)
    h *= l / h.sum()                       # chaque phase somme a ~1 : amplitude conservee
    return [h[k::l].astype(np.float32) for k in range(l)]


class Reducteur:
    """Etat partage : commandes du client (transformees vers le Pi) et flux du
    Pi (reconstruit pour les clients)."""

    def __init__(self):
        self.taux = None        # R demande par dsd-neo
        self.reduit = None      # (P, L) ou None : passe tel quel
        self.freq = None        # F demande par dsd-neo
        self.decale = False     # accord decale (0x0a) : cible au centre, pas de fs/4
        self._phases = None
        self._histo = None
        self._reste = b""
        self._phase = 0.0
        self._decalage_hz = 0.0

    # --- commandes du client vers le Pi -------------------------------------
    def commande(self, cmd):
        """5 octets du client -> liste de commandes a envoyer au Pi."""
        code, valeur = cmd[0], struct.unpack(">I", cmd[1:5])[0]
        if code == CMD_TAUX:
            self.taux = valeur
            self.reduit = taux_reduit(valeur)
            self._phases = _filtre(self.reduit[1]) if self.reduit else None
            self._histo, self._reste, self._phase = None, b"", 0.0
            sortie = [struct.pack(">BI", CMD_TAUX, self.reduit[0] if self.reduit else valeur)]
            if self.freq is not None:        # la frequence depend du taux : on la recale
                sortie.append(self._freq_pi())
            return sortie
        if code == CMD_FREQ:
            self.freq = valeur
            return [self._freq_pi()]
        if code == CMD_DECALE:
            self.decale = bool(valeur)
            sortie = [cmd]
            if self.freq is not None:
                sortie.append(self._freq_pi())
            return sortie
        return [cmd]

    def _freq_pi(self):
        if not self.reduit or self.taux is None:
            self._decalage_hz = 0.0
            return struct.pack(">BI", CMD_FREQ, self.freq)
        p = self.reduit[0]
        cible = self.freq - (0 if self.decale else self.taux // 4)
        centre = cible + p // 4
        # Une composante a f arrive a (f - centre) chez le Pi ; dsd-neo l'attend
        # a (f - freq) : on decale de (centre - freq).
        self._decalage_hz = float(centre - self.freq)
        return struct.pack(">BI", CMD_FREQ, int(centre))

    # --- flux du Pi vers les clients ----------------------------------------
    def flux(self, data):
        """Octets IQ uint8 du Pi -> octets IQ uint8 au taux demande."""
        if not self.reduit or not self._phases:
            return data
        data = self._reste + data
        utile = len(data) // 2 * 2
        self._reste = data[utile:]
        if not utile:
            return b""
        u = np.frombuffer(data[:utile], dtype=np.uint8).astype(np.float32)
        x = ((u[0::2] - 127.5) + 1j * (u[1::2] - 127.5)) / 127.5
        k = len(self._phases[0])
        if self._histo is None:
            self._histo = np.zeros(k - 1, dtype=np.complex64)
        xx = np.concatenate([self._histo, x.astype(np.complex64)])
        self._histo = xx[-(k - 1):]
        y = np.stack([np.convolve(xx, h, "valid") for h in self._phases], axis=1).reshape(-1)
        n = np.arange(len(y))
        pas = 2 * np.pi * self._decalage_hz / self.taux
        y = y * np.exp(1j * (self._phase + pas * n))
        self._phase = float((self._phase + pas * len(y)) % (2 * np.pi))
        sortie = np.empty(2 * len(y), dtype=np.float32)
        sortie[0::2], sortie[1::2] = y.real, y.imag
        return np.clip(np.rint(sortie * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()
