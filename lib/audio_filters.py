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
