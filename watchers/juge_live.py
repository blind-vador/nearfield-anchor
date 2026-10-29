"""Triage des juges a la volee (2026-10-25), comme "zapper" / "ignorer".

Ce que l'on entend en ce moment n'est pas decode par le bon juge ? La console
ecrit une demande ici ; la boucle de decodage numerique de radio_watch la lit
(comme maintien.zap_depuis) et agit aussitot, sur le signal en cours :
  "suivant" : on arrete ce decodeur et on passe au suivant sur le meme signal ;
              la confiance de ce mode baisse sur cette bande.
  "ok"      : c'est le bon decodeur ; sa confiance monte sur cette bande.
Si la communication se termine avant, radio_watch garde son flux brut : les
autres juges pourront l'examiner apres (juges_watch).

La confiance (trust) par bande est tenue dans le meme fichier que juges_watch
(juges_scores.json), lu par l'ordre des juges."""

import chemins
import json
import os
import time

DEMANDE_PATH = "/tmp/radio_watch_juge.json"          # demande de la console (suivant / ok)
ETAT_PATH = "/tmp/radio_watch_juge_etat.json"        # ce que radio_watch decode en ce moment
SCORES_PATH = chemins.base("juges_scores.json")
ETAT_FRAIS_SEC = 12                                  # au-dela, plus rien n'est "en cours"

_lu = {"t_demande": 0.0, "lu": 0.0}


def _ecrire(chemin, data):
    tmp = chemin + f".{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, chemin)


def _lire(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --- cote console ----------------------------------------------------------------------

def courant():
    """Ce que radio_watch decode en ce moment (ou None) : freq, bande, mode, protocole."""
    etat = _lire(ETAT_PATH)
    if not etat or time.time() - etat.get("t", 0) > ETAT_FRAIS_SEC:
        return None
    return etat


def demander(action):
    """La console demande "suivant" ou "ok" sur le decodage en cours."""
    _ecrire(DEMANDE_PATH, {"action": action, "t": time.time()})


# --- cote radio_watch ------------------------------------------------------------------

def debut(freq_hz, band, mode, protocole=None):
    _ecrire(ETAT_PATH, {"freq_hz": int(freq_hz), "band": band, "mode": mode,
                        "protocole": protocole, "t": time.time()})


def rafraichir(protocole):
    etat = _lire(ETAT_PATH)
    if etat:
        etat["protocole"] = protocole
        etat["t"] = time.time()
        _ecrire(ETAT_PATH, etat)


def fin():
    try:
        os.remove(ETAT_PATH)
    except OSError:
        pass


def action_depuis(debut_ecoute):
    """Action demandee depuis le debut de cette ecoute, consommee une seule fois
    (le fichier de demande est efface des qu'on la prend, pour qu'un "ok" ne soit
    pas compte en boucle). Relue au plus toutes les 0,3 s, comme maintien.zap_depuis."""
    now = time.time()
    if now - _lu["lu"] >= 0.3:
        _lu["lu"] = now
        d = _lire(DEMANDE_PATH)
        _lu["t_demande"] = float(d.get("t", 0) or 0)
        _lu["action"] = d.get("action")
    if _lu["t_demande"] > debut_ecoute:
        _lu["t_demande"] = 0.0
        try:
            os.remove(DEMANDE_PATH)      # consommee : plus aucune relecture ne la reprend
        except OSError:
            pass
        return _lu.get("action")
    return None


def noter(band, mode, ok):
    """Confiance du juge <mode> sur <band> : +1 oui si ok, +1 non sinon (meme
    fichier et meme structure que juges_watch)."""
    try:
        s = _lire(SCORES_PATH) or {}
        s.setdefault("bandes", {})
        b = s["bandes"].setdefault(band or "?", {"_protocoles": {}})
        js = b.setdefault(mode, {"oui": 0, "non": 0, "propositions": 0})
        js["oui" if ok else "non"] = js.get("oui" if ok else "non", 0) + 1
        _ecrire(SCORES_PATH, s)
    except OSError as exc:
        print(f"[juge_live] note impossible : {exc}")
