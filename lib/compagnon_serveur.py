"""Liaison directe avec les machines compagnes (thread de meteo_watch).

Chaque machine appairee se connecte au Pi (port pont_protocole.PORT) et se
presente avec un message signe ; le Pi repond signe aussi (les deux se
reconnaissent par leurs cles echangees a l'appairage). Ensuite :
  Pi -> machine   travail <id>   un travail l'attend sur le NAS
  machine -> Pi   battement      toutes les 30 s (stats)
                  pris / fini    travail commence / termine
Travail sans machine connectee : paquet de reveil (Wake-on-LAN) toutes les
10 minutes, la machine se reconnecte en se reveillant. Les resultats sont
relus des qu'une machine dit "fini" (et chaque minute par securite).
"""
import json
import os
import socketserver
import threading
import time

import compagnon
import compagnon_resultats
import lora_messages
import pont_protocole as proto

NOTIFY_EVERY_SEC = 15  # re-avis rapide si le Mac a rate le claim SMB
FORCE_NOTIFY_SPOOL = "/tmp/compagnon_force_notify"  # une ligne = job id (IPC console -> veille)
TICK_SEC = 2
# Resultats : relus aussitot sur l'avis "fini" de la liaison directe ; sinon
# (mode differe par le NAS) toutes les 12 h, pour ne pas solliciter les disques.
POLL_SEC = 8  # appliquer les resultats decrire_images sans attendre 12 h
READ_TIMEOUT_SEC = 120

_lock = threading.Lock()
_channels = {}  # id -> Channel
_presence = {}  # id -> {depuis, vu}
_notified = {}  # travail -> heure du dernier avis ou reveil
_results_ready = threading.Event()


def _save_presence():
    tmp = compagnon.PRESENCE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_presence, f)
    os.replace(tmp, compagnon.PRESENCE)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(READ_TIMEOUT_SEC)
        channel = proto.Channel(self.request, compagnon.key())
        cid = None
        try:
            hello = json.loads(channel.reader.readline(proto.MAX_LINE) or b"{}")
            cid = hello.get("compagnon")
            card = compagnon.trusted().get(cid)
            if (not card or hello.get("type") != "bonjour" or hello.get("pi") != compagnon.pi_id()
                    or not proto.verify(card["cle"], hello)):
                print(f"[compagnon] connexion refusee ({self.client_address[0]}, {cid})")
                return
            channel.send({"type": "bienvenue", "pi": compagnon.pi_id(), "reponse_a": hello.get("nonce")})
            compagnon.update_card(cid, nom=hello.get("nom", card.get("nom")), capacites=hello.get("capacites", []),
                                  adresse_materielle=hello.get("adresse_materielle") or card.get("adresse_materielle"),
                                  ips=hello.get("ips") or [self.client_address[0]], vu=time.time())
            compagnon.note_state(cid, hello.get("etat"))
            with _lock:
                old = _channels.pop(cid, None)
                _channels[cid] = channel
                _presence[cid] = {"depuis": time.time(), "vu": time.time()}
                _save_presence()
            if old:
                old.close()
            print(f"[compagnon] {hello.get('nom')} connectee depuis {self.client_address[0]}")
            _results_ready.set()  # un "fini" a pu se perdre pendant la coupure
            for job in compagnon.open_jobs():  # ce qui l'attendait
                _notify(cid, job["id"])
            while True:
                msg = channel.receive(card["cle"])
                if msg is None:
                    break
                with _lock:
                    _presence[cid]["vu"] = time.time()
                    _save_presence()
                kind = msg.get("type")
                if kind == "battement":
                    compagnon.update_card(cid, vu=time.time(), capacites=msg.get("capacites", []))
                    compagnon.note_state(cid, msg.get("etat"))
                elif kind == "pris":
                    print(f"[compagnon] travail {msg.get('travail')} pris par {card.get('nom')}")
                elif kind == "fini":
                    _results_ready.set()
                if not compagnon.trusted().get(cid):
                    break  # pont retire pendant la connexion
        except TimeoutError:
            # Plus aucun battement depuis READ_TIMEOUT_SEC : la machine dort (veille
            # demandee sur le Mac, 2026-10-14) ; elle se reconnecte en se reveillant.
            print(f"[compagnon] {cid} silencieuse depuis {READ_TIMEOUT_SEC} s : en veille probablement")
        except (OSError, ValueError) as exc:
            print(f"[compagnon] liaison {cid} : {exc!r}")
        finally:
            with _lock:
                if _channels.get(cid) is channel:
                    del _channels[cid]
                    _presence.pop(cid, None)
                    _save_presence()
            channel.close()


def force_notify(job_id):
    """Re-envoie l'avis TCP tout de suite (apres depot d'un travail images).

    Appelable depuis la console (autre process) : ecrit un spool lu par la
    veille meteo qui detient les canaux TCP. Si on est deja dans la veille,
    notifie directement.
    """
    _notified.pop(job_id, None)
    ok = False
    with _lock:
        has_channels = bool(_channels)
    if has_channels:
        job = next((j for j in compagnon.open_jobs() if j["id"] == job_id), None)
        kind = (job or {}).get("type")
        targets = [c for c in compagnon.companions()
                   if c.get("presente") and (not kind or kind in (c.get("capacites") or []))]
        for c in targets:
            if _notify(c["id"], job_id):
                print(f"[compagnon] avis force {job_id} -> {c.get('nom')}")
                ok = True
    # Spool toujours : la veille relayera meme si on a deja notifie (idempotent)
    try:
        with open(FORCE_NOTIFY_SPOOL, "a") as f:
            f.write(f"{job_id}\n")
    except OSError as exc:
        print(f"[compagnon] spool force_notify : {exc!r}")
    if not ok and not has_channels:
        print(f"[compagnon] avis force {job_id} : delegate a la veille (spool)")
    elif not ok:
        print(f"[compagnon] avis force {job_id} : aucune machine presente")
    return ok


def _drain_force_notify():
    """Relais IPC : console -> veille (canaux TCP)."""
    try:
        with open(FORCE_NOTIFY_SPOOL) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"[compagnon] lecture spool : {exc!r}")
        return
    try:
        os.remove(FORCE_NOTIFY_SPOOL)
    except OSError:
        pass
    for job_id in dict.fromkeys(lines):  # dedupe, garde l'ordre
        _notified.pop(job_id, None)
        job = next((j for j in compagnon.open_jobs() if j["id"] == job_id), None)
        if not job:
            continue
        kind = job.get("type")
        targets = [c for c in compagnon.companions()
                   if c.get("presente") and (not kind or kind in (c.get("capacites") or []))]
        sent = False
        for c in targets:
            if _notify(c["id"], job_id):
                print(f"[compagnon] avis spool {job_id} -> {c.get('nom')}")
                sent = True
        if not sent:
            # Pas de TCP : tente reveil WoL
            able = [c for c in compagnon.companions() if kind in c.get("capacites", [])]
            for c in able:
                if proto.wake_on_lan(c.get("adresse_materielle"), c.get("ips", [])):
                    print(f"[compagnon] reveil (spool) de {c['nom']} pour {job_id}")
                    sent = True
            if not sent:
                print(f"[compagnon] spool {job_id} : personne a prevenir")


def _notify(cid, job_id):
    channel = _channels.get(cid)
    if not channel:
        return False
    try:
        with _lock:
            channel.send({"type": "travail", "travail": job_id})
        _notified[job_id] = time.time()
        return True
    except OSError:
        return False


def _dispatch():
    """Previent une machine capable de chaque travail en attente, ou la reveille."""
    for cid, name in compagnon.read_revocations():
        print(f"[compagnon] pont retire par {name}")
        channel = _channels.get(cid)
        if channel:
            channel.close()  # la liaison tombe tout de suite
    for job in compagnon.open_jobs():
        able = [c for c in compagnon.companions() if job["type"] in c.get("capacites", [])]
        # LoRa : seule une liaison DIRECTE repond assez vite ; en differe, le Pi decode.
        if job["type"] == "lora" and lora_messages.needs_local(job, any(c.get("directe") for c in able)):
            # Secours : aucune machine capable ne repond, le Pi decode lui-meme.
            if job["id"] not in _local:
                _local.add(job["id"])
                threading.Thread(target=_decode_locally, args=(job,), daemon=True).start()
            continue
        if time.time() - _notified.get(job["id"], 0) < NOTIFY_EVERY_SEC:
            continue
        if any(_notify(c["id"], job["id"]) for c in able if c["presente"]):
            continue
        if any(c["presente"] for c in able):
            _notified[job["id"]] = time.time()  # presente par le NAS : elle releve d'elle-meme
            continue
        for c in able:
            if proto.wake_on_lan(c.get("adresse_materielle"), c.get("ips", [])):
                print(f"[compagnon] reveil de {c['nom']} pour {job['id']}")
        _notified[job["id"]] = time.time()


_local = set()


def _decode_locally(job):
    try:
        lora_messages.decode_locally(job)
    except Exception as exc:
        print(f"[compagnon] secours LoRa {job['id']} : {exc!r}")


def run():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer(("0.0.0.0", proto.PORT), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with _lock:
        _save_presence()  # personne de connecte au demarrage
    last_poll = 0
    while True:
        try:
            _drain_force_notify()
            _dispatch()
            if _results_ready.is_set() or time.time() - last_poll > POLL_SEC:
                _results_ready.clear()
                last_poll = time.time()
                compagnon_resultats.poll_once()
        except Exception as exc:
            print(f"[compagnon] {exc!r}")
        time.sleep(TICK_SEC)
