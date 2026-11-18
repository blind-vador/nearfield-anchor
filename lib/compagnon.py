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
  appairage/confirme-<id>.json  la personne a valide le code sur la machine
                                (signe par elle) : le Pi accepte tout seul
  appairage/refus-<id>.json     la personne a dit non sur la machine
  revocations/<id>.json         la machine retire le pont (message signe)
  travaux/<id>/travail.json     file d'attente : copie NON signee du travail, que les machines
                                renomment en travail.pris.json pour le prendre ; son contenu
                                n'est jamais cru (lire_travail lit travail.v2.json, signe)
  travaux/<id>/travail.v2.json  le travail, signe par le Pi (protocole v2 §6.1)
  travaux/<id>/resultat.v2.json resultat signe par la cle v2 de la machine
  travaux/<id>/resultat.json    resultat v1 (machine pas encore migree seulement)

Liaison directe : la machine se connecte au Pi (lib/compagnon_serveur.py) ;
le Pi lui pousse les travaux, la reveille si elle dort, et elle previent
quand c'est fini.
"""
import chemins
import contextlib
import fcntl
import json
import os
import shutil
import socket
import tempfile
import time
import uuid

import pont_protocole as proto
from phrases import dire, dire_ou, nombre

BRIDGE_DIR = "/media/nas/nd1/server/nearfield-anchor-bridge"
LOCAL_DIR = chemins.base("compagnon")
KEY_PATH = os.path.join(LOCAL_DIR, "cle.pem")
TRUST_DIR = os.path.join(LOCAL_DIR, "confiance")
PRESENCE = "/tmp/compagnons_connectes.json"  # tenu par compagnon_serveur
PAIRING_SEC = 180
NAS_BEAT_MAX_SEC = 13 * 3600  # signe de vie du mode differe : toutes les 12 h
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
    # Fichier temporaire unique (2026-10-27) : avec un ".tmp" commun, deux fils
    # qui ecrivaient la meme fiche en meme temps melangeaient leurs octets (JSON
    # casse, "Extra data") et le second os.replace tombait en FileNotFoundError.
    # La fiche de confiance illisible faisait refuser le Mac par le pont.
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


@contextlib.contextmanager
def _verrou_confiance():
    """Verrou exclusif sur les fiches de confiance, entre fils et entre programmes
    (flock : chaque ouverture a son propre verrou)."""
    os.makedirs(TRUST_DIR, exist_ok=True)
    with open(os.path.join(TRUST_DIR, ".verrou"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read_card(path, verrou_tenu=False):
    """Fiche de confiance ; si elle est abimee, on garde le premier objet JSON
    complet (une ecriture interrompue laisse des octets en trop a la fin) et on
    la reecrit proprement, plutot que d'oublier la machine appairee."""
    card = _read(path)
    if card is not None:
        return card
    try:
        with open(path, encoding="utf-8") as f:
            card, _ = json.JSONDecoder().raw_decode(f.read())
    except (OSError, ValueError):
        return None
    if not isinstance(card, dict) or not (card.get("cle") or card.get("v2")):
        return None
    with contextlib.nullcontext() if verrou_tenu else _verrou_confiance():
        if _read(path) is None:
            _write(path, card)
            print(f"[compagnon] fiche {os.path.basename(path)} abimee, reparee", flush=True)
    return card


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
            card = _read_card(os.path.join(TRUST_DIR, name))
            if card and (card.get("cle") or card.get("v2")):  # cle v1, ou machine appairee en v2
                found[name[:-5]] = card
    return found


def update_card(cid, **fields):
    path = os.path.join(TRUST_DIR, f"{cid}.json")
    with _verrou_confiance():
        card = _read_card(path, verrou_tenu=True)
        if card is None:
            return None
        for k, v in fields.items():
            if k == "stats":
                card.setdefault("stats", {}).update(v)
            else:
                card[k] = v
        _write(path, card)
    return card


def note_state(cid, state):
    """Etat de session envoye par la machine (2026-10-14 : ecran verrouille,
    inactivite) ; l'heure du dernier changement de verrouillage est gardee."""
    if not isinstance(state, dict) or "verrouille" not in state:
        return
    card = trusted().get(cid) or {}
    fields = {"etat": dict(state, t=time.time())}
    if (card.get("etat") or {}).get("verrouille") != state["verrouille"]:
        fields["verrou_depuis"] = time.time()
    update_card(cid, **fields)


def connected():
    """{id: {depuis, vu}} des machines presentes : liaison directe
    (compagnon_serveur), ou signe de vie signe depose sur le NAS depuis moins
    de NAS_BEAT_MAX_SEC (quand macOS refuse le reseau local a l'application :
    elle travaille alors en differe, deux fois par jour, "par" : "nas")."""
    presence = _read(PRESENCE) or {}
    found = {cid: p for cid, p in presence.items() if time.time() - p.get("vu", 0) < 90}
    cards = trusted()
    for cid, card in cards.items():
        if cid in found:
            continue
        beat = _battement_nas(cid, card)
        if beat:
            found[cid] = {"vu": beat["t"], "par": "nas", "capacites": beat.get("capacites", [])}
    return found


def _battement_nas(cid, card):
    """Signe de vie de la machine sur le NAS, verifie : en v2 (objet "battement_nas" signe
    par sa cle v2, moins de NAS_BEAT_MAX_SEC) pour une machine migree, sinon en v1."""
    beat = _read(_path("compagnons", f"{cid}.battement.json"))
    if not beat:
        return None
    if card.get("v2"):
        v2 = _pont_v2()
        if not v2 or "charge" not in beat:
            return None
        try:
            c = v2.np_.verifier_objet(beat, card["v2"]["identite"], "battement_nas", v2.parc())
        except Exception:
            return None
        if c.get("compagnon") != cid or time.time() - c.get("emis", 0) > NAS_BEAT_MAX_SEC:
            return None
        return dict(c, t=c["emis"])
    if (card.get("cle") and beat.get("compagnon") == cid and beat.get("pi") == pi_id()
            and proto.verify(card["cle"], beat, max_age=NAS_BEAT_MAX_SEC)):
        return beat
    return None


def companions():
    online = connected()
    out = []
    for cid, card in trusted().items():
        c = dict(card, id=cid, presente=cid in online,
                 directe=cid in online and online[cid].get("par") != "nas")  # sinon : differe
        if cid in online:
            # Union des capacites : un vieux battement NAS ne doit pas
            # effacer une capacite nouvellement declaree sur la fiche.
            if online[cid].get("capacites"):
                c["capacites"] = sorted(set(list(c.get("capacites") or []) + list(online[cid]["capacites"])))
            c["vu"] = online[cid].get("vu", c.get("vu"))
        out.append(c)
    return out


def available(capability):
    """Machine de confiance qui sait faire capability et sur laquelle on peut
    compter : connectee, ou vue cette semaine (le Pi la reveillera). Parmi les
    connectees, celle qui finirait le plus tot (rank)."""
    ranked = rank(capability)
    if ranked:
        return ranked[0]
    for c in companions():
        if capability in c.get("capacites", []) and (
                c["presente"] or time.time() - c.get("vu", 0) < WAKEABLE_SEC):
            return c
    return None


# --- choix de la machine qui epaule (2026-10-25) --------------------------------------
# Chaque machine envoie sa puissance reelle et sa disponibilite du moment
# (bridge/puissance.py) ; le Pi confie chaque travail a celle qui le FINIRA LE
# PLUS TOT, d'apres la duree apprise de ce type de travail sur elle (ramenee a
# pleine puissance), sa part libre et les travaux qu'elle a deja. Une machine
# jamais vue sur ce travail est estimee depuis une autre, au prorata de la
# puissance d'un coeur. La machine choisie a ATTRIBUTION_SEC pour le prendre
# (attribution.json dans le dossier du travail), sinon on passe a la suivante.
ATTRIBUTION_SEC = 45
PUISSANCE_FRAICHE_SEC = 120
FILES_PAR_MACHINE = 2   # le pont traite deux files a la fois (rapide, lourd)
APPRENTISSAGE = 0.3     # poids d'une nouvelle duree dans la moyenne


def note_power(cid, power):
    """Mesure envoyee par la machine (battement) ; datee a la reception par le
    Pi, pour ne pas dependre de l'horloge de la machine."""
    if isinstance(power, dict) and power.get("puissance"):
        update_card(cid, puissance=dict(power, recu=time.time()))


ECRITURE_BATTEMENT_SEC = 60  # moins que PUISSANCE_FRAICHE_SEC, battement compris
_battement_ecrit = {}


MAX_ETAT_EXTENSIONS = 32 * 1024


def note_battement(cid, capacites, voix_mac, etat, puissance, extensions=None):
    """Un battement : une seule ecriture, et au plus une par minute sauf changement
    utile (2026-11-06 : trois ecritures synchrones par battement sur la carte SD)."""
    now = time.time()
    card = trusted().get(cid) or {}
    fields = {"vu": now, "capacites": capacites, "voix_mac": voix_mac}
    change = capacites != card.get("capacites") or voix_mac != card.get("voix_mac")
    if isinstance(etat, dict) and "verrouille" in etat:
        fields["etat"] = dict(etat, t=now)
        if (card.get("etat") or {}).get("verrouille") != etat["verrouille"]:
            fields["verrou_depuis"] = now
            change = True
    if isinstance(puissance, dict) and puissance.get("puissance"):
        fields["puissance"] = dict(puissance, recu=now)
    # Etat des logiciels installes a part (travaux_externes) : transporte, jamais lu ici.
    if isinstance(extensions, dict) and extensions and len(json.dumps(extensions)) <= MAX_ETAT_EXTENSIONS:
        fields["extensions"] = {"etats": extensions, "recu": now}
    if change or now - _battement_ecrit.get(cid, 0) >= ECRITURE_BATTEMENT_SEC:
        _battement_ecrit[cid] = now
        update_card(cid, **fields)


def free_share(c):
    """Part libre de la machine (0..1) d'apres sa derniere mesure ; None sans mesure."""
    p = c.get("puissance") or {}
    if not p.get("puissance"):
        return None
    if time.time() - p.get("recu", 0) > PUISSANCE_FRAICHE_SEC:
        return 0.5  # mesure ancienne (mode differe par le NAS) : prudence
    return max(0.02, min(1.0, p.get("disponible", 0) / p["puissance"]))


def _duration_of(d, size_mb):
    if size_mb > 0.05 and d.get("s_mo"):
        return d["s_mo"] * size_mb
    return d.get("s")


def _full_power_seconds(c, kind, size_mb, machines):
    d = (c.get("durees") or {}).get(kind)
    if d:
        return _duration_of(d, size_mb)
    mine = (c.get("puissance") or {}).get("puissance_coeur")
    for o in machines:
        od, oc = (o.get("durees") or {}).get(kind), (o.get("puissance") or {}).get("puissance_coeur")
        if od and oc and mine:
            return _duration_of(od, size_mb) * oc / mine
    return 1000.0 / mine if mine else None  # personne n'a d'historique : la puissance seule departage


def job_size_mb(job_id):
    total = 0
    for name in _list("travaux", job_id):
        if not name.endswith(".json"):
            try:
                total += os.path.getsize(_path("travaux", job_id, name))
            except OSError:
                pass
    return total / (1024 * 1024)


def jobs_per_machine():
    """{id: travaux pris et pas finis + travaux qui lui sont confies et pas encore pris}."""
    count = {}
    for job_id in _list("travaux"):
        if termine(job_id):
            continue
        taken = _read(_path("travaux", job_id, "travail.pris.json"))
        if taken and taken.get("pris_par"):
            who = taken["pris_par"]
        else:
            a = attribution(job_id)
            who = a.get("pour") if a and a.get("jusqu", 0) > time.time() else None
        if who:
            count[who] = count.get(who, 0) + 1
    return count


def rank(kind, size_mb=0.0, exclude=()):
    """Machines presentes capables de kind, de celle qui finira le plus tot a la
    moins rapide ; chacune avec "fin_estimee" (s, ou None) et "part_libre"."""
    machines = [c for c in companions() if kind in c.get("capacites", []) and c["presente"]]
    load = jobs_per_machine()
    out = []
    for c in machines:
        if c["id"] in exclude:
            continue
        share = free_share(c)
        base = _full_power_seconds(c, kind, size_mb, machines)
        c = dict(c, part_libre=share)
        c["fin_estimee"] = (None if base is None or share is None
                            else base / share * (1 + load.get(c["id"], 0) / FILES_PAR_MACHINE))
        out.append(c)
    return sorted(out, key=lambda c: (c["fin_estimee"] is None, c["fin_estimee"] or 0))


def attribution(job_id):
    return _read(_path("travaux", job_id, "attribution.json"))


def attribute(job_id, c, tried, size_mb):
    _write(_path("travaux", job_id, "attribution.json"),
           {"pour": c["id"], "jusqu": time.time() + ATTRIBUTION_SEC, "essayees": tried, "taille_mo": size_mb,
            "part_libre": c.get("part_libre"), "fin_estimee": c.get("fin_estimee"), "t": time.time()})


def release(job_id):
    """Plus personne a qui le confier : le travail est ouvert a toute machine capable."""
    _remove(_path("travaux", job_id, "attribution.json"))


def learn_duration(cid, kind, seconds, size_mb, share):
    """Duree reelle d'un travail, ramenee a pleine puissance (duree x part libre
    au moment ou il a ete confie), en moyenne glissante par type."""
    card = trusted().get(cid)
    if not card or not kind:
        return
    full = max(float(seconds or 0), 0.5) * (share if share else 1.0)
    durees = card.get("durees") or {}
    d = dict(durees.get(kind) or {})
    a = APPRENTISSAGE if d else 1.0
    d["s"] = round((1 - a) * d.get("s", full) + a * full, 2)
    if size_mb and size_mb > 0.05:
        per_mb = full / size_mb
        a_mb = APPRENTISSAGE if d.get("s_mo") else 1.0
        d["s_mo"] = round((1 - a_mb) * d.get("s_mo", per_mb) + a_mb * per_mb, 3)
    d["n"] = d.get("n", 0) + 1
    durees[kind] = d
    update_card(cid, durees=durees)


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
        if name.startswith(("reponse-", "refus-", "confirme-")):
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


def confirmed_by_companion(answer):
    """La personne a valide le code sur la machine compagne (message signe
    par sa cle, pour la cle de ce Pi) : le Pi suit sans attendre Entree."""
    msg = _read(_path("appairage", f"confirme-{answer['id']}.json"))
    return bool(msg and msg.get("pi") == pi_id() and msg.get("compagnon") == answer["id"]
                and msg.get("cle_pi") == proto.public_b64(key()) and proto.verify(answer["cle"], msg))


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


def _oubli_valide(card, msg):
    """Retrait signe par la machine : objet v2 (cle d'identite v2 de la fiche) ou
    message v1 (cle v1 de la fiche)."""
    if card.get("v2") and isinstance(msg.get("charge"), dict):
        v2 = _pont_v2()
        if not v2:
            return False
        try:
            c = v2.np_.verifier_objet(msg, card["v2"]["identite"], "oubli", v2.parc())
        except Exception:
            return False
        return c.get("pi") == pi_id()
    return bool(card.get("cle")) and msg.get("pi") == pi_id() and proto.verify(card["cle"], msg, max_age=None)


def read_revocations():
    """Ponts retires depuis la machine compagne (message signe par elle) : [(id, nom)]."""
    cards = trusted()
    gone = []
    for name in _list("revocations"):
        path = _path("revocations", name)
        msg = _read(path) or {}
        cid = msg.get("compagnon")
        if cid not in cards and isinstance(msg.get("charge"), dict):  # oubli v2 : objet signe par la machine
            cid = msg["charge"].get("compagnon")
        if cid in cards and _oubli_valide(cards[cid], msg):
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


def _pont_v2():
    """Module lib/pont_v2 s'il est utilisable (import tardif : il importe ce module)."""
    try:
        import pont_v2
    except ImportError:
        return None
    return pont_v2 if pont_v2.actif() else None


def submit(job):
    """Publie le travail. Avec la v2, d'abord l'objet signe (travail.v2.json) : les
    machines migrees ne prennent que lui ; travail.json reste pour le code du Pi et
    pour les machines v1."""
    v2 = _pont_v2()
    if v2:
        try:
            v2.signer_travail(job, _path("travaux", job["id"]))
        except Exception as exc:  # une machine migree ne prendra pas ce travail
            print(f"[compagnon] travail {job['id']} non signe en v2 : {exc}")
    _write(_path("travaux", job["id"], "travail.json"), job)


def open_jobs():
    """Travaux deposes et pas encore pris : [travail]."""
    jobs = (_read(_path("travaux", n, "travail.json")) for n in sorted(_list("travaux")))
    return [j for j in jobs if j]


_refus_resultat = set()  # travaux dont le resultat a ete refuse (journalise une fois)
RESULTATS = ("resultat.v2.json", "resultat.json")


def termine(job_id):
    """Un resultat est depose (v2, ou v1 d'une machine pas migree) : le travail n'attend plus."""
    return any(os.path.exists(_path("travaux", job_id, n)) for n in RESULTATS)


def lire_travail(folder):
    """Le travail tel que le Pi l'a SIGNE (travail.v2.json, verifie avec notre cle v2) ;
    sans v2 seulement, la copie non signee (travail.json, puis travail.pris.json).
    Un travail.v2.json present mais invalide rend None : jamais de repli sur la copie."""
    v2 = _pont_v2()
    if v2 and os.path.exists(os.path.join(folder, "travail.v2.json")):
        try:
            return v2.travail_signe(folder)
        except Exception as exc:
            print(f"[compagnon] travail {os.path.basename(folder)} : objet signe refuse ({exc})")
            return None
    return _read(os.path.join(folder, "travail.json")) or _read(os.path.join(folder, "travail.pris.json"))


def _machine_v2(signataire, cards):
    for cid, card in cards.items():
        if (card.get("v2") or {}).get("id") == signataire:
            return cid, card
    return None, None


def finished_jobs():
    """Travaux termines, signes par une machine de confiance : [(travail, resultat, dossier)].
    La signature couvre l'etat et l'empreinte de chaque fichier rendu. Une machine migree
    ne rend que resultat.v2.json (signe par sa cle v2, lie a CE travail signe par nous)."""
    cards = trusted()
    found = []
    for job_id in sorted(_list("travaux")):
        folder = _path("travaux", job_id)
        objet = _read(os.path.join(folder, "resultat.v2.json"))
        if objet:
            cid, card = _machine_v2(objet.get("signataire"), cards)
            v2 = _pont_v2()
            try:
                if not card:
                    raise RuntimeError("signe par une machine inconnue")
                if not v2:
                    raise RuntimeError("module nearfield_pont absent")
                r = v2.resultat_signe(card, job_id, folder)
                job = lire_travail(folder)
                if not job:
                    raise RuntimeError("travail signe illisible")
            except Exception as exc:
                if job_id not in _refus_resultat:
                    _refus_resultat.add(job_id)
                    print(f"[compagnon] resultat de {job_id} refuse : {exc}")
                continue
            found.append((job, {"type": "resultat", "travail": job_id, "compagnon": cid, "etat": r["etat"],
                                "message": r["message"], "duree": r["duree"], "sorties": r["fichiers"]}, folder))
            continue
        result = _read(os.path.join(folder, "resultat.json"))
        job = _read(os.path.join(folder, "travail.json")) or _read(os.path.join(folder, "travail.pris.json"))
        if not result or not job or result.get("travail") != job_id:
            continue
        card = cards.get(result.get("compagnon"))
        if not card or card.get("v2") or not card.get("cle") or not proto.verify(card["cle"], result, max_age=None):
            continue  # pas d'une machine appairee, ou machine migree (seul son resultat v2 vaut)
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
        if termine(job_id):
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
    return sum(1 for j in _list("travaux") if not termine(j))


def _hm(t):
    return time.strftime("%H h %M", time.localtime(t))


def _session_text(c):
    """Ecran verrouille ou non (liaison directe), sinon depuis quand elle se tait
    (une machine qui ne repond plus est presque toujours en veille)."""
    etat = c.get("etat") or {}
    if c.get("directe") and "verrouille" in etat:
        if etat["verrouille"]:
            since = c.get("verrou_depuis")
            return (dire("compagnon.ecran_verrouille_depuis", heure=_hm(since)) if since else dire("compagnon.ecran_verrouille"))
        idle = etat.get("inactif_s")
        if idle is not None and idle >= 120:
            return dire("compagnon.ecran_libre_inactif", minutes=nombre(idle // 60, "minute"))
        return dire("compagnon.ecran_libre")
    if not c.get("presente") and c.get("vu"):
        return dire("compagnon.silencieuse", heure=_hm(c["vu"]))
    return ""


def status():
    """Commande "compagnon" : etat du pont."""
    found = companions()
    if not found:
        return (dire("compagnon.aucune_machine_compagne_appairee"))
    parts = []
    for c in found:
        caps = ", ".join(c.get("capacites", [])) or dire("compagnon.rien_declare")
        stats = c.get("stats", {})
        state = (dire("compagnon.connectee") if c.get("directe") else dire("compagnon.differe_nas_deux_fois") if c["presente"]
                 else dire("compagnon.connectee_reveillerai_au_besoin"))
        text = dire("compagnon.fiche", nom=c['nom'], etat=state, capacites=caps)
        session = _session_text(c)
        if session:
            text += ", " + session
        share = free_share(c) if c.get("presente") else None
        if share is not None:
            text += ", " + dire("compagnon.part_libre", pourcent=round(share * 100))
        done, failed = stats.get("faits", 0), stats.get("echecs", 0)
        if done or failed:
            text += ", " + dire("compagnon.bilan_travaux", reussis=nombre(done, "travail réussi", "travaux réussis"),
                                echecs=failed)
        parts.append(text)
    pending = pending_count()
    return (dire("compagnon.machines", machines=" ; ".join(parts))
            + (" " + dire("compagnon.en_cours", travaux=nombre(pending, "travail en cours", "travaux en cours"))
               if pending else ""))
