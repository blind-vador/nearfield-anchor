#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test du decodeur ASN / DSC VHF (lib/asn_dsc.py, 2026-11-01).

Fabrique de vrais signaux ASN (FSK 1300/2100 Hz, 1200 bauds, phase continue,
point + mise en phase + message en diversite DX/RX), avec du bruit, et verifie
qu'ils sont decodes -- y compris quand des caracteres DX sont abimes (la copie
RX doit prendre le relais). Ne necessite ni dongle ni reseau.
Lancer : python3 tests/test_asn_dsc.py
"""
import os
import sys

import numpy as np

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "lib"))
import asn_dsc  # noqa: E402

TAUX = 22050


def _mmsi_chars(mmsi):
    d = (mmsi + "0")[:10]
    return [int(d[i:i + 2]) for i in range(0, 10, 2)]


def fabriquer(message, abimer_dx=()):
    """Symboles DX/RX d'un appel complet, puis audio FSK S16 a TAUX."""
    paires = []
    for k in range(6):
        paires.append((125, 111 - k))
    total = len(message)
    for p in range(total + 2):
        dx = message[p] if p < total else 126
        rx = (105, 104)[p] if p < 2 else message[p - 2]
        paires.append((dx, rx))
    bits = [i % 2 for i in range(20)]   # point
    for i, (dx, rx) in enumerate(paires):
        bdx = asn_dsc.bits_symbole(dx)
        if i - 6 in abimer_dx:
            bdx = [1 - b for b in bdx[:3]] + bdx[3:]
        bits += bdx + asn_dsc.bits_symbole(rx)
    ech_bit = TAUX / asn_dsc.BAUDS
    phase, sortie = 0.0, []
    for i, b in enumerate(bits):
        n = int(round((i + 1) * ech_bit)) - int(round(i * ech_bit))
        f = asn_dsc.TON_Y if b else asn_dsc.TON_B
        for _ in range(n):
            phase += 2 * np.pi * f / TAUX
            sortie.append(np.sin(phase))
    silence = np.zeros(TAUX // 2)
    x = np.concatenate([silence, np.array(sortie), silence]) * 8000
    x += np.random.default_rng(1).normal(0, 1500, len(x))
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def decoder(pcm):
    return [asn_dsc.interpreter(c) for c in asn_dsc.chercher(asn_dsc.demoduler(pcm, TAUX))]


def main():
    echecs = 0
    # Detresse : MMSI 227006760, incendie, 45 26 N 004 23 E
    detresse = [112, 112] + _mmsi_chars("227006760") + [100] + [4, 52, 60, 4, 23] + [99, 99, 100, 127, 7, 127, 127]
    # Individuel : 227123450 appelle 227006760, routine, telephonie
    individuel = [120, 120] + _mmsi_chars("227006760") + [100] + _mmsi_chars("227123450") + [100, 126, 126, 126, 117, 3, 117, 117]
    cas = [
        ("detresse", fabriquer(detresse), ["DÉTRESSE", "227006760", "incendie", "45 degrés 26 nord"]),
        ("individuel", fabriquer(individuel), ["individuel", "routine", "de 227123450", "station 227006760", "téléphonie"]),
        ("DX abimes, RX en secours", fabriquer(individuel, abimer_dx={3, 7, 9}), ["de 227123450", "station 227006760"]),
        ("bruit seul", np.random.default_rng(2).normal(0, 3000, TAUX * 3).astype(np.int16).tobytes(), None),
    ]
    for nom, pcm, attendus in cas:
        textes = [t for t in decoder(pcm) if t]
        if attendus is None:
            ok = not textes
        else:
            ok = any(all(a in t for a in attendus) for t in textes)
        print(f"  [{'ok' if ok else 'ECHEC'}] {nom} : {textes[:1]}")
        echecs += not ok
    print("\nTOUS LES TESTS PASSENT" if not echecs else f"\n{echecs} ECHEC(S)")
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
