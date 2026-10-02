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


def _declick(x):
    """Gresillements : ecart a la mediane sur 5 points tres superieur a
    l'ecart habituel -> remplace par la mediane (la voix, elle, n'a pas de
    pics isoles d'un ou deux echantillons)."""
    if len(x) < 16:
        return x
    padded = np.pad(x, 2, mode="edge")
    med = np.median(np.lib.stride_tricks.sliding_window_view(padded, 5), axis=1)
    resid = x - med
    mad = np.median(np.abs(resid)) + 1e-9
    clicks = np.abs(resid) > CLICK_THRESHOLD * mad * 1.4826
    if clicks.any():
        x = x.copy()
        x[clicks] = med[clicks]
    return x


class GentleCompressor:
    def __init__(self, sample_rate):
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
            target = 10 ** ((COMP_MAKEUP_DB - over * (1 - 1 / COMP_RATIO)) / 20)
            ramp = np.linspace(self.gain, target, len(seg), endpoint=False)  # pas de saut audible
            out[start:start + len(seg)] = seg * ramp
            self.gain = target
        return out


class GentleCleaner:
    """Nettoyage en flux d'un son s16le mono ; noise = empreinte du bruit
    (noise_spectrum) ou None (alors gresillements et compresseur seulement)."""

    def __init__(self, sample_rate, noise=None):
        self.noise_pow = (np.asarray(noise, dtype=np.float64) ** 2 * OVERSUBTRACT) if noise is not None else None
        self.window = np.sqrt(np.hanning(FRAME + 1)[:-1])
        self.inbuf = np.zeros(FRAME - HOP)
        self.ola = np.zeros(FRAME)
        self.prev_gain = np.ones(FRAME // 2 + 1)
        self.floor = 10 ** (-MAX_REDUCTION_DB / 20)
        self.compressor = GentleCompressor(sample_rate)

    def _denoise(self, x):
        self.inbuf = np.concatenate([self.inbuf, x])
        n = (len(self.inbuf) - FRAME) // HOP + 1
        if n <= 0:
            return np.zeros(0)
        out = np.empty(n * HOP)
        for i in range(n):
            frame = self.inbuf[i * HOP:i * HOP + FRAME] * self.window
            spec = np.fft.rfft(frame)
            power = np.abs(spec) ** 2 + 1e-9
            gain = np.clip(1 - self.noise_pow / power, self.floor, 1.0)
            gain = np.convolve(gain, np.ones(3) / 3, mode="same")  # lisse entre frequences voisines
            gain = GAIN_SMOOTHING * self.prev_gain + (1 - GAIN_SMOOTHING) * gain  # et dans le temps
            self.prev_gain = gain
            self.ola += np.fft.irfft(spec * gain, FRAME) * self.window
            out[i * HOP:(i + 1) * HOP] = self.ola[:HOP]
            self.ola = np.concatenate([self.ola[HOP:], np.zeros(HOP)])
        self.inbuf = self.inbuf[n * HOP:]
        return out

    def process_s16le(self, chunk_bytes):
        x = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float64)
        x = _declick(x)
        if self.noise_pow is not None:
            x = self._denoise(x)
        x = self.compressor.process(x)
        return np.clip(x, -32768, 32767).astype(np.int16).tobytes()

    def process_f32le(self, chunk_bytes):
        """Meme traitement pour un son flottant (RTLSDR-Airband, -1..1)."""
        x = np.frombuffer(chunk_bytes, dtype=np.float32).astype(np.float64) * 32768
        x = _declick(x)
        if self.noise_pow is not None:
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
