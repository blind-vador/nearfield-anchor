"""Journal : le clavier ne se fige plus, chaque prise a son annonce, l'avis se
tait au journal ferme, les archives se rangent par jour. Sans son : aplay factice."""
import datetime
import os
import stat
import sys
import tempfile
import threading
import time
from unittest import mock

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "config", "bridge", "console", "watchers")]
TMP = tempfile.mkdtemp()
BIN = os.path.join(TMP, "bin")
os.makedirs(BIN)
for nom in ("aplay",):
    p = os.path.join(BIN, nom)
    with open(p, "w") as f:
        f.write("#!/bin/sh\ncat >/dev/null\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]

import subprocess

import audio_lock
import decodage
import journaux

audio_lock.JOURNAL_FLAG = os.path.join(TMP, "journal_ouvert.json")   # jamais le vrai drapeau

# Un petit mp3 et un wav de test
WAV = os.path.join(TMP, "a.wav")
MP3 = os.path.join(TMP, "a.mp3")
subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i", "sine=440:d=1", "-ar", "22050", WAV], check=True)
subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-i", WAV, MP3], check=True)

# --- decodage : memes octets que ffmpeg, secours sur un format inconnu, rien sur un fichier absent
for src in (WAV, MP3):
    ref = subprocess.run(["ffmpeg", "-v", "quiet", "-nostdin", "-i", src, "-f", "s16le", "-ac", "1", "-ar", "48000", "-"],
                         capture_output=True).stdout
    got = decodage.pcm(src, 48000)
    assert abs(len(got) - len(ref)) < 4800, (src, len(got), len(ref))
faux = os.path.join(TMP, "b.mp3")
with open(faux, "wb") as f:
    f.write(b"pas du son")
assert decodage.pcm(faux, 48000) == b""
assert decodage.pcm(os.path.join(TMP, "absent.mp3"), 48000) == b""
fx = decodage.Flux(MP3, 48000)
fx.arreter()
t = time.time()
while fx.lire(9600):
    pass
fx.fermer()
assert time.time() - t < 2

# --- annonce : Microsoft lache et le secours est espeak -> un fichier quand meme
import annonce_morceaux as morceaux
import ceremonie_radio as ceremonie
import voix_sources
edge = {"moteur": "edge", "voix": "fr-FR-HenriNeural", "debit": "+0%", "hauteur": "+0Hz", "aleatoire": False}
esp = {"moteur": "espeak", "voix": "espeak", "debit": "+0%", "hauteur": "+0Hz", "aleatoire": False}
with mock.patch.object(morceaux, "choose_voice", return_value=edge), \
        mock.patch.object(morceaux, "assemble", return_value=None), \
        mock.patch.object(voix_sources, "secours", return_value=esp):
    chemin = ceremonie._fichier_annonce("bande aviation, AM.")
assert chemin and os.path.getsize(chemin) > 100, chemin
assert os.path.basename(chemin).startswith(morceaux.PREFIXE_DIRECT)
ceremonie.jeter(chemin)
assert not os.path.exists(chemin)
garde = os.path.join(TMP + "_garde.wav")
open(garde, "w").close()
with mock.patch("tmp_ram.est_temporaire", return_value=False):
    ceremonie.jeter(garde)
assert os.path.exists(garde)
os.remove(garde)

# --- avis : journal ferme, rien ; ouvert, une fois par minute au plus, sans bloquer
import speak
dits = []
# Le tour de parole reel depend des sons du moment : ici il est donne tout de suite.
ticket = mock.Mock(dropped=False)
with mock.patch.object(speak, "speak_cloud", side_effect=lambda *a, **k: dits.append(a)), \
        mock.patch.object(audio_lock, "acquire", return_value=ticket), \
        mock.patch.object(audio_lock, "release"):
    audio_lock.set_journal_open(False)
    speak.journal_notice()
    time.sleep(0.2)
    assert not dits
    audio_lock.set_journal_open(True)
    t = time.time()
    speak.journal_notice()
    speak.journal_notice()
    assert time.time() - t < 0.2
    for _ in range(50):
        if dits:
            break
        time.sleep(0.1)
    assert len(dits) == 1, dits
    # ferme pendant l'attente du tour : rien non plus
    speak._journal_notice["t"] = 0.0
    def ferme_puis_prend(*a, **k):
        audio_lock.set_journal_open(False)
        return ticket
    with mock.patch.object(audio_lock, "acquire", side_effect=ferme_puis_prend):
        speak.journal_notice()
        time.sleep(0.5)
    assert len(dits) == 1, dits
    audio_lock.set_journal_open(False)

# --- archives : jour, mois, annee ; rien de perdu, rien d'ecrase, le reste intact
import archives
import journal
import propositions
import shutil
KEEP = os.path.join(TMP, "storage")
journaux.KEEP_DIR = KEEP
os.makedirs(os.path.join(KEEP, "lora"))
open(os.path.join(KEEP, "lora", "x.mp3"), "w").close()
open(os.path.join(KEEP, "notes.ini"), "w").close()
os.makedirs(os.path.join(KEEP, "archives", "messages"))
NOV = datetime.date(2026, 11, 6)
for chemin in (os.path.join(KEEP, "vendredi 6 novembre 2026 20h57m44s.mp3"),
               os.path.join(KEEP, "vendredi 6 novembre 2026 21h00m00s.mp3"),
               os.path.join(KEEP, "vendredi 2 octobre 2026 07h11m36s.mp3"),
               os.path.join(KEEP, "archives", "messages", "vendredi 6 novembre 2026 16h14m17s.mp3"),
               os.path.join(KEEP, "belfort en approche.mp3")):
    open(chemin, "w").close()
quand = time.mktime((2026, 10, 2, 12, 0, 0, 0, 0, -1))
os.utime(os.path.join(KEEP, "belfort en approche.mp3"), (quand, quand))
assert journaux.ranger_archives(NOV) == 5
assert journaux.ranger_archives(NOV) == 0
J6 = os.path.join(KEEP, "vendredi_6_novembre")
assert sorted(os.listdir(J6)) == ["16h14m17s messages.mp3", "20h57m44s.mp3", "21h00m00s.mp3"]
J2 = os.path.join(KEEP, "octobre", "vendredi_2_octobre")
assert sorted(os.listdir(J2)) == ["07h11m36s.mp3", "belfort en approche.mp3"]
assert os.path.exists(os.path.join(KEEP, "lora", "x.mp3")) and os.path.exists(os.path.join(KEEP, "notes.ini"))
assert not os.path.exists(os.path.join(KEEP, "archives"))
# meme nom en vrac et deja range : les deux restent
open(os.path.join(KEEP, "vendredi 6 novembre 2026 20h57m44s.mp3"), "w").close()
assert journaux.ranger_archives(NOV) == 1
assert "20h57m44s (2).mp3" in os.listdir(J6)
# nouveau mois : novembre part dans son dossier
DEC = datetime.date(2026, 12, 3)
assert journaux.ranger_archives(DEC) == 1
J6 = os.path.join(KEEP, "novembre", "vendredi_6_novembre")
assert "20h57m44s.mp3" in os.listdir(J6)
# nouvelle annee : les mois partent dans 2026, l'annee se lit dans le chemin
JAN = datetime.date(2027, 1, 2)
assert journaux.ranger_archives(JAN) == 2
assert sorted(os.listdir(KEEP)) == ["2026", "lora", "notes.ini"]
assert sorted(os.listdir(os.path.join(KEEP, "2026"))) == ["novembre", "octobre"]
J6 = os.path.join(KEEP, "2026", "novembre", "vendredi_6_novembre")
assert "16h14m17s messages.mp3" in os.listdir(J6)
# un jour reste a la racine longtemps sans archivage : son nom de jour retrouve l'annee
os.makedirs(os.path.join(KEEP, "lundi_11_septembre"))
assert journaux.date_du_dossier("lundi_11_septembre", ref=JAN) == datetime.date(2023, 9, 11)
os.rmdir(os.path.join(KEEP, "lundi_11_septembre"))
# deux dossiers pour le meme jour : fusionnes, sans rien ecraser
os.makedirs(os.path.join(KEEP, "vendredi_6_novembre"))
open(os.path.join(KEEP, "vendredi_6_novembre", "20h57m44s.mp3"), "w").close()
assert journaux.date_du_dossier("vendredi_6_novembre", ref=JAN) == NOV
assert journaux.ranger_archives(JAN) == 1
assert not os.path.exists(os.path.join(KEEP, "vendredi_6_novembre"))
assert "20h57m44s (3).mp3" in os.listdir(J6)
# lecture : chaque journal voit les siens, a tous les niveaux
AUJ = datetime.date.today()
journaux.ranger_archives(AUJ)
days = archives.scan_archives("radio")
assert sum(1 for r in days[NOV]) == 4, days[NOV]
assert all(r[2] is None for r in days[NOV])
assert sorted(r[2] for r in days[datetime.date(2026, 10, 2)] if r[2]) == ["belfort en approche"]
msg = archives.scan_archives("messages")
assert [r[0].strftime("%H%M%S") for r in msg[NOV]] == ["161417"]
assert journaux.journal_du_nom("16h14m17s messages.mp3") == "messages"
assert journaux.journal_du_nom("20h57m44s DMR.mp3") == "radio"
assert journaux.journal_du_nom("belfort en approche.mp3") == "radio"

# --- _keep : dossier cree au premier archivage ; le rendu entendu, pas la prise
LOG = os.path.join(TMP, "radio_log")
os.makedirs(LOG)
journal.LOG_DIR = LOG
stamp = datetime.datetime.combine(AUJ, datetime.time(8, 9, 10))
prise = os.path.join(LOG, f"canal_test_{journaux.nom_jour(AUJ)}_{AUJ.year}_08h09m10s.mp3")
shutil.copy(MP3, prise)
assert journal._keep(stamp, prise, None, "radio") == journal.dire("journal.garde")
KJ = os.path.join(KEEP, journaux.nom_jour(AUJ))
assert os.listdir(KJ) == ["08h09m10s.mp3"]
assert journal._keep(stamp, prise, None, "radio") == journal.dire("journal.deja_garde")
assert journal._keep(stamp, prise, None, "messages") == journal.dire("journal.garde")
assert "08h09m10s messages.mp3" in os.listdir(KJ)
shutil.copy(WAV, prise[:-4] + ".p1.mp3")
propositions._sauver(prise, {"courante": 1, "freq_hz": 446000000, "label": "x",
                             "items": [{"juge": "nfm", "fichier": None}, {"juge": "-fs", "fichier": os.path.basename(prise[:-4] + ".p1.mp3")}]})
assert journal._keep(stamp, prise, None, "radio") == journal.dire("journal.garde")
assert "08h09m10s DMR.mp3" in os.listdir(KJ)
os.remove(propositions._chemin(prise))
os.remove(prise[:-4] + ".p1.mp3")

# --- dialogue : les touches restent servies pendant une annonce longue
journal.ns.capture_info = lambda p: None
journal.ns.forget_capture = lambda p: None
journal.ns.last_capture = lambda: None
journal.ns.add_signature = lambda c: 0
propositions._noter = lambda *a, **k: None
propositions._apprendre = lambda *a, **k: True
propositions.noter_rejet_simple = lambda p: None
journal.notation.load_notes = lambda: []
journal.ctrl_held = lambda: False   # sans le suivi du clavier reel
journal.alt_held = lambda: False
journal.Player._hold = lambda self: None
journal.Player._let_go = lambda self: None
journal._journal_opened = lambda o: None
journal.conseils.intro = lambda *a: "intro"
journal.voix.prechauffer = lambda *a, **k: None
import journal_vus
journal_vus.mark = lambda p: None
journal.os.path.ismount = lambda p: True

parle = []
def say_lent(phrase, encore=None):
    parle.append(("debut", phrase))
    for _ in range(40):   # 4 s, interrompue par couper()
        if encore is not None and not encore():
            parle.append(("coupee", phrase))
            return
        time.sleep(0.1)
    parle.append(("fin", phrase))
journal.voix.say_blocking = say_lent
journal.voix.stop = lambda: None

joues = []
vrai_play = journal.Player.play
def espion(self, path, avant=(), apres=()):
    joues.append(os.path.basename(path))
    return vrai_play(self, path, avant, apres)
journal.Player.play = espion

for h, n in ((10, "a"), (11, "b"), (12, "c")):
    chemin = os.path.join(LOG, f"canal_{n}_samedi_7_novembre_2026_{h}h00m00s.mp3")
    shutil.copy(MP3, chemin)
    open(chemin[:-4] + ".flux.wav", "w").close()
A, B, C = [os.path.join(LOG, f"canal_{n}_samedi_7_novembre_2026_{h}h00m00s.mp3") for h, n in ((10, "a"), (11, "b"), (12, "c"))]
propositions._sauver(A, {"courante": 0, "freq_hz": 446000000, "label": "x",
                         "items": [{"juge": "nfm", "fichier": None}, {"juge": "-fs", "fichier": None}]})
os.remove(prise)   # seules a, b, c dans ce jour

DOWN, LEFT = "\x1b[B", "\x1b[D"
journal.propositions.habillage_courant = lambda p: None
delais = {}
def touches():
    yield DOWN                 # joue a
    time.sleep(0.4)
    t0 = time.time()
    yield LEFT                 # rejette le rendu nfm : phrase longue puis le rendu suivant
    delais["apres_rejet"] = time.time() - t0
    t0 = time.time()
    yield LEFT                 # plus aucun rendu : la prise part, phrase longue, b en route
    delais["apres_rejet_2"] = time.time() - t0
    assert not os.path.exists(A) and not os.path.exists(A[:-4] + ".flux.wav")
    time.sleep(0.2)
    t0 = time.time()
    yield DOWN                 # coupe la phrase et joue c tout de suite
    delais["apres_bas"] = time.time() - t0
    time.sleep(0.6)
    # prise disparue entre-temps : le rejet n'est pas une erreur
    os.remove(C)
    yield LEFT
    time.sleep(0.2)
    yield "\x1b"

debut = time.time()
fin = journal.run(touches(), lambda t: parle.append(("say", t)), lambda t: None, "radio")
assert fin == journal.dire("journal.ferme", nom="radio"), fin
for nom_, d in delais.items():
    assert d < 0.5, (nom_, d)
assert time.time() - debut < 6, time.time() - debut
assert any(p[0] == "coupee" for p in parle), parle
assert any("canal_c" in j for j in joues), joues
for reste in os.listdir(LOG):
    assert not reste.startswith("canal_a"), reste
time.sleep(0.5)

print("ok")
