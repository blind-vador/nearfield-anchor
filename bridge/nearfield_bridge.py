#!/usr/bin/env python3
"""Module compagnon de nearfield anchor : une machine plus puissante (Mac,
PC Linux) fait les calculs lourds a la place du Raspberry Pi.

Pas de reseau a ouvrir : tout passe par le disque partage du NAS, que la
machine monte deja (partage "server"). Voir lib/compagnon.py cote Pi.

  nearfield_bridge.py appairage   a lancer en meme temps que la commande
                                  "appairage" de la console du Pi ; les deux
                                  machines disent le meme code a 4 chiffres
  nearfield_bridge.py service     boucle de travail (lancee au demarrage par
                                  l'installateur)
  nearfield_bridge.py etat        ce que la machine sait faire, appairage
  nearfield_bridge.py oublier     annule l'appairage de cette machine

Compatible Python 3.9 (celui d'Apple).
"""
import hashlib
import hmac
import json
import os
import platform
import socket
import subprocess
import sys
import time
import traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.expanduser("~/.nearfield-bridge")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
BIN_DIR = os.path.join(CONFIG_DIR, "bin")
SHARES = ["/Volumes/server/server/nearfield-anchor-bridge",  # Mac
          "/mnt/server/server/nearfield-anchor-bridge",      # Linux (montage habituel)
          "/media/nas/nd1/server/nearfield-anchor-bridge"]   # le Pi lui-meme (essais)
BEAT_SEC = 30
POLL_SEC = 20
PAIRING_WAIT_SEC = 180


# --- outils -------------------------------------------------------------------------

def say(text):
    """Affiche et dit le texte (VoiceOver n'est pas toujours sur le terminal)."""
    print(text, flush=True)
    for cmd in (["say", text], ["espeak-ng", "-v", "fr", text], ["spd-say", "-l", "fr", text]):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            return
        except (OSError, subprocess.TimeoutExpired):
            continue


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def config():
    conf = _read(CONFIG) or {}
    if "id" not in conf:
        conf = {"id": uuid.uuid4().hex[:12], "nom": socket.gethostname().split(".")[0]}
        _write(CONFIG, conf)
    return conf


def bridge_dir(conf):
    for path in [conf.get("partage")] + SHARES:
        if path and os.path.isdir(os.path.dirname(path)):
            os.makedirs(path, exist_ok=True)
            return path
    return None


def capabilities():
    caps = []
    try:
        import numpy  # noqa: F401
        if all(os.access(os.path.join(BIN_DIR, b), os.X_OK) for b in ("meteor_demod", "meteor_decode")):
            caps.append("meteor")
    except ImportError:
        pass
    return caps


def signature(secret, job_id, state):
    return hmac.new(secret.encode(), ("%s|%s" % (job_id, state)).encode(), hashlib.sha256).hexdigest()


def secret_of(conf, share):
    accord = _read(os.path.join(share, "compagnons", conf["id"] + ".accord.json"))
    return accord.get("secret") if accord else None


# --- appairage ------------------------------------------------------------------------

def pairing():
    conf = config()
    share = bridge_dir(conf)
    if not share:
        say("Je ne trouve pas le disque partagé du NAS. Monte le partage server, puis recommence.")
        return 1
    caps = capabilities()
    if not caps:
        say("Attention : cette machine ne sait encore rien calculer. Relance l'installateur.")
    say("Mode appairage. Dis maintenant appairage sur la console du Pi. J'attends trois minutes.")
    request_path = os.path.join(share, "appairage", "demande.json")
    deadline = time.time() + PAIRING_WAIT_SEC
    request = None
    while time.time() < deadline:
        request = _read(request_path)
        if request and time.time() - request.get("t", 0) < PAIRING_WAIT_SEC:
            break
        request = None
        time.sleep(2)
    if not request:
        say("Le Pi ne s'est pas manifesté. Appairage abandonné.")
        return 1
    code = request["code"]
    say("Le Pi %s demande l'appairage. Code : %s. Vérifie que le Pi dit le même code, "
        "puis valide sur la console du Pi." % (request.get("pi", ""), " ".join(code)))
    _write(os.path.join(share, "appairage", "reponse-%s.json" % conf["id"]),
           {"id": conf["id"], "nom": conf["nom"], "code": code, "capacites": caps,
            "systeme": platform.platform(terse=True), "t": time.time()})
    accord_path = os.path.join(share, "compagnons", conf["id"] + ".accord.json")
    while time.time() < deadline + 60:
        if os.path.exists(accord_path):
            conf["partage"] = share
            _write(CONFIG, conf)
            heartbeat(conf, share)
            say("Appairage réussi. Cette machine calculera pour le Pi dès que le service tourne.")
            return 0
        if not os.path.isdir(os.path.join(share, "appairage")):
            break  # le Pi a refuse ou abandonne
        time.sleep(2)
    say("Le Pi n'a pas validé. Appairage abandonné.")
    return 1


def forget():
    conf = config()
    share = bridge_dir(conf)
    if share:
        for name in (conf["id"] + ".accord.json", conf["id"] + ".json"):
            try:
                os.remove(os.path.join(share, "compagnons", name))
            except OSError:
                pass
    say("Appairage annulé pour cette machine.")
    return 0


# --- service ----------------------------------------------------------------------------

def heartbeat(conf, share):
    _write(os.path.join(share, "compagnons", conf["id"] + ".json"),
           {"nom": conf["nom"], "capacites": capabilities(), "vu": time.time(),
            "systeme": platform.platform(terse=True)})


def _claim(share):
    """Prend le plus ancien travail libre (renommage atomique) : (id, dossier, travail)."""
    folder = os.path.join(share, "travaux")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    for job_id in names:
        src = os.path.join(folder, job_id, "travail.json")
        dst = os.path.join(folder, job_id, "travail.pris.json")
        if not os.path.exists(src):
            continue
        try:
            os.rename(src, dst)
        except OSError:
            continue  # une autre machine l'a pris
        job = _read(dst)
        if job:
            return job_id, os.path.join(folder, job_id), job
    return None


def _process(job, folder):
    kind = job.get("type")
    if kind == "meteor" and "meteor" in capabilities():
        sys.path.insert(0, HERE)
        import meteor
        return meteor.process(folder, job["params"], BIN_DIR)
    return "echec", [], "cette machine ne sait pas faire « %s »" % kind


def service():
    conf = config()
    print("[bridge] service demarre : %s (%s)" % (conf["nom"], conf["id"]), flush=True)
    last_beat = 0
    while True:
        share = bridge_dir(conf)
        secret = secret_of(conf, share) if share else None
        if not secret:
            time.sleep(POLL_SEC)  # pas (ou plus) appairee, ou NAS absent
            continue
        if time.time() - last_beat >= BEAT_SEC:
            heartbeat(conf, share)
            last_beat = time.time()
        claimed = _claim(share)
        if not claimed:
            time.sleep(POLL_SEC)
            continue
        job_id, folder, job = claimed
        print("[bridge] travail %s (%s)" % (job_id, job.get("type")), flush=True)
        heartbeat(conf, share)
        started = time.time()
        try:
            state, outputs, message = _process(job, folder)
        except Exception:
            traceback.print_exc()
            state, outputs, message = "echec", [], "erreur de calcul sur la machine compagne"
        _write(os.path.join(folder, "resultat.json"),
               {"etat": state, "sorties": outputs, "message": message, "par": conf["id"],
                "duree": round(time.time() - started), "fini": time.time(),
                "signature": signature(secret, job_id, state)})
        print("[bridge] %s : %s (%s), %d s" % (job_id, state, message, time.time() - started), flush=True)


def status():
    conf = config()
    share = bridge_dir(conf)
    caps = capabilities()
    paired = bool(share and secret_of(conf, share))
    say("Machine %s. Sait faire : %s. Disque du NAS : %s. %s" % (
        conf["nom"], ", ".join(caps) or "rien pour l'instant", "trouvé" if share else "introuvable",
        "Appairée avec le Pi." if paired else "Pas encore appairée."))
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "etat"
    actions = {"appairage": pairing, "service": service, "etat": status, "oublier": forget}
    if command not in actions:
        print(__doc__)
        sys.exit(2)
    sys.exit(actions[command]() or 0)
