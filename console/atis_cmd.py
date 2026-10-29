"""Commande "atis" : derniers bulletins meteo d'aeroport captes (lib/atis.py).
"atis lyon" / "atis saint-etienne" : un seul aeroport.
"""
import sys


import atis  # noqa: E402
import matcher  # noqa: E402
from phrases import dire  # noqa: E402

NAMES = {"lyon": "LFLL", "exupery": "LFLL", "saint": "LFMH", "etienne": "LFMH", "boutheon": "LFMH"}


def run(line):
    words = set(matcher.normalize(line).split())
    oaci = next((NAMES[w] for w in words if w in NAMES), None)
    bulletins = atis.latest(oaci)
    if not bulletins:
        return dire("atis.rien")
    parts = []
    for b in bulletins:
        detail = []
        if b.get("lettre"):
            detail.append(dire("atis.lettre", lettre=b["lettre"]))
        parts.append(dire("atis.bulletin", aeroport=b["aeroport"], lettre=" ".join(detail), resume=b["resume"].rstrip(".")))
        if b.get("avertissements"):
            parts.append(dire("atis.avertissement", texte=b["avertissements"].rstrip(".")))
    return " ".join(parts)
