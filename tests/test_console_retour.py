#!/usr/bin/env python3
"""Son de retour apres une absence (2026-11-14, signale par l'utilisateur : "le son
de notification coupe la parole de la console, il faut les superposer") : joue par
un clip a part, jamais par voix.stop() + jingle. Lancer : python3 tests/test_console_retour.py
"""
import ast
import os
import types
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Retour(unittest.TestCase):
    def test_superpose(self):
        src_path = os.path.join(RACINE, "console", "main.py")
        with open(src_path, encoding="utf-8") as f:
            source = f.read()
        noeud = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "_retour")
        joues, coupes = [], []

        class Clip:
            def __init__(self, chemin):
                joues.append(chemin)

            def wait(self, timeout=None):
                return 0
        espace = {
            "os": types.SimpleNamespace(path=types.SimpleNamespace(ismount=lambda p: True)),
            "journal": types.SimpleNamespace(unheard_counts=lambda: {"radio": 2}),
            "voix": types.SimpleNamespace(Clip=Clip, stop=lambda: coupes.append(1)),
            "sys": __import__("sys"),
        }
        import sys
        sys.modules["sons"] = types.SimpleNamespace(chemin=lambda e: f"/sons/{e}.mp3")
        try:
            exec(compile(ast.get_source_segment(source, noeud), src_path, "exec"), espace)
            espace["_retour"]()
        finally:
            del sys.modules["sons"]
        self.assertEqual(joues, ["/sons/non_lus.mp3"])
        self.assertEqual(coupes, [], "la parole de la console n'est pas coupee")


if __name__ == "__main__":
    unittest.main()
