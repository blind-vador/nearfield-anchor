#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test des decodeurs MDC1200, FleetSync II et MPT1327 (lib/ffsk_pro.py, 2026-11-01).

MDC1200 : paquets produits par l'encodeur de reference (mdc-encode-decode de
Matthew Kaufman, 22 050 Hz, S16LE) gardes dans tests/donnees/ : simple
(identification du poste 1234) et double (appel selectif, donnees 11 22 33 44).
FleetSync et MPT1327 : mots de code fabriques ici selon le format de SDRTrunk
(pas d'encodeur de reference) ; la table du CRC-15 est verifiee contre la
sienne. Sans dongle ni reseau. Lancer : python3 tests/test_ffsk_pro.py
"""
import os
import sys

import numpy as np

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "lib"))
import ffsk_pro as f  # noqa: E402

TAUX = 22050


def _bits(valeur, n):
    return [(valeur >> (n - 1 - i)) & 1 for i in range(n)]


def _mot(m48):
    """48 bits utiles -> mot de 64 bits (CRC-15 + parite paire)."""
    mot = list(m48) + _bits(f._crc15(m48), 15)
    return mot + [sum(mot) % 2]


def _ffsk(bits, inverse=False):
    """Bits -> audio FFSK 1200 bauds, 1200 Hz = 1 (inverse : 1800 Hz = 1)."""
    ech = TAUX / f.BAUDS
    phase, x = 0.0, []
    for i, b in enumerate(bits):
        ton = 1200 if (b ^ inverse) else 1800
        for _ in range(int(round((i + 1) * ech)) - int(round(i * ech))):
            phase += 2 * np.pi * ton / TAUX
            x.append(np.sin(phase))
    pad = np.zeros(TAUX // 2)
    y = np.concatenate([pad, np.array(x), pad]) * 9000 + np.random.default_rng(3).normal(0, 1200, len(x) + TAUX)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


def _champ(m, debut, fin, valeur):
    m[debut:fin] = _bits(valeur, fin - debut)


def fleetsync_ani():
    m = [0] * 85
    _champ(m, 29, 30, 1)                 # identification (ANI)
    _champ(m, 37, 45, 1)                 # flotte 100
    _champ(m, 45, 57, 1)                 # poste 1000
    _champ(m, 57, 69, 2)                 # vers 1001
    return [1, 0, 1, 0] * 4 + f.FS_SYNC + _mot(m[21:69])


def mpt_gtc(francais=False, abime=False):
    m = [0] * 68
    m[20] = 1                            # mot d'adresse
    _champ(m, 21, 28, 5)                 # prefixe 5
    _champ(m, 28, 41, 1234)              # appele
    _champ(m, 41, 42, 0)                 # GTC
    _champ(m, 43, 53, 123)               # canal 123
    _champ(m, 53, 66, 4321)              # appelant
    mot = _mot(m[20:68])
    if abime:
        mot[10] ^= 1                     # une erreur : le CRC-15 doit la corriger
    synchro = f.MPT_SYNCS[2][1] if francais else f.MPT_SYNCS[0][1]
    return [1, 0] * 8 + synchro + mot


def main():
    echecs = 0
    table = f._TABLE15[0] == 0x740A and f._TABLE15[1] == 0x3A05 and f._TABLE15[47] == 0x6815
    print(f"  [{'ok' if table else 'ECHEC'}] table CRC-15 identique a SDRTrunk")
    echecs += not table

    def lire(nom):
        return open(os.path.join(RACINE, "tests", "donnees", nom), "rb").read()
    silence = np.zeros(TAUX, dtype=np.int16).tobytes()
    cas = [
        ("MDC1200 simple (encodeur de reference)", silence + lire("mdc1200_simple_1234.raw") + silence,
         ("MDC1200", "poste 1234, identification")),
        ("MDC1200 double (encodeur de reference)", silence + lire("mdc1200_double_0abc.raw") + silence,
         ("MDC1200", "données 11 22 33 44")),
        ("FleetSync II identification", _ffsk(fleetsync_ani()), ("FLEETSYNC", "flotte 100, poste 1000 vers 1001")),
        ("MPT1327 attribution de canal", _ffsk(mpt_gtc()), ("MPT1327", "canal 123, 5-4321 vers 5-1234")),
        ("MPT1327 un bit faux, corrige", _ffsk(mpt_gtc(abime=True)), ("MPT1327", "canal 123")),
        ("MPT1327 variante francaise", _ffsk(mpt_gtc(francais=True), inverse=True), ("MPT1327", "variante française")),
        ("bruit seul (30 s)", np.random.default_rng(4).normal(0, 4000, TAUX * 30).astype(np.int16).tobytes(), None),
    ]
    for nom, pcm, attendu in cas:
        trouves = f.decoder(pcm, TAUX)
        ok = (not trouves) if attendu is None else any(p == attendu[0] and attendu[1] in t for p, t in trouves)
        print(f"  [{'ok' if ok else 'ECHEC'}] {nom} : {trouves[:2]}")
        echecs += not ok
    print("\nTOUS LES TESTS PASSENT" if not echecs else f"\n{echecs} ECHEC(S)")
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
