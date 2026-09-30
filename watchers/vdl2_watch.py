"""Veille VDL2 (VHF Data Link mode 2, 136,725 a 136,975 MHz) : successeur de
l'ACARS classique, beaucoup plus bavard en Europe.

dumpvdl2 (compile depuis les sources, comme acarsdec) ecoute les quatre
frequences europeennes d'un seul coup (moins de 250 kHz d'ecart) et sort
une ligne JSON par trame. Les messages ACARS transportes par VDL2 sont
convertis au format d'acarsdec et confies a acars_watch : memes regles
d'annonce (texte libre, traduction OOOI, anti-repetition), une seule
logique a maintenir. Toutes les trames sont archivees.
"""
import json
import os
import shutil
import subprocess
import threading
import time

import acars_watch
import veille_donnees as vd

VDL2_FREQUENCIES = ["136725000", "136775000", "136875000", "136975000"]
LOCAL_BINARY = "/home/vador/autopilot/bin/dumpvdl2"
GAIN_DB = "40"


def _binary():
    return shutil.which("dumpvdl2") or (LOCAL_BINARY if os.path.exists(LOCAL_BINARY) else None)


def _to_acars(frame):
    """Trame dumpvdl2 -> message au format acarsdec (ou None)."""
    acars = ((frame.get("vdl2") or {}).get("avlc") or {}).get("acars")
    if not acars:
        return None
    return {"flight": (acars.get("flight") or "").strip() or None,
            "tail": (acars.get("reg") or "").lstrip(".").strip() or None,
            "label": acars.get("label", ""), "sublabel": acars.get("sublabel"),
            "text": acars.get("msg_text") or ""}


def _reader(proc, frames):
    for raw in proc.stdout:
        try:
            frames.append(json.loads(raw))
        except ValueError:
            continue


def scan_window(dwell_sec=30):
    """Une passe d'ecoute VDL2. Rend True si au moins une annonce a eu lieu."""
    binary = _binary()
    if binary is None:
        print("[vdl2_watch] dumpvdl2 introuvable")
        return False
    proc = subprocess.Popen([binary, "--output", "decoded:json:file:path=-", "--rtlsdr", "0",
                             "--gain", GAIN_DB] + VDL2_FREQUENCIES,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frames = []
    threading.Thread(target=_reader, args=(proc, frames), daemon=True).start()
    announced = False
    count = 0
    try:
        deadline = time.time() + dwell_sec
        while time.time() < deadline:
            if proc.poll() is not None:
                print("[vdl2_watch] dumpvdl2 s'est arrete (dongle occupe ?)")
                break
            time.sleep(0.2)
            while frames:
                frame = frames.pop(0)
                count += 1
                vd.archive("vdl2", frame)
                msg = _to_acars(frame)
                if msg and acars_watch._process_message(msg):
                    announced = True
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if count:
        print(f"[vdl2_watch] {count} trame(s) VDL2 en {dwell_sec:.0f} s")
    return announced
