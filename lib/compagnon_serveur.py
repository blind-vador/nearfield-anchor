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

Voix macOS en direct (2026-10-22) : les programmes du Pi qui parlent
demandent une phrase par la prise locale VOIX_MAC_SOCKET (une ligne JSON
{texte, voix, debit}) ; on la transmet ("dire") a une machine connectee qui
a cette voix, et le son revient en morceaux ("son", PCM 16 bits mono en
base64, puis "son_fin"), relaye tel quel sur la prise au fil de l'eau.
Rien d'ecrit sur le NAS. Pas de machine, ou rien sous VOIX_MAC_DELAI_SEC :
la prise se ferme sans son, l'appelant passe a la voix suivante.

Protocole v2 (2026-11-15, lib/pont_v2.py) : une machine migree ouvre en plus une
session chiffree (Noise). Tout envoi passe par _envoyer : la v2 d'abord, la v1
en secours ; tout message recu, par l'une ou l'autre, passe par _traiter.

v2 seule (2026-11-16) : une machine qui se connecte en v2 sans liaison v1 est
"autonome" ; un CanalV2 tient alors sa place dans _channels, et tout le reste
(avis, voix, TETRA, numerique) marche sans changement. L'ecoute v1 (port 47811)
ne tourne plus que tant qu'une machine de confiance n'est pas autonome.
"""
import base64
import queue
import uuid
import json
import os
import socketserver
import tempfile
import threading
import time
import wave

import compagnon
import compagnon_resultats
import lora_messages
import pont_protocole as proto
import pont_v2

NOTIFY_EVERY_SEC = 15  # re-avis rapide si le Mac a rate le claim SMB
FORCE_NOTIFY_SPOOL = "/tmp/compagnon_force_notify"  # une ligne = job id (IPC console -> veille)
TICK_SEC = 2
# Resultats : relus aussitot sur l'avis "fini" de la liaison directe ; sinon
# (mode differe par le NAS) toutes les 12 h, pour ne pas solliciter les disques.
POLL_SEC = 8  # appliquer les resultats decrire_images sans attendre 12 h
READ_TIMEOUT_SEC = 120
VOIX_MAC_SOCKET = "/tmp/voix_mac.sock"
VOIX_MAC_DELAI_SEC = 15      # premier morceau
VOIX_MAC_ENTRE_SEC = 10      # entre deux morceaux
TETRA_SOCKET = "/tmp/tetra_pont.sock"   # radio_watch demande un decodage TETRA en direct
TETRA_DELAI_SEC = 90         # le decodage dure "secondes" + marge reseau/demarrage
NUMERIQUE_SOCKET = "/tmp/numerique_pont.sock"   # radio_watch delegue un decodage DMR/P25... (offload)
NUMERIQUE_DELAI_SEC = 90

_lock = threading.Lock()
_channels = {}  # id -> Channel
_presence = {}  # id -> {depuis, vu}
_notified = {}  # travail -> heure du dernier avis ou reveil
_results_ready = threading.Event()
_demandes_voix = {}  # id de "dire" -> queue des morceaux recus
_demandes_tetra = {}  # id de "tetra" -> queue (voix + resultat recus)
_demandes_numerique = {}  # id de "numerique" -> queue (voix + resultat recus)
V1_VERIF_SEC = 60  # ecoute v1 : ouverte ou fermee selon les fiches, revu chaque minute


class CanalV2:
    """Tient la place d'une liaison v1 pour une machine connectee en v2 seulement."""

    def __init__(self, cid):
        self.cid = cid

    def send(self, msg):
        if not pont_v2.envoyer(self.cid, msg):
            raise OSError("session v2 fermee ou message refuse")

    def close(self):
        pass


def _evenement_v2(kind, cid, card, ip):
    """Arrivee / depart d'une session v2 (lib/pont_v2). Avec une liaison v1 ouverte a
    cote (machine pas encore autonome), c'est la v1 qui tient la presence."""
    if kind == "arrivee":
        with _lock:
            actuel = _channels.get(cid)
            if actuel is not None and not isinstance(actuel, CanalV2):
                return
            _channels[cid] = CanalV2(cid)
            _presence[cid] = {"depuis": time.time(), "vu": time.time()}
            _save_presence()
        compagnon.update_card(cid, vu=time.time(), ips=[ip] if ip else (card.get("ips") or []), v2_autonome=time.time())
        if actuel is None and time.time() - _sons_vus.get(("parti", cid), 0) > DECONNEXION_SEC:
            _son("mac_connecte")
        print(f"[compagnon] {card.get('nom')} connectee en v2 seule depuis {ip}")
        _results_ready.set()
        for job in compagnon.open_jobs():
            _notify(cid, job["id"])
    elif kind == "depart":
        with _lock:
            if not isinstance(_channels.get(cid), CanalV2):
                return
            del _channels[cid]
            _presence.pop(cid, None)
            _save_presence()
            _sons_vus[("parti", cid)] = time.time()
            minuteur = threading.Timer(DECONNEXION_SEC, _parti, args=(cid,))
            minuteur.daemon = True
            minuteur.start()


def _save_presence():
    tmp = compagnon.PRESENCE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_presence, f)
    os.replace(tmp, compagnon.PRESENCE)


# Sons du pont (2026-10-23, sons.py) : Mac connecte, Mac endormi ou parti
# (apres DECONNEXION_SEC sans retour : une reconnexion rapide ne sonne pas),
# connexion refusee (une fois par adresse et par heure).
DECONNEXION_SEC = 30
_sons_vus = {}


def _son(evenement, cle=None, ecart=0):
    if cle is not None:
        if time.time() - _sons_vus.get(cle, 0) < ecart:
            return
        _sons_vus[cle] = time.time()

    def jouer():
        try:
            import audio_lock
            import sons
            from sound_player import play_sound_effect
            chemin = sons.chemin(evenement)
            if not chemin:
                return
            lock = audio_lock.acquire()
            try:
                play_sound_effect(chemin, timeout_sec=60, label="compagnon")
            finally:
                audio_lock.release(lock)
        except Exception as exc:
            print(f"[compagnon] son {evenement} : {exc!r}")
    threading.Thread(target=jouer, daemon=True).start()


def _parti(cid):
    with _lock:
        revenu = cid in _channels
    if not revenu:
        _son("mac_deconnecte")


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
                _son("pont_intrus", cle=("intrus", self.client_address[0]), ecart=3600)
                return
            channel.send({"type": "bienvenue", "pi": compagnon.pi_id(), "reponse_a": hello.get("nonce")})
            compagnon.update_card(cid, nom=hello.get("nom", card.get("nom")), capacites=hello.get("capacites", []),
                                  adresse_materielle=hello.get("adresse_materielle") or card.get("adresse_materielle"),
                                  ips=hello.get("ips") or [self.client_address[0]], vu=time.time(),
                                  voix_mac=hello.get("voix_mac") or [])
            compagnon.note_state(cid, hello.get("etat"))
            compagnon.note_power(cid, hello.get("puissance"))
            with _lock:
                old = _channels.pop(cid, None)
                _channels[cid] = channel
                _presence[cid] = {"depuis": time.time(), "vu": time.time()}
                _save_presence()
            if old:
                old.close()
            elif time.time() - _sons_vus.get(("parti", cid), 0) > DECONNEXION_SEC:
                _son("mac_connecte")  # vraie arrivee, pas une reconnexion eclair
            print(f"[compagnon] {hello.get('nom')} connectee depuis {self.client_address[0]}")
            # Pilote v2 (lib/pont_v2.py) : seulement si le module est installe ici ET
            # que la machine l'annonce ; sinon rien ne change pour la v1.
            v2 = pont_v2.actif() and hello.get("v2") is True
            if v2:
                try:
                    with _lock:
                        channel.send({"type": "migration_v2", "objet": pont_v2.objet_migration()})
                except Exception as exc:
                    print(f"[pont v2] annonce de migration impossible : {exc!r}")
                    v2 = False
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
                if msg.get("type") == "migration_v2" and v2:
                    try:
                        cert = pont_v2.recevoir_migration(cid, compagnon.trusted().get(cid) or card,
                                                          msg.get("objet"))
                        reponse = {"type": "certificat_v2", "objet": cert, "port": pont_v2.PORT}
                        print(f"[pont v2] {card.get('nom')} migree, certificat v2 delivre")
                    except Exception as exc:
                        print(f"[pont v2] migration de {card.get('nom')} refusee : {exc}")
                        _son("pont_intrus", cle=("migration", cid), ecart=3600)
                        reponse = {"type": "migration_v2_refusee", "raison": str(exc)[:200]}
                    with _lock:
                        channel.send(reponse)
                else:
                    _traiter(cid, card, msg)
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
                    _sons_vus[("parti", cid)] = time.time()
                    minuteur = threading.Timer(DECONNEXION_SEC, _parti, args=(cid,))
                    minuteur.daemon = True
                    minuteur.start()
            channel.close()


def _traiter(cid, card, msg):
    """Un message de la machine, arrive par la v1 ou par la v2 (deja traduit)."""
    kind = msg.get("type")
    if kind == "battement":
        compagnon.note_battement(cid, msg.get("capacites", []), msg.get("voix_mac") or [],
                                 msg.get("etat"), msg.get("puissance"))
        reseau = msg.get("reseau")
        if isinstance(reseau, dict):  # v2 : de quoi la reveiller (Wake-on-LAN)
            ips = [i for i in reseau.get("ips") or [] if isinstance(i, str) and len(i) <= 64][:8]
            mac = reseau.get("adresse_materielle")
            champs = {"ips": ips} if ips else {}
            if isinstance(mac, str) and len(mac) <= 32:
                champs["adresse_materielle"] = mac
            if champs and (card.get("ips") != champs.get("ips", card.get("ips"))
                           or card.get("adresse_materielle") != champs.get("adresse_materielle", card.get("adresse_materielle"))):
                compagnon.update_card(cid, **champs)
        with _lock:
            if cid in _presence:
                _presence[cid]["vu"] = time.time()
                _save_presence()
    elif kind in ("son", "son_fin"):
        file = _demandes_voix.get(msg.get("reponse_a"))
        if file is not None:
            file.put(msg)
    elif kind in ("tetra_voix", "tetra_resultat"):
        file = _demandes_tetra.get(msg.get("reponse_a"))
        if file is not None:
            file.put(msg)
    elif kind in ("numerique_voix", "numerique_audio", "numerique_flux", "numerique_resultat") \
            or str(kind).startswith("numerique_banc_") \
            or str(kind).startswith("numerique_rendu_"):
        file = _demandes_numerique.get(msg.get("reponse_a"))
        if file is not None:
            file.put(msg)
    elif kind == "pris":
        print(f"[compagnon] travail {msg.get('travail')} pris par {card.get('nom')}")
    elif kind == "fini":
        _results_ready.set()


def _envoyer(cid, channel, msg):
    """Vers une machine : par sa session v2 si elle en a une (liste fermee, chiffre),
    sinon (ou si la v2 refuse ce message) par la liaison v1."""
    if pont_v2.actif() and pont_v2.envoyer(cid, msg):
        return
    channel.send(msg)


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
            _envoyer(cid, channel, {"type": "travail", "travail": job_id})
        _notified[job_id] = time.time()
        return True
    except OSError:
        return False


def _signal_tickets():
    """Du neuf sur les tickets (tickets_pi.signaler, autre process) : on previent
    les machines par la liaison, elles n'ont pas a relire le NAS sans arret."""
    encours = "/tmp/tickets_signal.encours"
    try:
        os.replace("/tmp/tickets_signal", encours)
        with open(encours) as f:
            ids = sorted({ln.strip() for ln in f if ln.strip()})
    except OSError:
        return
    with _lock:
        for cid, channel in list(_channels.items()):
            try:
                _envoyer(cid, channel, {"type": "tickets", "ids": ids})
            except OSError:
                pass


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
        if _attribute(job):
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


def _attribute(job):
    """Confie le travail a la machine connectee qui le finira le plus tot
    (compagnon.rank, 2026-10-25) et ne previent qu'elle. Elle a
    ATTRIBUTION_SEC pour le prendre ; sinon on passe a la suivante, et quand
    toutes ont eu leur chance, le travail redevient ouvert a toutes (rend False :
    l'ancien envoi a toutes / reveil prend le relais)."""
    job_id = job["id"]
    a = compagnon.attribution(job_id)
    if a and a.get("jusqu", 0) > time.time():
        if time.time() - _notified.get(job_id, 0) >= NOTIFY_EVERY_SEC:
            _notify(a["pour"], job_id)
        return True
    tried = (a or {}).get("essayees", [])
    size = compagnon.job_size_mb(job_id)
    direct = [c for c in compagnon.rank(job["type"], size, exclude=tried) if c.get("directe")]
    if not direct:
        if a:
            compagnon.release(job_id)
        return False
    best = direct[0]
    compagnon.attribute(job_id, best, tried + [best["id"]], size)
    fin = best.get("fin_estimee")
    part = best.get("part_libre")
    print(f"[compagnon] {job_id} confie a {best.get('nom')}"
          + (f" (libre a {round(part * 100)} %" if part is not None else " (")
          + (f", fin estimee {fin:.0f} s)" if fin is not None else ")")
          + (f", {len(direct) - 1} autre(s) en reserve" if len(direct) > 1 else ""))
    _notify(best["id"], job_id)
    return True


_local = set()


def _decode_locally(job):
    try:
        lora_messages.decode_locally(job)
    except Exception as exc:
        print(f"[compagnon] secours LoRa {job['id']} : {exc!r}")


class _VoixMac(socketserver.StreamRequestHandler):
    """Une phrase en voix macOS : demande en JSON, son brut en retour."""

    def handle(self):
        try:
            req = json.loads(self.rfile.readline(65536) or b"{}")
        except ValueError:
            return
        voix = req.get("voix")
        with _lock:
            cibles = [(cid, ch) for cid, ch in _channels.items()
                      if voix in ((compagnon.trusted().get(cid) or {}).get("voix_mac") or [])]
        if not cibles or not (req.get("texte") or "").strip():
            return
        cid, channel = cibles[0]
        rid = uuid.uuid4().hex
        file = queue.Queue()
        _demandes_voix[rid] = file
        try:
            _envoyer(cid, channel, {"type": "dire", "id": rid, "texte": req["texte"], "voix": voix,
                          "debit": req.get("debit") or "+0%"})
            delai = VOIX_MAC_DELAI_SEC
            while True:
                msg = file.get(timeout=delai)
                if msg.get("type") == "son_fin":
                    if not msg.get("ok"):
                        print(f"[compagnon] voix mac {voix} : {msg.get('erreur')}")
                    return
                self.wfile.write(base64.b64decode(msg.get("pcm") or ""))
                self.wfile.flush()
                delai = VOIX_MAC_ENTRE_SEC
        except queue.Empty:
            print(f"[compagnon] voix mac {voix} : pas de reponse de {cid}")
        except (OSError, ValueError) as exc:
            if not isinstance(exc, BrokenPipeError):
                print(f"[compagnon] voix mac {voix} : {exc!r}")
        finally:
            _demandes_voix.pop(rid, None)


def _serveur_voix_mac():
    try:
        os.remove(VOIX_MAC_SOCKET)
    except OSError:
        pass
    socketserver.ThreadingUnixStreamServer.daemon_threads = True
    server = socketserver.ThreadingUnixStreamServer(VOIX_MAC_SOCKET, _VoixMac)
    os.chmod(VOIX_MAC_SOCKET, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()


class _TetraMac(socketserver.StreamRequestHandler):
    """Decodage TETRA en direct demande par radio_watch (qui sert deja le
    dongle par rtl_tcp). On choisit la machine compagne connectee la plus libre
    capable de "tetra", on lui transmet la demande par la liaison, on recupere
    l'eventuelle voix EN CLAIR (ecrite dans un wav temporaire) et les
    metadonnees reseau, puis on rend une ligne JSON a radio_watch. Jamais rien
    de dechiffre : on relaie seulement ce que la machine compagne a decode."""

    def handle(self):
        try:
            req = json.loads(self.rfile.readline(65536) or b"{}")
        except ValueError:
            return
        if not req.get("freq_hz"):
            self._rendre({"ok": False, "erreur": "demande invalide"})
            return
        with _lock:
            able = [(cid, ch) for cid, ch in _channels.items()
                    if "tetra" in ((compagnon.trusted().get(cid) or {}).get("capacites") or [])]
        if not able:
            self._rendre({"ok": False, "erreur": "aucune machine compagne pour le tetra"})
            return
        ordre = [c["id"] for c in compagnon.rank("tetra")]      # la plus libre d'abord
        able.sort(key=lambda t: ordre.index(t[0]) if t[0] in ordre else 99)
        cid, channel = able[0]
        rid = uuid.uuid4().hex
        file = queue.Queue()
        _demandes_tetra[rid] = file
        tmp = tempfile.mkdtemp(prefix="tetra_pi_")
        wav = os.path.join(tmp, "voix.wav")
        wout = None
        handed = False
        secondes = int(req.get("secondes", 12))
        max_secondes = max(secondes, int(req.get("max_secondes") or secondes))  # suivi d'appel (2026-10-27)
        try:
            _envoyer(cid, channel, {"type": "tetra", "id": rid,
                          "host": req.get("host") or compagnon._local_ip(),
                          "port": int(req.get("port", 1234)), "freq_hz": int(req["freq_hz"]),
                          "gain": req.get("gain", 32), "secondes": secondes, "max_secondes": max_secondes,
                          "ppm": req.get("ppm", 0)})
            delai = max_secondes + 30
            while True:
                msg = file.get(timeout=delai)
                if msg.get("type") == "tetra_voix":
                    if wout is None:
                        wout = wave.open(wav, "wb")
                        wout.setnchannels(1)
                        wout.setsampwidth(2)
                        wout.setframerate(msg.get("taux", 48000))
                    wout.writeframes(base64.b64decode(msg.get("pcm") or ""))
                    delai = TETRA_DELAI_SEC
                elif msg.get("type") == "tetra_resultat":
                    if wout is not None:
                        wout.close()
                        wout = None
                    res = {k: msg.get(k) for k in ("ok", "erreur", "reseau", "bsch", "sysinfo", "enc", "clair",
                                                   "mac_resource", "clip", "voix", "resume", "activite", "duree")}
                    res["voix_wav"] = wav if (msg.get("voix") and os.path.exists(wav)) else None
                    res["machine"] = (compagnon.trusted().get(cid) or {}).get("nom")
                    res["_tmp"] = tmp
                    handed = True
                    self._rendre(res)
                    return
        except queue.Empty:
            self._rendre({"ok": False, "erreur": "pas de reponse de la machine compagne"})
        except (OSError, ValueError) as exc:
            self._rendre({"ok": False, "erreur": repr(exc)[:200]})
        finally:
            _demandes_tetra.pop(rid, None)
            if wout is not None:
                try:
                    wout.close()
                except Exception:
                    pass
            if not handed:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)

    def _rendre(self, data):
        try:
            self.wfile.write((json.dumps(data) + "\n").encode())
            self.wfile.flush()
        except OSError:
            pass


def _serveur_tetra():
    try:
        os.remove(TETRA_SOCKET)
    except OSError:
        pass
    socketserver.ThreadingUnixStreamServer.daemon_threads = True
    server = socketserver.ThreadingUnixStreamServer(TETRA_SOCKET, _TetraMac)
    os.chmod(TETRA_SOCKET, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()


class _NumeriqueMac(socketserver.StreamRequestHandler):
    """Decodage numerique en direct (DMR/P25...) demande par radio_watch quand le
    Pi chauffe et DELEGUE (lib/offload.py). Le Pi sert deja son dongle par
    rtl_tcp ; on choisit la compagne connectee la plus libre capable de
    "numerique", on lui transmet la demande, on recupere l'eventuelle voix EN
    CLAIR (wav temporaire) et les metadonnees, et on rend une ligne JSON a
    radio_watch. Jamais rien de dechiffre. Calque de _TetraMac."""

    def handle(self):
        try:
            req = json.loads(self.rfile.readline(65536) or b"{}")
        except ValueError:
            return
        if not req.get("freq_hz"):
            self._rendre({"ok": False, "erreur": "demande invalide"})
            return
        with _lock:
            able = [(cid, ch) for cid, ch in _channels.items()
                    if "numerique" in ((compagnon.trusted().get(cid) or {}).get("capacites") or [])]
        if not able:
            self._rendre({"ok": False, "erreur": "aucune machine compagne pour le numerique"})
            return
        ordre = [c["id"] for c in compagnon.rank("numerique")]      # la plus libre d'abord
        able.sort(key=lambda t: ordre.index(t[0]) if t[0] in ordre else 99)
        cid, channel = able[0]
        rid = uuid.uuid4().hex
        file = queue.Queue()
        _demandes_numerique[rid] = file
        tmp = tempfile.mkdtemp(prefix="numerique_pi_")
        # Deux flux possibles d'une demod complete : voix NUMERIQUE en clair (-w)
        # ET audio ANALOGIQUE FM (-6). On ecrit chacun dans son wav.
        chemins = {"numerique_voix": os.path.join(tmp, "voix.wav"),
                   "numerique_audio": os.path.join(tmp, "audio.wav"),
                   "numerique_flux": os.path.join(tmp, "flux.wav")}
        wouts = {}
        handed = False
        secondes = int(req.get("secondes", 8))
        max_secondes = max(secondes, int(req.get("max_secondes") or secondes))

        def _ecrire(msg):
            w = wouts.get(msg["type"])
            if w is None:
                if msg["type"].startswith("numerique_banc_"):   # un son par modulation du banc
                    chemins[msg["type"]] = os.path.join(tmp, "banc_%s.wav" % msg["type"][len("numerique_banc_"):])
                elif msg["type"].startswith("numerique_rendu_"):
                    chemins[msg["type"]] = os.path.join(tmp, "rendu_%s.wav" % msg["type"][len("numerique_rendu_"):])
                w = wave.open(chemins[msg["type"]], "wb")
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(msg.get("taux", 48000))
                wouts[msg["type"]] = w
            w.writeframes(base64.b64decode(msg.get("pcm") or ""))

        try:
            _envoyer(cid, channel, {"type": "numerique", "id": rid,
                          "host": req.get("host") or compagnon._local_ip(),
                          "port": int(req.get("port", 1234)), "freq_hz": int(req["freq_hz"]),
                          "gain": req.get("gain", 32), "secondes": secondes, "max_secondes": max_secondes,
                          "ppm": req.get("ppm", 0), "mode": req.get("mode", "-fa"),
                          "banc": bool(req.get("banc")), "modes": req.get("modes") or None})
            delai = max_secondes + 30
            while True:
                msg = file.get(timeout=delai)
                if msg.get("type") in ("numerique_voix", "numerique_audio", "numerique_flux") \
                        or str(msg.get("type")).startswith("numerique_banc_") \
                        or str(msg.get("type")).startswith("numerique_rendu_"):
                    _ecrire(msg)
                    delai = NUMERIQUE_DELAI_SEC
                elif msg.get("type") == "numerique_resultat":
                    for w in wouts.values():
                        try:
                            w.close()
                        except Exception:
                            pass
                    wouts.clear()
                    res = {k: msg.get(k) for k in ("ok", "erreur", "protocole", "meta", "chiffre", "clair",
                                                   "voix", "audio", "numerique_confirme", "digital_actif",
                                                   "sync_hits", "voix_rms", "audio_rms", "resume", "duree", "preuve")}
                    res["voix_wav"] = chemins["numerique_voix"] if (
                        msg.get("voix") and os.path.exists(chemins["numerique_voix"])) else None
                    res["audio_wav"] = chemins["numerique_audio"] if (
                        msg.get("audio") and os.path.exists(chemins["numerique_audio"])) else None
                    res["flux_wav"] = chemins["numerique_flux"] if (
                        msg.get("flux") and os.path.exists(chemins["numerique_flux"])) else None
                    res["banc"] = {m: chemins["numerique_banc_" + m] for m in (msg.get("banc") or [])
                                   if os.path.exists(chemins.get("numerique_banc_" + m, ""))}
                    res["banc_preuve"] = msg.get("banc_preuve")
                    rendus = []
                    for item in msg.get("rendus") or []:
                        if not isinstance(item, dict):
                            continue
                        mode = str(item.get("mode") or "")
                        kind = "numerique_rendu_" + mode.lstrip("-")
                        wav = chemins.get(kind)
                        if item.get("voix") and wav and os.path.exists(wav):
                            rendus.append(dict(item, wav=wav))
                    res["rendus"] = rendus
                    res["machine"] = (compagnon.trusted().get(cid) or {}).get("nom")
                    res["_tmp"] = tmp
                    handed = True
                    self._rendre(res)
                    return
        except queue.Empty:
            self._rendre({"ok": False, "erreur": "pas de reponse de la machine compagne"})
        except (OSError, ValueError) as exc:
            self._rendre({"ok": False, "erreur": repr(exc)[:200]})
        finally:
            _demandes_numerique.pop(rid, None)
            for w in wouts.values():
                try:
                    w.close()
                except Exception:
                    pass
            if not handed:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)

    def _rendre(self, data):
        try:
            self.wfile.write((json.dumps(data) + "\n").encode())
            self.wfile.flush()
        except OSError:
            pass


def _serveur_numerique():
    try:
        os.remove(NUMERIQUE_SOCKET)
    except OSError:
        pass
    socketserver.ThreadingUnixStreamServer.daemon_threads = True
    server = socketserver.ThreadingUnixStreamServer(NUMERIQUE_SOCKET, _NumeriqueMac)
    os.chmod(NUMERIQUE_SOCKET, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def v1_necessaire(v2_actif):
    """L'ecoute v1 ne sert plus que tant qu'une machine de confiance ne s'est jamais
    connectee en v2 seule (amorce de sa migration)."""
    if not v2_actif:
        return True
    return any(not card.get("v2_autonome") for card in compagnon.trusted().values())


def _ecoute_v1(etat, v2_actif):
    """Ouvre ou ferme l'ecoute v1 (port 47811) selon v1_necessaire."""
    voulu = v1_necessaire(v2_actif)
    if voulu and etat[0] is None:
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        server = socketserver.ThreadingTCPServer(("0.0.0.0", proto.PORT), _Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        etat[0] = server
        print(f"[compagnon] ecoute v1 ouverte (port {proto.PORT})")
    elif not voulu and etat[0] is not None:
        etat[0].shutdown()
        etat[0].server_close()
        etat[0] = None
        print("[compagnon] toutes les machines sont en v2 seule : ecoute v1 fermee")
    elif not voulu and etat[0] is None and not getattr(_ecoute_v1, "annonce", False):
        _ecoute_v1.annonce = True
        print("[compagnon] toutes les machines sont en v2 seule : pas d'ecoute v1")


def run():
    _serveur_voix_mac()
    _serveur_tetra()
    _serveur_numerique()
    v2 = False
    if pont_v2.actif():
        try:
            pont_v2.demarrer(recu=_traiter, evenement=_evenement_v2)
            v2 = True
        except Exception as exc:
            print(f"[pont v2] ecoute impossible, la v1 continue seule : {exc!r}")
        try:  # point d'acces local des extensions (logiciel IA de PrismaLab…), lot 2 §5
            import extensions
            extensions.demarrer()
        except Exception as exc:
            print(f"[extensions] point d'acces impossible : {exc!r}")
    ecoute_v1 = [None]
    _ecoute_v1(ecoute_v1, v2)
    derniere_verif = time.time()
    with _lock:
        _save_presence()  # personne de connecte au demarrage
    last_poll = 0
    derniere_reception = 0
    while True:
        if time.time() - derniere_verif > V1_VERIF_SEC:
            derniere_verif = time.time()
            _ecoute_v1(ecoute_v1, v2)
        try:
            _drain_force_notify()
            _signal_tickets()
            _dispatch()
            if v2 and time.time() - derniere_reception > 3600:
                derniere_reception = time.time()
                if pont_v2.publier_reception():
                    print("[pont v2] cle de reception publiee", flush=True)
            if _results_ready.is_set() or time.time() - last_poll > POLL_SEC:
                _results_ready.clear()
                last_poll = time.time()
                compagnon_resultats.poll_once()
        except Exception as exc:
            print(f"[compagnon] {exc!r}")
        time.sleep(TICK_SEC)
