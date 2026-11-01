"""Banc de demodulation complet sur la machine compagne (2026-11-01).

Option la plus fiable pour choisir le demodulateur, trop lourde pour le Pi :
au lieu de deviner la modulation d'apres des signatures (lib/modulation_iq,
qui s'abstient quand le signal est faible), on DEMODULE le meme IQ dans toutes
les modulations a la fois -- AM, FM etroite, BLU haute, BLU basse -- et le Pi ne
retient que celle dont la voix est PROUVEE (RNNoise + rythme syllabique, sur le
Pi : une seule reference de jugement). Banc de detecteurs en parallele, juge
sur le contenu verifie plutot que sur une statistique.

Branche sur le relais iq_fanout : il recoit l'IQ du Pi au taux reduit P (256 ou
288 kS/s), cible a -P/4 (lib iq_reduit). Ramene la cible a 0 Hz, filtre et
decime a 32 kHz, puis demodule ; sorties 16 kHz mono. La FM large (200 kHz) ne
tient pas dans ce flux reduit : elle reste au chemin local du Pi.

Mesure sur signal de test (2026-11-01) : le rythme syllabique NE distingue PAS
les demodulations (une AM lue en BLU garde le rythme de ses syllabes). Le banc
rend donc aussi la PREUVE DE MODULATION, mesuree sur l'IQ pendant l'emission
elle-meme : signatures de lib/modulation_iq (enveloppe, frequence instantanee,
porteuse, spectre) sur chaque demi-seconde ou le signal est present, mediane
sur toute la fenetre -- et non sur 0,5 s capturees apres coup."""
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "decodeurs"))
import modulation_iq  # noqa: E402  (le meme module qu'au Pi : bridge/construire_decodeurs_mac.sh)

TAUX_BANC = 32_000
TAUX_SORTIE = 16_000
MODULATIONS = ("am", "nfm", "usb", "lsb")
DEVIATION_HZ = 5_000           # FM etroite : excursion nominale ramenee a +-1
ACTIVITE_RAPPORT = 4.0         # puissance concentree au canal vs etalee sur la bande : signal present
TRANCHE = TAUX_BANC // 2       # 0,5 s de canal par mesure de signatures


def _passe_bas(coupure_hz, taux, prises):
    n = np.arange(prises) - (prises - 1) / 2
    h = np.sinc(2 * coupure_hz / taux * n) * np.hamming(prises)
    return (h / h.sum()).astype(np.float32)


class _Filtre:
    """Convolution continue d'un flux par morceaux (garde la traine)."""

    def __init__(self, h, complexe=False):
        self.h = h
        self.traine = np.zeros(len(h) - 1, dtype=np.complex64 if complexe else np.float32)

    def __call__(self, x):
        xx = np.concatenate([self.traine, x.astype(self.traine.dtype)])
        self.traine = xx[-(len(self.h) - 1):]
        return np.convolve(xx, self.h, "valid")


class Banc:
    def __init__(self, taux_entree, decalage_cible_hz):
        """taux_entree : P (kS/s du Pi) ; decalage_cible_hz : position de la
        cible dans ce flux (-P/4 avec iq_reduit)."""
        if taux_entree % TAUX_BANC:
            raise ValueError("taux %d non multiple de %d" % (taux_entree, TAUX_BANC))
        self.P = taux_entree
        self.D = taux_entree // TAUX_BANC
        self.pas = -2 * np.pi * decalage_cible_hz / taux_entree
        self.phase = 0.0
        self.reste = b""
        self.dec_phase = 0
        self.canal = _Filtre(_passe_bas(12_000, taux_entree, 129), complexe=True)
        bas = _passe_bas(3_400, TAUX_BANC, 63)
        self.audio = {m: _Filtre(bas) for m in MODULATIONS}
        blu = _passe_bas(1_350, TAUX_BANC, 127)
        t = np.arange(len(blu))
        self.usb = _Filtre((blu * np.exp(2j * np.pi * 1_650 * t / TAUX_BANC)).astype(np.complex64), complexe=True)
        self.lsb = _Filtre((blu * np.exp(-2j * np.pi * 1_650 * t / TAUX_BANC)).astype(np.complex64), complexe=True)
        self.moyenne_am = _Filtre(np.ones(1_600, dtype=np.float32) / 1_600)   # 50 ms : retire la porteuse
        self.precedent = np.complex64(1)
        self.sorties = {m: [] for m in MODULATIONS}
        self.actif = False                 # le dernier morceau portait un signal concentre au canal
        self.tranche = []                  # canal (32 kHz) en cours d'accumulation pour les signatures
        self.mesures = []                  # signatures des tranches actives

    def ajouter(self, octets):
        """Octets IQ uint8 du Pi (taux P)."""
        data = self.reste + octets
        utile = len(data) // 2 * 2
        self.reste = data[utile:]
        if not utile:
            return
        u = np.frombuffer(data[:utile], dtype=np.uint8).astype(np.float32)
        x = ((u[0::2] - 127.5) + 1j * (u[1::2] - 127.5)) / 127.5
        n = np.arange(len(x))
        x = x * np.exp(1j * (self.phase + self.pas * n))
        self.phase = float((self.phase + self.pas * len(x)) % (2 * np.pi))
        y = self.canal(x.astype(np.complex64))
        z = y[self.dec_phase::self.D]
        self.dec_phase = (self.dec_phase - len(y)) % self.D
        if not len(z):
            return
        etale = float(np.mean(np.abs(x) ** 2)) + 1e-12
        concentre = float(np.mean(np.abs(z) ** 2)) / (TAUX_BANC / self.P)
        self.actif = concentre / etale >= ACTIVITE_RAPPORT
        if self.actif:
            self.tranche.append(z)
            if sum(len(t) for t in self.tranche) >= TRANCHE:
                m = modulation_iq.mesurer(np.concatenate(self.tranche), TAUX_BANC)
                if m:
                    self.mesures.append(m)
                self.tranche = []
        # FM etroite : frequence instantanee
        zz = np.concatenate([[self.precedent], z])
        self.precedent = z[-1]
        fm = np.angle(zz[1:] * np.conj(zz[:-1])) * TAUX_BANC / (2 * np.pi) / DEVIATION_HZ
        # AM : enveloppe moins sa moyenne glissante (la porteuse)
        env = np.abs(z).astype(np.float32)
        am = env - self.moyenne_am(env)
        brut = {"nfm": fm.astype(np.float32), "am": am,
                "usb": np.real(self.usb(z)).astype(np.float32), "lsb": np.real(self.lsb(z)).astype(np.float32)}
        for m in MODULATIONS:
            a = self.audio[m](brut[m])
            self.sorties[m].append(a[::TAUX_BANC // TAUX_SORTIE])

    def preuve(self):
        """(modulations prouvees, raison, mesures medianes, tranches actives)."""
        if not self.mesures:
            return [], "aucune emission presente pendant la fenetre", None, 0
        med = {k: float(np.median([m[k] for m in self.mesures])) for k in self.mesures[0]}
        mods, raison = modulation_iq.decider(med)
        return mods, raison, {k: round(v, 3) for k, v in med.items()}, len(self.mesures)

    def ecrire(self, dossier):
        """Un wav 16 kHz mono par modulation ; rend {modulation: chemin}."""
        import os
        chemins = {}
        for m in MODULATIONS:
            if not self.sorties[m]:
                continue
            a = np.concatenate(self.sorties[m])
            crete = float(np.percentile(np.abs(a), 99.9)) or 1.0
            pcm = np.clip(a / crete * 0.5 * 32767, -32768, 32767).astype("<i2")
            chemin = os.path.join(dossier, "banc_%s.wav" % m)
            with wave.open(chemin, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(TAUX_SORTIE)
                w.writeframes(pcm.tobytes())
            chemins[m] = chemin
        return chemins
