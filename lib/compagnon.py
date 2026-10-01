"""Pont avec une machine compagne (Mac, PC...) qui fait les calculs lourds a
la place du Pi (images de satellites meteo...), sans le faire chauffer.

Confiance (bridge/pont_protocole.py) : le Pi et chaque machine ont leur paire
de cles Ed25519, echangees a l'appairage et verifiees par un code a 4
chiffres dit des deux cotes. La liste des machines de confiance reste sur la
carte SD du Pi (TRUST_DIR), pas sur le NAS : ecrire sur le NAS ne suffit pas
pour se faire passer pour une machine appairee.

Donnees : le disque partage du NAS (BRIDGE_DIR, partage "server").
  appairage/demande.json        demande du Pi (sa cle publique)
  appairage/reponse-<id>.json   reponse de la machine (sa cle publique)
  appairage/accord-<id>.json    accord signe par le Pi
  appairage/refus-<id>.json     la personne a dit non sur la machine
  revocations/<id>.json         la machine retire le pont (message signe)
  travaux/<id>/travail.json     travail depose par le Pi (+ ses fichiers)
  travaux/<id>/resultat.json    resultat signe par la machine

Liaison directe : la machine se connecte au Pi (lib/compagnon_serveur.py) ;
le Pi lui pousse les travaux, la reveille si elle dort, et elle previent
quand c'est fini.
"""
import json
import os
import shutil
import socket
import time
import uuid

import pont_protocole as proto

BRIDGE_DIR = "/media/nas/nd1/server/nearfield-anchor-bridge"
LOCAL_DIR = "/home/vador/autopilot/compagnon"
KEY_PATH = os.path.join(LOCAL_DIR, "cle.pem")
TRUST_DIR = os.path.join(LOCAL_DIR, "confiance")
PRESENCE = "/tmp/compagnons_connectes.json"  # tenu par compagnon_serveur
PAIRING_SEC = 180
WAKEABLE_SEC = 7 * 86400  # machine vue depuis : on compte sur elle (reveil possible)


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


def key():
    return proto.load_or_create_key(KEY_PATH)


def pi_id():
    return socket.gethostname()


# --- machines de confiance -----------------------------------------------------------

def trusted():
    """{id: fiche} des machines appairees (cle, nom, capacites, adresses, stats)."""
    found = {}
    try:
        names = os.listdir(TRUST_DIR)
    except OSError:
        return {}
    for name in names:
        if name.endswith(".json"):
            card = _read(os.path.join(TRUST_DIR, name))
            if card and card.get("cle"):
                found[name[:-5]] = card
    return found


def update_card(cid, **fields):
    path = os.path.join(TRUST_DIR, f"{cid}.json")
    card = _read(path)
    if card is None:
        return None
    for k, v in fields.items():
        if k == "stats":
            card.setdefault("stats", {}).update(v)
        else:
            card[k] = v
    _write(path, card)
    return card


def connected():
    """{id: {depuis, vu}} des machines connectees en ce moment."""
    presence = _read(PRESENCE) or {}
    return {cid: p for cid, p in presence.items() if time.time() - p.get("vu", 0) < 90}


def companions():
    online = connected()
    return [dict(card, id=cid, presente=cid in online) for cid, card in trusted().items()]


def available(capability):
    """Machine de confiance qui sait faire capability et sur laquelle on peut
    compter : connectee, ou vue cette semaine (le Pi la reveillera)."""
    for c in companions():
        if capability in c.get("capacites", []) and (
                c["presente"] or time.time() - c.get("vu", 0) < WAKEABLE_SEC):
            return c
    return None


def forget(cid):
    try:
        os.remove(os.path.join(TRUST_DIR, f"{cid}.json"))
    except OSError:
        pass


# --- appairage (cote Pi, commande "appairage" de la console) -------------------------

def _local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def start_pairing():
    stop_pairing()
    for name in _list("appairage"):  # repart d'un dossier propre
        if name.startswith(("reponse-", "refus-")):
            _remove(_path("appairage", name))
    _write(_path("appairage", "demande.json"),
           {"pi": pi_id(), "cle": proto.public_b64(key()), "hote": f"{pi_id()}.local", "ip": _local_ip(),
            "port": proto.PORT, "t": time.time()})


def _list(*parts):
    try:
        return os.listdir(_path(*parts))
    except OSError:
        return []


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def pairing_answer():
    """Derniere reponse d'une machine, avec le code calcule des deux cles ; ou None."""
    answers = [_read(_path("appairage", n)) for n in _list("appairage")
               if n.startswith("reponse-") and n.endswith(".json")]
    answers = [a for a in answers if a and a.get("cle") and a.get("id")]
    if not answers:
        return None
    answer = max(answers, key=lambda a: a.get("t", 0))
    answer["code"] = proto.pairing_code(proto.public_b64(key()), answer["cle"])
    return answer


def refused(cid):
    return os.path.exists(_path("appairage", f"refus-{cid}.json"))


def accept(answer):
    """La personne a valide le code sur le Pi : confiance locale + accord signe."""
    _write(os.path.join(TRUST_DIR, f"{answer['id']}.json"),
           {"nom": answer.get("nom") or answer["id"], "cle": answer["cle"],
            "capacites": answer.get("capacites", []), "adresse_materielle": answer.get("adresse_materielle"),
            "ips": answer.get("ips", []), "appaire": time.time(), "vu": time.time(), "stats": {}})
    _write(_path("appairage", f"accord-{answer['id']}.json"),
           proto.sign(key(), {"type": "accord", "pi": pi_id(), "compagnon": answer["id"],
                              "cle_compagnon": answer["cle"]}))


def stop_pairing():
    """Efface la demande ; un accord deja ecrit reste le temps que la machine
    le lise (nettoye au bout de quelques minutes)."""
    _remove(_path("appairage", "demande.json"))
    for name in _list("appairage"):
        try:
            if time.time() - os.path.getmtime(_path("appairage", name)) > PAIRING_SEC + 120:
                os.remove(_path("appairage", name))
        except OSError:
            pass


def read_revocations():
    """Ponts retires depuis la machine compagne (message signe par elle) : [(id, nom)]."""
    cards = trusted()
    gone = []
    for name in _list("revocations"):
        path = _path("revocations", name)
        msg = _read(path) or {}
        cid = msg.get("compagnon")
        if cid in cards and msg.get("pi") == pi_id() and proto.verify(cards[cid]["cle"], msg, max_age=None):
            forget(cid)
            gone.append((cid, cards[cid].get("nom", cid)))
            _remove(path)
        elif time.time() - os.path.getmtime(path) > 86400:
            _remove(path)
    return gone


# --- travaux --------------------------------------------------------------------------

def new_job(kind, params):
    """Cree le dossier d'un travail ; rend (id, dossier, travail). Les
    fichiers d'entree y sont ecrits par l'appelant, puis submit() le publie."""
    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{uuid.uuid4().hex[:6]}"
    folder = _path("travaux", job_id)
    os.makedirs(folder, exist_ok=True)
    return job_id, folder, {"id": job_id, "type": kind, "params": params, "cree": time.time(), "pi": pi_id()}


def submit(job):
    _write(_path("travaux", job["id"], "travail.json"), job)


def open_jobs():
    """Travaux deposes et pas encore pris : [travail]."""
    jobs = (_read(_path("travaux", n, "travail.json")) for n in sorted(_list("travaux")))
    return [j for j in jobs if j]


def finished_jobs():
    """Travaux termines, signes par une machine de confiance : [(travail, resultat, dossier)].
    La signature couvre l'etat et l'empreinte de chaque fichier rendu."""
    cards = trusted()
    found = []
    for job_id in sorted(_list("travaux")):
        folder = _path("travaux", job_id)
        result = _read(os.path.join(folder, "resultat.json"))
        job = _read(os.path.join(folder, "travail.json")) or _read(os.path.join(folder, "travail.pris.json"))
        if not result or not job or result.get("travail") != job_id:
            continue
        card = cards.get(result.get("compagnon"))
        if not card or not proto.verify(card["cle"], result, max_age=None):
            continue  # pas d'une machine appairee : ignore
        try:
            if any(proto.file_digest(os.path.join(folder, name)) != digest
                   for name, digest in result.get("sorties", {}).items()):
                continue  # fichier modifie depuis la signature
        except OSError:
            continue
        found.append((job, result, folder))
    return found


def drop_stale(max_age):
    """Efface les travaux sans resultat depuis max_age (jamais pris, ou
    machine coupee en plein calcul) : ce sont de gros fichiers I/Q."""
    for job_id in _list("travaux"):
        folder = _path("travaux", job_id)
        if os.path.exists(os.path.join(folder, "resultat.json")):
            continue
        job = _read(os.path.join(folder, "travail.json")) or _read(os.path.join(folder, "travail.pris.json"))
        try:
            created = job.get("cree") if job else os.path.getmtime(folder)
        except OSError:
            continue
        if time.time() - created > max_age:
            print(f"[compagnon] travail {job_id} sans resultat, efface")
            shutil.rmtree(folder, ignore_errors=True)


def pending_count():
    return sum(1 for j in _list("travaux") if not os.path.exists(_path("travaux", j, "resultat.json")))


def status():
    """Commande "compagnon" : etat du pont."""
    found = companions()
    if not found:
        return ("Aucune machine compagne appairée. Ouvre Nearfield Bridge sur la machine, choisis appairer, "
                "et dis appairage ici en même temps.")
    parts = []
    for c in found:
        caps = ", ".join(c.get("capacites", [])) or "rien de déclaré"
        stats = c.get("stats", {})
        text = (f"{c['nom']}, {'connectée' if c['presente'] else 'pas connectée, je la réveillerai au besoin'}, "
                f"sait faire : {caps}")
        done, failed = stats.get("faits", 0), stats.get("echecs", 0)
        if done or failed:
            text += (f", {done} {'travaux réussis' if done > 1 else 'travail réussi'}, "
                     f"{failed} sans résultat")
        parts.append(text)
    pending = pending_count()
    return ("Machines compagnes : " + " ; ".join(parts) + "."
            + (" 1 travail en cours." if pending == 1 else f" {pending} travaux en cours." if pending else ""))
