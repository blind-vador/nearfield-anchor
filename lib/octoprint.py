"""Client OctoPrint partage : pilotage vocal de l'imprimante (console/
imprimante_cmd.py) et annonces de ses evenements (watchers/
imprimante_watch.py), 2026-10-11.

API REST d'OctoPrint (cle "octoprint" des reglages) pour tout ce qu'elle
expose : etat, temperatures, deplacements, chauffe, travaux, fichiers.
Ce qu'elle n'expose PAS -- la position reelle des axes -- se lit en
envoyant M114 au firmware et en guettant sa reponse dans le flux du
websocket d'OctoPrint (/sockjs/websocket, lignes "Recv: X:.. Y:.. Z:..").
Mesure sur deepspace9 (Ender 3 Pro, Marlin 2.1.2.5) : reponse en 0,5 s.
"""
import asyncio
import json
import re
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
                for payload in json.loads(msg.data).values():
                    if not isinstance(payload, dict):
                        continue
                    for line in payload.get("logs") or []:
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

def commande(*gcodes):
    appel("POST", "/api/printer/command", {"commands": list(gcodes)})


def deplacer(**axes):
    """Deplacement relatif : deplacer(x=10) ; vitesse du profil de l'axe."""
    speed = min(profil()["vitesses"][a] for a in axes)
    appel("POST", "/api/printer/printhead", {"command": "jog", "speed": speed, **axes})


def origine(axes):
    appel("POST", "/api/printer/printhead", {"command": "home", "axes": list(axes)})


def cible_buse(temp):
    appel("POST", "/api/printer/tool", {"command": "target", "targets": {"tool0": int(temp)}})


def cible_plateau(temp):
    appel("POST", "/api/printer/bed", {"command": "target", "target": int(temp)})


def extruder(mm):
    appel("POST", "/api/printer/tool", {"command": "select", "tool": "tool0"})
    appel("POST", "/api/printer/tool", {"command": "extrude", "amount": mm})


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


def connecter(on=True):
    appel("POST", "/api/connection", {"command": "connect", "autoconnect": True} if on else {"command": "disconnect"})
