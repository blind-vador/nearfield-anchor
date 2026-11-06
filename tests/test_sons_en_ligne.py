"""Recherche de sons : fournisseurs, depot, dialogue au clavier. Sans son : aplay factice."""
import os
import stat
import subprocess
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
with open(os.path.join(BIN, "aplay"), "w") as f:
    f.write("#!/bin/sh\ncat >/dev/null\n")
os.chmod(os.path.join(BIN, "aplay"), 0o755)
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]

import reglages
import sonneries
sonneries.DEPOT = os.path.join(TMP, "depot")
import sons_en_ligne as sl
sl.CREDITS = os.path.join(TMP, "credits.txt")

MP3 = os.path.join(TMP, "a.mp3")
subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i", "sine=440:d=1", "-ar", "22050", MP3], check=True)
SON = open(MP3, "rb").read()

# noms de fichiers : rien de dangereux, jamais vide, pas d'ecrasement
assert sl.nom_de_fichier({"titre": "../../etc/passwd", "ext": ".mp3"}) == "etc passwd.mp3"
assert sl.nom_de_fichier({"titre": "???", "ext": ".ogg"}) == "son.ogg"
assert len(sl.nom_de_fichier({"titre": "x" * 500, "ext": ".mp3"})) <= 74
assert sl._titre("Bicycle-bell_1.wav") == "Bicycle-bell 1"
assert sl._ext("mp3") == ".mp3" and sl._ext("oga") == ".ogg" and sl._ext("mid") is None
assert sl._ext(None, "https://x/y/a.WAV?utm=1") == ".wav"
assert sl._licence_freesound("Creative Commons 0") == "CC0"
assert sl._licence_freesound("https://creativecommons.org/licenses/by-nc/4.0/") == "CC BY-NC"
assert sl._licence_freesound("Attribution") == "CC BY"

# fournisseurs : Freesound seulement avec sa cle
with mock.patch.object(reglages, "cle", return_value=""):
    assert sl.disponibles() == ["openverse"]
with mock.patch.object(reglages, "cle", return_value="k"):
    assert sl.disponibles() == ["openverse", "freesound"]

# depot : le son se lit, deux fois le meme nom cote a cote, credit ecrit ; une page d'erreur refusee
ap = sl.Apercus()
def reponse(contenu, code=200):
    r = mock.MagicMock()
    r.__enter__.return_value = r
    r.iter_content.return_value = [contenu]
    r.raise_for_status.side_effect = None if code == 200 else sl.requests.HTTPError()
    return r
s1 = dict(titre="Cloche", auteur="A", licence="CC0", url="https://x/1.mp3", page="https://x/1", duree=2, ext=".mp3")
with mock.patch.object(sl.requests, "get", return_value=reponse(SON)) as g:
    d1 = sl.deposer(s1, ap)
    d2 = sl.deposer(dict(s1, url="https://x/2.mp3", fichier=None), ap)
    assert g.call_count == 2
    sl.deposer(s1, ap)
    assert g.call_count == 2    # deja telecharge pour l'ecoute : pas redemande
assert os.path.basename(d1) == "Cloche.mp3" and os.path.basename(d2) == "Cloche (2).mp3"
assert "Cloche.mp3 ; Cloche ; A ; CC0 ; https://x/1" in open(sl.CREDITS, encoding="utf-8").read()
nb = len(os.listdir(sonneries.DEPOT))
with mock.patch.object(sl.requests, "get", return_value=reponse(b"<html>erreur</html>")):
    assert sl.deposer(dict(s1, url="https://x/3.mp3", fichier=None), ap) is None
with mock.patch.object(sl.requests, "get", return_value=reponse(b"", 404)):
    assert sl.deposer(dict(s1, url="https://x/4.mp3", fichier=None), ap) is None
assert len(os.listdir(sonneries.DEPOT)) == nb
gros = reponse(b"")
gros.iter_content.return_value = [b"x" * (sl.TAILLE_MAX + 1)]
with mock.patch.object(sl.requests, "get", return_value=gros):
    assert ap.fichier(dict(s1, url="https://x/5.mp3", fichier=None)) is None
ap.fermer()
assert not os.path.exists(ap.dossier)

# dialogue
import journal
import sons_en_ligne_cmd as cmd
import voix
journal.Player._hold = lambda self: None
journal.Player._let_go = lambda self: None
cmd.conseils.intro = lambda *a: "intro"
parle = []
voix.say = lambda t, **k: parle.append(t)
def lent(t, encore=None):
    parle.append(t)
    for _ in range(20):
        if encore is not None and not encore():
            return
        time.sleep(0.05)
voix.say_blocking = lent
voix.stop = lambda: None
joues = []
vrai = journal.Player.play
journal.Player.play = lambda self, p, *a: (joues.append(p), vrai(self, p, *a))[1]

SONS = [dict(s1, titre=f"Son{i}", url=f"https://x/{i}.mp3") for i in range(3)]
cherche = []
def faux_chercher(f, mot):
    cherche.append((f, mot))
    if mot == "panne":
        raise sl.requests.ConnectionError()
    return [] if mot == "rien" else [dict(s) for s in SONS]
sl.chercher = faux_chercher
sl.disponibles = lambda: ["openverse", "freesound"]
sl._telecharger = lambda url, dest: open(dest, "wb").write(SON)
mots = iter(["panne", "rien", "cloche", ""])
DOWN, UP, TAB, STAB = "\x1b[B", "\x1b[A", "\t", "\x1b[Z"
avant = set(os.listdir(sonneries.DEPOT))
delais = {}
def touches():
    yield DOWN                 # Freesound
    yield TAB                  # panne, rien, cloche : 3 essais dans le meme read_line factice
    time.sleep(0.2)
    t = time.time(); yield DOWN; delais["bas"] = time.time() - t    # Son1, sans attendre la phrase
    t = time.time(); yield " "; delais["espace"] = time.time() - t
    yield DOWN                 # Son2 : la phrase, puis le son
    time.sleep(1.5)
    yield " "
    yield " "                  # decoche Son2
    yield UP                   # Son1 : dit coche
    time.sleep(0.2)
    yield "i"
    yield "\x1b[D"             # touche inconnue
    yield "\r"
out = []
r = cmd.run("", touches(), lambda t: parle.append(t), out.append, lambda ks: next(mots))
assert r == cmd.dire("sons_ligne.fini", sons=cmd.nombre(1, "son")), r
assert cherche == [("freesound", "panne"), ("freesound", "rien"), ("freesound", "cloche")], cherche
for n, d in delais.items():
    assert d < 0.3, (n, d)
assert set(os.listdir(sonneries.DEPOT)) - avant == {"Son1.mp3"}, os.listdir(sonneries.DEPOT)
assert any(p.startswith(cmd.dire("sons_ligne.coche_devant", titre="Son1")) for p in parle), parle
assert any(cmd.dire("sons_ligne.recherche_impossible") == p for p in parle)
assert joues

# Entree sans coche, Echap : rien au depot ; mot vide : retour au fournisseur
mots = iter(["", "cloche"])
avant = set(os.listdir(sonneries.DEPOT))
def touches2():
    yield TAB                  # mot vide
    yield TAB                  # cloche
    time.sleep(0.1)
    yield "\r"                 # rien coche
    yield " "
    yield STAB                 # coches gardees, retour fournisseur
    yield "\x1b"
assert cmd.run("", touches2(), lambda t: parle.append(t), out.append, lambda ks: next(mots)) == cmd.dire("sons_ligne.ferme")
assert set(os.listdir(sonneries.DEPOT)) == avant
assert cmd.dire("sons_ligne.rien_coche") in parle

# la commande se reconnait, sans prendre la place des autres
import commands
import matcher
def trouve(texte):
    r = matcher.match(texte, commands.COMMANDS)
    return getattr(r[0] if isinstance(r, tuple) else r, "name", r)
for texte in ("cherche des sons", "telecharge des sons", "banque de sons", "cherche un son", "telecharger un son"):
    assert trouve(texte) == "cherche des sons", (texte, trouve(texte))
for texte, nom in (("sons", "sons"), ("sonneries", "sonneries"), ("son", "son")):
    assert trouve(texte) == nom, (texte, trouve(texte))
print("ok")
