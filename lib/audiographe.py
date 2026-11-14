"""Audiographes de diagnostic (2026-10-22, demande utilisateur : "des
audiographes a la place de graphiques") : une courbe de mesure radio rendue
en son, pour l'entendre au lieu de la voir.

Lecture d'un audiographe :
  - le TEMPS suit la frequence : debut = bas de la bande, fin = haut, et le
    son glisse de l'oreille gauche vers l'oreille droite ;
  - la HAUTEUR suit le niveau au-dessus du bruit de fond : grave = rien que
    du bruit, aigu = signal fort (3 octaves de 0 a +30 dB) ;
  - un TIC marque chaque graduation de frequence ;
  - deux sons de reference avant : le bruit de fond, puis le seuil de
    detection (+10 dB) -- ce qui monte plus haut est un vrai signal ;
  - les parasites du quartz de la cle (multiples de 14,4 MHz) sont effaces ;
  - un CARILLON par trame de capteur decodee pendant l'ecoute.

Ce module ne fait que les sons (fichiers WAV) ; les phrases qui les
presentent sont dites par la voix de la console (console/antenne_cmd.py).
"""
import math
import os
import struct
import subprocess
import tempfile
import wave

RATE = 22050
FLOOR_HZ = 220.0
OCTAVES_PAR_30DB = 3.0
SEUIL_DB = 10.0
QUARTZ_HZ = 14.4e6          # moitie du quartz 28,8 MHz du RTL-SDR : ses harmoniques sortent en pics
QUARTZ_TOL_HZ = 60e3
POINTS = 600                # courbe ramenee a 600 points (le max par groupe : les pics restent)
BALAYAGE_SEC = 8.0
CARILLONS_MAX = 30


def parasite(freq):
    n = freq / QUARTZ_HZ
    return abs(n - round(n)) * QUARTZ_HZ < QUARTZ_TOL_HZ


def courbe(bins, n_points=POINTS):
    """bins [(freq, db)] d'une passe rtl_power -> {"f0", "f1", "db": [dB au-dessus
    du plancher (20e percentile), n_points valeurs]}, parasites effaces."""
    bins = sorted(bins)
    valeurs = sorted(db for _, db in bins)
    plancher = valeurs[len(valeurs) // 5]
    f0, f1 = bins[0][0], bins[-1][0]
    groupes = [[] for _ in range(n_points)]
    for f, db in bins:
        k = min(n_points - 1, int((f - f0) / max(1.0, f1 - f0) * n_points))
        groupes[k].append(plancher if parasite(f) else db)
    return {"f0": f0, "f1": f1,
            "db": [round(max(0.0, (max(g) if g else plancher) - plancher), 1) for g in groupes]}


def pics(c):
    """Nombre de pics au-dessus du seuil de detection dans une courbe."""
    db = c["db"]
    return sum(1 for j in range(1, len(db) - 1)
               if db[j] >= SEUIL_DB and db[j] >= db[j - 1] and db[j] >= db[j + 1])


def _hauteur(db):
    return FLOOR_HZ * 2 ** (min(db, 30.0) / 30.0 * OCTAVES_PAR_30DB)


class _Son:
    def __init__(self):
        self.g, self.d = [], []

    def silence(self, sec):
        n = int(sec * RATE)
        self.g += [0.0] * n
        self.d += [0.0] * n

    def ton(self, freq, sec, vol=0.3):
        n = int(sec * RATE)
        for i in range(n):
            s = vol * min(1.0, i / 400, (n - i) / 400) * math.sin(2 * math.pi * freq * i / RATE)
            self.g.append(s)
            self.d.append(s)

    def carillon(self):
        n = int(0.18 * RATE)
        for i in range(n):
            s = 0.25 * math.exp(-i / (0.05 * RATE)) * (math.sin(2 * math.pi * 1568 * i / RATE)
                                                       + 0.5 * math.sin(2 * math.pi * 2349 * i / RATE))
            self.g.append(s)
            self.d.append(s)
        self.silence(0.07)

    def balayage(self, c, graduation_mhz, duree=BALAYAGE_SEC):
        db, f0, f1 = c["db"], c["f0"], c["f1"]
        n = int(duree * RATE)
        phase = 0.0
        pas = graduation_mhz * 1e6
        prochaine = math.ceil(f0 / pas) * pas
        tic, tic_len = 0, int(0.012 * RATE)
        for i in range(n):
            x = i / n
            pos = x * (len(db) - 1)
            k = int(pos)
            a = pos - k
            v = db[k] * (1 - a) + db[min(k + 1, len(db) - 1)] * a
            phase += 2 * math.pi * _hauteur(v) / RATE
            s = 0.22 * math.sin(phase)
            if f0 + x * (f1 - f0) >= prochaine:
                prochaine += pas
                tic = tic_len
            if tic:
                s += 0.35 * (1 if (tic // 8) % 2 else -1) * tic / tic_len
                tic -= 1
            env = min(1.0, i / 600, (n - i) / 600)
            self.g.append(s * env * (1 - x * 0.8))
            self.d.append(s * env * (0.2 + x * 0.8))

    def ecrire(self):
        fd, chemin = tempfile.mkstemp(prefix="audiographe_", suffix=".wav")
        os.close(fd)
        with wave.open(chemin, "w") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(struct.pack("<hh", int(max(-1, min(1, g)) * 32767),
                                               int(max(-1, min(1, d)) * 32767))
                                   for g, d in zip(self.g, self.d)))
        return chemin


def wav_bruit():
    s = _Son()
    s.ton(FLOOR_HZ, 0.8)
    return s.ecrire()


def wav_seuil():
    s = _Son()
    s.ton(_hauteur(SEUIL_DB), 0.8)
    return s.ecrire()


def wav_balayage(c, graduation_mhz, duree=BALAYAGE_SEC):
    s = _Son()
    s.balayage(c, graduation_mhz, duree)
    s.silence(0.3)
    return s.ecrire()


def wav_direct(c, graduation_mhz, force_db, mieux=False, moins=False, duree=1.5):
    """Une passe de l'antenne en direct (lib/carte_antenne) : le balayage court du
    segment, puis la FORCE en hauteur (comme le balayage : aigu = fort), puis un
    carillon montant si c'est le meilleur reglage de la seance (le centrage), ou
    un ton grave bref si l'antenne perd nettement."""
    s = _Son()
    s.balayage(c, graduation_mhz, duree)
    s.silence(0.12)
    s.ton(_hauteur(force_db), 0.35, vol=0.35)
    if mieux:
        s.silence(0.05)
        s.carillon()
        s.carillon()
    elif moins:
        s.silence(0.05)
        s.ton(FLOOR_HZ / 2, 0.25, vol=0.3)
    return s.ecrire()


def wav_carillons(n):
    s = _Son()
    for _ in range(min(n, CARILLONS_MAX)):
        s.carillon()
    return s.ecrire()


def jouer(chemin):
    """Joue un WAV sur "default" (dmix) et attend la fin ; le fichier est efface."""
    try:
        subprocess.run(["aplay", "-q", "-D", "default", chemin], timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            os.remove(chemin)
        except OSError:
            pass
