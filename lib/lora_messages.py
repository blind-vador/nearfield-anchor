"""Rafales LoRa (watchers/lora_watch.py) decodees tout de suite : chaque
rafale devient un travail du pont (lib/compagnon.py) des sa capture. La
machine compagne la decode (bridge/lora_decode.py, gr-lora_sdr) et le Pi
annonce le message aussitot. Le Pi ne decode lui-meme qu'en secours, si
aucune machine capable ne repond (choix utilisateur 2026-10-01 : decodage
LoRa et Meshtastic eteint sur le Pi tant que le Mac repond).

Interpretation (legere, toujours sur le Pi) : Meshtastic par
lib/meshtastic_lite.py (canal public par defaut seulement), APRS LoRa par
aprslib et la meme annonce que l'APRS classique (watchers/aprs_watch.py).
"""
import chemins
import json
import os
import shutil
import subprocess
import time

import compagnon
from phrases import dire
import veille_donnees as vd

GR_PYTHON = chemins.base("bin/python3-gnuradio")
DECODER = chemins.depot("bridge/lora_decode.py")  # script du depot (axe 4)
SOUND = "/media/nas/nd1/server/radio_scaner/decoding_numeric.mp3"
SYNC = {"meshtastic": 0x2B, "lora_aprs": 0x12}
SF_FIRST = {"meshtastic": 11, "lora_aprs": 12}  # LongFast europeen ; APRS LoRa
FALLBACK_AFTER_SEC = 20      # machine connectee mais muette : le Pi s'y met
LOCAL_MAX_TEMP_C = 72.0      # au-dessus, le secours attend que le Pi refroidisse
_nodes = vd.Memory("/tmp/lora_noeuds.json")


def submit_burst(cu8_path, info):
    """Appele par lora_watch a chaque rafale gardee : un travail par rafale."""
    profile = info["profil"]
    first = SF_FIRST.get(profile, 12)
    params = {"profil": profile, "fichier": "rafale.cu8", "taux": info["taux"], "largeur": info["largeur"],
              "sync": SYNC.get(profile, 0x12), "sfs": [first] + [sf for sf in (12, 11, 10, 9, 8, 7) if sf != first],
              "fiche": cu8_path[:-4] + ".json", "freq": info["freq"]}
    job_id, folder, job = compagnon.new_job("lora", params)
    shutil.copyfile(cu8_path, os.path.join(folder, "rafale.cu8"))
    compagnon.submit(job)
    return job_id


# --- secours sur le Pi --------------------------------------------------------------

def _temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except OSError:
        return 0.0


def needs_local(job, capable_connected):
    """Le Pi doit-il decoder lui-meme ce travail LoRa ?"""
    if _temp_c() > LOCAL_MAX_TEMP_C:
        return False
    return not capable_connected or time.time() - job.get("cree", 0) > FALLBACK_AFTER_SEC


def decode_locally(job):
    """Prend le travail (comme une machine compagne) et le decode au ralenti,
    avec le seul facteur d'etalement du profil (le Mac, lui, les essaie tous)."""
    folder = os.path.join(compagnon.BRIDGE_DIR, "travaux", job["id"])
    try:
        os.rename(os.path.join(folder, "travail.json"), os.path.join(folder, "travail.pris.json"))
    except OSError:
        return  # pris entre-temps par une machine compagne
    p = job["params"]
    try:
        out = subprocess.run(["nice", "-n", "19", GR_PYTHON, DECODER, os.path.join(folder, p["fichier"]),
                              str(p["taux"]), str(p["largeur"]), str(p["sync"]), str(p["sfs"][0])],
                             capture_output=True, text=True, timeout=180).stdout
        frames = json.loads(out.strip().splitlines()[-1]) if out.strip() else []
    except (subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        print(f"[lora] decodage local {job['id']} : {exc!r}")
        frames = []
    try:
        handle(job, frames, dire("lora.pi"))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# --- interpretation et annonce ---------------------------------------------------------

def _node_name(node_id):
    return _nodes.data.get("noms", {}).get(node_id) or vd.spell(node_id.lstrip("!"))


def _meshtastic(frame):
    import meshtastic_lite
    info = meshtastic_lite.decode_meshtastic(frame)
    if not info:
        return None, info
    sender = info["de"]
    if not info.get("lisible"):
        return None, info  # canal prive : archive seulement
    kind = info.get("type")
    if kind == "nœud" and info.get("nom"):
        known = _nodes.data.setdefault("noms", {})
        new = sender not in known
        known[sender] = info["nom"]
        _nodes.save()
        return (dire("lora.nouveau_ud_meshtastic", nom=info['nom']) if new else None), info
    if kind == "texte" and info.get("texte"):
        to = dire("lora.tous") if info["pour"] == "tous" else dire("lora.phrase", v1=_node_name(info['pour']))
        return dire("lora.message_meshtastic", v1=_node_name(sender), to=to, texte=info['texte']), info
    if kind == "position" and info.get("lat") and _nodes.due(f"pos|{sender}", 3600):
        return dire("lora.ud_meshtastic", v1=_node_name(sender), where=vd.where(info['lat'], info['lon'])), info
    return None, info


def _lora_aprs(frame):
    import meshtastic_lite
    text = meshtastic_lite.decode_lora_aprs(frame)
    if not text:
        return False, {"brut": frame.hex()}
    try:
        import aprslib
        import aprs_watch
        packet = aprslib.parse(text)
        return aprs_watch._process(packet), {"tnc2": text}  # annonce comme l'APRS classique
    except Exception as exc:
        return False, {"tnc2": text, "erreur": repr(exc)}


def handle(job, frames, by):
    """Trames decodees (par la machine compagne ou le Pi) -> annonces, fiche."""
    p = job["params"]
    good = [bytes.fromhex(f["octets"]) for f in frames if f.get("crc_ok")]
    decoded = []
    for frame in good:
        if p["profil"] == "meshtastic":
            text, info = _meshtastic(frame)
            if text:
                vd.announce(text, SOUND, "message_meshtastic", "lora",
                            fiche={"protocole": "Meshtastic", "frequence_hz": p.get("freq"), "source": (info or {}).get("de")})
        else:
            _, info = _lora_aprs(frame)
        decoded.append(info)
    record = {"profil": p["profil"], "freq": p.get("freq"), "par": by, "trames": len(good),
              "trames_abimees": len(frames) - len(good), "detail": decoded}
    vd.archive("lora_decode", record)
    try:  # la fiche de la rafale, sur le disque de stockage
        fiche = json.load(open(p["fiche"], encoding="utf-8"))
        fiche["decode"] = record
        json.dump(fiche, open(p["fiche"], "w", encoding="utf-8"), ensure_ascii=False, default=str)
    except (OSError, ValueError, KeyError):
        pass
    print(f"[lora] {p['profil']} decode par {by} : {len(good)} trame(s) valide(s), {len(frames) - len(good)} abimee(s)")
    if good:  # la piqure LoRa ne vient que d'une vraie trame (2026-10-03)
        try:
            import attention
            attention.stimulate("lora", attention.CAPTURE_POINTS)
        except Exception as exc:
            print(f"[lora] attention : {exc!r}")
