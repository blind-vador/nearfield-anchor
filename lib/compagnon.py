"""Pont avec une machine compagne (Mac, PC...) qui fait les calculs lourds a
la place du Pi (images de satellites meteo...), sans le faire chauffer.

Tout passe par le disque partage du NAS (dossier BRIDGE_DIR du partage
"server"), que la machine compagne monte aussi : pas de reseau a ouvrir.

  compagnons/<id>.json          presence de la machine (toutes les 30 s) et
                                ce qu'elle sait faire ("capacites")
  compagnons/<id>.accord.json   secret partage, ecrit par le Pi a l'appairage
  appairage/demande.json        demande du Pi (code a 4 chiffres)
  appairage/reponse-<id>.json   reponse de la machine compagne (meme code)
  travaux/<id>/travail.json     travail depose par le Pi (+ ses fichiers)
  travaux/<id>/resultat.json    resultat signe (HMAC du secret) de la machine

La machine compagne : bridge/nearfield_bridge.py (module installable,
bridge/installer.sh). Une fonction lourde n'est proposee que si une machine
appairee, qui sait la faire, s'est signalee recemment.
"""
import hashlib
import hmac
import json
import os
import random
import secrets
import shutil
import time
import uuid

BRIDGE_DIR = "/media/nas/nd1/server/nearfield-anchor-bridge"
PRESENT_SEC = 180
PAIRING_SEC = 180


def _path(*parts):
    return os.path.join(BRIDGE_DIR, *parts)


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


def signature(secret, job_id, state):
    return hmac.new(secret.encode(), f"{job_id}|{state}".encode(), hashlib.sha256).hexdigest()


# --- machines appairees ------------------------------------------------------------

def companions():
    """Machines appairees : [{id, nom, capacites, presente, secret}]."""
    found = []
    folder = _path("compagnons")
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        if not name.endswith(".accord.json"):
            continue
        cid = name[: -len(".accord.json")]
        accord = _read(_path("compagnons", name)) or {}
        beat = _read(_path("compagnons", f"{cid}.json")) or {}
        found.append({"id": cid, "nom": beat.get("nom") or accord.get("nom") or cid,
                      "capacites": beat.get("capacites", []), "secret": accord.get("secret"),
                      "presente": time.time() - beat.get("vu", 0) < PRESENT_SEC})
    return found


def available(capability):
    """La machine appairee et presente qui sait faire capability, ou None."""
    return next((c for c in companions() if c["presente"] and capability in c["capacites"]), None)


# --- appairage (cote Pi, commande "appairage" de la console) -------------------------

def start_pairing():
    code = f"{random.randint(0, 9999):04d}"
    stop_pairing()  # repart d'un dossier propre
    _write(_path("appairage", "demande.json"), {"code": code, "t": time.time(), "pi": os.uname().nodename})
    return code


def pairing_answer(code):
    """Reponse d'une machine compagne au code, ou None."""
    try:
        names = os.listdir(_path("appairage"))
    except OSError:
        return None
    for name in names:
        if name.startswith("reponse-") and name.endswith(".json"):
            answer = _read(_path("appairage", name))
            if answer and answer.get("code") == code:
                return answer
    return None


def accept(answer):
    secret = secrets.token_hex(32)
    _write(_path("compagnons", f"{answer['id']}.accord.json"),
           {"secret": secret, "nom": answer.get("nom"), "accepte": time.time()})
    stop_pairing()


def stop_pairing():
    shutil.rmtree(_path("appairage"), ignore_errors=True)


def forget(cid):
    for name in (f"{cid}.accord.json", f"{cid}.json"):
        try:
            os.remove(_path("compagnons", name))
        except OSError:
            pass


# --- travaux --------------------------------------------------------------------------

def new_job(kind, params):
    """Cree le dossier d'un travail ; rend (id, dossier). Les fichiers
    d'entree y sont ecrits par l'appelant, puis submit() le publie."""
    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{uuid.uuid4().hex[:6]}"
    folder = _path("travaux", job_id)
    os.makedirs(folder, exist_ok=True)
    return job_id, folder, {"id": job_id, "type": kind, "params": params, "cree": time.time()}


def submit(job):
    _write(_path("travaux", job["id"], "travail.json"), job)


def finished_jobs():
    """Travaux termines et signes par une machine appairee : [(travail, resultat, dossier)]."""
    secrets_by_id = {c["id"]: c["secret"] for c in companions() if c["secret"]}
    found = []
    try:
        names = sorted(os.listdir(_path("travaux")))
    except OSError:
        return []
    for job_id in names:
        folder = _path("travaux", job_id)
        result = _read(os.path.join(folder, "resultat.json"))
        job = _read(os.path.join(folder, "travail.json")) or _read(os.path.join(folder, "travail.pris.json"))
        if not result or not job or result.get("lu"):
            continue
        secret = secrets_by_id.get(result.get("par"))
        if not secret or not hmac.compare_digest(result.get("signature", ""),
                                                 signature(secret, job_id, result.get("etat", ""))):
            continue  # pas d'une machine appairee : ignore
        found.append((job, result, folder))
    return found


def mark_read(folder, result):
    result["lu"] = time.time()
    _write(os.path.join(folder, "resultat.json"), result)


def drop_stale(max_age):
    """Efface les travaux sans resultat depuis max_age (jamais pris, ou
    machine coupee en plein calcul) : ce sont de gros fichiers I/Q."""
    try:
        names = os.listdir(_path("travaux"))
    except OSError:
        return
    for job_id in names:
        folder = _path("travaux", job_id)
        if os.path.exists(os.path.join(folder, "resultat.json")):
            continue
        job = _read(os.path.join(folder, "travail.json")) or _read(os.path.join(folder, "travail.pris.json"))
        if job and time.time() - job.get("cree", time.time()) > max_age:
            print(f"[compagnon] travail {job_id} sans resultat, efface")
            shutil.rmtree(_path("travaux", job_id), ignore_errors=True)


def pending_count():
    try:
        return sum(1 for j in os.listdir(_path("travaux"))
                   if not os.path.exists(_path("travaux", j, "resultat.json")))
    except OSError:
        return 0


def status():
    """Commande "compagnon" : etat du pont."""
    found = companions()
    if not found:
        return ("Aucune machine compagne appairée. Lance le module compagnon sur une machine, puis dis "
                "appairage ici en même temps.")
    parts = []
    for c in found:
        caps = ", ".join(c["capacites"]) or "rien de déclaré"
        parts.append(f"{c['nom']}, {'présente' if c['presente'] else 'absente'}, sait faire : {caps}")
    pending = pending_count()
    return ("Machines compagnes : " + " ; ".join(parts) + "."
            + (f" {pending} travail en attente." if pending == 1 else f" {pending} travaux en attente." if pending else ""))
