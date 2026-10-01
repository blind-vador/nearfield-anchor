#!/usr/bin/env python3
"""Module compagnon de nearfield anchor : une machine plus puissante (Mac,
PC Linux) fait les calculs lourds a la place du Raspberry Pi.

Un "pont" relie cette machine a un Pi. Elle garde une paire de cles Ed25519
(~/.nearfield-bridge/cle.pem) ; a l'appairage elle echange sa cle publique
avec celle du Pi par le disque du NAS, et les deux disent un code a 4
chiffres tire des deux cles. Ensuite le service se connecte au Pi, se fait
reconnaitre par signature, attend qu'il annonce un travail, le calcule sur
le disque du NAS et previent le Pi quand c'est fini. Voir
bridge/pont_protocole.py et, cote Pi, lib/compagnon.py.

Commandes (utilisees par l'application Nearfield Bridge, ou a la main) :
  service                 le service de fond (lance par l'installateur)
  etat                    ponts, connexions, statistiques
  appairage               appairage complet dans le terminal
  appairage-attendre      etape 1 : attend la demande du Pi, rend le code
  appairage-code          etape 2 : dialogue du code ; valider d'un seul cote
                          suffit (Oui ici, ou Entree sur le Pi), l'autre suit
  ponts                   liste des ponts (id, tabulation, description)
  oublier <pi>            supprime le pont avec ce Pi (le Pi est prevenu)
  service-pause / service-reprise / service-etat

Compatible Python 3.9 (celui d'Apple).
"""
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pont_protocole as proto  # noqa: E402

HOME = os.path.expanduser("~/.nearfield-bridge")
CONFIG = os.path.join(HOME, "config.json")
KEY_PATH = os.path.join(HOME, "cle.pem")
BRIDGES = os.path.join(HOME, "ponts")
STATS = os.path.join(HOME, "stats")
PENDING = os.path.join(HOME, "appairage.json")
BIN_DIR = os.path.join(HOME, "bin")
LABEL = "local.nearfield.bridge"
PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)
SHARE_GUESSES = ["/Volumes/server/server/nearfield-anchor-bridge",  # Mac
                 "/mnt/server/server/nearfield-anchor-bridge",      # Linux
                 "/media/nas/nd1/server/nearfield-anchor-bridge"]   # le Pi lui-meme (essais)
BEAT_SEC = 30
SCAN_SEC = 300
PAIRING_WAIT_SEC = 180
RECONNECT_SEC = (10, 120)


# --- outils -------------------------------------------------------------------------

def log(text):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text), flush=True)


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


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def config():
    conf = _read(CONFIG) or {}
    if "id" not in conf:
        conf = {"id": uuid.uuid4().hex[:12], "nom": socket.gethostname().split(".")[0]}
        _write(CONFIG, conf)
    return conf


def key():
    return proto.load_or_create_key(KEY_PATH)


def bridges():
    """{pi: fiche} des ponts de confiance."""
    found = {}
    try:
        names = os.listdir(BRIDGES)
    except OSError:
        return {}
    for name in names:
        card = _read(os.path.join(BRIDGES, name)) if name.endswith(".json") else None
        if card and card.get("cle"):
            found[name[:-5]] = card
    return found


def stats(pi):
    return _read(os.path.join(STATS, pi + ".json")) or {}


_stats_lock = threading.Lock()


def update_stats(pi, **fields):
    with _stats_lock:
        data = stats(pi)
        data.update(fields)
        _write(os.path.join(STATS, pi + ".json"), data)
        return data


def capabilities():
    caps = []
    try:
        import numpy  # noqa: F401
        if all(os.access(os.path.join(BIN_DIR, b), os.X_OK) for b in ("meteor_demod", "meteor_decode")):
            caps.append("meteor")
    except ImportError:
        pass
    return caps


def network_identity():
    """(adresse materielle, [ips]) de l'interface de la route par defaut."""
    iface, mac, ips = None, None, []
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True).stdout
            m = re.search(r"interface:\s*(\S+)", out)
            iface = m.group(1) if m else None
            out = subprocess.run(["ifconfig", iface], capture_output=True, text=True).stdout if iface else ""
            m = re.search(r"ether\s+([0-9a-f:]{17})", out)
            mac = m.group(1) if m else None
            ips = re.findall(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        else:
            out = subprocess.run(["ip", "-o", "route", "get", "1.1.1.1"], capture_output=True, text=True).stdout
            m = re.search(r"dev\s+(\S+).*src\s+(\S+)", out)
            if m:
                iface, ips = m.group(1), [m.group(2)]
                with open("/sys/class/net/%s/address" % iface) as f:
                    mac = f.read().strip()
    except (OSError, IndexError):
        pass
    return mac, ips


def find_share(card=None):
    """Dossier du pont sur le NAS ; essaie de remonter le partage s'il manque."""
    candidates = ([card["partage"]] if card and card.get("partage") else []) + SHARE_GUESSES
    for path in candidates:
        if os.path.isdir(os.path.dirname(path)):
            os.makedirs(path, exist_ok=True)
            return path
    if card and card.get("montage") and sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'mount volume "%s"' % card["montage"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if os.path.isdir(os.path.dirname(card["partage"])):
            return card["partage"]
    return None


def _mount_url(path):
    """URL smb:// du partage qui contient path (pour le remonter apres la veille)."""
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True).stdout
    except OSError:
        return None
    best = None
    for line in out.splitlines():
        m = re.match(r"//(\S+?) on (\S+) \((smbfs|cifs)", line)
        if m and path.startswith(m.group(2) + "/") and (not best or len(m.group(2)) > len(best[1])):
            best = ("smb://" + m.group(1).split(";")[-1], m.group(2))
    return best[0] if best else None


# --- appairage ------------------------------------------------------------------------

def pairing_wait():
    """Etape 1 : rend ("OK", pi, code) ou ("ERREUR", message)."""
    conf = config()
    share = find_share()
    if not share:
        return "ERREUR", "Je ne trouve pas le disque partagé du NAS. Monte le partage server, puis recommence."
    request_path = os.path.join(share, "appairage", "demande.json")
    deadline = time.time() + PAIRING_WAIT_SEC
    while time.time() < deadline:
        request = _read(request_path)
        if request and request.get("cle") and time.time() - request.get("t", 0) < PAIRING_WAIT_SEC:
            break
        time.sleep(2)
    else:
        return "ERREUR", "Le Pi ne s'est pas manifesté en trois minutes. Appairage abandonné."
    my_pub = proto.public_b64(key())
    mac, ips = network_identity()
    _write(os.path.join(share, "appairage", "reponse-%s.json" % conf["id"]),
           {"id": conf["id"], "nom": conf["nom"], "cle": my_pub, "capacites": capabilities(),
            "adresse_materielle": mac, "ips": ips, "systeme": platform.platform(terse=True), "t": time.time()})
    code = proto.pairing_code(request["cle"], my_pub)
    _write(PENDING, {"demande": request, "partage": share, "code": code, "t": time.time()})
    return "OK", request.get("pi", "inconnu"), " ".join(code)


def _accord(pending):
    """Accord du Pi : "ok" (valide et enregistre), "faux", ou None (pas encore)."""
    conf = config()
    request, share = pending["demande"], pending["partage"]
    accord_path = os.path.join(share, "appairage", "accord-%s.json" % conf["id"])
    accord = _read(accord_path)
    if not accord:
        return None
    if not (accord.get("compagnon") == conf["id"] and accord.get("cle_compagnon") == proto.public_b64(key())
            and proto.verify(request["cle"], accord, max_age=None)):
        return "faux"
    pi = request["pi"]
    _write(os.path.join(BRIDGES, pi + ".json"),
           {"pi": pi, "cle": request["cle"], "hote": request.get("hote"), "ip": request.get("ip"),
            "port": request.get("port", proto.PORT), "partage": share, "montage": _mount_url(share),
            "appaire": time.time()})
    _remove(accord_path)
    return "ok"


def _ask(question, wait_while):
    """Question Oui / Non dans un dialogue systeme (langue du systeme, lu par
    VoiceOver). Le dialogue est ferme tout seul si wait_while() rend False
    (l'autre machine a deja tranche) : rend "Oui", "Non" ou None."""
    script = ('activate\ndisplay dialog %s with title "Appairage" buttons {"Non", "Oui"} default button "Oui"'
              % json.dumps(question, ensure_ascii=False))
    proc = subprocess.Popen(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True)
    while proc.poll() is None:
        if not wait_while():
            proc.terminate()
            return None
        time.sleep(0.5)
    out = proc.stdout.read()
    return "Oui" if "Oui" in out else "Non"


def pairing_decide():
    """Etape 2 : dialogue du code. Valider d'un seul cote suffit : Oui ici
    (le Pi suit tout seul), ou Entree sur le Pi (ce dialogue se ferme)."""
    conf = config()
    pending = _read(PENDING)
    if not pending:
        return "ERREUR", "Aucun appairage en cours."
    request, share = pending["demande"], pending["partage"]
    pi = request.get("pi", "inconnu")
    done = "Pont créé avec %s : nos clés sont échangées. Le service s'y connecte dans quelques secondes." % pi
    state = {}

    def still_waiting():
        state["accord"] = _accord(pending)
        return state["accord"] is None

    try:
        answer = _ask("Pi trouvé : %s.\nCode : %s.\n\nSi le Pi affiche le même code, valide ici ou sur la console "
                      "du Pi : l'autre suivra tout seul." % (pi, " ".join(pending["code"])), still_waiting)
        if answer is None:  # le Pi a tranche pendant que le dialogue etait ouvert
            if state.get("accord") == "ok":
                return "OK", "Le Pi a validé le code. " + done
            return "ERREUR", "L'accord reçu n'est pas signé par ce Pi. Rien n'est enregistré."
        if answer == "Non":
            _write(os.path.join(share, "appairage", "refus-%s.json" % conf["id"]), {"t": time.time()})
            return "OK", "Appairage refusé, rien n'est enregistré. Le Pi ferme son dialogue tout seul."
        _write(os.path.join(share, "appairage", "confirme-%s.json" % conf["id"]),
               proto.sign(key(), {"type": "confirme", "pi": pi, "compagnon": conf["id"], "cle_pi": request["cle"]}))
        deadline = time.time() + 60
        while time.time() < deadline:
            result = _accord(pending)
            if result == "ok":
                return "OK", done
            if result == "faux":
                return "ERREUR", "L'accord reçu n'est pas signé par ce Pi. Rien n'est enregistré."
            time.sleep(0.5)
        return "ERREUR", "Le Pi n'a pas répondu. Est-il toujours en mode appairage ?"
    finally:
        _remove(PENDING)


def pairing_terminal():
    result = pairing_wait()
    if result[0] != "OK":
        print(result[1])
        return 1
    result = pairing_decide()
    print(result[1])
    return 0 if result[0] == "OK" else 1


def forget(pi):
    """Supprime le pont ; le Pi est prevenu par un message signe sur le NAS."""
    conf = config()
    card = bridges().get(pi)
    if not card:
        return "ERREUR", "Pas de pont avec %s." % pi
    share = find_share(card)
    if share:
        _write(os.path.join(share, "revocations", conf["id"] + ".json"),
               proto.sign(key(), {"type": "oubli", "compagnon": conf["id"], "pi": pi}))
    _remove(os.path.join(BRIDGES, pi + ".json"))
    _remove(os.path.join(STATS, pi + ".json"))
    return "OK", ("Pont avec %s supprimé%s." % (pi, "" if share else
                  ", mais le disque du NAS est absent : le Pi ne le saura qu'au prochain essai"))


# --- service ----------------------------------------------------------------------------

_jobs = queue.Queue()
_links = {}  # pi -> Link


class Link(threading.Thread):
    """Connexion au Pi : presentation signee, battements, avis de travail."""

    def __init__(self, pi):
        threading.Thread.__init__(self, daemon=True)
        self.pi = pi
        self.channel = None
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def send(self, msg):
        with self.lock:
            if self.channel:
                try:
                    self.channel.send(msg)
                    return True
                except OSError:
                    self.channel.close()
        return False

    def shutdown(self):
        """Pont supprime : coupe la liaison tout de suite."""
        self.stop.set()
        with self.lock:
            if self.channel:
                self.channel.close()

    def _connect(self, card):
        for host in (card.get("hote"), card.get("ip")):
            if not host:
                continue
            try:
                sock = socket.create_connection((host, card.get("port", proto.PORT)), timeout=10)
            except OSError:
                continue
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            return sock
        return None

    def _session(self, card):
        conf = config()
        sock = self._connect(card)
        if not sock:
            return False
        channel = proto.Channel(sock, key())
        mac, ips = network_identity()
        hello = {"type": "bonjour", "compagnon": conf["id"], "pi": self.pi, "nom": conf["nom"],
                 "capacites": capabilities(), "adresse_materielle": mac, "ips": ips, "nonce": uuid.uuid4().hex}
        channel.send(hello)
        welcome = channel.receive(card["cle"])
        if not welcome or welcome.get("type") != "bienvenue" or welcome.get("reponse_a") != hello["nonce"]:
            channel.close()
            update_stats(self.pi, connecte=False, erreur="le Pi n'a pas reconnu cette machine")
            return False
        sock.settimeout(None)
        with self.lock:
            self.channel = channel
        update_stats(self.pi, connecte=True, depuis=time.time(), erreur=None)
        log("connecte a %s" % self.pi)
        _jobs.put(self.pi)  # ce qui attendait
        beat = threading.Thread(target=self._beat, daemon=True)
        beat.start()
        try:
            while not self.stop.is_set():
                msg = channel.receive(card["cle"])
                if msg is None:
                    break
                if msg.get("type") == "travail":
                    log("%s : travail %s annonce" % (self.pi, msg.get("travail")))
                    _jobs.put(self.pi)
        except (OSError, ValueError) as exc:
            log("%s : liaison coupee (%r)" % (self.pi, exc))
        finally:
            with self.lock:
                self.channel = None
            channel.close()
            update_stats(self.pi, connecte=False, vu=time.time())
        return True

    def _beat(self):
        while not self.stop.is_set():
            with self.lock:
                channel = self.channel
            if channel is None:
                return
            if not self.send({"type": "battement", "capacites": capabilities()}):
                return
            time.sleep(BEAT_SEC)

    def run(self):
        delay = RECONNECT_SEC[0]
        while not self.stop.is_set():
            card = bridges().get(self.pi)
            if not card:
                return  # pont supprime
            try:
                if self._session(card):
                    delay = RECONNECT_SEC[0]
            except (OSError, ValueError) as exc:
                update_stats(self.pi, connecte=False, erreur="Pi injoignable (%s)" % exc.__class__.__name__)
            self.stop.wait(delay)
            delay = min(delay * 2, RECONNECT_SEC[1])


def _claim(share):
    """Prend le plus ancien travail libre (renommage atomique) : (id, dossier, travail)."""
    folder = os.path.join(share, "travaux")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    caps = capabilities()
    for job_id in names:
        src = os.path.join(folder, job_id, "travail.json")
        job = _read(src)
        if not job or job.get("type") not in caps:
            continue
        dst = os.path.join(folder, job_id, "travail.pris.json")
        try:
            os.rename(src, dst)
        except OSError:
            continue  # une autre machine l'a pris
        return job_id, os.path.join(folder, job_id), job
    return None


def _process(job, folder):
    if job.get("type") == "meteor":
        import meteor
        return meteor.process(folder, job["params"], BIN_DIR)
    return "echec", [], "cette machine ne sait pas faire « %s »" % job.get("type")


def _work(pi):
    card = bridges().get(pi)
    share = find_share(card) if card else None
    if not share:
        update_stats(pi, erreur="disque du NAS absent") if card else None
        return
    while True:
        claimed = _claim(share)
        if not claimed:
            return
        job_id, folder, job = claimed
        link = _links.get(pi)
        if link:
            link.send({"type": "pris", "travail": job_id})
        log("%s : travail %s (%s)" % (pi, job_id, job.get("type")))
        update_stats(pi, en_cours=job_id)
        started = time.time()
        # Empeche la veille pendant le calcul (sans toucher aux reglages du Mac).
        awake = None
        if sys.platform == "darwin":
            awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        try:
            state, outputs, message = _process(job, folder)
        except Exception:
            traceback.print_exc()
            state, outputs, message = "echec", [], "erreur de calcul sur la machine compagne"
        finally:
            if awake:
                awake.terminate()
        duration = round(time.time() - started)
        conf = config()
        result = proto.sign(key(), {
            "type": "resultat", "travail": job_id, "compagnon": conf["id"], "etat": state, "message": message,
            "duree": duration, "sorties": {name: proto.file_digest(os.path.join(folder, name)) for name in outputs}})
        _write(os.path.join(folder, "resultat.json"), result)
        data = stats(pi)
        ok = state == "fait"
        update_stats(pi, faits=data.get("faits", 0) + ok, echecs=data.get("echecs", 0) + (not ok),
                     secondes=data.get("secondes", 0) + duration, en_cours=None,
                     dernier={"t": time.time(), "type": job.get("type"), "etat": state, "message": message,
                              "duree": duration})
        log("%s : %s %s (%s), %d s" % (pi, job_id, state, message, duration))
        if link:
            link.send({"type": "fini", "travail": job_id})


def service():
    conf = config()
    key()
    log("service demarre : %s (%s), sait faire : %s" % (conf["nom"], conf["id"], ", ".join(capabilities()) or "rien"))

    def worker():
        while True:
            pi = _jobs.get()
            try:
                _work(pi)
            except Exception:
                traceback.print_exc()

    threading.Thread(target=worker, daemon=True).start()
    last_scan = 0
    while True:
        current = bridges()
        for pi in current:
            if pi not in _links or not _links[pi].is_alive():
                _links[pi] = Link(pi)
                _links[pi].start()
        for pi in list(_links):
            if pi not in current:
                _links.pop(pi).shutdown()
        if time.time() - last_scan > SCAN_SEC:  # filet de securite si un avis s'est perdu
            last_scan = time.time()
            for pi in current:
                _jobs.put(pi)
        time.sleep(5)


# --- etat --------------------------------------------------------------------------------

def _when(t):
    if not t:
        return "jamais"
    day = time.strftime("%Y-%m-%d", time.localtime(t))
    today = time.strftime("%Y-%m-%d")
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    prefix = "aujourd'hui" if day == today else "hier" if day == yesterday else "le " + time.strftime(
        "%d/%m", time.localtime(t))
    return "%s à %s" % (prefix, time.strftime("%H h %M", time.localtime(t)))


def _plural(n, word):
    return "%d %s%s" % (n, word, "s" if n > 1 else "")


def _duration(sec):
    sec = int(sec or 0)
    if sec < 90:
        return _plural(sec, "seconde")
    if sec < 5400:
        return "%d minutes" % round(sec / 60)
    return "%d heures %02d" % (sec // 3600, (sec % 3600) // 60)


def service_running():
    if sys.platform == "darwin":
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        return LABEL in out
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", "nearfield-bridge"]).returncode == 0


def status_text():
    conf = config()
    caps = capabilities()
    lines = ["Service de fond : %s." % ("actif" if service_running() else "en pause"),
             "Cette machine : %s, sait faire : %s." % (conf["nom"], ", ".join(caps) or "rien pour l'instant")]
    current = bridges()
    if not current:
        lines.append("Aucun pont. Choisis « Appairer avec un Pi » pour en créer un.")
    for pi, card in sorted(current.items()):
        data = stats(pi)
        if data.get("connecte"):
            link = "connecté depuis %s" % _when(data.get("depuis"))
        else:
            link = "pas connecté" + (" (%s)" % data["erreur"] if data.get("erreur") else "")
        lines.append("Pont avec %s : %s." % (pi, link))
        done = data.get("faits", 0)
        lines.append("  %s %s, %d sans résultat, %s de calcul au total." % (
            _plural(done, "travail").replace("travails", "travaux"), "réussi" + ("s" if done > 1 else ""),
            data.get("echecs", 0), _duration(data.get("secondes"))))
        if data.get("en_cours"):
            lines.append("  Travail en cours : %s." % data["en_cours"])
        last = data.get("dernier")
        if last:
            lines.append("  Dernier travail : %s, %s, %s (%s, %s)." % (
                _when(last.get("t")), last.get("type"), "réussi" if last.get("etat") == "fait" else "sans résultat",
                last.get("message"), _duration(last.get("duree"))))
        lines.append("  Disque du NAS : %s. Créé %s." % (
            "accessible" if find_share(card) else "absent", _when(card.get("appaire"))))
    return "\n".join(lines)


def bridges_list():
    out = []
    for pi in sorted(bridges()):
        data = stats(pi)
        out.append("%s\tPont avec %s, %s" % (pi, pi, "connecté" if data.get("connecte") else "pas connecté"))
    return "\n".join(out)


def service_control(action):
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "unload" if action == "pause" else "load", PLIST],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["systemctl", "--user", "stop" if action == "pause" else "start", "nearfield-bridge"])
    time.sleep(1)
    running = service_running()
    if action == "pause":
        return "OK", ("Service en pause : le Pi ne peut plus confier de travail à cette machine."
                      if not running else "Le service tourne encore.")
    return "OK", "Service relancé." if running else "Le service n'a pas démarré."


def _print(result):
    print("\t".join(result))
    return 0 if result[0] == "OK" else 1


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "etat"
    if command == "service":
        service()
    elif command == "etat":
        print(status_text())
    elif command == "ponts":
        print(bridges_list())
    elif command == "appairage":
        sys.exit(pairing_terminal())
    elif command == "appairage-attendre":
        sys.exit(_print(pairing_wait()))
    elif command == "appairage-code":
        sys.exit(_print(pairing_decide()))
    elif command == "oublier" and len(sys.argv) > 2:
        sys.exit(_print(forget(sys.argv[2])))
    elif command in ("service-pause", "service-reprise"):
        sys.exit(_print(service_control(command.split("-")[1])))
    elif command == "service-etat":
        print("actif" if service_running() else "en pause")
    else:
        print(__doc__)
        sys.exit(2)
