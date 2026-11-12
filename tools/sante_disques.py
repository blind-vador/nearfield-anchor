#!/usr/bin/env python3
"""Bilan quotidien de la sante des disques (2026-11-12), lance par
sante-disques.timer chaque matin ; le dimanche, lance aussi un auto-test SMART
(court, ou long le premier dimanche du mois), lu au bilan suivant.
La logique est dans lib/sante_disques.py.

  python3 tools/sante_disques.py [--test court|long|aucun]
"""
import sys

import sante_disques


def main():
    test = sante_disques.test_du_jour()
    if "--test" in sys.argv:
        choix = sys.argv[sys.argv.index("--test") + 1]
        test = None if choix == "aucun" else choix
    rapport = sante_disques.bilan(lancer_test=test)
    for d in rapport["disques"]:
        print(f"[sante_disques] {d['id']} ({d['niveau']}) {d['resume']} {' '.join(d['constats'])}", flush=True)


if __name__ == "__main__":
    main()
