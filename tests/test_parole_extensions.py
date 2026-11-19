"""Parole demandee par un logiciel installe : catalogue, voix imposee, controles."""
import os
import sys
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
for d in ("lib", "watchers", "tools", "config", "bridge"):
    sys.path.insert(0, os.path.join(ICI, "..", d))

import voix_sources  # noqa: E402

MS = ["fr-FR-DeniseNeural", "fr-FR-HenriNeural"]
LOCALES = ["piper:fr_FR-siwis-medium", "piper:mbrola#fr1"]
DISTANTES = ["piper:mac#Thomas", "piper:google#fr"]


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(voix_sources, "_microsoft_voix", return_value=MS),
                  mock.patch("piper_out.voix_locales", return_value=LOCALES),
                  mock.patch("piper_out.voix_distantes", return_value=DISTANTES),
                  mock.patch.object(voix_sources, "preferences",
                                    return_value={"piper": "desactive", "mbrola": "desactive"}),
                  mock.patch.object(voix_sources, "niveaux_mac", side_effect=lambda n: {x: "normale" for x in n}),
                  mock.patch.object(voix_sources, "_tout", return_value={}),
                  mock.patch("gemini_tts.catalogue", return_value=[])]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_seulement_les_voix_permises_et_presentes(self):
        with mock.patch.object(voix_sources, "microsoft_ok", return_value=True):
            ids = [v["id"] for v in voix_sources.catalogue()]
        self.assertEqual(ids, MS + DISTANTES + ["espeak"])
        with mock.patch.object(voix_sources, "microsoft_ok", return_value=False):
            ids = [v["id"] for v in voix_sources.catalogue()]
        self.assertEqual(ids, DISTANTES + ["espeak"])

    def test_voix_imposee(self):
        with mock.patch.object(voix_sources, "microsoft_ok", return_value=True):
            c = voix_sources.choix_impose("fr-FR-HenriNeural", "logiciels", "+20%", "-5Hz")
            self.assertEqual((c["moteur"], c["voix"], c["debit"], c["hauteur"], c["aleatoire"]),
                             ("edge", "fr-FR-HenriNeural", "+20%", "-5Hz", False))
            c = voix_sources.choix_impose("piper:mac#Thomas", "logiciels")
            self.assertEqual((c["voix"], c["aleatoire"]), ("piper:mac#Thomas", False))
            with mock.patch.object(voix_sources, "choisir", return_value={"moteur": "edge", "voix": "x"}) as ch:
                c = voix_sources.choix_impose("piper:fr_FR-siwis-medium", "logiciels")  # desactivee
                ch.assert_called_once_with("logiciels")


class VoixGemini(unittest.TestCase):
    """Voix neurales de Gemini : catalogue, choix, synthèse et repli (2026-11-19)."""

    def setUp(self):
        import gemini_tts
        self.g = gemini_tts
        self.repos = {}
        self.p = [mock.patch.object(gemini_tts, "_cle", return_value="cle-essai"),
                  mock.patch("connecteurs_ia._en_repos", side_effect=lambda n: n in self.repos),
                  mock.patch("connecteurs_ia._reposer", side_effect=lambda n, s, w: self.repos.__setitem__(n, w))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def reponse(self, statut, son=None, mime="audio/wav"):
        import base64
        r = mock.Mock(status_code=statut, headers={})
        r.json.return_value = {"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": mime, "data": base64.b64encode(son or b"").decode()}}]}}]}
        return r

    def test_catalogue_et_choix(self):
        c = self.g.catalogue()
        self.assertEqual(len(c), 30)
        self.assertIn({"id": "gemini:Charon", "nom": "Charon (Gemini, informative)", "fournisseur": "gemini"}, c)
        self.assertEqual(voix_sources.fournisseur("gemini:Kore"), "gemini")
        with mock.patch.object(voix_sources, "catalogue", return_value=c), \
                mock.patch.object(voix_sources, "_tout", return_value={}):
            ch = voix_sources.choix_impose("gemini:Kore", "logiciels", "+25%")
        self.assertEqual((ch["moteur"], ch["voix"], ch["debit"]), ("gemini", "gemini:Kore", "+25%"))
        for m in self.g.MODELES:
            self.repos[self.g._repos(m)] = "quota"
        self.assertEqual(self.g.catalogue(), [])                    # tous au repos : plus proposées
        with mock.patch.object(self.g, "_cle", return_value=""):
            self.repos.clear()
            self.assertEqual(self.g.catalogue(), [])                # pas de clé

    def test_synthese_wav_et_pcm(self):
        poster = mock.Mock(return_value=self.reponse(200, b"RIFF....WAVE"))
        chemin = self.g.fichier("Bonjour  toi", "gemini:Charon", "+25%", poster=poster)
        try:
            self.assertEqual(open(chemin, "rb").read(), b"RIFF....WAVE")
            corps = poster.call_args.kwargs["json"]
            self.assertEqual(corps["contents"][0]["parts"][0]["text"], "Bonjour toi")
            self.assertEqual(corps["contents"][0]["parts"][0]["speech_metadata"]["style"], self.g.style("+25%"))
            self.assertEqual(corps["generationConfig"]["speechConfig"]["voiceConfig"], {"voice": "Charon"})
            self.assertEqual(poster.call_args.kwargs["headers"], {"x-goog-api-key": "cle-essai"})
        finally:
            os.remove(chemin)
        pcm = b"\x01\x00" * 10
        chemin = self.g.fichier("a", "gemini:Kore", poster=mock.Mock(return_value=self.reponse(
            200, pcm, "audio/L16;codec=pcm;rate=24000")))
        try:
            d = open(chemin, "rb").read()
            self.assertEqual((d[:4], d[8:12], d[-20:]), (b"RIFF", b"WAVE", pcm))
        finally:
            os.remove(chemin)

    def test_repli_ancienne_forme_quota_et_cle(self):
        poster = mock.Mock(side_effect=[self.reponse(400), self.reponse(200, b"RIFFx")])
        chemin = self.g.fichier("a", "gemini:Kore", poster=poster)
        os.remove(chemin)
        self.assertEqual(poster.call_args.kwargs["json"]["generationConfig"]["speechConfig"]["voiceConfig"],
                         {"prebuiltVoiceConfig": {"voiceName": "Kore"}})
        poster = mock.Mock(side_effect=[self.reponse(429), self.reponse(200, b"RIFFy")])
        chemin = self.g.fichier("a", "gemini:Kore", poster=poster)
        os.remove(chemin)
        self.assertEqual(self.repos, {self.g._repos(self.g.MODELES[0]): "quota"})   # modèle suivant
        self.assertIsNone(self.g.fichier("a", "gemini:Kore", poster=mock.Mock(return_value=self.reponse(403))))
        self.assertIsNone(self.g.fichier("a", "gemini:Inconnue", poster=mock.Mock()))
        self.assertIsNone(self.g.fichier("", "gemini:Kore", poster=mock.Mock()))
        self.assertIsNone(self.g.fichier("a", "gemini:Kore", poster=mock.Mock(side_effect=OSError("muet"))))

    def test_service_lent_borne(self):
        # Un délai dépassé : aucun autre modèle n'est tenté pour cette phrase (la voix de la source la dit).
        poster = mock.Mock(side_effect=TimeoutError("lent"))
        self.assertIsNone(self.g.fichier("a", "gemini:Kore", poster=poster))
        self.assertEqual(poster.call_count, 1)
        self.assertLessEqual(poster.call_args.kwargs["timeout"], self.g.DELAI_S)
        # Tout le budget est déjà pris : on ne commence même pas un nouvel essai.
        horloge = iter([0.0, self.g.BUDGET_S])
        poster = mock.Mock(side_effect=[self.reponse(400), self.reponse(200, b"RIFFz")])
        with mock.patch("gemini_tts.time.monotonic", side_effect=lambda: next(horloge, 99.0)):
            self.assertIsNone(self.g.fichier("a", "gemini:Kore", poster=poster))
        self.assertEqual(poster.call_count, 0)

    def test_parler_gemini_puis_repli(self):
        import speak
        verrou = mock.Mock(dropped=False)
        with mock.patch("audio_lock.acquire", return_value=verrou), mock.patch("audio_lock.release"), \
                mock.patch.object(voix_sources, "choix_impose", return_value={"moteur": "gemini", "voix": "gemini:Kore"}), \
                mock.patch.object(speak, "play_sound_effect") as jouer, mock.patch.object(speak, "_annoncer") as ann, \
                mock.patch.object(speak, "_speech_timeout", return_value=5):
            fd, son = __import__("tempfile").mkstemp()
            os.close(fd)
            with mock.patch("gemini_tts.fichier", return_value=son):
                self.assertEqual(speak.parler("Bonjour", voix="gemini:Kore"), "gemini:Kore")
            jouer.assert_called_once()
            ann.assert_not_called()
            self.assertFalse(os.path.exists(son))                    # fichier effacé après
            with mock.patch("gemini_tts.fichier", return_value=None), \
                    mock.patch.object(voix_sources, "choisir", return_value={"moteur": "edge", "voix": "fr-FR-HenriNeural"}):
                self.assertEqual(speak.parler("Bonjour", voix="gemini:Kore"), "fr-FR-HenriNeural")
            ann.assert_called_once()


class Requete(unittest.TestCase):
    def test_controles(self):
        import extensions
        cx = mock.Mock(id="x")
        with mock.patch("speak.parler", return_value="fr-FR-HenriNeural") as p:
            r = extensions.parler(cx, {"texte": "Bonjour\n  toi", "voix": "fr-FR-HenriNeural", "debit": "+25%"})
            self.assertEqual(r, {"dit": True, "voix": "fr-FR-HenriNeural"})
            p.assert_called_once_with("Bonjour toi", voix="fr-FR-HenriNeural", debit="+25%", hauteur=None)
            for mauvais in ({"texte": ""}, {"texte": "a" * 3001}, {"texte": "a", "debit": "vite"},
                            {"texte": "a", "debit": "+300%"}, {"texte": "a", "hauteur": "+90Hz"},
                            {"texte": "a", "voix": 3}):
                with self.assertRaises(extensions.Refus):
                    extensions.parler(cx, mauvais)
        with mock.patch("speak.parler", return_value=None):
            self.assertEqual(extensions.parler(cx, {"texte": "a"})["dit"], False)
        self.assertIn("parler", extensions.LONGUES)


if __name__ == "__main__":
    unittest.main()
