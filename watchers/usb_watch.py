"""Entretien et reparation automatique des peripheriques USB (2026-10-11).

Trouve en session sur ce Pi : des appareils bien branches restaient
invisibles ou figes sans que personne ne s'en apercoive --
  - la webcam s'est deconnectee une seconde (mise en veille USB
    automatique) : mjpg-streamer est mort ("Error grabbing frames") mais
    webcamd restait "active", la camera revenue n'etait plus jamais
    reprise -> vision.py et les photos des plantes muettes pendant 14 h ;
  - un appareil lent branche directement sur le Pi n'a jamais ete reconnu
    au demarrage (port bloque dans l'etat "default", erreurs -32/-71) ;
  - l'imprimante rebranchee (CH340) n'est pas reconnectee par OctoPrint,
    qui n'essaie qu'a son propre demarrage.
Meme esprit que nas_watch.py : on detecte, on repare tout seul, on ne
parle que des changements (et de ce qu'il faut faire a la main quand la
reparation automatique ne suffit pas).

Ce qui est surveille toutes les CHECK_INTERVAL_SEC :
  1) les ports USB bloques (appareil present electriquement, jamais
     reconnu) -> coupure/remise du courant du port, puis meme geste avec
     l'ancien schema d'enumeration du noyau ; sinon on le dit ;
  2) les appareils du projet (ROLES), reconnus par leur type et non par
     leur numero de port : pilote absent, carte son "Device" absente,
     pilote TNT sur le dongle radio, camera en veille ou flux mort,
     imprimante rebranchee mais deconnectee d'OctoPrint ;
  3) la disparition / le retour d'un appareil deja vu (souvenir garde sur
     la carte SD : une camera absente au redemarrage est signalee).
Les disques du NAS restent a nas_watch.py ; les hubs et disques ne sont
jamais coupes (garde-fou aussi dans tools/usb-repare).

Branchements de reference (ce Pi, face ports) :
  bas-gauche USB3  = hub alimente NAS (nd1+nd2) ;
  haut-gauche USB3 = hub alimente (imprimante + camera derriere
    interrupteurs USB a bouton, carte son, dongle radio) ;
  haut-droite USB2 = clavier ; bas-droite USB2 = vide.
Camera/imprimante absentes = interrupteur coupe, pas une panne.

Les gestes root passent par /usr/local/sbin/usb-repare (sudo -n, regle
sudoers generee par install.sh). Etat publie dans STATE_FILE pour la
commande "usb" de la console (console/usb_cmd.py).
"""
import chemins
import glob
import json

import usb_eteints
import os
import subprocess
import time

import requests

import audio_lock
import reglages
from phrases import Etiquettes, dire
from speak import speak_cloud

CHECK_INTERVAL_SEC = 15
SYS = "/sys/bus/usb/devices"
HELPER = ["sudo", "-n", "/usr/local/sbin/usb-repare"]
STATE_FILE = "/tmp/usb_watch_etat.json"
SEEN_FILE = chemins.base("usb_watch_attendus.json")  # survit au redemarrage
PORTS_IGNORE_FILE = chemins.base("usb_watch_ports_ignores.json")  # ports fantomes
OCTOPRINT_URL = "http://127.0.0.1:5000"
SNAPSHOT_URL = "http://127.0.0.1:8080/?action=snapshot"  # meme source que console/vision.py

# Un port est "bloque" s'il reste dans un de ces etats deux passages de
# suite : l'enumeration normale prend moins d'une seconde.
STUCK_STATES = {"attached", "powered", "default", "addressed", "reconnecting"}
PORT_LADDER = ["port", "port-ancien"]
# Apres echec de toute l'echelle : nouvel essai discret plus tard, rappel
# vocal espace (pas toutes les 30 s comme un disque : rien n'est en danger).
RETRY_AFTER_SEC = 1800
REMIND_EVERY_SEC = 2 * 3600
REMIND_HOURS = (8, 22)     # rappels seulement le jour (une annonce a 2 h du matin passe inapercue)
ABSENT_CYCLES = 2          # ~30 s d'absence avant d'annoncer un debranchement
ROLE_FAILS_BEFORE_REPAIR = 2
REPAIR_COOLDOWN_SEC = 120  # entre deux gestes sur le meme appareil
PRINTER_CONNECT_TRIES = 3

NOMS = Etiquettes("usb.nom", {"clavier": "Le clavier", "son": "La carte son", "radio": "Le dongle radio",
                              "camera": "La caméra", "imprimante": "L'imprimante"},
                  "nom d'un appareil USB, en début de phrase")
SERIAL_DRIVERS = {"ch341", "cp210x", "ftdi_sio", "cdc_acm", "pl2303"}
RTL_IDS = {("0bda", "2838"), ("0bda", "2832")}


# --- lecture de sysfs ---------------------------------------------------------

def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _interfaces(dev):
    """[(nom, classe, sous-classe, protocole, pilote)] des interfaces d'un appareil."""
    result = []
    for intf in sorted(glob.glob(f"{SYS}/{dev}:*")):
        drv = os.path.basename(os.readlink(f"{intf}/driver")) if os.path.islink(f"{intf}/driver") else ""
        result.append((os.path.basename(intf), _read(f"{intf}/bInterfaceClass"),
                       _read(f"{intf}/bInterfaceSubClass"), _read(f"{intf}/bInterfaceProtocol"), drv))
    return result


def _devices():
    """{"1-1.3.3": {...}} : tous les appareils USB (pas les hubs racine)."""
    devices = {}
    for path in glob.glob(f"{SYS}/[0-9]*-[0-9]*"):
        name = os.path.basename(path)
        if ":" in name or not os.path.exists(f"{path}/idVendor"):
            continue
        devices[name] = {"vid": _read(f"{path}/idVendor"), "pid": _read(f"{path}/idProduct"),
                         "produit": _read(f"{path}/product"), "classe": _read(f"{path}/bDeviceClass"),
                         "veille": _read(f"{path}/power/control"), "interfaces": _interfaces(name)}
    return devices


def _ports():
    """{"1-1-port4": etat} de tous les ports de tous les hubs."""
    return {os.path.basename(p): _read(f"{p}/state")
            for p in glob.glob(f"{SYS}/*/*:1.0/*-port[0-9]*")}


def _port_child(port):
    """'1-1-port4' -> '1-1.4' ; 'usb2-port1' -> '2-1'."""
    hub, n = port.rsplit("-port", 1)
    return f"{hub[3:]}-{n}" if hub.startswith("usb") else f"{hub}.{n}"


def _port_on_pi(port):
    """Port du Pi lui-meme (hub racine, ou hub interne VL805 du Pi 4 : 1-1)
    plutot que d'un hub ajoute."""
    hub = port.rsplit("-port", 1)[0]
    return hub.startswith("usb") or hub.count(".") == 0 and _read(f"{SYS}/{hub}/idVendor") == "2109"


def _port_peer(port):
    """Nom du port USB3 jumeau (sysfs peer), ou None."""
    matches = glob.glob(f"{SYS}/*/*:1.0/{port}")
    if not matches:
        return None
    peer = os.path.join(matches[0], "peer")
    if not os.path.islink(peer):
        return None
    return os.path.basename(os.readlink(peer))


def _load_ignored_ports():
    try:
        with open(PORTS_IGNORE_FILE) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _save_ignored_ports(ports):
    try:
        with open(PORTS_IGNORE_FILE, "w") as f:
            json.dump(sorted(ports), f)
    except OSError as exc:
        print(f"[usb_watch] impossible d'ecrire {PORTS_IGNORE_FILE} : {exc}")


def _ignore_port(port, reason):
    """Abandonne un port fantome : plus de power-cycle, et coupe le port
    pour stopper la tempete d'enumeration noyau (qui secoue le hub jumeau)."""
    ignored = _load_ignored_ports()
    if port not in ignored:
        ignored.add(port)
        _save_ignored_ports(ignored)
        print(f"[usb_watch] port ignore desormais : {port} ({reason})")
    _helper("port-off", port)



# --- appareils du projet ------------------------------------------------------

def _classes(dev):
    return {i[1] for i in dev["interfaces"]}


def _match(role, dev):
    classes = _classes(dev)
    if role == "clavier":   # interface HID "boot keyboard"
        return any(i[1:4] == ("03", "01", "01") for i in dev["interfaces"])
    if role == "son":       # audio sans video (la camera a aussi un micro)
        return "01" in classes and "0e" not in classes
    if role == "radio":
        return (dev["vid"], dev["pid"]) in RTL_IDS
    if role == "camera":
        return "0e" in classes
    if role == "imprimante":
        return any(i[4] in SERIAL_DRIVERS for i in dev["interfaces"]) or dev["vid"] == "1a86"
    return False


def _unbound(dev, cls):
    """Interfaces de cette classe sans pilote (appareil vu mais pas pris en charge)."""
    return [i[0] for i in dev["interfaces"] if i[1] == cls and not i[4]]


def _webcamd_active():
    return subprocess.run(["systemctl", "is-active", "--quiet", "webcamd"]).returncode == 0


def _snapshot_ok():
    try:
        resp = requests.get(SNAPSHOT_URL, timeout=5)
        return resp.status_code == 200 and len(resp.content) > 1000
    except requests.RequestException:
        return False


def _octoprint(method, path, **kwargs):
    key = reglages.cle("octoprint")
    if not key:
        return None
    try:
        return requests.request(method, f"{OCTOPRINT_URL}{path}", headers={"X-Api-Key": key}, timeout=10, **kwargs)
    except requests.RequestException:
        return None


def _job_occupe():
    """True si OctoPrint croit encore imprimer / annuler : ne pas spammer
    connect ni crier 'debranche' sur un micro-blanc USB des moteurs."""
    resp = _octoprint("GET", "/api/job")
    if resp is None or resp.status_code != 200:
        return False
    try:
        state = (resp.json().get("state") or "").lower()
    except ValueError:
        return False
    return any(w in state for w in ("print", "paus", "cancel"))


def _printer_closed():
    """True si OctoPrint n'est connecte a rien, None si on ne sait pas."""
    resp = _octoprint("GET", "/api/connection")
    if resp is None or resp.status_code != 200:
        return None
    state = resp.json().get("current", {}).get("state", "")
    # Connecting/Opening bloques apres kill : traiter comme "pas pret"
    if state.startswith(("Closed", "Offline", "Error")):
        return True
    if state in ("Connecting", "Opening serial connection", "Detecting serial connection"):
        # laisse une chance courte : le caller gere les tries
        return True
    return False


def _diagnose(role, name, dev):
    """None si l'appareil va bien, sinon (code du probleme, [gestes dans
    l'ordre]). Codes dits par la console : sections [usb.etat.<code>]."""
    if dev["veille"] == "auto" and role in ("camera", "son", "clavier", "imprimante"):
        # Cause de la deconnexion de la webcam du 2026-10-10 : on retire la
        # veille avant toute autre chose (regle udev aussi, voir install.sh).
        return "veille", [("sans-veille", name)]
    if role == "son":
        if _unbound(dev, "01") or not os.path.exists("/proc/asound/Device"):
            return "sans_pilote", [("rebranche", name)]
    elif role == "clavier":
        if _unbound(dev, "03"):
            return "sans_pilote", [("rebranche", name)]
    elif role == "radio":
        # usbfs = dongle ouvert par rtl_power/rtl_fm/airband : normal.
        # Seuls les pilotes noyau (DVB/TNT) bloquent l acces userspace.
        bound = [i[0] for i in dev["interfaces"]
                 if i[4] and i[4] != "usbfs"]
        if bound:
            return "pilote_tnt", [("detache", i) for i in bound]
    elif role == "camera":
        if _unbound(dev, "0e"):
            return "sans_pilote", [("rebranche", name), ("webcam", "")]
        if _webcamd_active() and not _snapshot_ok():
            return "flux_mort", [("webcam", ""), ("rebranche", name), ("webcam", "")]
    return None


# --- etat et voix -------------------------------------------------------------

def _jour():
    return REMIND_HOURS[0] <= time.localtime().tm_hour < REMIND_HOURS[1]


def _speak(text):
    print(f"[usb_watch] annonce : {text}")
    lock = audio_lock.acquire()
    try:
        speak_cloud(text, repetitive=True)
    except Exception as exc:  # carte son absente : on ne plante pas la veille
        print(f"[usb_watch] voix indisponible : {exc}")
    finally:
        audio_lock.release(lock)


def _load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _save_seen(seen):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(sorted(seen), f)
    except OSError as exc:
        print(f"[usb_watch] impossible d'ecrire {SEEN_FILE} : {exc}")


def _helper(action, target):
    cmd = HELPER + [action] + ([target] if target else [])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    ok = proc.returncode == 0
    print(f"[usb_watch] usb-repare {action} {target} : {'ok' if ok else proc.stderr.strip()}")
    return ok


class Watch:
    def __init__(self):
        self.seen = _load_seen()
        self.absent = {}        # role -> passages consecutifs sans l'appareil
        self.announced_gone = set()
        self.role_rappel = {}   # role -> heure du dernier rappel d'absence
        self.role_reason = {}   # role -> code du probleme en cours
        self.role_fails = {}    # role -> passages consecutifs en panne
        self.role_step = {}     # role -> prochain geste de l'echelle de ce probleme
        self.role_acted = set()  # roles ayant recu un vrai geste (pas juste la veille)
        self.role_last = {}     # role -> heure du dernier geste
        self.role_failed = {}   # role -> heure de l'annonce d'echec
        self.ports_stuck = {}   # port -> passages consecutifs bloque
        self.port_step = {}
        self.port_given_up = {}  # port -> heure de l'abandon (rappel/nouvel essai)
        self.ports_ignored = _load_ignored_ports()
        # Au demarrage : recouper les ports deja abandonnes (disable est
        # perdu au reboot / apres un power-cycle) pour ne pas relancer la
        # tempete d'enumeration ni l'echelle de reparation.
        for port in list(self.ports_ignored):
            self.port_given_up[port] = {"essai": time.time(), "annonce": 0}
            _helper("port-off", port)
        self.printer_tries = 0
        self.printer_asked = False
        self.printer_present = None
        self.status = {}
        self.history = []

    def _log(self, what, ok, resolu=False):
        """resolu : l'appareil remarche (ce que compte la commande "usb"),
        par opposition a un geste tente."""
        self.history = (self.history + [{"t": time.time(), "quoi": what, "ok": ok, "resolu": resolu}])[-20:]

    # 1) ports bloques
    def check_ports(self, devices):
        stuck_now = set()
        for port, state in _ports().items():
            if state not in STUCK_STATES or _port_child(port) in devices:
                continue
            # Ports deja abandonnes (persistes) : ne plus jamais power-cycler.
            if port in self.ports_ignored:
                stuck_now.add(port)
                continue
            peer = _port_peer(port)
            if peer and peer.startswith("2-"):
                # Jumeau USB2 d'un hub USB3 (hub periph sur ce Pi) :
                # power-cycler reset TOUT le hub (radio/son/camera...).
                # 1-1-port1 = montee du hub USB2 : NE JAMAIS couper.
                # 1-1-port2/4 = fantomes sur ports vides du hub : ignorer+couper.
                stuck_now.add(port)
                if port in ("1-1-port2", "1-1-port4"):
                    print(f"[usb_watch] {port} fantome (jumeau {peer}) : ignore, coupe")
                    _ignore_port(port, f"peer fantome {peer}")
                    self.ports_ignored.add(port)
                    self.port_given_up[port] = {"essai": time.time(), "annonce": 0}
                else:
                    if self.ports_stuck.get(port, 0) == 0:
                        print(f"[usb_watch] {port} bloque (jumeau {peer}, "
                              f"port critique) : aucune reparation auto")
                    self.ports_stuck[port] = self.ports_stuck.get(port, 0) + 1
                continue
            stuck_now.add(port)
            self.ports_stuck[port] = self.ports_stuck.get(port, 0) + 1
            if self.ports_stuck[port] < 2:
                continue
            given_up = self.port_given_up.get(port)
            if given_up:
                # Pas de nouvel essai auto apres abandon.
                if time.time() - given_up["annonce"] >= REMIND_EVERY_SEC and _jour():
                    given_up["annonce"] = time.time()
                    _speak(dire("usb.port_bloque", ou=dire("usb.ou_pi" if _port_on_pi(port) else "usb.ou_hub")))
                continue
            step = self.port_step.get(port, 0)
            if step < len(PORT_LADDER):
                self.port_step[port] = step + 1
                self._log(f"port {port} : {PORT_LADDER[step]}", _helper(PORT_LADDER[step], port))
                self.ports_stuck[port] = 0  # laisser le temps a l'enumeration
            elif not given_up:
                # Un vrai appareil qui ne repond plus (dongle radio, 2026-10-19) :
                # le port reste ALIMENTE -- coupe pour toujours, l'appareil
                # rebranche au meme endroit restait invisible. On previent, et
                # on rappelle le jour (plus haut). La liste des ports ignores ne
                # sert plus qu'aux ports fantomes du hub.
                print(f"[usb_watch] {port} : toujours bloque apres reparation, on previent")
                self.port_given_up[port] = {"essai": time.time(), "annonce": time.time()}
                _speak(dire("usb.port_bloque", ou=dire("usb.ou_pi" if _port_on_pi(port) else "usb.ou_hub")))
        for port in set(self.ports_stuck) | set(self.ports_ignored):
            if port in stuck_now:
                continue
            child = _port_child(port)
            if child in devices:
                product = devices[child]["produit"] or dire("usb.appareil_sans_nom")
                print(f"[usb_watch] {port} : appareil enfin reconnu ({product})")
                self._log(f"port {port} : reconnu ({product})", True, resolu=True)
                _speak(dire("usb.port_repare", produit=product))
                if port in self.ports_ignored:
                    self.ports_ignored.discard(port)
                    _save_ignored_ports(self.ports_ignored)
                    _helper("port-on", port)
            for d in (self.ports_stuck, self.port_step, self.port_given_up):
                d.pop(port, None)
        # Ports fantomes (ignores, ports vides du hub) : jamais signales a l'utilisateur.
        return sorted(p for p in stuck_now if p not in self.ports_ignored and self.port_step.get(p))

    # 2) et 3) appareils du projet
    def check_roles(self, devices):
        for role in NOMS:
            found = [(n, d) for n, d in sorted(devices.items()) if d["classe"] != "09" and _match(role, d)]
            if not found:
                self._missing(role)
                continue
            if role in usb_eteints.lire():
                usb_eteints.marquer(role, False)  # rallume : la marque "eteint" part d'elle-meme
            if role in self.announced_gone:
                self.announced_gone.discard(role)
                _speak(dire("usb.revenu", appareil=NOMS[role]))
            self.absent[role] = 0
            if role not in self.seen:
                self.seen.add(role)
                _save_seen(self.seen)
            name, dev = found[0]
            if role == "imprimante":
                self._printer(name)
                continue
            problem = _diagnose(role, name, dev)
            self._repair(role, problem)

    def _missing(self, role):
        if role in usb_eteints.lire():
            # Eteint volontairement ("usb camera off") : ni annonce ni rappel.
            self.status[role] = "eteint"
            self.absent[role] = 0
            self.announced_gone.discard(role)
            return
        if role == "imprimante":
            self.printer_present = False
            # Pendant une impression, un blanc USB de 1-2 s (moteurs) est
            # frequent : ne pas hurler ni encherir les reconnexions.
            if _job_occupe():
                self.status[role] = "glitch"
                return
        if role not in self.seen:
            self.status[role] = "absent"
            return
        self.status[role] = "debranche"
        self.absent[role] = self.absent.get(role, 0) + 1
        if self.absent[role] == ABSENT_CYCLES and role not in self.announced_gone:
            self.announced_gone.add(role)
            self.role_rappel[role] = time.time()
            print(f"[usb_watch] {role} absent depuis {ABSENT_CYCLES} passages")
            _speak(dire("usb.debranche", appareil=NOMS[role]))
        elif role in self.announced_gone and _jour() and \
                time.time() - self.role_rappel.get(role, 0) >= REMIND_EVERY_SEC:
            # Toujours absent : rappel espace, le jour (le dongle radio perdu a
            # 1 h 54 n'a ete annonce qu'une fois, en pleine nuit, 2026-10-19).
            self.role_rappel[role] = time.time()
            _speak(dire("usb.toujours_absent", appareil=NOMS[role]))

    def _repair(self, role, problem):
        if problem is None:
            if role in self.role_acted:
                print(f"[usb_watch] {role} repare")
                self._log(f"{role} : repare", True, resolu=True)
                _speak(dire("usb.repare", appareil=NOMS[role]))
            for d in (self.role_reason, self.role_fails, self.role_step, self.role_failed):
                d.pop(role, None)
            self.role_acted.discard(role)
            self.status[role] = "ok"
            return
        reason, ladder = problem
        self.status[role] = reason
        if self.role_reason.get(role) != reason:  # nouveau probleme : son echelle depuis le debut
            self.role_reason[role] = reason
            for d in (self.role_fails, self.role_step, self.role_failed):
                d.pop(role, None)
        self.role_fails[role] = self.role_fails.get(role, 0) + 1
        # La veille automatique se retire sans attendre ni compter : sans danger.
        quick = ladder[0][0] == "sans-veille"
        if not quick and (self.role_fails[role] < ROLE_FAILS_BEFORE_REPAIR
                          or time.time() - self.role_last.get(role, 0) < REPAIR_COOLDOWN_SEC):
            return
        step = self.role_step.get(role, 0)
        if step >= len(ladder):
            failed = self.role_failed.get(role)
            if failed is None or time.time() - failed >= REMIND_EVERY_SEC:
                self.role_failed[role] = time.time()
                _speak(dire("usb.echec", appareil=NOMS[role]))
            if time.time() - self.role_last.get(role, 0) >= RETRY_AFTER_SEC:
                self.role_step[role] = 0  # nouvel essai discret de toute l'echelle
            return
        action, target = ladder[step]
        print(f"[usb_watch] {role} : {reason} -> {action} {target}")
        self.role_step[role] = step + 1
        if not quick:
            self.role_last[role] = time.time()
            self.role_acted.add(role)
        self._log(f"{role} : {reason}, {action}", _helper(action, target))

    def _printer(self, name):
        """Reconnexion d'OctoPrint seulement quand l'imprimante (re)apparait
        (ou au demarrage de la veille) : si on la deconnecte a la main dans
        OctoPrint, on ne la reconnecte pas derriere son dos."""
        if not self.printer_present:
            self.printer_tries = PRINTER_CONNECT_TRIES
        self.printer_present = True
        closed = _printer_closed()
        self.status["imprimante"] = "deconnectee" if closed else "ok"
        if not closed:
            if closed is False and self.printer_asked:
                self._log("imprimante : reconnectee a OctoPrint", True, resolu=True)
                _speak(dire("usb.imprimante_reconnectee"))
            self.printer_tries, self.printer_asked = 0, False
            return
        if self.printer_tries > 0:
            # Si un job tourne encore cote OctoPrint, attendre qu'il bascule
            # Offline tout seul : reconnecter trop tot combat le firmware.
            if _job_occupe() and self.printer_tries > 1:
                print(f"[usb_watch] imprimante sur {name} : job encore actif, "
                      f"on differe la reconnexion ({self.printer_tries})")
                return
            self.printer_tries -= 1
            resp = _octoprint("POST", "/api/connection", json={"command": "connect", "autoconnect": True})
            self.printer_asked = resp is not None and resp.status_code == 204
            print(f"[usb_watch] imprimante sur {name} : connexion OctoPrint demandee "
                  f"({'ok' if self.printer_asked else 'refus'})")
            self._log("imprimante : connexion a OctoPrint", self.printer_asked)

    def publish(self, stuck):
        state = {"maj": time.time(), "appareils": self.status, "ports_bloques": stuck,
                 "ports_sur_pi": [p for p in stuck if _port_on_pi(p)], "reparations": self.history}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)

    def cycle(self):
        devices = _devices()
        stuck = self.check_ports(devices)
        self.check_roles(devices)
        self.publish(stuck)


def main():
    print("[usb_watch] demarrage de l'entretien des peripheriques USB")
    usb_eteints.appliquer()
    watch = Watch()
    while True:
        try:
            watch.cycle()
        except Exception as exc:  # une erreur de lecture ne doit jamais arreter la veille
            print(f"[usb_watch] erreur pendant le passage : {exc!r}")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
