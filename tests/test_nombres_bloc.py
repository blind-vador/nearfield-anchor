#!/usr/bin/env python3
"""Nombres dits d'un bloc (2026-11-14, signale par l'utilisateur : espeak annoncait
des avions a plus de 9 millions de metres). Les mots colles du cache (« 10000 »,
« 100 », « vingt », « trois ») reviennent en chiffres pour une voix qui lit la
phrase entiere ; les lettres restent en alphabet radio.
Lancer : python3 tests/test_nombres_bloc.py
"""
import os
import sys
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "config")]
import atomes_parole  # noqa: E402
import annonce_morceaux as m  # noqa: E402


class Bloc(unittest.TestCase):
    def test_en_chiffres(self):
        for n in (10123, 2500, 9875, 271, 1200000, 85, 91, 71, 300000, 100200, 2000000001):
            self.assertEqual(atomes_parole.en_chiffres(atomes_parole.mots_entier(n)), str(n), n)
        self.assertIsNone(atomes_parole.en_chiffres(["zéro", "zéro", "sept"]))
        self.assertIsNone(atomes_parole.en_chiffres(["17 heures", "quarante"]))

    def test_phrase(self):
        self.assertEqual(m.spoken(m.whole("Avion à 10123 mètres.")), "Avion a 10123 mètres.")
        self.assertEqual(m.spoken(m.whole("AFR410 à 2500 pieds")), "alfa foxtrotte roméo 410 a 2500 pieds")
        self.assertEqual(m.spoken(m.whole("1 novembre")), "premier novembre")
        self.assertEqual(m.spoken(m.whole("0,5 mètre")), "zéro virgule cinq mètre")
        self.assertNotIn("10000 100", m.spoken(m.whole("10123")))

    def test_cache_inchange(self):
        """Le cache des morceaux garde ses mots (seul le texte d'un bloc change)."""
        self.assertEqual([t for t, _ in m.whole("10123")], ["10000", "100", "vingt", "trois"])


if __name__ == "__main__":
    unittest.main()
