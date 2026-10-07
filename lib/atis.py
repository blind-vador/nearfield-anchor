"""ATIS (2026-10-07) : bulletin meteo automatique d'aeroport, diffuse en
boucle, en clair, en AM sur la bande aviation. Le Pi capte une boucle,
Gemini la transcrit et en tire la meteo ; stocke par aeroport pour la
commande "atis" et le flash. Donnee publique recue en direct : aucune
communication privee, aucun echange individuel.

Capte par radio_watch (fenetre "atis" du cycle, meme cle) : rtl_fm -M am,
une boucle d'environ CAPTURE_SEC secondes, encodee en ogg/opus (leger pour
Gemini). Gemini n'est appele que lorsqu'un aeroport est "du" (REFRESH_SEC) :
l'ATIS ne change qu'a chaque nouvelle lettre d'information (environ 30 min).
"""
import json
import os
import subprocess
import time

import gemini

STORE_PATH = "/home/vador/autopilot/atis.json"
CAPTURE_SEC = 40          # une boucle ATIS complete tient en general sous 40 s
REFRESH_SEC = 1500        # pas plus d'un appel Gemini par aeroport toutes les 25 min
RATE = 12000

# Aeroports suivis : OACI -> (nom parle, frequence Hz). Les deux locaux
# (reglage "avions") ; Saint-Etienne n'emet parfois qu'aux heures ouvrables.
AIRPORTS = {
    "LFMH": ("Saint-Étienne Bouthéon", 132_800_000),
    "LFLL": ("Lyon Saint-Exupéry", 126_175_000),
}

PROMPT = """Tu transcris un message ATIS d'aeroport (meteo automatique en boucle, en anglais ou francais aeronautique)
capte a {aeroport}. Rends la meteo pour une personne aveugle, en francais clair, en JSON strict :
{{
 "entendu": true/false,                 // false si l'audio ne contient pas d'ATIS lisible (silence, parasites)
 "lettre": "lettre d'information (Alpha, Bravo...) ou chaine vide",
 "resume": "une phrase a lire a voix haute : l'essentiel (vent, visibilite, plafond, temperature, QNH, piste)",
 "piste": "piste en service ou chaine vide",
 "vent": "vent en clair (direction en degres, vitesse en noeuds) ou chaine vide",
 "qnh": "pression QNH en hectopascals ou chaine vide",
 "avertissements": "cisaillement, givrage, orages... ou chaine vide"
}}
N'invente rien : si tu n'entends pas une valeur, laisse sa chaine vide. Convertis les termes aeronautiques en mots simples."""


def _load():
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _touched(entry):
    """Dernier contact (succes ou simple essai) : pour la rotation entre
    aeroports et le quota, un aeroport muet ne doit pas monopoliser les essais."""
    return max(entry.get("t", 0) or 0, entry.get("dernier_essai", 0) or 0)


def _save(data):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, STORE_PATH)


def capture(freq_hz, seconds=CAPTURE_SEC):
    """Enregistre une boucle AM et la rend en ogg/opus (octets), ou None."""
    from lecture_flux import arreter
    samples = int((seconds + 0.3) * RATE)
    rtl = subprocess.Popen(["rtl_fm", "-f", str(int(freq_hz)), "-M", "am", "-s", str(RATE),
                            "-g", "40", "-E", "dc", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    enc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(RATE),
                            "-ac", "1", "-i", "pipe:0", "-t", str(seconds), "-c:a", "libopus", "-b:a", "12k",
                            "-f", "ogg", "pipe:1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    audio = bytearray()
    try:
        end = time.time() + seconds
        written = 0
        target = samples * 2
        while time.time() < end and written < target:
            chunk = rtl.stdout.read(65536)
            if not chunk:
                break
            try:
                enc.stdin.write(chunk)
                written += len(chunk)
            except BrokenPipeError:
                break
    finally:
        arreter(rtl, "rtl_fm (ATIS)")
        try:
            out, _ = enc.communicate(timeout=15)
            audio = out
        except subprocess.TimeoutExpired:
            enc.kill()
            audio = b""
    return bytes(audio) or None


def refresh_one(oaci, force=False):
    """Capture + transcription de cet aeroport si du (ou force). Rend le dict stocke, ou None."""
    if oaci not in AIRPORTS:
        return None
    name, freq = AIRPORTS[oaci]
    data = _load()
    prev = data.get(oaci) or {}
    if not force and time.time() - _touched(prev) < REFRESH_SEC:
        return prev or None
    audio = capture(freq)
    if not audio:
        return prev or None
    answer, reason = gemini.ask(PROMPT.format(aeroport=name), temperature=0.1, json_mode=True,
                                audio=audio, audio_mime="audio/ogg", timeout=90)
    try:
        result = json.loads(answer) if answer else None
    except ValueError:
        result = None
    if not isinstance(result, dict):
        print(f"[atis] {oaci} : {reason or 'reponse illisible'}")
        return prev or None
    if not result.get("entendu"):
        # Rien de lisible : on note l'essai sans ecraser un bulletin encore valable.
        prev["dernier_essai"] = time.time()
        data[oaci] = prev
        _save(data)
        return prev or None
    result.update(oaci=oaci, aeroport=name, frequence_hz=freq, t=time.time(), dernier_essai=time.time())
    data[oaci] = result
    _save(data)
    return result


def due_airport():
    """OACI le plus en retard de rafraichissement (le plus ancien d'abord)."""
    data = _load()
    return min(AIRPORTS, key=lambda o: _touched(data.get(o) or {}))


def refresh_due():
    """Un aeroport du, pour la fenetre du scanner. Rend le dict ou None."""
    return refresh_one(due_airport())


def latest(oaci=None, max_age_sec=4 * 3600):
    """Dernier bulletin frais d'un aeroport (ou tous), les plus recents d'abord."""
    data = _load()
    now = time.time()
    items = [v for k, v in data.items() if (oaci is None or k == oaci) and v.get("resume") and now - v.get("t", 0) < max_age_sec]
    return sorted(items, key=lambda v: v.get("t", 0), reverse=True)
