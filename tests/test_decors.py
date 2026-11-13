"""Decors de la gare (2026-11-13) : conditions empilees, une fois ou de temps en
temps, et le dialogue complet au clavier (liste, conditions, action, telechargement,
suppression confirmee)."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import phrases  # noqa: E402
phrases.BASE_DIR = os.path.join(ICI, "..", "phrases")
import decors  # noqa: E402

UP, DOWN, LEFT, RIGHT = "\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C"
PGDN, END, ESC, BS = "\x1b[6~", "\x1b[F", "\x1b", "\x7f"


def t(texte):
    return time.strptime(texte, "%Y-%m-%d %H:%M")


def decor(**k):
    return dict(decors.nouveau(), **dict({"id": "x"}, **k))


class Regles(unittest.TestCase):
    def test_conditions_empilees(self):
        noel = decor(jour=25, mois=12, heure="ouverture")
        self.assertTrue(decors.conditions(noel, t("2026-12-25 09:00"), set(), True))
        self.assertFalse(decors.conditions(noel, t("2026-12-25 03:00"), set(), False))   # gare fermee
        self.assertFalse(decors.conditions(noel, t("2026-12-24 09:00"), set(), True))
        decembre = decor(mois=12, heure="toujours", meteo="neige")
        self.assertTrue(decors.conditions(decembre, t("2027-12-02 03:00"), {"neige"}, False))
        self.assertFalse(decors.conditions(decembre, t("2027-12-02 03:00"), {"pluie"}, False))
        sec = decor(heure="toujours", meteo="sec")
        self.assertTrue(decors.conditions(sec, t("2026-11-13 12:00"), set(), True))
        self.assertFalse(decors.conditions(sec, t("2026-11-13 12:00"), {"vent"}, True))
        minuit = decor(heure=0, minute=0)
        self.assertTrue(decors.conditions(minuit, t("2026-11-14 00:00"), set(), True))
        self.assertFalse(decors.conditions(minuit, t("2026-11-14 00:01"), set(), True))

    def test_une_fois_par_occasion(self):
        faits = {}
        ouverture = decor(heure="ouverture")
        self.assertEqual(len(decors.a_jouer([ouverture], t("2026-11-13 05:00"), set(), True, faits)), 1)
        self.assertEqual(decors.a_jouer([ouverture], t("2026-11-13 18:00"), set(), True, faits), [])
        self.assertEqual(len(decors.a_jouer([ouverture], t("2026-11-14 05:00"), set(), True, faits)), 1)
        chaque_heure = decor(id="h", heure="toujours", minute=30)
        self.assertEqual(len(decors.a_jouer([chaque_heure], t("2026-11-13 10:30"), set(), True, faits)), 1)
        self.assertEqual(decors.a_jouer([chaque_heure], t("2026-11-13 10:30"), set(), True, faits), [])
        self.assertEqual(len(decors.a_jouer([chaque_heure], t("2026-11-13 11:30"), set(), True, faits)), 1)

    def test_ponctuel_au_hasard(self):
        p = decor(ponctuel=True, heure="toujours")
        faits = {}
        self.assertEqual(decors.a_jouer([p], t("2026-11-13 10:00"), set(), True, faits, hasard=lambda: 0.99), [])
        self.assertEqual(len(decors.a_jouer([p], t("2026-11-13 10:05"), set(), True, faits, hasard=lambda: 0.0)), 1)
        self.assertEqual(decors.a_jouer([p], t("2026-11-13 10:40"), set(), True, faits, hasard=lambda: 0.0), [])
        self.assertEqual(len(decors.a_jouer([p], t("2026-11-13 11:01"), set(), True, faits, hasard=lambda: 0.0)), 1)
        # environ PONCTUEL_PAR_HEURE par heure : 120 tirages de 30 s
        par_tour = 1 - (1 - decors.PONCTUEL_PAR_HEURE) ** (30 / 3600)
        self.assertAlmostEqual(1 - (1 - par_tour) ** 120, decors.PONCTUEL_PAR_HEURE, places=6)

    def test_conditions_impossibles(self):
        maintenant = t("2026-11-13 10:00")
        self.assertEqual(decors.erreur(decor(jour=31, mois=2, texte="a"), maintenant), "decors.erreur_jour_mois")
        self.assertIsNone(decors.erreur(decor(jour=29, mois=2, texte="a"), maintenant))
        self.assertEqual(decors.erreur(decor(annee=2025, texte="a"), maintenant), "decors.erreur_annee_passee")
        self.assertEqual(decors.erreur(decor(annee=2026, mois=3, texte="a"), maintenant), "decors.erreur_date_passee")
        self.assertIsNone(decors.erreur(decor(annee=2026, mois=12, jour=25, texte="a"), maintenant))
        self.assertEqual(decors.erreur(decor(texte="  "), maintenant), "decors.erreur_texte")
        self.assertEqual(decors.erreur(decor(action="son", sons=["../../etc/passwd"]), maintenant), "decors.erreur_son")
        self.assertEqual(decors.erreur(decor(action="son", sons=[]), maintenant), "decors.erreur_son")

    def test_ancien_son_unique_relu(self):
        with mock.patch.object(decors.reglages, "get", lambda nom: {"decors": [{"id": "a", "son": "horn1.mp3"}]}):
            d, = decors.lire()
        self.assertEqual(d["sons"], ["horn1.mp3"])
        self.assertNotIn("son", d)


class Dialogue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        gare = os.path.join(self.tmp, "train_scaner")
        os.makedirs(os.path.join(gare, "ter_passby"))
        open(os.path.join(gare, "ter_passby", "1.wav"), "wb").close()
        open(os.path.join(gare, "horn1.mp3"), "wb").close()
        self.gare = gare
        self.store = {}
        import decors_cmd
        import voix
        self.cmd = decors_cmd
        self.joues = []
        self.p = [mock.patch.object(decors, "TRAIN_SCANER_DIR", gare),
                  mock.patch.object(decors, "DEPOT", os.path.join(gare, "decors", "depot")),
                  mock.patch.object(decors.reglages, "get",
                                    lambda nom: {"decors": list(self.store.get("decors", []))}),
                  mock.patch.object(decors.reglages, "save",
                                    lambda nom, data: self.store.update({nom: data["decors"]}) or True),
                  mock.patch.object(decors_cmd, "_jouer", lambda c: self.joues.append(c)),
                  mock.patch.object(decors_cmd.conseils, "intro", lambda f, titre, c: titre),
                  mock.patch.object(voix, "stop", lambda: None)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lancer(self, touches, lignes=()):
        parle = []
        lignes = iter(lignes)
        fin = self.cmd.run("", iter(touches), parle.append, lambda s: None, lambda ks: next(lignes))
        return fin, parle

    def test_noel_puis_suppression(self):
        touches = [
            "\r",                                   # Nouveau decor
            DOWN,                                   # nom -> ponctuel
            DOWN, PGDN, PGDN, RIGHT, RIGHT, RIGHT, RIGHT, RIGHT,   # jour 25
            DOWN, END,                              # decembre
            DOWN, LEFT,                             # annee : fleche, rien
            DOWN, DOWN, DOWN, RIGHT,                # meteo pluie
            "\r",                                   # suivant
            DOWN, "\r",                             # texte
            DOWN, RIGHT,                            # jingle retro
            DOWN, "\r",                             # enregistrer
        ]
        fin, parle = self.lancer(touches + [ESC], ["Joyeux Noël à tous"])
        self.assertEqual(fin, self.cmd.dire("decors.ferme"))
        d, = self.store["decors"]
        self.assertEqual((d["jour"], d["mois"], d["meteo"], d["heure"], d["texte"], d["jingle"]),
                         (25, 12, "pluie", "ouverture", "Joyeux Noël à tous", "retro"))
        self.assertTrue(d["id"])
        dit = parle[-1]
        self.assertIn("le 25 décembre, tous les ans, gare ouverte, quand il pleut", dit)
        self.assertIn("annonce « Joyeux Noël à tous », jingle rétro", dit)
        self.assertTrue(self.joues and self.joues[-1].endswith("sncf_retro.mp3"))
        # Retour arriere : N garde, O supprime
        fin, parle = self.lancer([DOWN, BS, "n", BS, "o", ESC])
        self.assertTrue(any(p.startswith(self.cmd.dire("decors.garde")) for p in parle), parle)
        self.assertTrue(any(p.startswith(self.cmd.dire("decors.supprime")) for p in parle), parle)
        self.assertEqual(self.store["decors"], [])

    def test_son_telecharge_et_jour_impossible(self):
        import sons_en_ligne_cmd

        def faux_telechargement(line, key_source, say, out, read_line, depot=None):
            os.makedirs(depot, exist_ok=True)
            chemin = os.path.join(depot, "cloche.mp3")
            open(chemin, "wb").close()
            faux_telechargement.poses = [chemin]
            return "1 son dans le dépôt des décors de la gare."
        faux_telechargement.poses = []
        touches = ["\r", DOWN, RIGHT, DOWN, END, DOWN, RIGHT, RIGHT,   # ponctuel, le 31 fevrier
                   "\r", LEFT, LEFT, "\r",                    # refuse ; tous les mois ; suivant
                   RIGHT,                           # action : jouer un son
                   DOWN, DOWN, "\r",                # telecharger -> revient sur le son
                   DOWN, DOWN, "\r", ESC]
        with mock.patch.object(sons_en_ligne_cmd, "run", faux_telechargement):
            fin, parle = self.lancer(touches)
        self.assertIn(self.cmd.dire("decors.erreur_jour_mois"), parle)
        d, = self.store["decors"]
        dossier = self.cmd.quand(d)              # sans nom : le dossier porte les conditions
        self.assertEqual((d["ponctuel"], d["jour"], d["mois"], d["action"], d["sons"]),
                         (True, 31, 0, "son", [f"{dossier}/cloche.mp3"]))
        self.assertTrue(any("cloche, du dépôt, coché" in p for p in parle), parle)
        self.assertIn("de temps en temps, au hasard", self.cmd.decrire(d))
        # le son telecharge et coche a quitte le depot pour le dossier du decor
        self.assertFalse(os.path.exists(os.path.join(self.gare, "decors", "depot", "cloche.mp3")))
        self.assertTrue(os.path.isfile(os.path.join(self.gare, dossier, "cloche.mp3")))

    def test_plusieurs_sons_nom_et_renommage(self):
        depot = os.path.join(self.gare, "decors", "depot")
        os.makedirs(depot)
        for nom in ("a vapeur.mp3", "b sifflet.mp3", "c reste.mp3"):
            open(os.path.join(depot, nom), "wb").close()
        touches = ["\r", "\r",                              # nouveau ; Entree sur le nom : le taper
                   DOWN, "\r",                               # ponctuel ; suivant
                   RIGHT,                                    # action : jouer un son
                   DOWN, RIGHT, " ",                         # a vapeur : coche
                   RIGHT, RIGHT, " ",                        # c reste : coche
                   " ",                                      # ... decoche
                   LEFT, " ",                                # b sifflet : coche
                   RIGHT, RIGHT, RIGHT,                      # fin de liste : jamais les sons de la gare
                   DOWN, DOWN, "\r", ESC]
        fin, parle = self.lancer(touches, ["Train à vapeur"])
        self.assertIn("Décoché.", " ".join(parle))
        self.assertFalse(any("TER" in p or "klaxon" in p for p in parle), parle)
        d, = self.store["decors"]
        self.assertEqual(d["nom"], "Train à vapeur")
        self.assertEqual(d["sons"], ["Train à vapeur/a vapeur.mp3", "Train à vapeur/b sifflet.mp3"])
        self.assertEqual(sorted(os.listdir(depot)), ["c reste.mp3"])       # le non coche reste au depot
        self.assertEqual(sorted(os.listdir(os.path.join(self.gare, "Train à vapeur"))), ["a vapeur.mp3", "b sifflet.mp3"])
        self.assertTrue(self.cmd.decrire(d).startswith("Train à vapeur. "))
        self.assertIn("un son au hasard parmi 2 sons", self.cmd.decrire(d))
        # chaque tirage peut prendre un son different
        tires = {decors.son_au_hasard(d) for _ in range(200)}
        self.assertEqual(tires, {os.path.join(self.gare, c) for c in d["sons"]})
        # rouvert : renomme, et b decoche -> le dossier suit le nom, b retourne au depot
        fin, parle = self.lancer([DOWN, "\r", "\r", DOWN, "\r",
                                  DOWN, RIGHT, " ", DOWN, DOWN, "\r", ESC], ["Vapeur rétro"])
        self.assertTrue(any("2 sons cochés" in p and "a vapeur, d'un décor, coché" in p for p in parle), parle)
        d, = self.store["decors"]
        self.assertEqual(d["sons"], ["Vapeur rétro/a vapeur.mp3"])
        self.assertEqual(sorted(os.listdir(depot)), ["b sifflet.mp3", "c reste.mp3"])
        self.assertEqual(os.listdir(os.path.join(self.gare, "Vapeur rétro")), ["a vapeur.mp3"])
        self.assertFalse(os.path.exists(os.path.join(self.gare, "Train à vapeur")))

    def test_sons_de_la_gare(self):
        # jamais proposes : on les confondrait avec les vrais trains
        self.assertEqual(decors.candidats(), [])
        self.assertIsNone(decors.son_au_hasard(decor(sons=["ter_passby/1.wav", "horn1.mp3"])))
        self.assertEqual(self.cmd._nom_son("ter_passby/1.wav"), "TER qui passe 1, son de la gare")
        self.assertEqual(self.cmd._nom_son("horn1.mp3"), "klaxon, son de la gare")


if __name__ == "__main__":
    unittest.main()
