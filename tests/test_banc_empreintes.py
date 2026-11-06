"""Banc : silencieux sur l'IQ et filtres par modulation. Empreintes : un decodeur,
une frequence. Fleche gauche : les deux a la fois, et l'avis sur le juge a part."""
import json
import os
import sys
import tempfile
import wave

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("lib", "bridge", "config", "watchers"):
    sys.path.insert(0, os.path.join(RACINE, d))

import numpy as np  # noqa: E402

import banc_demod  # noqa: E402
import noise_signatures as ns  # noqa: E402
import preuve_voix  # noqa: E402
import propositions  # noqa: E402

P = 256_000
GRAINE = 7


def voix(t):
    """Une parole grossiere : un son harmonique module a 4 syllabes par seconde."""
    porteuse = sum(np.sin(2 * np.pi * f * t) / (i + 1) for i, f in enumerate((300, 600, 1200, 2100)))
    return porteuse * (np.sin(2 * np.pi * 4 * t) > 0.1)


def iq(emission, duree=20.0, debut=8.0, fin=14.0, snr_db=6.0, bruit=8.0):
    """IQ uint8 a P, cible a -P/4 : bruit partout, emission entre debut et fin."""
    rng = np.random.default_rng(GRAINE)
    n = int(duree * P)
    t = np.arange(n) / P
    x = (rng.normal(0, bruit, n) + 1j * rng.normal(0, bruit, n)).astype(np.complex64)
    # snr_db mesure dans 16 kHz de canal
    a = bruit * np.sqrt(2 * 16_000 / P) * 10 ** (snr_db / 20)
    s = emission(t) * a
    s[(t < debut) | (t >= fin)] = 0
    x = (x + s.astype(np.complex64)) * np.exp(-2j * np.pi * (P // 4) * t).astype(np.complex64)
    return np.clip(np.stack([x.real, x.imag], axis=1).reshape(-1) + 127.5, 0, 255).astype(np.uint8).tobytes()


def fm(t):
    return np.exp(1j * 2 * np.pi * 5_000 * np.cumsum(voix(t) / 3) / P)


def am(t):
    return 1 + 0.8 * voix(t) / 2


def banc(octets):
    b = banc_demod.Banc(P, -(P // 4))
    for i in range(0, len(octets), 131_072):
        b.ajouter(octets[i:i + 131_072])
    d = tempfile.mkdtemp()
    return b, b.ecrire(d)


def lire(chemin):
    with wave.open(chemin) as w:
        return w.readframes(w.getnframes()), w.getframerate()


def test_fm_ne_garde_que_l_emission_et_passe_le_rythme():
    b, sons = banc(iq(fm))
    assert 5.0 <= b.emission["secondes"] <= 7.5, b.emission
    pcm, taux = lire(sons["nfm"])
    assert len(pcm) / 2 / taux <= 7.5
    ok, mesure = preuve_voix.est_voix(pcm, taux)
    assert ok, mesure


def test_bruit_seul_aucune_emission_son_entier():
    b, sons = banc(iq(lambda t: np.zeros(len(t)), duree=8.0))
    assert b.emission["secondes"] == 0, b.emission
    pcm, taux = lire(sons["nfm"])
    assert len(pcm) / 2 / taux >= 7.5     # l'oreille peut encore dire le contraire


def test_raie_fixe_du_dongle_n_est_pas_une_emission():
    b, _ = banc(iq(lambda t: np.exp(2j * np.pi * 1_000 * t), duree=8.0, debut=0.0, fin=8.0, snr_db=10.0))
    assert b.emission["secondes"] == 0, b.emission


def test_porteuse_qui_va_et_vient_compte():
    b, _ = banc(iq(lambda t: np.ones(len(t), dtype=np.complex64), duree=12.0, debut=5.0, fin=8.0, snr_db=6.0))
    assert 2.5 <= b.emission["secondes"] <= 4.0, b.emission


def test_recentrage():
    decale = lambda t: am(t) * np.exp(2j * np.pi * 1_800 * t)   # noqa: E731
    b, _ = banc(iq(decale, snr_db=12.0))
    assert abs(b.emission["recentrage_hz"] - 1_800) <= 100, b.emission


def test_fsk_se_cale_au_milieu_pas_sur_un_symbole():
    """Deux tonalites a +-4,5 kHz, decalees de 1 kHz : le milieu, pas l'une d'elles."""
    def fsk(t):
        symboles = np.sign(np.sin(2 * np.pi * 37 * t) + np.sin(2 * np.pi * 91 * t))
        return np.exp(2j * np.pi * np.cumsum(symboles * 4_500 + 1_000) / (t.size / (t[-1] - t[0])))
    b, _ = banc(iq(fsk, snr_db=20.0))
    assert abs(b.emission["recentrage_hz"] - 1_000) <= 200, b.emission


def test_calage_blu():
    """Voix : le bord au bas du canal, chaque cote le sien. Etroit : au milieu du canal.
    Trop large pour etre de la BLU, ou pas de bande connue : rien."""
    assert banc_demod.calage_blu((-1_000, 1_000), 1) == -1_300
    assert banc_demod.calage_blu((-1_000, 1_000), -1) == 1_300
    assert banc_demod.calage_blu((-100, 100), 1) == -1_500
    assert banc_demod.calage_blu((-100, 100), -1) == 1_500
    assert banc_demod.calage_blu((-5_000, 5_000), 1) == 0
    assert banc_demod.calage_blu(None, 1) == 0


def test_recaler_freedv_au_milieu_du_canal():
    import juges_sync
    taux, n = 8_000, 8_000 * 4
    bruit = np.random.default_rng(2).normal(0, 1, n)
    spectre = np.fft.rfft(bruit)
    f = np.fft.rfftfreq(n, 1 / taux)
    spectre[(f < 500) | (f > 1_500)] *= 0.01     # une bande de 1 kHz centree sur 1 kHz
    x = np.fft.irfft(spectre, n)
    pcm = (x / np.abs(x).max() * 20_000).astype("<i2").tobytes()
    y = np.frombuffer(juges_sync._recaler_freedv(pcm, taux), dtype="<i2").astype(float)
    p = np.abs(np.fft.rfft(y)) ** 2
    centre = (p * f).sum() / p.sum()
    assert abs(centre - 1_500) < 60, centre


def test_am_filtre_etroit_gagne_sur_l_ancien():
    """Le meme signal faible, demodule en AM a travers +-12 kHz comme avant : le
    rapport voix/souffle doit etre meilleur a +-4,5 kHz."""
    octets = iq(am, snr_db=-2.0)
    _, sons = banc(octets)
    pcm, taux = lire(sons["am"])
    x = np.frombuffer(pcm, dtype=np.int16).astype(float)
    spec = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / taux)
    raies = sum(spec[np.abs(f - h) < 30].sum() for h in (300, 600, 1200, 2100))
    assert raies / (spec[(f > 300) & (f < 3000)].sum() + 1) > 0.05


def _empreinte(graine):
    rng = np.random.default_rng(graine)
    v = rng.normal(0, 1, ns.SIGNATURE_BANDS)
    v -= v.mean()
    return [float(a) for a in v / np.linalg.norm(v)]


class _Fichier:
    def __enter__(self):
        self.d = tempfile.mkdtemp()
        self.vieux = ns._signatures_path
        ns._signatures_path = lambda: os.path.join(self.d, "parasites.json")
        ns._cache.update(path=None, mtime=None, entries=[])
        return self

    def __exit__(self, *a):
        ns._signatures_path = self.vieux
        ns._cache.update(path=None, mtime=None, entries=[])


def test_empreinte_nominative_et_precise():
    with _Fichier():
        pcm = (np.random.default_rng(3).normal(0, 3000, 32_000)).astype("<i2").tobytes()
        fp = ns.fingerprint(pcm, 16_000, "s16le")
        ns.add_signature({"freq_hz": 145_287_500, "label": "x", "fingerprint": fp, "juge": "nfm"})
        assert ns.matches_known_noise(145_287_500, pcm, 16_000, "s16le", "nfm")[0]
        assert ns.matches_known_noise(145_289_000, pcm, 16_000, "s16le", "nfm")[0]    # le pic bouge
        assert not ns.matches_known_noise(145_287_500, pcm, 16_000, "s16le", "usb")[0]   # autre decodeur
        assert not ns.matches_known_noise(145_300_000, pcm, 16_000, "s16le", "nfm")[0]   # canal voisin
        assert ns.has_signatures_near(145_287_500, "nfm")
        assert not ns.has_signatures_near(145_287_500, "usb")
        # le meme son appris par un autre decodeur n'est pas un doublon
        assert ns.add_signature({"freq_hz": 145_287_500, "label": "x", "fingerprint": fp, "juge": "usb"}) == 2
        assert ns.oublier_ressemblantes(145_287_500, fp, "usb") == 1
        assert ns.matches_known_noise(145_287_500, pcm, 16_000, "s16le", "nfm")[0]


def test_fleche_gauche_apprend_au_decodeur_meme_s_il_reste_des_rendus():
    with _Fichier():
        d = tempfile.mkdtemp()
        scores = propositions.SCORES
        propositions.SCORES = os.path.join(d, "scores.json")
        try:
            journal = os.path.join(d, "prise.mp3")
            open(journal, "wb").close()
            a, b = _empreinte(1), _empreinte(2)
            doc = {"freq_hz": 145_287_500, "label": "x", "courante": 0,
                   "items": [{"juge": "usb", "fichier": None, "empreinte": a},
                             {"juge": "nfm", "fichier": None, "empreinte": b}]}
            propositions._sauver(journal, doc)
            suite = propositions.rejeter(journal)
            assert suite
            appris = json.load(open(ns._signatures_path()))
            assert [(e["juge"], e["freq_hz"]) for e in appris] == [("usb", 145_287_500)]
            # l'avis sur le juge est compte a part
            assert json.load(open(propositions.SCORES))
        finally:
            propositions.SCORES = scores


if __name__ == "__main__":
    fautes = 0
    for nom, f in sorted(globals().items()):
        if nom.startswith("test_"):
            try:
                f()
                print("ok ", nom)
            except AssertionError as exc:
                fautes += 1
                print("NON", nom, exc)
    sys.exit(1 if fautes else 0)
