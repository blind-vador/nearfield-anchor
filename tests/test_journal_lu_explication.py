"""« journal messages lu » fait taire les notifications d'un seul journal ;
touche E des journaux messages et donnees : l'IA explique, la reponse est
gardee dans la fiche. Sans son ni reseau : aplay et IA factices."""
import datetime
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "config", "bridge", "console", "watchers")]
TMP = tempfile.mkdtemp()
BIN = os.path.join(TMP, "bin")
os.makedirs(BIN)
with open(os.path.join(BIN, "aplay"), "w") as f:
    f.write("#!/bin/sh\ncat >/dev/null\n")
os.chmod(os.path.join(BIN, "aplay"), 0o755)
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]

import audio_lock
audio_lock.JOURNAL_FLAG = os.path.join(TMP, "journal_ouvert.json")
audio_lock.REGISTRY = os.path.join(TMP, "arbitre.json")
import journal_vus
journal_vus.PATH = os.path.join(TMP, "ecoutes.json")
import journal
import journaux
import matcher
import explication_messages as em
import gemini

# --- la commande : "lu" se lit apres n'importe quel journal
for phrase, attendu in (("journal messages lu", "messages"), ("Journal données lues", "donnees"),
                        ("journal radio lu", "radio"), ("journal lu", "radio")):
    mots = matcher.normalize(phrase).split()
    assert {"lu", "lus", "lue", "lues"} & set(mots), (phrase, mots)
    assert journaux.from_words(matcher.normalize(phrase)) == attendu, phrase

# --- des entrees dans deux journaux
LOG = os.path.join(TMP, "radio_log")
os.makedirs(LOG)
journal.LOG_DIR = LOG
journal.os.path.ismount = lambda p: True
WAV = os.path.join(TMP, "a.wav")
subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i", "sine=440:d=1", WAV], check=True)
AUJ = datetime.date.today()
J = f"{journaux.nom_jour(AUJ)}_{AUJ.year}"
journal_vus._save({"depuis": time.time() - 60, "ecoutes": {}})
MSG = [os.path.join(LOG, f"message_avion_ACARS_DL0175_{J}_10h0{i}m00s.mp3") for i in range(3)]
RADIO = os.path.join(LOG, f"canal_x_{J}_09h00m00s.mp3")
for p in MSG + [RADIO]:
    shutil.copy(WAV, p)
FICHE = {"protocole": "ACARS", "source": "DL0175", "details": {"label": "H1", "texte": "221N1817 0175"},
         "texte": "Un Boeing envoie le message suivant.", "t": 1.0}
for p in MSG[:2]:
    with open(p[:-4] + ".json", "w", encoding="utf-8") as f:
        json.dump(FICHE, f)

avant = journal.unheard_counts()
assert avant.get("messages") == 3 and avant.get("radio") == 1, avant
r = journal.marquer_lu("messages")
assert r == journal.dire("journal.lu_fait", nom="messages", entrees=journal.nombre(3, "entrée", "entrées")), r
apres = journal.unheard_counts()
assert "messages" not in apres and apres.get("radio") == 1, apres   # les autres journaux gardent leurs notifications
assert journal.marquer_lu("messages") == journal.dire("journal.lu_rien", nom="messages")
assert all(os.path.exists(p) for p in MSG)                           # rien n'est supprime

# --- la question et le nettoyage
q = em.question("messages", MSG[0], dict(FICHE, explication_ia="vieux"))
assert "221N1817" in q and "Boeing" in q and "vieux" not in q and "messages" in q
assert em.nettoyer("**ACARS** :\n- un *message*\n1. [lien](http://x)\n# Titre") == "ACARS : un message lien Titre"

# --- expliquer : une requete, puis la fiche repond seule
questions = []
def ask_lent(prompt, **k):
    questions.append(prompt)
    time.sleep(0.6)
    return "**C'est** un message ACARS de Delta.", None
gemini.ask = ask_lent
assert em.expliquer("messages", MSG[2]) == (None, "fiche")
assert em.expliquer("messages", MSG[0]) == ("C'est un message ACARS de Delta.", None)
assert em.expliquer("messages", MSG[0])[0] == "C'est un message ACARS de Delta." and len(questions) == 1
with open(MSG[0][:-4] + ".json", encoding="utf-8") as f:
    garde = json.load(f)
assert garde["explication_ia"] == "C'est un message ACARS de Delta." and garde["source"] == "DL0175"
os.remove(MSG[0][:-4] + ".json")
with open(MSG[0][:-4] + ".json", "w", encoding="utf-8") as f:
    json.dump(FICHE, f)
questions.clear()

# --- dans le journal : E rend la main, la reponse arrive ; on a bouge -> on se tait, E la redit
journal.ns.capture_info = lambda p: None
journal.notation.load_notes = lambda: []
journal.ctrl_held = lambda: False
journal.alt_held = lambda: False
journal.Player._hold = lambda self: None
journal.Player._let_go = lambda self: None
journal._journal_opened = lambda o: None
journal.conseils.intro = lambda *a: "intro"
journal.voix.prechauffer = lambda *a, **k: None
journal.voix.say_blocking = lambda *a, **k: None
journal.voix.stop = lambda: None
journal.propositions.habillage_courant = lambda p: None
journal_vus.mark = lambda p: None
DOWN = "\x1b[B"
dit, delais = [], []
def touches():
    # ordre du jour : 10h00 (fiche), 10h01 (fiche), 10h02 (sans fiche)
    yield DOWN
    t = time.time(); yield "e"; delais.append(time.time() - t)
    time.sleep(1.0)                    # la reponse arrive, on n'a pas bouge : dite
    yield DOWN                         # 10h01
    yield "e"                          # question partie...
    yield "e"                          # ... deja en cours
    yield DOWN                         # on part avant la reponse
    time.sleep(1.0)                    # reponse arrivee ailleurs : rien n'est dit
    yield "e"                          # 10h02 : pas de fiche
    yield "\x1b[A"                     # retour sur 10h01
    yield "e"                          # gardee : dite tout de suite, sans requete
    time.sleep(0.2)
    yield "\x1b"
fin = journal.run(touches(), dit.append, lambda t: None, "messages")
assert fin == journal.dire("journal.ferme", nom="messages"), fin
assert delais[0] < 0.3, delais
reponse = "C'est un message ACARS de Delta."
assert len(questions) == 2, len(questions)
assert dit.count(journal.dire("journal.explication_demandee")) == 2, dit
assert journal.dire("journal.explication_en_cours") in dit, dit
assert journal.dire("journal.explication_sans_fiche") in dit, dit
assert dit.count(reponse) == 2, dit          # une fois a l'arrivee, une fois redite par E
i = dit.index(journal.dire("journal.explication_sans_fiche"))
assert dit[i + 1:].count(reponse) == 1, dit  # pas dite pendant qu'on etait sur 10h02

# --- E garde son sens ailleurs : l'examen du Mac dans le journal radio
appels = []
import examen
examen.demander = lambda nom: appels.append(nom) or True
journal.run(iter([DOWN, "e", "\x1b"]), lambda t: None, lambda t: None, "radio")
assert appels == [os.path.basename(RADIO)], appels

shutil.rmtree(TMP)
print("ok")
