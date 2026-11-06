"""Connecteurs d'IA : sans cle rien ne change, repli et repos, Gemini garde les images."""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "config", "bridge")]

import connecteurs_ia as ia
import gemini
import reglages

TMP = tempfile.mkdtemp()
ia.ETAT = os.path.join(TMP, "etat.json")
CLES = {}
reglages.cle = lambda nom: CLES.get(nom, "")

REPONSES = {}   # nom -> liste de (code, contenu, entetes) ; le dernier reste
RECUS = []


class Faux(BaseHTTPRequestHandler):
    def do_POST(self):
        nom = self.path.strip("/")
        corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        RECUS.append((nom, corps, self.headers.get("Authorization")))
        file_ = REPONSES[nom]
        code, contenu, entetes = file_.pop(0) if len(file_) > 1 else file_[0]
        if contenu == "LENT":
            time.sleep(3)
            contenu = "trop tard"
        data = json.dumps({"choices": [{"message": {"content": contenu}}]}).encode()
        self.send_response(code)
        for k, v in entetes.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Faux)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}/"
ia.CONNECTEURS = tuple(dict(c, url=BASE + c["nom"]) for c in ia.CONNECTEURS)
OK = (200, "Bonjour **toi**", {})


def rendu_google(texte):
    r = mock.Mock(ok=True, status_code=200)
    r.json.return_value = {"candidates": [{"content": {"parts": [{"text": texte}]}}]}
    return r


def remise():
    RECUS.clear()
    REPONSES.clear()
    CLES.clear()
    if os.path.exists(ia.ETAT):
        os.remove(ia.ETAT)


vrai_post = gemini.requests.post
# sans autre cle : Gemini seul, comme avant
remise()
CLES["gemini"] = "g"
with mock.patch.object(gemini.requests, "post", return_value=rendu_google("salut")) as p:
    assert gemini.ask("q") == ("salut", None)
    assert p.call_count == 1 and "googleapis" in p.call_args[0][0]
assert ia.actifs() == []
# aucune cle du tout : la phrase de toujours
remise()
assert gemini.ask("q")[0] is None and gemini.ask("q")[1]

# une cle : le service repond, Gemini n'est pas appele
remise()
CLES.update(gemini="g", groq="secret-groq")
REPONSES["groq"] = [OK]
with mock.patch.object(gemini, "_demander_gemini") as g:
    assert gemini.ask("q") == ("Bonjour toi", None)
    assert not g.called
assert RECUS[0][2] == "Bearer secret-groq"
assert ia.actifs() == ["groq"]

# quota : repos respecte (Retry-After), le suivant repond, puis on ne rappelle pas le premier
remise()
CLES.update(groq="a", cerebras="b")
REPONSES["groq"] = [(429, "", {"Retry-After": "120"})]
REPONSES["cerebras"] = [OK]
assert ia.demander("q") == "Bonjour toi"
assert ia.demander("q") == "Bonjour toi"
assert [r[0] for r in RECUS] == ["groq", "cerebras", "cerebras"], RECUS
assert 100 < json.load(open(ia.ETAT))["repos"]["groq"] - time.time() <= 120
assert json.load(open(ia.ETAT))["appels"]["cerebras"] == 2

# repos partage et fini : le service reprend
remise()
CLES["groq"] = "a"
REPONSES["groq"] = [(429, "", {"Retry-After": "1"}), OK]
assert ia.demander("q") is None
time.sleep(1.1)
assert ia.demander("q") == "Bonjour toi"

# cle refusee, panne : au suivant, jusqu'a Gemini
remise()
CLES.update(gemini="g", groq="a", mistral="c", openrouter="d")
REPONSES["groq"] = [(401, "", {})]
REPONSES["mistral"] = [(503, "", {})]
REPONSES["openrouter"] = [(200, "", {})]   # vide : inutilisable
with mock.patch.object(gemini, "_demander_gemini", return_value=("de Gemini", None)) as g:
    assert gemini.ask("q") == ("de Gemini", None)
    assert g.called
assert json.load(open(ia.ETAT))["appels"]["gemini"] == 1

# modele retire : le suivant du meme service
remise()
CLES["groq"] = "a"
REPONSES["groq"] = [(404, "", {}), OK]
assert ia.demander("q") == "Bonjour toi"
assert [r[1]["model"] for r in RECUS] == ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"]

# JSON : format demande, repris sans lui si refuse, enveloppe retiree, faux JSON ecarte
remise()
CLES["groq"] = "a"
REPONSES["groq"] = [(400, "", {}), (200, 'Voici :\n```json\n{"nom": "x"}\n```', {})]
assert json.loads(ia.demander("q", json_mode=True)) == {"nom": "x"}
assert "response_format" in RECUS[0][1] and "response_format" not in RECUS[1][1]
assert RECUS[0][1]["messages"][0]["role"] == "system"
remise()
CLES["groq"] = "a"
REPONSES["groq"] = [(200, "pas du json", {})]
assert ia.demander("q", json_mode=True) is None
remise()
CLES["groq"] = "a"
REPONSES["groq"] = [(200, "<think>hmm</think>Oui.", {})]
assert ia.demander("q") == "Oui."

# delai : le service lent n'empeche pas la reponse suivante
remise()
CLES["groq"] = "a"
REPONSES["groq"] = [(200, "LENT", {})]
t = time.time()
assert ia.demander("q", timeout=1.5) is None
assert time.time() - t < 2.5
time.sleep(1.8)

# Grok, payant : seulement apres l'echec de Gemini
remise()
CLES.update(gemini="g", grok="x")
REPONSES["grok"] = [OK]
with mock.patch.object(gemini, "_demander_gemini", return_value=("Gemini", None)):
    assert gemini.ask("q") == ("Gemini", None)
assert not RECUS
with mock.patch.object(gemini, "_demander_gemini", return_value=(None, "raison")):
    assert gemini.ask("q") == ("Bonjour toi", None)
assert ia.actifs() == ["grok"]
# tout echoue : la raison de Gemini reste
remise()
CLES.update(gemini="g", grok="x")
REPONSES["grok"] = [(402, "", {})]
with mock.patch.object(gemini, "_demander_gemini", return_value=(None, "raison")):
    assert gemini.ask("q") == (None, "raison")

# image ou son : Gemini seul
remise()
CLES.update(gemini="g", groq="a")
REPONSES["groq"] = [OK]
with mock.patch.object(gemini, "_demander_gemini", return_value=("vu", None)):
    assert gemini.ask("q", jpeg=b"x") == ("vu", None)
    assert gemini.ask("q", audio=b"x") == ("vu", None)
assert not RECUS

# Gemini : un modele muet ou epuise n'arrete pas les suivants, et se repose
remise()
CLES["gemini"] = "g"
quota = mock.Mock(ok=False, status_code=429)
quota.json.return_value = {"error": {"details": [{"retryDelay": "300s"}]}}
appels = []
def poste(url, **k):
    appels.append(url)
    if gemini.MODELS[0] in url:
        return quota
    if gemini.MODELS[1] in url:
        raise gemini.requests.Timeout()
    return rendu_google("lite")
with mock.patch.object(gemini.requests, "post", side_effect=poste):
    assert gemini.ask("q") == ("lite", None)
    assert len(appels) == 3
    assert gemini.ask("q") == ("lite", None)
    assert len(appels) == 4, appels   # seul celui qui repond est rappele
repos = json.load(open(ia.ETAT))["repos"]
assert 290 < repos[f"gemini:{gemini.MODELS[0]}"] - time.time() <= 300
assert repos[f"gemini:{gemini.MODELS[1]}"] - time.time() > 1000
# tous en repos : aucune requete, la raison du quota
with mock.patch.object(ia, "_en_repos", return_value=True), \
        mock.patch.object(gemini.requests, "post") as p:
    assert gemini.ask("q") == (None, gemini.dire("gemini.quota_gemini_epuise"))
    assert not p.called

# etat illisible : aucune question perdue
remise()
CLES["groq"] = "a"
REPONSES["groq"] = [OK]
with open(ia.ETAT, "w") as f:
    f.write("{abime")
assert ia.demander("q") == "Bonjour toi"

# fichiers de cle : facultatifs, jamais annonces comme manquants, fichier vide = absent
import importlib
importlib.reload(reglages)
assert "groq" not in reglages.keys_status() and "gemini" in reglages.keys_status()
assert "groq" not in reglages.KEY_LABELS
assert "groq.txt" in reglages.README
srv.shutdown()
print("ok")
