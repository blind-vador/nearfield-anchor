"""Revue des propositions (2026-11-11) : nettoyage, nom et frequence trouvee sont
des entrees separees, chacune avec sa fiche vocale retiree une fois tranchee ;
l'examen ne recoit que le son capte, jamais l'habillage ; un resultat d'examen
est dit une fois, sans prise au journal radio, et compte dans les notifications."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "watchers", "bridge", "config", "tools")]
import phrases  # noqa: E402
phrases.BASE_DIR = os.path.join(ICI, "..", "phrases")
import frequences_actives as fa  # noqa: E402
import frequences_ia  # noqa: E402
import nommage  # noqa: E402
import prise_vocale  # noqa: E402
import propositions  # noqa: E402
import propositions_reglages as pr  # noqa: E402
import radio_log  # noqa: E402
import relais  # noqa: E402
import revue_propositions as revue  # noqa: E402
import stations  # noqa: E402


def faux_synthese(texte, path, source=None, effet=True):
    with open(path, "w", encoding="utf-8") as f:
        f.write(texte)
    return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        j = lambda n: os.path.join(self.tmp, n)  # noqa: E731
        self.anciens = (pr.STORE_NAS, pr.STORE_LOCAL, pr.LOCK_PATH, nommage.PATH, frequences_ia.TROUVEES,
                        fa.PATH, fa.LOCK, revue.AUDIO_DIR, revue.VUES, propositions.SCORES,
                        relais.STORE_PATH, relais.LOCAL_PATH)
        pr.STORE_NAS = pr.STORE_LOCAL = j("propositions.json")
        pr.LOCK_PATH = j("p.lock")
        nommage.PATH, frequences_ia.TROUVEES = j("nommage.json"), j("trouvees.json")
        fa.PATH, fa.LOCK = j("fa.json"), j("fa.lock")
        revue.AUDIO_DIR, revue.VUES = j("propositions_audio"), j("vues.json")
        propositions.SCORES, relais.STORE_PATH, relais.LOCAL_PATH = j("jf.json"), j("x/r.json"), j("r.json")
        self.p = [mock.patch.object(prise_vocale, "synthesize", side_effect=faux_synthese),
                  mock.patch.object(revue, "preparer"),      # les fiches sont faites a la demande ici
                  mock.patch.object(stations, "load", return_value={"stations": []}),
                  mock.patch.object(stations, "name_near", return_value=None),
                  mock.patch.object(stations, "proposer_nom"),
                  mock.patch.object(fa, "_relais_trouve")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        (pr.STORE_NAS, pr.STORE_LOCAL, pr.LOCK_PATH, nommage.PATH, frequences_ia.TROUVEES,
         fa.PATH, fa.LOCK, revue.AUDIO_DIR, revue.VUES, propositions.SCORES,
         relais.STORE_PATH, relais.LOCAL_PATH) = self.anciens
        shutil.rmtree(self.tmp, ignore_errors=True)

    def peupler(self):
        pr.ajouter({"id": "17", "le": time.time() - 30, "resume": "Examen demandé — bande VHF : parole comprise "
                    "à 40 % au brut, 80 % après nettoyage léger.", "brut": "a.brut.mp3",
                    "traitements": [{"niveau": "léger", "audio": "x.wav", "action": {}}], "action": {}})
        nommage.deposer(160_650_000, "nfm", None, "bande VHF", "Chantier, levage", "on monte", "Mac", "a.mp3",
                        pourquoi="ballet de grue", explication="Deux équipes guident une grue.")
        frequences_ia.ranger([{"mhz": 438.6125, "modulation": "DMR", "certitude": 0.9,
                               "phrase": "QSY sur 438.6125", "role": "rendez-vous"}],
                             "analyse de station", 160_650_000)


class Separees(Base):
    def test_trois_entrees_independantes(self):
        self.peupler()
        e = revue.entrees()
        self.assertEqual([x["genre"] for x in e], ["nettoyage", "nommage", "frequence"])
        # on refuse le nettoyage et on accepte le nom de la meme prise
        revue.refuser(e[0])
        revue.accepter(e[1])
        self.assertEqual(pr._read()[0]["etat"], "refuse")
        stations.proposer_nom.assert_called_once()
        self.assertEqual([x["genre"] for x in revue.entrees()], ["frequence"])

    def test_fiches_vocales(self):
        self.peupler()
        e = revue.entrees()
        textes = [open(revue.fiche(x), encoding="utf-8").read() for x in e]
        self.assertIn("Nettoyage audio proposé", textes[0])
        self.assertNotIn("Examen demandé", textes[0])
        self.assertIn("Niveau conseillé : léger", textes[0])
        self.assertIn("Chantier, levage", textes[1])
        self.assertIn("Pourquoi : ballet de grue", textes[1])
        self.assertIn("438,6125 mégahertz, DMR", textes[2])
        self.assertIn("échange transcrit par Whisper, entendu sur 160,65 mégahertz", textes[2])
        self.assertIn("quatorze jours", textes[2])
        for x in e:
            self.assertTrue(os.path.isfile(revue.chemin_fiche(x)))
        revue.accepter(e[0], 0)
        self.assertFalse(os.path.exists(revue.chemin_fiche(e[0])))       # retiree une fois tranchee
        nommage.refuser(e[1]["item"])                                      # tranche dans le journal des noms
        revue.menage()
        self.assertFalse(os.path.exists(revue.chemin_fiche(e[1])))
        self.assertTrue(os.path.exists(revue.chemin_fiche(e[2])))

    def test_nouvelles_pour_les_notifications(self):
        self.peupler()
        self.assertEqual(revue.nouvelles(), 3)
        revue.marquer_entendue(revue.entrees()[0])
        self.assertEqual(revue.nouvelles(), 2)


    def test_anciennes_propositions(self):
        pr.ajouter({"id": "9", "resume": "canal : voix de 0.93 à 0.97 après nettoyage.",
                    "traitements": [{"niveau": "léger", "audio": "x.wav", "action": {}}], "action": {}})
        nommage.deposer(446_031_250, "nfm", None, "446 civil", "Jumelage", "on se retrouve", "Mac")
        t = {x["genre"]: revue.texte(x) for x in revue.entrees()}
        self.assertIn("voix de 93 % à 97 %", t["nettoyage"])
        self.assertNotIn("Pourquoi", t["nommage"])
        self.assertIn("on se retrouve.", t["nommage"])


class Frequences(Base):
    def test_garder_ou_oublier(self):
        self.peupler()
        frequences_ia.ranger([{"mhz": 161.0, "certitude": 1}], "message message_x", None)
        f1, f2 = [x for x in revue.entrees() if x["genre"] == "frequence"]
        self.assertIn("message décodé", revue.texte(f2))
        revue.accepter(f1)
        e = fa.load()[fa._cle(438_612_500)]
        self.assertNotIn("expire", e)
        self.assertTrue(fa._utile(e))
        revue.refuser(f2)
        self.assertNotIn(fa._cle(161_000_000), fa.load())
        res = frequences_ia.ranger([{"mhz": 161.0, "certitude": 1}], "test")   # plus jamais surveillee
        self.assertEqual(res["surveillees"], [])
        self.assertEqual(revue.entrees(), [x for x in revue.entrees() if x["genre"] != "frequence"])

    def test_devenue_permanente_quitte_la_revue(self):
        frequences_ia.ranger([{"mhz": 162.0, "certitude": 1}], "test")
        self.assertEqual(len(frequences_ia.a_revoir()), 1)
        fa.noter(162_000_000, "voix", modulation="nfm")
        self.assertEqual(frequences_ia.a_revoir(), [])


class AnnonceSansJournal(unittest.TestCase):
    def test_dite_une_fois_sans_prise(self):
        with mock.patch.object(prise_vocale, "record") as record, \
                mock.patch.object(prise_vocale, "_dire_seulement") as dire_seul:
            prise_vocale.announce("Examen terminé.", None, "examen", "compagnon", wait=True, journal=False)
        record.assert_not_called()
        dire_seul.assert_called_once()


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg absent")
class BrutSansHabillage(unittest.TestCase):
    def test_brut_ne_garde_que_le_son_capte(self):
        tmp = tempfile.mkdtemp()
        try:
            def ton(nom, sec):
                p = os.path.join(tmp, nom)
                subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=d={sec}", p],
                               check=True)
                return p
            with mock.patch.object(radio_log, "RADIO_LOG_DIR", tmp), mock.patch.object(radio_log, "_touch"):
                s = radio_log.RadioLogSession("canal test", exiger_contenu=True)
                s.add_audio_file(ton("carillon.wav", 2))
                s.add_audio_file(ton("annonce.wav", 2))
                s.add_raw_pcm(b"\x00\x10" * 22050, 22050, "s16le")       # 1 s capte
                s.add_audio_file(ton("fin.wav", 2))
                out = s.finalize()
            brut = out[:-4] + ".brut.mp3"
            self.assertTrue(os.path.isfile(brut))
            duree = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                          "csv=p=0", brut], capture_output=True, text=True).stdout)
            self.assertLess(duree, 1.5)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DerniereTranchee(unittest.TestCase):
    """2026-11-12 : apres avoir tranche la derniere entree, Echap levait
    IndexError (la liste vide etait encore lue)."""
    def test_echap_apres_la_derniere(self):
        import proposition_cmd
        entree = {"genre": "frequence", "id": "f1", "le": 0, "item": {}}
        dits = []
        with mock.patch.object(revue, "entrees", return_value=[entree]), \
                mock.patch.object(revue, "menage"), \
                mock.patch.object(revue, "fiche", return_value=None), \
                mock.patch.object(revue, "texte", return_value="x"), \
                mock.patch.object(revue, "marquer_entendue"), \
                mock.patch.object(revue, "accepter", return_value="Gardée."), \
                mock.patch.object(proposition_cmd, "Player"), \
                mock.patch.object(proposition_cmd.voix, "say_blocking", side_effect=dits.append), \
                mock.patch.object(proposition_cmd.voix, "stop"):
            fin = proposition_cmd.run("journal proposition", iter(["c", "\x1b"]), dits.append, lambda t: None)
        self.assertEqual(fin, "")                                 # dit ici, la console ne redit rien
        self.assertIn("Gardée.", dits[-1])                        # 2026-11-13 : plus de fermeture muette
        self.assertIn("Plus de proposition", dits[-1])

    def test_captations_annoncees_pas_jouees(self):
        """2026-11-13 : pendant la revue, le drapeau "journal ouvert" est pose."""
        import audio_lock
        import proposition_cmd
        vu = []
        entree = {"genre": "frequence", "id": "f1", "le": 0, "item": {}}

        def touches():
            vu.append(audio_lock.journal_open())
            yield "\x1b"
        with mock.patch.object(audio_lock, "JOURNAL_FLAG", os.path.join(tempfile.mkdtemp(), "j.json")), \
                mock.patch.object(revue, "entrees", return_value=[entree]), \
                mock.patch.object(revue, "menage"), \
                mock.patch.object(revue, "fiche", return_value=None), \
                mock.patch.object(revue, "texte", return_value="x"), \
                mock.patch.object(revue, "marquer_entendue"), \
                mock.patch.object(proposition_cmd, "Player"), \
                mock.patch.object(proposition_cmd.voix, "stop"):
            proposition_cmd.run("journal proposition", touches(), lambda t: None, lambda t: None)
            self.assertEqual(vu, [True])
            self.assertFalse(audio_lock.journal_open())          # retire a la fermeture


class RetourArriere(unittest.TestCase):
    """2026-11-12 : retour arriere refuse d'un coup tous les niveaux d'un nettoyage."""
    def test_tous_les_niveaux_d_un_coup(self):
        import proposition_cmd
        nettoyage = {"genre": "nettoyage", "id": "n1", "le": 0,
                     "item": {"traitements": [{"niveau": "léger"}, {"niveau": "moyen"}, {"niveau": "fort"}]}}
        autre = {"genre": "frequence", "id": "f1", "le": 1, "item": {}}
        refuses = []
        with mock.patch.object(revue, "entrees", return_value=[nettoyage, autre]), \
                mock.patch.object(revue, "menage"), \
                mock.patch.object(revue, "fiche", return_value=None), \
                mock.patch.object(revue, "texte", return_value="x"), \
                mock.patch.object(revue, "marquer_entendue"), \
                mock.patch.object(revue, "refuser", side_effect=lambda e: refuses.append(e["id"]) or "Refusée."), \
                mock.patch.object(proposition_cmd, "Player"), \
                mock.patch.object(proposition_cmd.voix, "say_blocking"), \
                mock.patch.object(proposition_cmd.voix, "stop"):
            sorties = []
            proposition_cmd.run("journal proposition", iter(["\x7f", "\x1b"]), lambda t: None, sorties.append)
        self.assertEqual(refuses, ["n1"])                       # une seule touche, la prise est ecartee
        self.assertIn("3 niveaux refusés", "".join(sorties))


if __name__ == "__main__":
    unittest.main()
