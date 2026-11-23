#!/usr/bin/env python3
"""Veille DMR agnostique (2026-11-13, signale par l'utilisateur : "sur veille dmr
tout sort en FM etroite, et quand je rejette c'est DMR qui perd en confiance").

Constat : un relais DMR au repos (synchros IDLE, pas de voix) sur un canal mixte ;
l'ancienne veille ouvrait ensuite la FM SANS arbitre, la FM "entendait" le souffle
numerique et la prise etait rangee sous le juge DMR (-fs).

Ici : _dmr_ecoute et _suivi_mixte passent par la prise unique (FM + tous les
decodeurs, l'arbitre donne la parole) et la prise porte le juge qui a vraiment
parle. Les fonctions sont extraites du source et jouees avec des doublures (pas de
dongle). Lancer : python3 tests/test_veille_dmr.py
"""
import ast
import os
import types
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(RACINE, "watchers", "radio_watch.py")
NOMS = ("_reception_a_presenter", "_suivi_mixte", "_dmr_ecoute")


def _charger(espace):
    with open(SRC, encoding="utf-8") as f:
        source = f.read()
    for noeud in ast.parse(source).body:
        if isinstance(noeud, ast.FunctionDef) and noeud.name in NOMS:
            exec(compile(ast.get_source_segment(source, noeud), SRC, "exec"), espace)
    espace["_suivi_mixte"].juges = []
    return espace


class Session:
    def __init__(self, *a, **k):
        self.jetee = self.gardee = False

    def discard(self):
        self.jetee = True

    def finalize(self):
        self.gardee = True
        return "/nas/veille_DMR.mp3"


class Veille(unittest.TestCase):
    def monter(self, tours, canal=None):
        """tours : rendus successifs de _ecoute_prise, (protocole, fm, juge, clair, muet)."""
        self.captures, self.rendus, self.sessions, self.prises = [], [], [], []
        tours = list(tours)
        tdvd = types.SimpleNamespace(last_muted=False, last_had_audio=False, juge_entendu=None, last_dmr={})

        def ecoute_prise(prise, freq, label, target, prelisten_max=None):
            self.prises.append((freq, target, prelisten_max))
            protocole, fm, juge, clair, muet = tours.pop(0) if tours else (None, False, None, False, False)
            tdvd.juge_entendu, tdvd.last_had_audio, tdvd.last_muted = juge, clair, muet
            return protocole, fm

        def session(*a, **k):
            s = Session()
            self.sessions.append(s)
            return s
        ns = types.SimpleNamespace
        espace = {
            "dire": lambda cle, **k: "veille DMR",
            "audio_lock": ns(acquire=lambda *a: ns(silent=False), release=lambda l: None),
            "radio_log": ns(RadioLogSession=session),
            "sound_player": ns(set_recording_session=lambda s: None),
            "prise_unique": ns(active=lambda: True),
            "_ouvrir_prise": lambda f: object(),
            "_ecoute_prise": ecoute_prise,
            "try_digital_voice_decode": tdvd,
            "_canal_mixte": lambda f: canal,
            "ceremonie": ns(fermer=lambda *a, **k: None),
            "noise_signatures": ns(record_capture=lambda *a: self.captures.append(a), capture_ended=lambda: None,
                                   capture_started=lambda *a: None),
            "_last_stream_fingerprint": "empreinte FM",
            "_deposer_rendus": lambda *a: self.rendus.append(a),
            "journal_notice": lambda: None,
            "stream_until_quiet": lambda *a, **k: self.fail("FM sans arbitre"),
            "MIXTE_TOURS": 12, "MIXTE_SONDE_SEC": 4, "MIXTE_VIDES": 2,
        }
        return _charger(espace)

    def test_relais_au_repos_rien_garde(self):
        """Synchros DMR valides sans voix (IDLE) : appris, rien au journal."""
        f = self.monter([("DMR", False, "-fs", False, False)])
        self.assertTrue(f["_dmr_ecoute"](467_240_800))
        self.assertEqual(self.captures, [])
        self.assertTrue(self.sessions[0].jetee)

    def test_relais_au_repos_canal_mixte_pas_de_fm(self):
        """Le cas signale : canal mixte, relais DMR au repos. L'arbitre empeche la
        FM de s'ouvrir (fm False) : plus de "veille DMR, FM etroite"."""
        canal = {"fm": 467_237_500, "dmr": 467_240_800}
        f = self.monter([("DMR", False, "-fs", False, False)], canal=canal)
        f["_dmr_ecoute"](467_240_800)
        self.assertEqual(self.captures, [])
        self.assertEqual(len(self.prises), 1, "pas de suite de conversation sans rien d'entendu")

    def test_vraie_fm_rangee_sous_la_fm(self):
        """Voix FM sur une frequence DMR connue : gardee, juge nfm, empreinte FM.
        Un rejet baissera la FM, pas le DMR."""
        f = self.monter([(None, True, "nfm", False, False)])
        self.assertTrue(f["_dmr_ecoute"](467_240_800))
        (_, freq, mode, label, target, path, empreinte, meta), = self.captures
        self.assertEqual((meta["juge"], meta["protocole"], meta["numerique"]), ("nfm", "FM", False))
        self.assertEqual(empreinte, "empreinte FM")
        self.assertEqual(self.rendus[0][3], "nfm")

    def test_voix_dmr_rangee_sous_dmr(self):
        f = self.monter([("DMR", False, "-fs", True, False)])
        f["_dmr_ecoute"](467_240_800)
        meta = self.captures[0][-1]
        self.assertEqual((meta["juge"], meta["protocole"], meta["numerique"]), ("-fs", "DMR", True))

    def test_tous_les_decodeurs_ecoutent(self):
        """Agnostique : la prise unique (tous les decodeurs + FM), pas -fs seul, et
        pendant toute la fenetre de la veille."""
        f = self.monter([("NXDN", False, "-fi", True, False)])
        f["_dmr_ecoute"](446_100_000, dwell_sec=20)
        self.assertEqual(self.prises[0], (446_100_000, "dmr", 20))
        self.assertEqual(self.captures[0][-1]["juge"], "-fi")

    def test_canal_mixte_conversation(self):
        """DMR puis reponse FM sur un canal mixte : une entree, les deux juges."""
        canal = {"fm": 467_237_500, "dmr": 467_240_800}
        f = self.monter([("DMR", False, "-fs", True, False),     # veille : voix DMR
                         (None, True, "nfm", False, False),       # suite : reponse FM
                         (None, False, None, False, False),
                         (None, False, None, False, False)], canal=canal)
        f["_dmr_ecoute"](467_240_800)
        meta = self.captures[0][-1]
        self.assertEqual(meta["juges"], ["-fs", "nfm"])
        self.assertEqual(meta["protocole"], "DMR et FM")
        self.assertEqual(len(self.prises), 4)   # 1 + suite jusqu'a deux tours vides

    def test_chiffre_rien_garde(self):
        f = self.monter([("DMR", False, "-fs", True, True)])
        self.assertTrue(f["_dmr_ecoute"](467_240_800))
        self.assertEqual(self.captures, [])


class ImportsConsole(unittest.TestCase):
    """"journal proposition" plantait : apres la commande voix, ~/autopilot/console
    (ancienne copie) passait en tete du chemin d'import et proposition_cmd venait de
    la. Plus aucun module n'ajoute un dossier ~/autopilot au chemin."""

    def test_pas_de_dossier_autopilot_dans_le_chemin(self):
        for rel in ("watchers/voix_preparation.py", "console/simulation.py"):
            with open(os.path.join(RACINE, rel), encoding="utf-8") as f:
                src = f.read()
            for interdit in ('chemins.base("console")', 'chemins.base("pi_console")', 'chemins.base("nas_manager")'):
                self.assertNotIn(interdit, src, rel)


if __name__ == "__main__":
    unittest.main()
