"""Client OctoPrint partage : pilotage vocal de l'imprimante (console/
imprimante_cmd.py) et annonces de ses evenements (watchers/
imprimante_watch.py), 2026-10-11.

API REST d'OctoPrint (cle "octoprint" des reglages) pour tout ce qu'elle
expose : etat, temperatures, deplacements, chauffe, travaux, fichiers.
Ce qu'elle n'expose PAS -- la position reelle des axes -- se lit en
envoyant M114 au firmware et en guettant sa reponse dans le flux du
websocket d'OctoPrint (/sockjs/websocket, lignes "Recv: X:.. Y:.. Z:..").
Mesure sur ce Pi (Ender 3 Pro, Marlin 2.1.2.5) : reponse en 0,5 s.
"""
import chemins
import asyncio
import collections
import json
import re
import threading
import time

import requests

import reglages

URL = "http://127.0.0.1:5000"
WS_URL = "ws://127.0.0.1:5000/sockjs/websocket"
TIMEOUT_SEC = 8
PROFILE_TTL_SEC = 300
POSITION_RE = re.compile(r"Recv:\s*X:\s*(-?[\d.]+)\s+Y:\s*(-?[\d.]+)\s+Z:\s*(-?[\d.]+)")
# Reponses du firmware qui meritent d'etre dites (refus, erreurs).
FIRMWARE_PROBLEM_RE = re.compile(r"Recv:\s*(Error:.*|echo:\s*(Unknown command.*|.*cold extrusion.*|"
                                 r".*[Hh]ome .* first.*|.*[Pp]rinter halted.*))")


class Erreur(Exception):
    """code : "cle" (pas de cle API), "refus" (cle refusee), "injoignable",
    "occupee" (409 : imprimante deconnectee, ou en train d'imprimer),
    "introuvable" (404)."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code} {detail}".strip())
        self.code, self.detail = code, detail


def _headers():
    key = reglages.cle("octoprint")
    if not key:
        raise Erreur("cle")
    return {"X-Api-Key": key}


def appel(method, path, data=None, timeout=TIMEOUT_SEC):
    """Appel REST ; rend le JSON (ou None pour 204). Leve Erreur."""
    try:
        resp = requests.request(method, URL + path, headers=_headers(), json=data, timeout=timeout)
    except requests.RequestException as exc:
        raise Erreur("injoignable", str(exc))
    if resp.status_code in (401, 403):
        raise Erreur("refus")
    if resp.status_code == 409:
        raise Erreur("occupee", resp.text.strip())
    if resp.status_code == 404:
        raise Erreur("introuvable")
    if resp.status_code >= 400:
        raise Erreur("injoignable", f"HTTP {resp.status_code} {resp.text.strip()[:200]}")
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


# --- lecture -------------------------------------------------------------------

def connexion():
    """Etat de la liaison serie : "Operational", "Printing", "Closed"..."""
    return (appel("GET", "/api/connection").get("current") or {}).get("state") or ""


def etat():
    """{"connecte", "texte", "flags", "buse": (reelle, cible), "plateau":
    (reelle, cible), "job": {...}}. Imprimante non connectee : seulement
    connecte=False et texte = etat de la liaison."""
    try:
        printer = appel("GET", "/api/printer")
    except Erreur as exc:
        if exc.code == "occupee":
            return {"connecte": False, "texte": connexion()}
        raise
    state = printer.get("state") or {}
    temps = printer.get("temperature") or {}

    def couple(key):
        t = temps.get(key) or {}
        return (t.get("actual"), t.get("target"))

    return {"connecte": True, "texte": state.get("text") or "", "flags": state.get("flags") or {},
            "erreur": state.get("error") or "", "buse": couple("tool0"), "plateau": couple("bed"),
            "job": appel("GET", "/api/job") or {}}


_profile = {"t": 0.0, "data": None}


def profil():
    """Profil courant : {"largeur", "profondeur", "hauteur", "vitesses": {x,y,z,e}}."""
    if _profile["data"] and time.time() - _profile["t"] < PROFILE_TTL_SEC:
        return _profile["data"]
    data = {"largeur": 200.0, "profondeur": 200.0, "hauteur": 200.0,
            "vitesses": {"x": 6000, "y": 6000, "z": 200, "e": 300}}
    try:
        for p in (appel("GET", "/api/printerprofiles").get("profiles") or {}).values():
            if p.get("current"):
                vol = p.get("volume") or {}
                data = {"largeur": float(vol.get("width") or 200), "profondeur": float(vol.get("depth") or 200),
                        "hauteur": float(vol.get("height") or 200),
                        "vitesses": {a: int((p.get("axes") or {}).get(a, {}).get("speed") or data["vitesses"][a])
                                     for a in "xyze"}}
    except Erreur:
        pass
    _profile.update(t=time.time(), data=data)
    return data


def prechauffages():
    """[(nom, buse, plateau)] des prechauffages d'OctoPrint (PLA, ABS...)."""
    try:
        profiles = (appel("GET", "/api/settings").get("temperature") or {}).get("profiles") or []
    except Erreur:
        return [("PLA", 200, 60), ("ABS", 240, 100)]
    return [(p.get("name") or "", int(p.get("extruder") or 0), int(p.get("bed") or 0)) for p in profiles if p.get("name")]


def fichiers():
    """[{"nom", "chemin", "origine", "duree"}] : fichiers d'impression,
    stockage local puis carte SD, plus recent d'abord."""
    data = appel("GET", "/api/files?recursive=true") or {}
    result = []

    def walk(entries):
        for e in entries:
            if e.get("type") == "folder":
                walk(e.get("children") or [])
            elif e.get("type") == "machinecode":
                result.append({"nom": e.get("display") or e.get("name"), "chemin": e.get("path") or e.get("name"),
                               "origine": e.get("origin") or "local", "date": e.get("date") or 0,
                               "duree": ((e.get("gcodeAnalysis") or {}).get("estimatedPrintTime"))})

    walk(data.get("files") or [])
    return sorted(result, key=lambda f: -(f["date"] or 0))


# --- reponses du firmware (websocket) -----------------------------------------

async def _query(gcodes, pattern, timeout):
    import aiohttp
    login = appel("POST", "/api/login", {"passive": True}) or {}
    if not login.get("session"):
        raise Erreur("refus", "pas de session")
    lines = []
    # Seules comptent les lignes arrivees APRES l'envoi de nos gcodes : a la
    # connexion, OctoPrint renvoie l'historique (cle "history") et le premier
    # message "current" peut encore porter des lignes plus anciennes. Le
    # 16/10 a 23:52, une ancienne position (X117,5) lue ici a fait croire la
    # sonde mal placee : l'origine abandonnait avant Z.
    premier = gcodes[0].split(";")[0].strip() if gcodes else ""
    envoi_re = re.compile(r"Send:\s*(N\d+\s+)?" + re.escape(premier)) if premier else None
    envoye = envoi_re is None
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL, heartbeat=20) as ws:
            await ws.send_str(json.dumps({"auth": f"{login['name']}:{login['session']}"}))
            appel("POST", "/api/printer/command", {"commands": list(gcodes)})
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    msg = await ws.receive(timeout=max(0.1, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                for cle, payload in json.loads(msg.data).items():
                    if cle != "current" or not isinstance(payload, dict):
                        continue
                    for line in payload.get("logs") or []:
                        if not envoye:
                            envoye = bool(envoi_re.search(line))
                            continue
                        lines.append(line)
                        found = pattern.search(line)
                        if found:
                            return found, lines
    return None, lines


def interroger(gcodes, pattern, timeout=5):
    """Envoie les gcodes et attend la premiere ligne du firmware qui
    correspond a pattern. Rend (match ou None, lignes vues)."""
    return asyncio.run(_query(gcodes, pattern, timeout))


def probleme_firmware(lines):
    """Premier refus/erreur du firmware dans ces lignes, en clair, ou ""."""
    for line in lines:
        found = FIRMWARE_PROBLEM_RE.search(line)
        if found:
            return found.group(1).strip()
    return ""


def position(timeout=5, attendre=True):
    """{"x", "y", "z"}, ou None sans reponse. attendre : apres la fin des
    mouvements en cours (M400) -- JAMAIS pendant une impression : M400 viderait
    le tampon de mouvements du firmware et marquerait la piece."""
    found, _ = interroger(["M400", "M114"] if attendre else ["M114"], POSITION_RE, timeout)
    if not found:
        return None
    return {a: float(v) for a, v in zip("xyz", found.groups())}


# --- actions -------------------------------------------------------------------

def _commande_brute(*gcodes):
    appel("POST", "/api/printer/command", {"commands": list(gcodes)})


def _sons(gcodes):
    """Boucle sonore tant que ces commandes font bouger la tete, le plateau
    ou le filament (lib/sons_imprimante.py, 2026-10-18)."""
    try:
        import sons_imprimante as sons
        nom = sons.genre(gcodes)
        if nom:
            sons.suivre_mouvement(nom, _commande_brute)
    except Exception as exc:  # un son ne doit jamais empecher une commande
        print(f"[octoprint] sons : {exc!r}")


def commande(*gcodes):
    _commande_brute(*gcodes)
    _sons(gcodes)


def deplacer(**axes):
    """Deplacement relatif : deplacer(x=10) ; vitesse du profil de l'axe."""
    speed = min(profil()["vitesses"][a] for a in axes)
    appel("POST", "/api/printer/printhead", {"command": "jog", "speed": speed, **axes})
    _sons(["G1 " + " ".join(f"{a.upper()}{v}" for a, v in axes.items())])


def origine(axes):
    appel("POST", "/api/printer/printhead", {"command": "home", "axes": list(axes)})
    _sons(["G28 " + " ".join(a.upper() for a in axes)])


def cible_buse(temp):
    appel("POST", "/api/printer/tool", {"command": "target", "targets": {"tool0": int(temp)}})


def cible_plateau(temp):
    appel("POST", "/api/printer/bed", {"command": "target", "target": int(temp)})


def extruder(mm):
    appel("POST", "/api/printer/tool", {"command": "select", "tool": "tool0"})
    appel("POST", "/api/printer/tool", {"command": "extrude", "amount": mm})
    _sons([f"G1 E{mm}"])


def vitesse(pourcent):
    appel("POST", "/api/printer/printhead", {"command": "feedrate", "factor": int(pourcent)})


def debit(pourcent):
    appel("POST", "/api/printer/tool", {"command": "flowrate", "factor": int(pourcent)})


def travail(action):
    """"pause", "resume", "cancel"."""
    if action in ("pause", "resume"):
        appel("POST", "/api/job", {"command": "pause", "action": action})
    else:
        appel("POST", "/api/job", {"command": action})


def imprimer(fichier):
    appel("POST", f"/api/files/{fichier['origine']}/{fichier['chemin']}", {"command": "select", "print": True})



def televerser(chemin_local, dossier="pieces", selectionner=False):
    """Envoie un fichier vers OctoPrint (stockage local), sans lancer d'impression.

    Rend {nom, chemin, origine}. L'imprimante peut etre eteinte : seul le
    serveur OctoPrint doit repondre.
    """
    import os
    key = reglages.cle("octoprint")
    if not key:
        raise Erreur("cle")
    path = os.path.abspath(chemin_local)
    name = os.path.basename(path)
    folder = (dossier or "").strip().strip("/")
    try:
        with open(path, "rb") as f:
            resp = requests.post(
                f"{URL}/api/files/local",
                headers={"X-Api-Key": key},
                files={"file": (name, f)},
                data={
                    "select": "true" if selectionner else "false",
                    "print": "false",
                    "path": folder,
                },
                timeout=120,
            )
    except requests.RequestException as exc:
        raise Erreur("injoignable", str(exc)) from exc
    if resp.status_code in (401, 403):
        raise Erreur("refus")
    if resp.status_code == 404:
        raise Erreur("introuvable")
    if resp.status_code >= 400:
        raise Erreur("refus", resp.text[:200])
    data = resp.json() if resp.content else {}
    files = data.get("files") or {}
    local = files.get("local") or data.get("file") or {}
    return {
        "nom": local.get("display") or local.get("name") or name,
        "chemin": local.get("path") or (f"{folder}/{name}" if folder else name),
        "origine": local.get("origin") or "local",
    }



def force_commande(*gcodes, baud=115200, port=None, reconnect=True):
    """Envoie des G-code en contournant la file OctoPrint.

    Necessaire quand Marlin est en "busy: paused for user" : sans
    EMERGENCY_PARSER, OctoPrint n'envoie plus M108 (ni cancel). On
    deconnecte un instant, parle au CH340, puis reconnecte.
    Rend les lignes lues (bytes decodees) ou leve Erreur.
    """
    import serial as pyserial
    # Port courant
    try:
        cur = (appel("GET", "/api/connection").get("current") or {})
        port = port or cur.get("port") or "/dev/ttyUSB0"
        baud = int(cur.get("baudrate") or baud)
    except Erreur:
        port = port or "/dev/ttyUSB0"
    # Liberer le port
    try:
        connecter(False)
    except Erreur:
        pass
    time.sleep(1.2)
    lines = []
    try:
        with pyserial.Serial(port, baudrate=baud, timeout=2) as ser:
            time.sleep(0.3)
            ser.reset_input_buffer()
            for g in gcodes:
                cmd = (g if g.endswith("\n") else g + "\n").encode("ascii", "ignore")
                ser.write(cmd)
                ser.flush()
                time.sleep(0.4)
            # Lire un peu de reponse
            deadline = time.time() + 2.5
            while time.time() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                lines.append(raw.decode("utf-8", "replace").strip())
    except Exception as exc:
        if reconnect:
            try:
                connecter(True)
            except Erreur:
                pass
        raise Erreur("injoignable", f"force_commande: {exc}") from exc
    if reconnect:
        time.sleep(0.8)
        last = None
        for _ in range(8):
            try:
                # Port explicite : evite un Connecting infini sans cible
                appel("POST", "/api/connection", {
                    "command": "connect",
                    "port": port,
                    "baudrate": baud,
                    "autoconnect": True,
                })
                last = None
                break
            except Erreur as exc:
                last = exc
                time.sleep(1.5)
        if last is not None:
            raise Erreur("injoignable", f"reconnect: {last}") from last
        # Pendant pause filament Marlin ne repond plus au handshake :
        # Connecting / Detecting + serial qui avance = liaison utile pour la veille.
        ok_states = (
            "Operational", "Printing", "Paused", "Cancelling", "Starting",
            "Detecting serial connection", "Connecting", "Opening serial connection",
        )
        for _ in range(16):
            st = connexion()
            if st in ("Operational", "Printing", "Paused", "Cancelling", "Starting"):
                break
            if st in ok_states and _serial_fenetre(6):
                break
            time.sleep(0.75)
    return lines


def _serial_tail(max_bytes=12000):
    """Derniers octets du serial.log OctoPrint."""
    path = chemins.home(".octoprint/logs/serial.log")
    try:
        import os
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _serial_fenetre(fenetre_sec=8, max_bytes=12000):
    """Lignes serial.log dont l'horodatage tombe dans la fenetre (secondes)."""
    import datetime as _dt
    tail = _serial_tail(max_bytes)
    if not tail:
        return ""
    now = _dt.datetime.now()
    kept = []
    for line in tail.splitlines():
        # 2026-10-13 19:18:30,752 - Recv: ...
        if len(line) < 23 or line[4] != "-" or line[10] != " ":
            continue
        try:
            ts = _dt.datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
        except ValueError:
            continue
        if (now - ts).total_seconds() <= fenetre_sec:
            kept.append(line)
    return "\n".join(kept)


def marlin_en_pause_filament(fenetre_sec=8):
    """True si dialogue filament encore actif (pause utilisateur OU processing)."""
    etat = etat_filament_serial()
    return bool(etat.get("erreur") or etat.get("phase") in ("attente", "action"))


def etat_filament_serial():
    """Releve technique du dialogue filament Marlin (serial.log).

    `busy: processing` sert aussi a M109/M190 : ne compte comme filament
    que avec un vrai contexte pause utilisateur / insert (pas "runout OFF").
    """
    recent = _serial_fenetre(12, 20000).lower()
    lookback = _serial_tail(24000).lower()
    base = recent if recent else lookback[-3000:]
    pause = "busy: paused for user" in base
    processing = "busy: processing" in base
    mid = _serial_fenetre(25, 24000).lower() or lookback[-5000:]
    pause_mid = "busy: paused for user" in mid
    processing_mid = "busy: processing" in mid
    ctx = _serial_fenetre(90, 60000).lower()
    insert_msg = "insert filament" in (ctx or mid)
    # Ne pas matcher "filament runout off"
    runout_on = (
        "filament runout on" in lookback[-16000:]
        or ("filament runout" in lookback[-16000:] and "filament runout off" not in lookback[-4000:])
    )
    filament_ctx = (
        "busy: paused for user" in (ctx or "")
        or insert_msg
        or ("insert filament" in lookback[-12000:])
    )
    demande = insert_msg or ("insert filament" in lookback[-12000:])
    bouton = "press button to heat nozzle" in lookback[-16000:]
    extrusion = "too long extrusion prevented" in lookback[-16000:]

    if pause or pause_mid:
        phase = "attente"
    elif filament_ctx and (processing or processing_mid):
        phase = "action"
    else:
        phase = "ok" if base else "inconnu"

    kind = None
    if extrusion:
        kind = "extrusion"
    elif bouton:
        kind = "bouton"
    elif insert_msg or (demande and phase == "attente"):
        kind = "manque"
    elif phase == "action":
        kind = "action"
    elif phase == "attente":
        kind = "manque"

    en_dialogue = phase in ("attente", "action")
    return {
        "pause": pause or pause_mid,
        "processing": bool(filament_ctx and (processing or processing_mid)),
        "phase": phase,
        "erreur": en_dialogue,
        "ok": phase == "ok",
        "demande_fil": bool(demande and phase == "attente"),
        "chauffe_bloquee": bouton,
        "extrusion_refusee": extrusion,
        "kind": kind,
        "runout_firmware": "cap:runout:1" in lookback,
        "ecran": (
            "inserer_fil_bouton" if insert_msg and phase != "ok"
            else "bouton_chauffe" if bouton
            else "ejection_ou_charge" if phase == "action"
            else "attente_bouton" if phase == "attente"
            else "ok" if phase == "ok"
            else "inconnu"
        ),
    }



def avance_dialogue_filament(max_etapes=6, timeout_etape=90):
    """Pousse le dialogue ADVANCED_PAUSE (equivalent boutons ecran).

    Sequence observee : attente → processing (charge) → parfois rejet → attente.
    Cause frequente du charge-puis-rejet : capteur RUNOUT encore ouvert, Marlin
    relance M600. On envoie M108 a chaque attente, et M412 S0 des que possible
    pour desactiver le capteur et casser la boucle.
    Rend un dict {ok, phase, etapes, detail}.
    """
    import serial as pyserial

    try:
        cur = (appel("GET", "/api/connection").get("current") or {})
        port = cur.get("port") or "/dev/ttyUSB0"
        baud = int(cur.get("baudrate") or 115200)
    except Erreur:
        port, baud = "/dev/ttyUSB0", 115200

    try:
        connecter(False)
    except Erreur:
        pass
    time.sleep(1.0)

    etapes = []
    ok = False
    detail = ""
    try:
        with pyserial.Serial(port, baudrate=baud, timeout=1.0) as ser:
            time.sleep(0.3)
            ser.reset_input_buffer()

            def _lire(sec=1.0):
                raw = b""
                t0 = time.time()
                while time.time() - t0 < sec:
                    n = ser.in_waiting
                    if n:
                        raw += ser.read(n)
                    else:
                        time.sleep(0.05)
                return raw.decode("utf-8", "replace").lower()

            def _envoi(*cmds):
                for c in cmds:
                    ser.write((c + "\n").encode("ascii", "ignore"))
                    ser.flush()
                    time.sleep(0.25)

            # Desactiver runout tot (souvent ignore pendant pause, reessaie plus tard)
            _envoi("M412 S0", "M108")
            buf = _lire(1.5)
            etapes.append("init")

            for i in range(max_etapes):
                t_fin = time.time() + timeout_etape
                phase = "inconnu"
                while time.time() < t_fin:
                    chunk = _lire(1.2)
                    buf = (buf + chunk)[-4000:]
                    if "busy: paused for user" in chunk or (
                        "busy: paused for user" in buf[-800:] and "busy: processing" not in chunk
                    ):
                        phase = "attente"
                        break
                    if "busy: processing" in chunk:
                        phase = "action"
                        # Pendant charge/ejection : ne pas spammer M108
                        continue
                    # Plus de busy : peut-etre sorti
                    if chunk and "busy:" not in chunk and "T:" in chunk:
                        # petites fenetre sans busy
                        if "busy: paused for user" not in buf[-600:] and "busy: processing" not in buf[-600:]:
                            phase = "libre"
                            break
                etapes.append(phase)
                if phase == "libre":
                    _envoi("M412 S0", "M108")
                    ok = True
                    detail = "dialogue termine"
                    break
                if phase == "attente":
                    # Bouton virtuel + coupe capteur
                    _envoi("M412 S0", "M108", "M108")
                    _lire(0.8)
                    continue
                if phase == "action":
                    # Attendre la fin du mouvement puis reboucler
                    t2 = time.time() + 120
                    while time.time() < t2:
                        chunk = _lire(1.0)
                        if "busy: paused for user" in chunk:
                            break
                        if "busy: processing" not in chunk and "busy:" not in chunk:
                            # trou : renvoyer M108 au cas ou
                            _envoi("M108")
                    continue
                # timeout etape
                _envoi("M412 S0", "M108")
                detail = f"timeout etape {i}"
            else:
                detail = detail or "max etapes"
            # Etat final d'apres derniers octets
            fin = _lire(2.0)
            buf = (buf + fin)[-2000:]
            if "busy: paused for user" not in buf and "busy: processing" not in buf:
                ok = True
                detail = detail or "plus de busy"
    except Exception as exc:
        detail = str(exc)
        raise Erreur("injoignable", f"avance_dialogue: {exc}") from exc
    finally:
        time.sleep(0.5)
        try:
            appel("POST", "/api/connection", {
                "command": "connect",
                "port": port,
                "baudrate": baud,
                "autoconnect": True,
            })
        except Erreur:
            try:
                connecter(True)
            except Erreur:
                pass
        for _ in range(12):
            st = connexion()
            if st in (
                "Operational", "Printing", "Paused", "Cancelling", "Starting",
                "Detecting serial connection", "Connecting", "Opening serial connection",
            ):
                break
            time.sleep(0.75)

    return {"ok": ok, "etapes": etapes, "detail": detail, "filament": etat_filament_serial()}



def desactiver_runout(persist=True):
    """Coupe le capteur filament (plus de dialogue bouton / M600)."""
    cmds = ["M412 S0"]
    if persist:
        cmds.append("M500")
    commande(*cmds)
    return True


def activer_runout(persist=True):
    cmds = ["M412 S1"]
    if persist:
        cmds.append("M500")
    commande(*cmds)
    return True



_MACHINE_CACHE = None
_MACHINE_CACHE_AT = 0.0
MACHINE_CACHE_TTL = 120.0


def _parse_caps_from_serial(tail):
    caps = {}
    for line in tail.splitlines():
        low = line.lower()
        if "recv: cap:" in low:
            part = line.split("Cap:", 1)[-1].strip()
            if ":" in part:
                k, v = part.split(":", 1)
                caps[k.strip().upper()] = v.strip()
    return caps


def _parse_m851(tail):
    # M851 X-49.99 Y-9.99 Z-0.50
    m = re.search(r"M851\s+X([-\d.]+)\s+Y([-\d.]+)\s+Z([-\d.]+)", tail, re.I)
    if not m:
        return None
    return {"x": float(m.group(1)), "y": float(m.group(2)), "z": float(m.group(3))}


def _parse_m119(tail):
    etats = {}
    for name in ("x_min", "y_min", "z_min", "filament"):
        m = re.search(rf"{name}:\s*(\w+)", tail, re.I)
        if m:
            etats[name] = m.group(1).lower()
    return etats


def detecter_machine(force=False):
    """Profil adaptatif de la machine connectee (caps + probe + plateau).

    Sur cette Ender : Z_PROBE=1 mais AUTOLEVEL=0 → probe pour G28 Z / G30,
    PAS de mesh M420. Offset probe typique M851 hors plateau a X0 Y0 →
    un G28 Z naif provoque « Z Probe Past Bed » / marteau-piqueur.
    """
    global _MACHINE_CACHE, _MACHINE_CACHE_AT
    now = time.time()
    if not force and _MACHINE_CACHE and now - _MACHINE_CACHE_AT < MACHINE_CACHE_TTL:
        return _MACHINE_CACHE

    prof = {}
    try:
        prof = profil()
    except Erreur:
        prof = {"largeur": 200.0, "profondeur": 200.0, "hauteur": 200.0}

    # Interroger firmware
    try:
        interroger(["M115", "M851", "M119", "M114"], re.compile(r"."), 12)
    except Erreur:
        pass
    time.sleep(0.4)
    tail = _serial_tail(12000)
    caps = _parse_caps_from_serial(tail)
    m851 = _parse_m851(tail)
    fins = _parse_m119(tail)
    # Position : meme ligne que Count → couper avant Count
    pos = None
    for line in reversed(tail.splitlines()):
        if 'X:' not in line or 'Y:' not in line:
            continue
        base = line.split('Count', 1)[0]
        m = re.search(r'X:([-\d.]+)\s+Y:([-\d.]+)\s+Z:([-\d.]+)', base)
        if m:
            pos = {'x': float(m.group(1)), 'y': float(m.group(2)), 'z': float(m.group(3))}
            break

    z_probe = caps.get("Z_PROBE", "0") == "1"
    autolevel = caps.get("AUTOLEVEL", "0") == "1"
    leveling_data = caps.get("LEVELING_DATA", "0") == "1"
    # AUTOLEVEL via caps seulement — ne PAS tester M420 (Unknown command vocalise)
    m420_ok = False

    largeur = float(prof.get("largeur") or 200)
    profondeur = float(prof.get("profondeur") or 200)
    ox = (m851 or {}).get("x") or 0.0
    oy = (m851 or {}).get("y") or 0.0
    # Nozzle XY pour que la SONDE (tete plastique a gauche, offset M851) soit au centre
    safe_x = max(5.0, min(largeur - 5.0, largeur / 2.0 - ox))
    safe_y = max(5.0, min(profondeur - 5.0, profondeur / 2.0 - oy))

    z_min = (fins or {}).get("z_min")
    # Avec BLTouch/CR-Touch sur z_min : "triggered" au repos = souvent sonde RENTREE,
    # pas la buse sur le plateau. Ne jamais annoncer "buse au contact" la-dessus.
    crash_suspect = False
    if z_probe:
        if z_min == "triggered":
            sonde_etat = "rentree_ou_appui"
        elif z_min == "open":
            sonde_etat = "libre"
        else:
            sonde_etat = z_min or "inconnu"
    else:
        sonde_etat = z_min or "inconnu"

    info = {
        "firmware": "Marlin",
        "caps": caps,
        "z_probe": z_probe,
        "autolevel": autolevel,
        "leveling_data": leveling_data,
        "m420": False,
        "probe_offset": m851,
        "fins_course": fins,
        "sonde_etat": sonde_etat,
        "sonde_explique": (
            "Sonde plateau (tete plastique a gauche de la buse), pas la buse."
            if z_probe else "Fin de course Z mecanique."
        ),
        "position": pos,
        "plateau": {"largeur": largeur, "profondeur": profondeur},
        "homing_safe_xy": {"x": round(safe_x, 2), "y": round(safe_y, 2)},
        "crash_suspect": False,
        "mode_leveling": (
            "mesh" if autolevel else ("probe_z" if z_probe else "manuel")
        ),
        "t": now,
    }
    _MACHINE_CACHE, _MACHINE_CACHE_AT = info, now
    try:
        path = "/tmp/imprimante_machine.json"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f)
        import os
        os.replace(tmp, path)
    except OSError:
        pass
    return info


def homing_sur(axes="xyz", timeout=180):
    """Origine adaptive en PHASES verifiees (anti marteau-piqueur).

    Sur machine a sonde (tete plastique a gauche, M851) :
      1) remonter Z
      2) toujours G28 X Y d'abord si on veut Z (meme pour « origine Z » seule)
      3) aller au XY ou la SONDE est au centre du plateau
      4) verifier M114 (sinon refuse G28 Z)
      5) G28 Z seulement alors, puis remonter a Z10

    Un G28 Z a X0 Y0 place la sonde hors plateau → descente sans trigger.
    """
    machine = detecter_machine(force=True)
    axes = (axes or "xyz").lower()
    want_z = "z" in axes or axes == "xyz"
    want_xy = "x" in axes or "y" in axes or axes == "xyz" or (
        want_z and machine.get("z_probe")
    )
    # Toute demande Z avec sonde implique XY d'abord
    if want_z and machine.get("z_probe"):
        want_xy = True

    def _phase(cmds, wait_pat=None, wait_sec=90):
        pat = wait_pat or POSITION_RE
        found, lines = interroger(cmds, pat, wait_sec)
        text = "\n".join(lines).lower()
        if "z probe past bed" in text:
            return None, lines, "probe_hors_plateau"
        prob = probleme_firmware(lines)
        if prob:
            return None, lines, prob
        return found, lines, None

    def _pos_ok(found, sx, sy, tol=8.0):
        if not found:
            return False
        try:
            x, y, z = (float(v) for v in found.groups())
        except Exception:
            return False
        return abs(x - sx) <= tol and abs(y - sy) <= tol

    # --- phase 0 : soft endstops + leve-toi ---
    _phase(["M211 S1", "G91", "G1 Z8 F400", "G90", "M400", "M114"], wait_sec=60)

    if want_xy:
        found, lines, err = _phase(["G28 X Y", "M400", "M114"], wait_sec=min(timeout, 120))
        if err:
            return False, err, machine
        if not found:
            return False, "xy_sans_reponse", machine

    if want_z:
        if machine.get("z_probe"):
            sx = float(machine["homing_safe_xy"]["x"])
            sy = float(machine["homing_safe_xy"]["y"])
            # Deplacement puis ATTENTE position confirmee (commande separee)
            found, lines, err = _phase(
                ["G90", f"G1 X{sx:.2f} Y{sy:.2f} F3000", "M400", "M114"],
                wait_sec=90,
            )
            if err:
                return False, err, machine
            if not _pos_ok(found, sx, sy):
                # Une seconde chance : renvoyer M114
                found2, _, err2 = _phase(["M114"], wait_sec=15)
                if err2 or not _pos_ok(found2, sx, sy):
                    return False, "pas_au_centre_sonde", machine
            # SEULEMENT MAINTENANT : G28 Z
            found, lines, err = _phase(
                ["G28 Z", "M400", "G1 Z12 F500", "M400", "M114"],
                wait_sec=min(timeout, 120),
            )
            if err:
                return False, err, machine
            if not found:
                return False, "z_sans_reponse", machine
        else:
            found, lines, err = _phase(
                ["G28 Z", "M400", "G1 Z12 F500", "M400", "M114"],
                wait_sec=min(timeout, 120),
            )
            if err:
                return False, err, machine
            if not found:
                return False, "z_sans_reponse", machine
    elif not want_xy:
        return False, "axes_invalides", machine

    machine = detecter_machine(force=True)
    return True, "ok", machine



def reconnect_imprimante(timeout=45):
    """Retablit OctoPrint apres M112 / coupure / Offline after error.

    1) disconnect API  2) reset DTR CH340 (reveille Marlin)  3) connect explicite
    """
    import serial as pyserial
    try:
        connecter(False)
    except Erreur:
        pass
    time.sleep(1.2)
    port = "/dev/ttyUSB0"
    try:
        with pyserial.Serial(port, baudrate=115200, timeout=1) as ser:
            ser.dtr = False
            time.sleep(0.15)
            ser.dtr = True
            time.sleep(2.0)
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
    except Exception as exc:
        # Port busy / absent : on tente quand meme le connect API
        pass
    time.sleep(0.5)
    last = None
    for _ in range(8):
        try:
            appel("POST", "/api/connection", {
                "command": "connect",
                "port": port,
                "baudrate": 115200,
                "printerProfile": "_default",
                "autoconnect": True,
            })
            last = None
            break
        except Erreur as exc:
            last = exc
            time.sleep(1.5)
    if last is not None:
        raise Erreur("injoignable", f"reconnect: {last}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = connexion()
        if st == "Operational":
            return st
        if st in ("Closed", "Offline", "Offline after error", "Error"):
            try:
                appel("POST", "/api/connection", {
                    "command": "connect",
                    "port": port,
                    "baudrate": 115200,
                    "autoconnect": True,
                })
            except Erreur:
                pass
        time.sleep(1.0)
    raise Erreur("injoignable", f"timeout reconnect ({connexion()})")



def connecter(on=True):
    appel("POST", "/api/connection", {"command": "connect", "autoconnect": True} if on else {"command": "disconnect"})


# --- flux en direct (calibrage, 2026-10-14) -----------------------------------
# interroger() ouvre un websocket par question : 1 a 2 s perdues a chaque fois.
# Pour le calibrage, un seul websocket reste ouvert ; chaque ligne du firmware
# est numerotee, les fins de course (M119) et les temperatures sont tenues a
# jour au fil de l'eau, et un sondeur relance M119 en boucle quand on le lui
# demande. Avec une sonde BLTouch, Marlin met ~0,5 s a repondre a M119 : la
# lecture des capteurs tourne donc a environ un releve par seconde.

ENDSTOP_RE = re.compile(r"Recv:\s*(x_min|y_min|z_min|z_probe|filament):\s*(\w+)", re.I)
BED_PROBE_RE = re.compile(r"Recv:\s*Bed X:\s*(-?[\d.]+)\s+Y:\s*(-?[\d.]+)\s+Z:\s*(-?[\d.]+)")
M211_RE = re.compile(r"Max:\s*X(-?[\d.]+)\s+Y(-?[\d.]+)\s+Z(-?[\d.]+)")
# Echecs de mouvement : Marlin les ecrit dans la langue de son ecran (ici le
# francais : "Echec origine"), parfois en anglais. Tout ce qui fige la machine.
ECHEC_RE = re.compile(r"Recv:.*(Homing Failed|Probing Failed|[EÉ]chec (origine|sonde|du palpage)|"
                      r"Printer halted|kill\(\) called|Z Probe Past Bed|(Stow|Deploy) Failed|BLTouch.*(alarm|fail))", re.I)
FIN_M119_RE = re.compile(r"Recv:\s*filament:", re.I)  # derniere ligne de M119 sur cette Ender
SEND_M114_RE = re.compile(r"Send:\s*(N\d+\s+)?M114\b")
# "Count" : pas reellement faits par les moteurs. Apres un arret sur butee ou
# sonde, X: Y: Z: donnent encore la position VISEE (vu le 14/10 : Z:142 annonce,
# moteur arrete a 143,95) ; seul Count dit ou la tete s'est arretee.
COUNT_RE = re.compile(r"Recv:\s*X:\s*(-?[\d.]+)\s+Y:\s*(-?[\d.]+)\s+Z:\s*(-?[\d.]+).*Count X:\s*(-?\d+)\s+"
                      r"Y:\s*(-?\d+)\s+Z:\s*(-?\d+)")
M92_RE = re.compile(r"M92 X(-?[\d.]+) Y(-?[\d.]+) Z(-?[\d.]+)")


class Flux:
    """Websocket OctoPrint ouvert en continu (voir plus haut)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._lignes = collections.deque(maxlen=600)   # (numero, ligne)
        self._numero = 0
        self.capteurs = {}        # {"x_min": "open"|"triggered", ...}
        self.capteurs_t = 0.0     # heure du dernier releve M119 complet
        self.temps = {}           # {"buse": (reelle, cible), "plateau": (...)}
        self.vivant = False
        self._arret = False
        self._sondage = 0.0       # periode du sondeur M119 (0 : arrete)
        self._abonnes = []        # fonctions appelees a chaque changement de capteur

    # -- cycle de vie --
    def demarrer(self):
        threading.Thread(target=self._boucle, daemon=True).start()
        threading.Thread(target=self._sondeur, daemon=True).start()
        deadline = time.time() + 6
        with self._cond:
            while not self.vivant and time.time() < deadline:
                self._cond.wait(0.2)
        return self.vivant

    def arreter(self):
        self._arret = True
        self._sondage = 0.0

    def abonner(self, fonction):
        """fonction(nom, ancien, nouveau) a chaque changement d'un capteur."""
        self._abonnes.append(fonction)

    def sonder(self, periode):
        """Relance M119 toutes les `periode` s (0 : arrete le sondeur)."""
        self._sondage = periode

    @property
    def numero(self):
        return self._numero

    # -- attente de reponses --
    def attendre(self, motif, depuis, timeout, echec=True):
        """Premiere ligne apres `depuis` qui correspond a motif. Rend
        (match ou None, lignes vues, ligne d'echec ou "")."""
        found, vues, ligne, _ = self._attendre(motif, depuis, timeout, echec)
        return found, vues, ligne

    def position_fraiche(self, gcodes, timeout, echec=True):
        """gcodes finissant par M114 : rend ({"x","y","z","count_z"} ou None,
        lignes vues, ligne d'echec). Ne lit que la reponse qui suit l'envoi de
        CE M114 : une position d'avant, arrivee en retard, ne peut pas servir."""
        depuis = self._numero
        commande(*gcodes)
        envoi, vues, ligne, n = self._attendre(SEND_M114_RE, depuis, timeout, echec)
        if ligne or not envoi:
            return None, vues, ligne
        found, vues2, ligne, _ = self._attendre(COUNT_RE, n, timeout, echec)
        if ligne or not found:
            return None, vues + vues2, ligne
        x, y, z = (float(v) for v in found.groups()[:3])
        return {"x": x, "y": y, "z": z, "count_z": int(found.group(6))}, vues + vues2, ""

    def _attendre(self, motif, depuis, timeout, echec=True):
        deadline = time.time() + timeout
        vues = []
        vu = depuis
        with self._cond:
            while True:
                for n, ligne in self._lignes:
                    if n <= vu:
                        continue
                    vu = n
                    vues.append(ligne)
                    if echec and ECHEC_RE.search(ligne):
                        return None, vues, ligne.split("Recv:", 1)[-1].strip(), n
                    found = motif.search(ligne)
                    if found:
                        return found, vues, "", n
                reste = deadline - time.time()
                if reste <= 0 or self._arret:
                    return None, vues, "", vu
                self._cond.wait(min(reste, 0.5))

    def envoyer(self, gcodes, motif, timeout, echec=True):
        """Envoie les gcodes et attend la ligne `motif` (voir attendre)."""
        depuis = self._numero
        commande(*gcodes)
        return self.attendre(motif, depuis, timeout, echec)

    # -- interne --
    def _ligne(self, ligne):
        changes = []
        with self._cond:
            self._numero += 1
            self._lignes.append((self._numero, ligne))
            found = ENDSTOP_RE.search(ligne)
            if found:
                nom, valeur = found.group(1).lower(), found.group(2).lower()
                ancien = self.capteurs.get(nom)
                self.capteurs[nom] = valeur
                self.capteurs_t = time.time()
                if ancien != valeur:
                    changes.append((nom, ancien, valeur))
            self._cond.notify_all()
        for nom, ancien, valeur in changes:
            for fonction in list(self._abonnes):
                try:
                    fonction(nom, ancien, valeur)
                except Exception as exc:  # un abonne qui plante ne coupe pas le flux
                    print(f"[octoprint] abonne capteurs : {exc!r}")

    def _temps(self, liste):
        if not liste:
            return
        dernier = liste[-1]
        for cle, nom in (("tool0", "buse"), ("bed", "plateau")):
            t = dernier.get(cle) or {}
            if t.get("actual") is not None:
                self.temps[nom] = (t.get("actual"), t.get("target"))

    def _sondeur(self):
        while not self._arret:
            periode = self._sondage
            if periode and self.vivant:
                # Attendre la reponse avant le M119 suivant : jamais de file
                # qui s'allonge dans OctoPrint (0,5 s par M119 avec la sonde).
                try:
                    self.envoyer(["M119"], FIN_M119_RE, 3, echec=False)
                except Erreur:
                    time.sleep(1.0)
                time.sleep(periode)
            else:
                time.sleep(0.2)

    def _boucle(self):
        while not self._arret:
            try:
                asyncio.run(self._session())
            except Exception as exc:
                print(f"[octoprint] flux interrompu : {exc!r}")
            with self._cond:
                self.vivant = False
                self._cond.notify_all()
            if not self._arret:
                time.sleep(1.0)

    async def _session(self):
        import aiohttp
        login = appel("POST", "/api/login", {"passive": True}) or {}
        if not login.get("session"):
            raise Erreur("refus", "pas de session")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL, heartbeat=20) as ws:
                await ws.send_str(json.dumps({"auth": f"{login['name']}:{login['session']}"}))
                with self._cond:
                    self.vivant = True
                    self._cond.notify_all()
                while not self._arret:
                    try:
                        msg = await ws.receive(timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        return
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    # "current" seulement : "history", envoye a la connexion,
                    # rejouerait de vieux releves et de vieux echecs.
                    payload = json.loads(msg.data).get("current")
                    if not isinstance(payload, dict):
                        continue
                    self._temps(payload.get("temps"))
                    for ligne in payload.get("logs") or []:
                        self._ligne(ligne)


def limites_firmware(flux=None, timeout=6):
    """Volume reel d'apres Marlin (M211) : {"x", "y", "z"} maxi, ou None.
    Le profil OctoPrint de ce Pi dit 200 x 200 alors que le firmware
    accepte 235 x 235 : le firmware fait foi pour les coins du plateau."""
    if flux is not None:
        found, _, _ = flux.envoyer(["M211"], M211_RE, timeout, echec=False)
    else:
        found, _ = interroger(["M211"], M211_RE, timeout)
    if not found:
        return None
    return {a: float(v) for a, v in zip("xyz", found.groups())}


# --- en clair (2026-10-14) ------------------------------------------------------
# OctoPrint et Marlin parlent anglais (ou le francais abrege de l'ecran) :
# ces deux fonctions rendent la phrase a dire. Cle "" : message a taire.
_LIAISONS = (("offline after error", "offline_after_error"), ("operational", "operational"),
             ("printing", "printing"), ("paused", "paused"), ("closed", "closed"), ("offline", "offline"),
             ("error", "error"), ("detecting", "detecting"), ("connecting", "connecting"),
             ("opening", "connecting"))
_MESSAGES = (
    ("homing failed", "imprimante.fw.echec_origine"), ("echec origine", "imprimante.fw.echec_origine"),
    ("échec origine", "imprimante.fw.echec_origine"),
    ("probing failed", "imprimante.fw.echec_mesure"), ("echec sonde", "imprimante.fw.echec_mesure"),
    ("échec sonde", "imprimante.fw.echec_mesure"),
    ("printer halted", "imprimante.fw.arret_securite"), ("kill() called", "imprimante.fw.arret_securite"),
    ("thermal runaway", "imprimante.fw.emballement"), ("heating failed", "imprimante.fw.chauffe_echouee"),
    ("mintemp", "imprimante.fw.temp_basse"), ("maxtemp", "imprimante.fw.temp_haute"),
    ("cold extrusion", "imprimante.fw.extrusion_froide"),
    ("stow failed", "imprimante.fw.sonde_tige"), ("deploy failed", "imprimante.fw.sonde_tige"),
    # Seulement la vraie alarme ("!! STOP called because of BLTouch error") :
    # le test M43 S ecrit "Check for BLTOUCH", "BLTouch ... detected" sans
    # aucune panne (fausse alerte du 2026-10-16).
    ("because of bltouch", "imprimante.fw.sonde_tige"),
    ("z probe past bed", "imprimante.evt_probe_hors_plateau"),
    ("endstops hit", "imprimante.fw.butee_touchee"),
)


def liaison_en_clair(texte):
    """"Offline after error" -> "coupee apres une erreur"."""
    from phrases import dire
    low = (texte or "").lower()
    for needle, code in _LIAISONS:
        if needle in low:
            return dire(f"imprimante.liaison.{code}")
    return message_en_clair(texte) if texte else dire("imprimante.liaison.closed")


def message_en_clair(texte):
    """Message du firmware ou d'erreur OctoPrint, en francais clair."""
    from phrases import dire
    low = (texte or "").lower()
    if "home" in low and "first" in low:  # "Home XYZ First", "Home Z First"
        return dire("imprimante.fw.origine_dabord")
    for needle, cle in _MESSAGES:
        if needle in low:
            return dire(cle)
    return dire("imprimante.fw.non_traduit", texte=texte)
