import numpy as np

# --- Filtre passe-bande vocal, temps reel, sans dependance externe ---
# Ajoute en session (2026-08-31, demande explicite de l'utilisateur) suite
# a l'analyse spectrale de noise_study/ (voir ROADMAP.md) : bruit structure
# identifie sur tous les canaux -- harmoniques secteur 50Hz (16/48/100/200/
# 300Hz...) sur les canaux AM aviation, tonalites hautes (6000/7234Hz) sur
# PMR446. Premiere etape proposee par l'utilisateur : "detourer en coupant
# tout ce qui est hors de la freq vocale" -- coupe tout en dehors de
# 300-3000Hz (bande vocale radio classique, coherente avec la bande passante
# audio typique des radios NFM/AM elles-memes).
#
# Implementation : deux biquads du second ordre (formules RBJ "Audio EQ
# Cookbook", standard et fiable, evite scipy -- absent sur ce Pi et lourd a
# compiler en ARM 32 bits) en cascade (passe-haut puis passe-bas), etat
# persistant entre chunks pour un filtrage continu sans discontinuite
# audible aux limites de chunk.

VOICE_HIGHPASS_HZ = 300.0
VOICE_LOWPASS_HZ = 3000.0
BIQUAD_Q = 0.7071  # 1/sqrt(2) -- reponse Butterworth (maximalement plate)


class Biquad:
    """Filtre biquad Direct Form I, etat persistant entre appels a
    process() pour un traitement continu par morceaux (streaming)."""

    def __init__(self, b0, b1, b2, a1, a2):
        self.b0, self.b1, self.b2 = b0, b1, b2
        self.a1, self.a2 = a1, a2
        self.x1 = self.x2 = 0.0
        self.y1 = self.y2 = 0.0

    def process(self, samples):
        """samples : numpy array de float. Retourne un nouvel array filtre
        de meme taille. Boucle Python pure -- un filtre IIR est
        intrinsequement sequentiel (chaque sortie depend des precedentes),
        pas vectorisable avec numpy seul ; reste largement dans le budget
        temps reel pour des morceaux de quelques milliers d'echantillons."""
        out = np.empty_like(samples, dtype=np.float64)
        b0, b1, b2, a1, a2 = self.b0, self.b1, self.b2, self.a1, self.a2
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2
        for i, x0 in enumerate(samples):
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            out[i] = y0
            x2 = x1
            x1 = x0
            y2 = y1
            y1 = y0
        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        return out


def _highpass_biquad(cutoff_hz, sample_rate, q=BIQUAD_Q):
    w0 = 2 * np.pi * cutoff_hz / sample_rate
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)
    b0 = (1 + cos_w0) / 2
    b1 = -(1 + cos_w0)
    b2 = (1 + cos_w0) / 2
    a0 = 1 + alpha
    a1 = -2 * cos_w0
    a2 = 1 - alpha
    return Biquad(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def _lowpass_biquad(cutoff_hz, sample_rate, q=BIQUAD_Q):
    w0 = 2 * np.pi * cutoff_hz / sample_rate
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)
    b0 = (1 - cos_w0) / 2
    b1 = 1 - cos_w0
    b2 = (1 - cos_w0) / 2
    a0 = 1 + alpha
    a1 = -2 * cos_w0
    a2 = 1 - alpha
    return Biquad(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


class VoiceBandpassFilter:
    """Passe-haut + passe-bas en cascade -- ne garde que 300-3000Hz. Une
    instance par capture (etat frais a chaque nouvelle reception, pas de
    memoire entre deux transmissions differentes)."""

    def __init__(self, sample_rate, highpass_hz=VOICE_HIGHPASS_HZ, lowpass_hz=VOICE_LOWPASS_HZ):
        self.sample_rate = sample_rate
        self._hp = _highpass_biquad(highpass_hz, sample_rate)
        self._lp = _lowpass_biquad(lowpass_hz, sample_rate)

    def process_s16le(self, chunk_bytes):
        x = np.frombuffer(chunk_bytes, dtype="<i2").astype(np.float64) / 32768.0
        y = self._lp.process(self._hp.process(x))
        y = np.clip(y * 32768.0, -32768, 32767).astype("<i2")
        return y.tobytes()

    def process_f32le(self, chunk_bytes):
        x = np.frombuffer(chunk_bytes, dtype="<f4").astype(np.float64)
        y = self._lp.process(self._hp.process(x))
        return y.astype("<f4").tobytes()


# --- Nettoyage doux du son capte (2026-10-02, demande de l'utilisateur) ---
# But : retirer le souffle propre a la frequence et les gresillements, faire
# un peu ressortir la voix, SANS detruire le fond sonore autour de la
# personne qui parle. RNNoise reste ce qu'il est (decision voix / bruit, sur
# le son non nettoye). Trois etapes, en flux :
#   1. gresillements : echantillons isoles tres au-dessus de leurs voisins
#      (claquements) remplaces par la mediane locale ;
#   2. soustraction spectrale de l'EMPREINTE DU BRUIT de la frequence
#      (lib/profils_bruit.py, apprise hors emission), plafonnee a
#      MAX_REDUCTION_DB et lissee dans le temps (pas de "son metallique") ;
#   3. compresseur tres doux et lent (2:1 au-dessus de -26 dBFS) qui remonte
#      un peu la voix sans ecraser les sons faibles.
# Tout en numpy vectorise (trames de 512 points) : leger pour le Pi.

FRAME = 512
HOP = 256
MAX_REDUCTION_DB = 10.0
OVERSUBTRACT = 1.5
GAIN_SMOOTHING = 0.6
CLICK_THRESHOLD = 8.0
COMP_THRESHOLD_DB = -26.0
COMP_RATIO = 2.0
COMP_MAKEUP_DB = 3.0
COMP_ATTACK_SEC = 0.01
COMP_RELEASE_SEC = 0.3


def cleaner_for(sample_rate, noise, settings):
    """GentleCleaner selon les reglages d'une bande (lib/reglages_audio.py)."""
    return GentleCleaner(sample_rate, noise, max_reduction_db=settings["reduction_bruit_db"],
                         click_threshold=settings["anti_crachotements"], anti_whistle=settings["anti_sifflement"],
                         anti_hum=settings["anti_bourdonnement"], gain_db=settings["gain_db"])


def noise_spectrum(pcm_s16, sample_rate):
    """Empreinte du bruit : amplitude moyenne par frequence (FRAME//2+1
    valeurs) d'un passage sans emission, ou None s'il est trop court."""
    x = np.frombuffer(pcm_s16, dtype=np.int16).astype(np.float64)
    if len(x) < sample_rate // 2:
        return None
    n = (len(x) - FRAME) // HOP + 1
    frames = np.lib.stride_tricks.sliding_window_view(x, FRAME)[::HOP][:n]
    window = np.sqrt(np.hanning(FRAME + 1)[:-1])
    return np.abs(np.fft.rfft(frames * window, axis=1)).mean(axis=0)


def _declick(x, threshold=CLICK_THRESHOLD):
    """Gresillements : ecart a la mediane sur 5 points tres superieur a
    l'ecart habituel -> remplace par la mediane (la voix, elle, n'a pas de
    pics isoles d'un ou deux echantillons)."""
    if len(x) < 16:
        return x
    padded = np.pad(x, 2, mode="edge")
    med = np.median(np.lib.stride_tricks.sliding_window_view(padded, 5), axis=1)
    resid = x - med
    mad = np.median(np.abs(resid)) + 1e-9
    clicks = np.abs(resid) > threshold * mad * 1.4826
    if clicks.any():
        x = x.copy()
        x[clicks] = med[clicks]
    return x


class HumCanceller:
    """Bourdonnement du secteur (harmoniques de 50 Hz, de 300 Hz a 2 kHz),
    tolerant aux derives du reseau : analyse fine (cases de ~5 Hz) ; sur les
    cases proches d'une harmonique, le plancher qui PERSISTE (minimum sur
    ~1,5 s) est le bourdonnement : on le retire, plafonne a 20 dB. La voix,
    qui va et vient, reste au-dessus de ce plancher."""

    N = 4096
    H = 1024

    def __init__(self, sample_rate, mains_hz=50.0):
        freqs = np.fft.rfftfreq(self.N, 1 / sample_rate)
        near = np.zeros(len(freqs), dtype=bool)
        for k in range(6, 41):
            near |= np.abs(freqs - k * mains_hz) <= 1.5 * sample_rate / self.N + 0.002 * k * mains_hz
        self.bins = near
        self.window = np.sqrt(np.hanning(self.N + 1)[:-1])
        self.inbuf = np.zeros(self.N - self.H)
        self.ola = np.zeros(self.N)
        self.smooth = None
        self.history = []
        self.keep = max(4, int(1.5 * sample_rate / self.H))
        self.floor_gain = 10 ** (-20 / 20)

    def process(self, x):
        self.inbuf = np.concatenate([self.inbuf, x])
        n = (len(self.inbuf) - self.N) // self.H + 1
        if n <= 0:
            return np.zeros(0)
        out = np.empty(n * self.H)
        for i in range(n):
            spec = np.fft.rfft(self.inbuf[i * self.H:i * self.H + self.N] * self.window)
            power = np.abs(spec) ** 2
            self.smooth = power if self.smooth is None else 0.7 * self.smooth + 0.3 * power
            self.history = (self.history + [self.smooth[self.bins]])[-self.keep:]
            floor = np.min(self.history, axis=0)
            gain = np.ones(len(spec))
            gain[self.bins] = np.clip(1 - 2.0 * floor / (power[self.bins] + 1e-9), self.floor_gain, 1.0)
            # Recouvrement de 75 % : les fenetres se somment a 2, d'ou le 0,5.
            self.ola += 0.5 * np.fft.irfft(spec * gain, self.N) * self.window
            out[i * self.H:(i + 1) * self.H] = self.ola[:self.H]
            self.ola = np.concatenate([self.ola[self.H:], np.zeros(self.H)])
        self.inbuf = self.inbuf[n * self.H:]
        return out


class GentleCompressor:
    def __init__(self, sample_rate, gain_db=0.0):
        self.makeup_db = COMP_MAKEUP_DB + gain_db
        self.block = max(1, sample_rate // 100)  # 10 ms
        self.attack = np.exp(-self.block / (COMP_ATTACK_SEC * sample_rate))
        self.release = np.exp(-self.block / (COMP_RELEASE_SEC * sample_rate))
        self.env_db = -90.0
        self.gain = 1.0

    def process(self, x):
        out = np.empty_like(x)
        for start in range(0, len(x), self.block):
            seg = x[start:start + self.block]
            level = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) / 32768 + 1e-9)
            coef = self.attack if level > self.env_db else self.release
            self.env_db = coef * self.env_db + (1 - coef) * level
            over = max(0.0, self.env_db - COMP_THRESHOLD_DB)
            target = 10 ** ((self.makeup_db - over * (1 - 1 / COMP_RATIO)) / 20)
            ramp = np.linspace(self.gain, target, len(seg), endpoint=False)  # pas de saut audible
            out[start:start + len(seg)] = seg * ramp
            self.gain = target
        return out


class GentleCleaner:
    """Nettoyage en flux d'un son s16le mono ; noise = empreinte du bruit
    (noise_spectrum) ou None. Reglages par bande (lib/reglages_audio.py) :
    retrait maximal, seuil des claquements, filtres anti-sifflement (tonalite
    fixe qui dure) et anti-bourdonnement (harmoniques du 50 Hz), volume."""

    def __init__(self, sample_rate, noise=None, max_reduction_db=MAX_REDUCTION_DB, click_threshold=CLICK_THRESHOLD,
                 anti_whistle=False, anti_hum=False, gain_db=0.0):
        self.noise_pow = (np.asarray(noise, dtype=np.float64) ** 2 * OVERSUBTRACT) if noise is not None else None
        self.window = np.sqrt(np.hanning(FRAME + 1)[:-1])
        self.inbuf = np.zeros(FRAME - HOP)
        self.ola = np.zeros(FRAME)
        self.prev_gain = np.ones(FRAME // 2 + 1)
        self.floor = 10 ** (-max_reduction_db / 20)
        self.click_threshold = click_threshold
        self.compressor = GentleCompressor(sample_rate, gain_db)
        freqs = np.fft.rfftfreq(FRAME, 1 / sample_rate)
        self.voice_band = (freqs > 300) & (freqs < 3400)
        self.anti_whistle = anti_whistle
        self.m1 = self.m2 = None
        # Harmoniques du secteur de 300 Hz a 1 kHz (en dessous, le filtre vocal
        # les a deja retirees) : cases les plus proches de k x 50 Hz.
        self.hum = HumCanceller(sample_rate) if anti_hum else None
        self.spectral = self.noise_pow is not None or anti_whistle

    def _denoise(self, x):
        self.inbuf = np.concatenate([self.inbuf, x])
        n = (len(self.inbuf) - FRAME) // HOP + 1
        if n <= 0:
            return np.zeros(0)
        out = np.empty(n * HOP)
        for i in range(n):
            frame = self.inbuf[i * HOP:i * HOP + FRAME] * self.window
            spec = np.fft.rfft(frame)
            mag = np.abs(spec)
            power = mag ** 2 + 1e-9
            if self.noise_pow is not None:
                gain = np.clip(1 - self.noise_pow / power, self.floor, 1.0)
            else:
                gain = np.ones(len(spec))
            gain = np.convolve(gain, np.ones(3) / 3, mode="same")  # lisse entre frequences voisines
            gain = GAIN_SMOOTHING * self.prev_gain + (1 - GAIN_SMOOTHING) * gain  # et dans le temps
            self.prev_gain = gain
            # Filtres francs, apres le lissage (sinon il les dilue) :
            if self.anti_whistle:
                # Un parasite fixe (tonalite, secteur) est STABLE dans le temps
                # et en pointe sur ses voisines ; la voix varie sans cesse.
                # Moyennes glissantes sur ~0,4 s de l'amplitude et de son carre.
                if self.m1 is None:
                    self.m1, self.m2 = mag.copy(), mag ** 2
                else:
                    self.m1 = 0.97 * self.m1 + 0.03 * mag
                    self.m2 = 0.97 * self.m2 + 0.03 * mag ** 2
                variation = np.sqrt(np.maximum(self.m2 - self.m1 ** 2, 0)) / (self.m1 + 1e-9)
                neighbours = (np.roll(self.m1, 4) + np.roll(self.m1, -4)) / 2 + 1e-9
                steady_peak = (variation < 0.35) & (self.m1 > 4 * neighbours) & self.voice_band
                steady_peak = steady_peak | np.roll(steady_peak, 1) | np.roll(steady_peak, -1)
                gain = gain.copy()
                gain[steady_peak] = np.minimum(gain[steady_peak], 0.05)
            self.ola += np.fft.irfft(spec * gain, FRAME) * self.window
            out[i * HOP:(i + 1) * HOP] = self.ola[:HOP]
            self.ola = np.concatenate([self.ola[HOP:], np.zeros(HOP)])
        self.inbuf = self.inbuf[n * HOP:]
        return out

    def process_s16le(self, chunk_bytes):
        x = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float64)
        if self.hum is not None:
            x = self.hum.process(x)
        x = _declick(x, self.click_threshold)
        if self.spectral:
            x = self._denoise(x)
        x = self.compressor.process(x)
        return np.clip(x, -32768, 32767).astype(np.int16).tobytes()

    def process_f32le(self, chunk_bytes):
        """Meme traitement pour un son flottant (RTLSDR-Airband, -1..1)."""
        x = np.frombuffer(chunk_bytes, dtype=np.float32).astype(np.float64) * 32768
        if self.hum is not None:
            x = self.hum.process(x)
        x = _declick(x, self.click_threshold)
        if self.spectral:
            x = self._denoise(x)
        x = self.compressor.process(x)
        return (np.clip(x, -32768, 32767) / 32768).astype(np.float32).tobytes()


class FmPilotFilter:
    """Radio FM : passe-bas a 15 kHz (largeur audio de la radio FM) qui
    supprime le pilote stereo a 19 kHz, sifflement constant entendu meme
    sur une station bien recue (2026-10-02). Filtre RIF vectorise."""

    def __init__(self, sample_rate, cutoff_hz=15_500.0, taps=81):
        n = np.arange(taps) - (taps - 1) / 2
        h = np.sinc(2 * cutoff_hz / sample_rate * n) * np.blackman(taps)
        self.h = h / h.sum()
        self.tail = np.zeros(taps - 1)
        self.active = sample_rate > 2 * cutoff_hz

    def process_s16le(self, chunk_bytes):
        if not self.active:
            return chunk_bytes
        x = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float64)
        xx = np.concatenate([self.tail, x])
        y = np.convolve(xx, self.h, mode="valid")
        self.tail = xx[-(len(self.h) - 1):]
        return np.clip(y, -32768, 32767).astype(np.int16).tobytes()
