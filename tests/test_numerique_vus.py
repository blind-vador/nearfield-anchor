"""Un mode numerique prouve sans voix se dit au journal, sans le remplir."""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "config", "bridge")]

import numpy as np

import banc_demod
import numerique_vus as nv

nv.PATH = os.path.join(tempfile.mkdtemp(), "vus.json")
nv.ECRITURE = nv.PATH + ".verrou"

# le bruit (au plus 6 propres releves) ne prouve rien ; un vrai relais, si
assert not nv.prouve(6, 6)
assert not nv.prouve(25, 200)
assert not nv.prouve(None, None)
assert nv.prouve(26, 27)

F = 467237500
T = 1000000.0
assert nv.a_dire(F, "DMR", maintenant=T)
assert not nv.a_dire(F + 100, "DMR", maintenant=T + 60), "meme canal, relais au repos : silence"
assert nv.a_dire(F, "P25", maintenant=T + 60), "un autre mode sur le canal se dit tout de suite"
assert nv.a_dire(F, "DMR", groupes=[102], maintenant=T + 120), "groupe nouveau"
assert not nv.a_dire(F, "DMR", groupes=[102], maintenant=T + 180)
assert nv.a_dire(F, "DMR", chiffre=True, maintenant=T + 240), "chiffrement nouveau"
assert not nv.a_dire(F, "DMR", maintenant=T + 300)
assert nv.a_dire(F, "DMR", maintenant=T + 240 + nv.REDIRE_SEC), "on le redit de temps en temps"

# le flux des juges : pas de composante continue, meme avec un signal mal centre
banc = banc_demod.Banc(256000, -64000)
n = banc_demod.TAUX_BANC * 3
symboles = np.repeat(np.random.default_rng(1).choice([-1.0, 1.0], n // 6 + 1), 7)[:n]
phase = np.cumsum(symboles * 1500 + 3000) * 2 * np.pi / banc_demod.TAUX_BANC
banc.z = np.exp(1j * phase).astype(np.complex64)
chemin = os.path.join(tempfile.mkdtemp(), "flux.wav")
assert banc.ecrire_flux(chemin)
import wave
with wave.open(chemin) as w:
    assert w.getframerate() == 48000
    pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float)
assert abs(pcm[48000:-48000].mean()) < 300, pcm.mean()
assert pcm.std() > 2000
print("ok")
