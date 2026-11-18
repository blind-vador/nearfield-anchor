"""Gemini avec recherche web (2026-11-17) : l outil
google_search part dans la requete, les autres services sont sautes, le texte revient tel quel."""
import os
import sys
import tempfile
import unittest
from unittest import mock

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "config", "bridge")]

import connecteurs_ia as ia
import gemini
import reglages

ia.ETAT = os.path.join(tempfile.mkdtemp(), "etat.json")
reglages.cle = lambda nom: "cle-factice" if nom == "gemini" else ""


class Rep:
    status_code, ok = 200, True

    def __init__(self, texte):
        self.texte = texte

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": self.texte}]}}]}


class Recherche(unittest.TestCase):
    def test_outil_et_texte_brut(self):
        envoyes = []

        def post(url, json=None, **kw):
            envoyes.append(json)
            return Rep("```json\n{\"modeles\": []}\n```")
        with mock.patch.object(gemini.requests, "post", post), \
                mock.patch.object(ia, "demander", side_effect=AssertionError("pas d autre service")):
            texte, raison = gemini.ask("veille", json_mode=True, recherche=True)
        self.assertIsNone(raison)
        self.assertEqual(texte, "```json\n{\"modeles\": []}\n```")
        self.assertEqual(envoyes[0]["tools"], [{"google_search": {}}])
        self.assertNotIn("responseMimeType", envoyes[0]["generationConfig"])

    def test_sans_recherche_inchange(self):
        envoyes = []

        def post(url, json=None, **kw):
            envoyes.append(json)
            return Rep("{}")
        with mock.patch.object(gemini.requests, "post", post), mock.patch.object(ia, "demander", return_value=None):
            gemini.ask("q", json_mode=True)
        self.assertNotIn("tools", envoyes[0])
        self.assertEqual(envoyes[0]["generationConfig"]["responseMimeType"], "application/json")


if __name__ == "__main__":
    unittest.main()
