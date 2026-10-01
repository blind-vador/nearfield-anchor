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
import pont_protocole as proto

NOTIFY_EVERY_SEC = 600
TICK_SEC = 5
POLL_SEC = 60
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
            with _lock:
                old = _channels.pop(cid, None)
                _channels[cid] = channel
                _presence[cid] = {"depuis": time.time(), "vu": time.time()}
                _save_presence()
            if old:
                old.close()
            print(f"[compagnon] {hello.get('nom')} connectee depuis {self.client_address[0]}")
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
                elif kind == "pris":
                    print(f"[compagnon] travail {msg.get('travail')} pris par {card.get('nom')}")
                elif kind == "fini":
                    _results_ready.set()
                if not compagnon.trusted().get(cid):
                    break  # pont retire pendant la connexion
        except (OSError, ValueError) as exc:
            print(f"[compagnon] liaison {cid} : {exc!r}")
        finally:
            with _lock:
                if _channels.get(cid) is channel:
                    del _channels[cid]
                    _presence.pop(cid, None)
                    _save_presence()
            channel.close()


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
    for name in compagnon.read_revocations():
        print(f"[compagnon] pont retire par {name}")
    for job in compagnon.open_jobs():
        if time.time() - _notified.get(job["id"], 0) < NOTIFY_EVERY_SEC:
            continue
        able = [c for c in compagnon.companions() if job["type"] in c.get("capacites", [])]
        if any(_notify(c["id"], job["id"]) for c in able if c["presente"]):
            continue
        for c in able:
            if proto.wake_on_lan(c.get("adresse_materielle"), c.get("ips", [])):
                print(f"[compagnon] reveil de {c['nom']} pour {job['id']}")
        _notified[job["id"]] = time.time()


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
            _dispatch()
            if _results_ready.is_set() or time.time() - last_poll > POLL_SEC:
                _results_ready.clear()
                last_poll = time.time()
                compagnon_resultats.poll_once()
        except Exception as exc:
            print(f"[compagnon] {exc!r}")
        time.sleep(TICK_SEC)
