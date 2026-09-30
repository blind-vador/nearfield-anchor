"""Veille des balises de detresse COSPAS-SARSAT, 406,0 a 406,1 MHz (bateaux,
randonneurs, petits avions). Tres rare, mais capitale si ca arrive pres
d'ici.

dec406_scan (logiciel libre, licence MIT, compile depuis les sources)
detecte les salves sur toute la bande et les decode. Une balise emet une
salve toutes les ~50 s : fenetre de DWELL au moins aussi longue, espacee
par l'attention (DATA_MIN_INTERVAL). Toute detection est annoncee (les
salves d'essai aussi, en le disant) et archivee.
"""
import os
import re
import shutil
import subprocess
import threading
import time

import veille_donnees as vd
from sound_paths import RADIO_SCANER_DIR

LOCAL_BINARY = "/home/vador/autopilot/bin/dec406_scan"
SOUND = os.path.join(RADIO_SCANER_DIR, "scaner_error.mp3")
STATE_PATH = "/tmp/balises_watch_state.json"
REPEAT_SEC = 600
POSITION = re.compile(r"Position[^:]*:\s*(-?[\d.]+)\s*([NS]),\s*(-?[\d.]+)\s*([EW])")

_memory = vd.Memory(STATE_PATH)


def _binary():
    return shutil.which("dec406_scan") or (LOCAL_BINARY if os.path.exists(LOCAL_BINARY) else None)


def _field(block, name):
    m = re.search(rf"^{name}:\s*(.+)$", block, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _describe(block):
    hex_id = _field(block, "Hex ID")
    country = _field(block, "Country")
    ident = _field(block, "Identification")
    test = _field(block, "Test Protocol").lower() in ("active", "yes", "oui", "1", "true")
    text = "Essai de balise de détresse capté" if test else "Balise de détresse 406 mégahertz captée"
    if country and not country.startswith("0 "):
        text += f", pays {country.split('(')[-1].rstrip(')')}"
    if ident:
        text += f", {ident}"
    m = POSITION.search(block)
    if m:
        lat = float(m.group(1)) * (1 if m.group(2) == "N" else -1)
        lon = float(m.group(3)) * (1 if m.group(4) == "E" else -1)
        if abs(lat) > 0.01 or abs(lon) > 0.01:
            text += f", position {vd.where(lat, lon)}"
    return hex_id, text + "."


def _reader(proc, out):
    for raw in proc.stdout:
        out.append(raw.decode("utf-8", errors="replace").rstrip())


def scan_window(dwell_sec=55):
    binary = _binary()
    if binary is None:
        print("[balises_watch] dec406_scan introuvable")
        return False
    env = dict(os.environ, DEC406_BACKEND="rtl")
    proc = subprocess.Popen([binary, "406.0M", "406.1M"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd=os.path.dirname(binary), env=env)
    out = []
    threading.Thread(target=_reader, args=(proc, out), daemon=True).start()
    announced = False
    try:
        deadline = time.time() + dwell_sec
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    text = "\n".join(out)
    for block in text.split("=== 406 MHz BEACON DECODE")[1:]:
        vd.archive("balises", {"sortie": block[:2000]})
        hex_id, sentence = _describe(block)
        if _memory.due(f"balise|{hex_id or sentence}", REPEAT_SEC):
            vd.announce(sentence, SOUND, f"balise_406_{hex_id}", "balises_watch")
            announced = True
    _memory.save()
    return announced
