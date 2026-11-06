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
sur toute la fenetre -- et non sur 0,5 s capturees apres coup.

Reprise du 2026-11-05 (les prises ne sortaient plus que du souffle) :
  - chaque modulation a son propre filtre de canal. Le meme +-12 kHz pour tout
    laissait 24 kHz de bruit devant une AM ou une FM etroite qui n'en occupent
    que 9 a 16 : sous le seuil de la detection, plus rien ne sortait ;
  - un silencieux pris sur l'IQ. La FM sans porteuse rend du souffle a plein
    volume : sur 99 s dont 10 de parole, la preuve de voix mesurait surtout le
    souffle et refusait la vraie FM. On mesure l'exces de puissance au canal
    par rapport au bruit du dongle a cote, et les sons ne gardent que les
    moments d'emission. Rien n'est jete : le flux entier reste aux tickets ;
  - le centre de l'emission est recale avant les filtres etroits."""
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
TRANCHE = TAUX_BANC // 2       # 0,5 s de canal par mesure de signatures
CANAL_LARGE_HZ = 12_000        # filtre d'entree ; le flux et le numerique en ont besoin
CANAL_FM_HZ = 8_000            # 5 kHz d'excursion + 3 kHz de voix (Carson)
CANAL_AM_HZ = 4_500
DESACCENTUATION_S = 750e-6     # FM de phonie : le souffle aigu retombe, la voix reste
TRAME = TAUX_BANC // 20        # 50 ms : grain du silencieux
BANDE_LARGE_HZ = 6_500         # FM et AM occupent la bande
BLU_BAS_HZ, BLU_HAUT_HZ = 300, 3_300
EXCES_MIN = 2.0                # +3 dB sur le bruit du dongle, soutenus
TRAMES_OUVERTURE = 2           # 100 ms : une pointe seule n'ouvre pas
TRAMES_MAINTIEN = 10           # 500 ms : les blancs entre les mots restent
TRAMES_AVANT = 4               # 200 ms : l'attaque du premier mot
RETIRE_RAIES = 0.03            # une raie fixe du dongle ne compte pas comme une emission
RAIE_VARIATION_DB = 6.0        # une porteuse qui va et vient, elle, compte (morse)
RECENTRAGE_MAX_HZ = 4_500      # le pic du balayage peut etre faux d'une case, 3 906 Hz (mesure 2026-11-05)
LARGEUR_VUE_HZ = 5_500         # une FSK de +/- 4,5 kHz decalee d'une case reste vue en entier
PORTEUSE_PART = 0.5            # une raie est une porteuse si elle porte au moins ca de la puissance
BLU_LARGEUR_MAX_HZ = 4_000
BLU_ETROIT_HZ = 600            # FT8, FT4 : moins que ca. FreeDV et la voix se calent par leur bord
BLU_MILIEU_HZ = 1_500          # le milieu du canal audio
BLU_BORD_HZ = 300
PLANCHER_NFFT = 2_048


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


def _spectres(z):
    """Puissance par case de chaque trame de 50 ms : un bruit de variance s2 donne s2 partout."""
    n = len(z) // TRAME
    if not n:
        return None, None
    w = np.hanning(TRAME).astype(np.float32)
    x = np.fft.fftshift(np.fft.fft(z[:n * TRAME].reshape(n, TRAME) * w, axis=1), axes=1)
    return (np.abs(x) ** 2 / float((w ** 2).sum())).astype(np.float32), np.fft.fftshift(np.fft.fftfreq(TRAME, 1 / TAUX_BANC))


def _moyenne_sans_raies(p):
    k = max(1, int(p.shape[1] * (1 - RETIRE_RAIES)))
    return np.sort(p, axis=1)[:, :k].mean(axis=1)


def exces_par_trame(p, f, bruit):
    """(exces de puissance au canal, raie la plus forte), par trame ; bruit : un
    scalaire ou un tableau par trame. La voix BLU n'occupe que 3 kHz : mesuree sur
    13, elle se noierait."""
    bandes = [np.abs(f) <= BANDE_LARGE_HZ, (f >= BLU_BAS_HZ) & (f <= BLU_HAUT_HZ),
              (f <= -BLU_BAS_HZ) & (f >= -BLU_HAUT_HZ)]
    exces = np.max([_moyenne_sans_raies(p[:, b]) for b in bandes], axis=0) / bruit
    return exces, p[:, bandes[0]].max(axis=1) / bruit


def masque_emission(exces, raies):
    """True pendant l'emission : le plancher du dongle est la seule reference."""
    brut = exces >= EXCES_MIN
    if len(raies):
        haut, bas = np.percentile(raies, [90, 10])
        if 10 * np.log10((haut + 1e-9) / (bas + 1e-9)) >= RAIE_VARIATION_DB:
            brut = brut | (raies >= np.sqrt(haut * bas))     # moitie du chemin, en decibels
    masque = np.zeros(len(brut), dtype=bool)
    suite = 0
    for i, oui in enumerate(brut):
        suite = suite + 1 if oui else 0
        if suite >= TRAMES_OUVERTURE:
            masque[max(0, i - suite + 1 - TRAMES_AVANT):i + TRAMES_MAINTIEN + 1] = True
    return masque


def _bande_occupee(fd, md):
    """(bas, haut) de ce qui depasse le bruit, sans les 3 % de puissance de chaque bord."""
    plancher = np.median(md)
    occupe = md >= 4 * plancher
    if occupe.sum() < 3:
        return None
    exces = np.where(occupe, md - plancher, 0.0)
    cumul = np.cumsum(exces) / np.sum(exces)
    return float(fd[np.searchsorted(cumul, 0.03)]), float(fd[np.searchsorted(cumul, 0.97)])


def centre_emission(p, f, masque):
    """(decalage de l'emission en Hz, bande occupee apres recentrage ou None). Une porteuse
    se lit a sa raie, tout le reste au milieu de ce qu'il occupe. La raie la plus forte
    n'est une porteuse que si elle domine : en FSK (mesure 2026-11-05), c'est un symbole,
    et se caler dessus jetait l'autre tonalite d'un POCSAG hors du canal."""
    if not masque.any():
        return 0.0, None
    moy = p[masque].mean(axis=0)
    dans = np.abs(f) <= RECENTRAGE_MAX_HZ + LARGEUR_VUE_HZ
    fd, md = f[dans], moy[dans]
    plancher = np.median(md)
    i = int(np.argmax(md))
    exces = np.maximum(md - plancher, 0.0)
    autour = np.abs(fd - fd[i]) <= 2 * (fd[1] - fd[0])
    if md[i] >= 10 * plancher and abs(fd[i]) <= RECENTRAGE_MAX_HZ and exces[autour].sum() >= PORTEUSE_PART * exces.sum():
        return float(fd[i]), None
    bande = _bande_occupee(fd, md)
    if bande is None or (md >= 4 * plancher).sum() < 10:
        return 0.0, bande
    decalage = float(np.clip((bande[0] + bande[1]) / 2, -RECENTRAGE_MAX_HZ, RECENTRAGE_MAX_HZ))
    return decalage, (bande[0] - decalage, bande[1] - decalage)


def calage_blu(bande, cote):
    """Decalage a appliquer pour lire en BLU une emission qui occupe `bande` (recentree).
    Sans porteuse, le milieu de la bande n'est pas le zero de la BLU : centree, la moitie du
    signal tombe du mauvais cote. Etroit (donnees) : au milieu du canal audio. Large (voix) :
    son bord au bas du canal. Trop large pour etre de la BLU : on ne touche a rien."""
    if not bande:
        return 0.0
    bas, haut = bande
    largeur = haut - bas
    if largeur > BLU_LARGEUR_MAX_HZ:
        return 0.0
    if cote > 0:
        return (bas + haut) / 2 - BLU_MILIEU_HZ if largeur < BLU_ETROIT_HZ else bas - BLU_BORD_HZ
    return (bas + haut) / 2 + BLU_MILIEU_HZ if largeur < BLU_ETROIT_HZ else haut + BLU_BORD_HZ


def _decale(z, hz):
    if not hz:
        return z
    return z * np.exp(-2j * np.pi * hz * np.arange(len(z)) / TAUX_BANC).astype(np.complex64)


def demoduler(z, bande=None):
    """Canal a 32 kHz (cible a 0 Hz) -> {modulation: son 16 kHz, flottants}.
    bande : ce que l'emission occupe, pour caler la BLU (voir calage_blu)."""
    fm_iq = np.convolve(z, _passe_bas(CANAL_FM_HZ, TAUX_BANC, 65), "same")
    fm = np.angle(fm_iq[1:] * np.conj(fm_iq[:-1])) * TAUX_BANC / (2 * np.pi) / DEVIATION_HZ
    fm = np.concatenate([[0.0], fm]).astype(np.float32)
    fm -= fm.mean()
    tau = np.exp(-np.arange(int(5 * DESACCENTUATION_S * TAUX_BANC)) / (DESACCENTUATION_S * TAUX_BANC)).astype(np.float32)
    fm = np.convolve(fm, tau / tau.sum(), "same")
    am_iq = np.convolve(z, _passe_bas(CANAL_AM_HZ, TAUX_BANC, 65), "same")
    env = np.abs(am_iq).astype(np.float32)
    am = env - np.convolve(env, np.ones(1_600, dtype=np.float32) / 1_600, "same")   # 50 ms : retire la porteuse
    blu = _passe_bas(1_500, TAUX_BANC, 127)   # 150 a 3 150 Hz : le bas de FreeDV 700E, cale a 300 Hz, doit rester entier
    k = np.arange(len(blu))
    usb = np.convolve(_decale(z, calage_blu(bande, 1)), (blu * np.exp(2j * np.pi * 1_650 * k / TAUX_BANC)).astype(np.complex64), "same").real
    lsb = np.convolve(_decale(z, calage_blu(bande, -1)), (blu * np.exp(-2j * np.pi * 1_650 * k / TAUX_BANC)).astype(np.complex64), "same").real
    bas = _passe_bas(3_400, TAUX_BANC, 63)
    pas = TAUX_BANC // TAUX_SORTIE
    return {m: np.convolve(a.astype(np.float32), bas, "same")[::pas]
            for m, a in (("nfm", fm), ("am", am), ("usb", usb), ("lsb", lsb))}


class Banc:
    def __init__(self, taux_entree, decalage_cible_hz):
        """taux_entree : P (kS/s du Pi) ; decalage_cible_hz : position de la
        cible dans ce flux (-P/4 avec iq_reduit)."""
        if taux_entree % TAUX_BANC:
            raise ValueError("taux %d non multiple de %d" % (taux_entree, TAUX_BANC))
        self.P = taux_entree
        self.D = taux_entree // TAUX_BANC
        self.decalage = decalage_cible_hz
        self.pas = -2 * np.pi * decalage_cible_hz / taux_entree
        self.phase = 0.0
        self.reste = b""
        self.dec_phase = 0
        self.canal = _Filtre(_passe_bas(CANAL_LARGE_HZ, taux_entree, 129), complexe=True)
        self.morceaux = []                 # canal a 32 kHz, demodule en fin de fenetre
        self.bruits = []                   # (trames couvertes, bruit attendu par case du canal)
        self.actif = False                 # le dernier morceau portait une emission
        self.mesures = []                  # signatures des tranches actives
        self.emission = {"secondes": 0.0, "part": 0.0, "segments": 0, "exces_db": None, "recentrage_hz": 0}

    def _plancher(self, x):
        """Bruit du dongle a cote du canal, ramene a une case du canal. La mediane
        ignore les autres emissions ; on reste pres de la cible, la reponse du
        tuner n'est pas plate."""
        n = len(x) // PLANCHER_NFFT
        if not n:
            return None
        n = min(n, 16)
        w = np.hanning(PLANCHER_NFFT).astype(np.float32)
        blocs = x[:n * PLANCHER_NFFT].reshape(n, PLANCHER_NFFT) * w
        p = np.abs(np.fft.fft(blocs, axis=1)) ** 2 / float((w ** 2).sum())
        f = np.fft.fftfreq(PLANCHER_NFFT, 1 / self.P)
        ecart = np.abs(f - self.decalage)
        cote = (ecart >= 16_000) & (ecart <= 48_000) & (np.abs(f) >= 4_000)
        if not cote.any():
            return None
        return float(np.median(p[:, cote]) / np.log(2) * TAUX_BANC / self.P)

    def ajouter(self, octets):
        """Octets IQ uint8 du Pi (taux P)."""
        data = self.reste + octets
        utile = len(data) // 2 * 2
        self.reste = data[utile:]
        if not utile:
            return
        u = np.frombuffer(data[:utile], dtype=np.uint8).astype(np.float32)
        x = ((u[0::2] - 127.5) + 1j * (u[1::2] - 127.5)) / 127.5
        plancher = self._plancher(x)
        n = np.arange(len(x))
        x = x * np.exp(1j * (self.phase + self.pas * n))
        self.phase = float((self.phase + self.pas * len(x)) % (2 * np.pi))
        y = self.canal(x.astype(np.complex64))
        z = y[self.dec_phase::self.D]
        self.dec_phase = (self.dec_phase - len(y)) % self.D
        if not len(z):
            return
        self.morceaux.append(z)
        if plancher:
            self.bruits.append((len(z), plancher))
            p, f = _spectres(z)
            if p is not None:
                exces, _ = exces_par_trame(p, f, plancher)
                self.actif = bool(np.median(exces) >= EXCES_MIN)    # une emission en cours prolonge la fenetre

    def _bruit_par_trame(self, n):
        if not self.bruits:
            return None
        valeurs = np.concatenate([np.full(max(1, k // TRAME), v, dtype=np.float32) for k, v in self.bruits])
        if len(valeurs) < n:
            valeurs = np.concatenate([valeurs, np.full(n - len(valeurs), valeurs[-1], dtype=np.float32)])
        return valeurs[:n]

    def analyser(self):
        """Canal entier, recentre, et masque d'emission par trame de 50 ms."""
        z = np.concatenate(self.morceaux) if self.morceaux else np.zeros(0, dtype=np.complex64)
        p, f = _spectres(z)
        if p is None:
            return z, np.zeros(0, dtype=bool)
        bruit = self._bruit_par_trame(len(p))
        if bruit is None:
            return z, np.zeros(len(p), dtype=bool)
        exces, raies = exces_par_trame(p, f, bruit)
        masque = masque_emission(exces, raies)
        decalage, self.bande = centre_emission(p, f, masque)
        if decalage:
            z = z * np.exp(-2j * np.pi * decalage * np.arange(len(z)) / TAUX_BANC).astype(np.complex64)
        fronts = np.diff(np.concatenate([[0], masque.astype(np.int8), [0]]))
        self.emission = {"secondes": round(float(masque.sum()) * TRAME / TAUX_BANC, 1),
                         "part": round(float(masque.mean()), 3), "segments": int((fronts == 1).sum()),
                         "exces_db": round(float(10 * np.log10(np.median(exces[masque]))), 1) if masque.any() else None,
                         "recentrage_hz": int(round(decalage)),
                         "bande_hz": [int(round(x)) for x in self.bande] if self.bande else None}
        return z, masque

    def _signatures(self, z, masque):
        actifs = np.repeat(masque, TRAME)
        canal = z[:len(actifs)][actifs]
        self.mesures = []
        for i in range(0, len(canal) - TRANCHE + 1, TRANCHE):
            m = modulation_iq.mesurer(canal[i:i + TRANCHE], TAUX_BANC)
            if m:
                self.mesures.append(m)

    def ecrire_flux(self, chemin):
        """Discriminateur 48 kHz de toute la fenetre, pour les decodeurs (tickets).
        Filtre de canal de 16 kHz (2026-11-05) : sans lui, le souffle de toute la bande
        passe dans le discriminateur et le DMR disparait. Mesure sur un vrai relais noye
        de bruit, a 6 dB dans 12,5 kHz : 0 synchro sur 413 sans filtre, 392 avec. 16 kHz
        laisse passer l'AIS, plus large que le DMR. Rend True s'il existe."""
        z = getattr(self, "z", None)
        if z is None or len(z) < TAUX_BANC:
            return False
        z = np.convolve(z, _passe_bas(CANAL_FM_HZ, TAUX_BANC, 65), "same")
        fm = np.angle(z[1:] * np.conj(z[:-1])) * TAUX_BANC / (2 * np.pi) / DEVIATION_HZ
        # Un reste de decalage devient une composante continue, et sous 10 dB elle suffit a
        # perdre la synchro. Le recentrage suit la raie la plus forte : en FSK, c'est un
        # symbole, pas le centre. Les symboles s'equilibrent sur 200 ms, pas ce decalage.
        n = TAUX_BANC // 5
        cumul = np.concatenate([[0.0], np.cumsum(fm, dtype=np.float64)])
        bas = np.clip(np.arange(len(fm)) - n // 2, 0, len(fm))
        haut = np.clip(np.arange(len(fm)) + n // 2, 0, len(fm))
        fm = fm - (cumul[haut] - cumul[bas]) / np.maximum(haut - bas, 1)
        t = np.arange(len(fm) * 3 // 2) * 2 / 3
        a = np.interp(t, np.arange(len(fm)), fm)
        with wave.open(chemin, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48_000)
            w.writeframes(np.clip(a * 0.5 * 32767, -32768, 32767).astype("<i2").tobytes())
        return True

    def preuve(self):
        """(modulations prouvees, raison, mesures medianes, tranches actives)."""
        if not self.mesures:
            return [], "aucune emission presente pendant la fenetre", None, 0
        med = {k: float(np.median([m[k] for m in self.mesures])) for k in self.mesures[0]}
        mods, raison = modulation_iq.decider(med)
        return mods, raison, {k: round(v, 3) for k, v in med.items()}, len(self.mesures)

    def ecrire(self, dossier):
        """Un wav 16 kHz mono par modulation ; rend {modulation: chemin}. Le son ne
        garde que les moments d'emission ; sans aucune emission mesuree il reste
        entier, pour que l'oreille puisse encore dire le contraire."""
        z, masque = self.analyser()
        if not len(z):
            return {}
        self.z = z
        self._signatures(z, masque)
        sons = demoduler(z, getattr(self, "bande", None))
        pas = TRAME * TAUX_SORTIE // TAUX_BANC
        chemins = {}
        for m in MODULATIONS:
            a = sons[m]
            if masque.any():
                a = a[:len(masque) * pas][np.repeat(masque, pas)[:len(a)]]
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
