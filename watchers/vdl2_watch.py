"""Veille VDL2 (VHF Data Link mode 2, 136,725 a 136,975 MHz) : successeur de
l'ACARS classique, beaucoup plus bavard en Europe.

dumpvdl2 (compile depuis les sources, comme acarsdec) ecoute les quatre
frequences europeennes d'un seul coup (moins de 250 kHz d'ecart) et sort
une ligne JSON par trame. Toutes les trames sont archivees.

Annonces (2026-10-03) : les trames d'un passage sont regroupees par avion
(emises par lui ou par une station au sol qui lui parle) et dites en
langage humain par lib/acars_traduction.py : qui est l'avion, a quoi il
ressemble, sa phase de vol, ce que ses messages veulent dire. Un vrai
contenu (ACARS, passage de relais avec destination, controle aerien,
position) est explique par Gemini a chaque nouveaute ; le seul entretien
de la liaison est dit en une phrase fixe, une fois par AIRCRAFT_REPEAT_SEC.
"""
import json
import os
import shutil
import subprocess
import threading
import time

import acars_traduction as tr
import acars_watch
import avions_info
import veille_donnees as vd

VDL2_FREQUENCIES = ["136725000", "136775000", "136875000", "136975000"]
LOCAL_BINARY = "/home/vador/autopilot/bin/dumpvdl2"
GAIN_DB = "40"
SOUND = acars_watch.ACARS_SOUND
AIRCRAFT_REPEAT_SEC = 1800   # un avion deja signale n'est redit qu'apres 30 min
_memory = vd.Memory("/tmp/vdl2_watch_state.json")


def _binary():
    return shutil.which("dumpvdl2") or (LOCAL_BINARY if os.path.exists(LOCAL_BINARY) else None)


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
    by_aircraft = {}   # adresse ICAO -> trames du passage
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
                avlc = (frame.get("vdl2") or {}).get("avlc") or {}
                plane = next((side.get("addr") for side in (avlc.get("src") or {}, avlc.get("dst") or {})
                              if side.get("type") == "Aircraft" and side.get("addr")), None)
                if plane:
                    by_aircraft.setdefault(plane, []).append(avlc)
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if count:
        print(f"[vdl2_watch] {count} trame(s) VDL2 en {dwell_sec:.0f} s")
    for plane, frames_of in by_aircraft.items():
        try:
            if _announce_aircraft(plane, frames_of):
                announced = True
        except Exception as exc:  # une annonce ratee n'arrete pas la veille
            print(f"[vdl2_watch] annonce {plane} : {exc!r}")
    _memory.save()
    return announced


def _announce_aircraft(plane, frames_of):
    messages = []
    for avlc in frames_of:
        kind, detail = tr.frame_kind(avlc)
        messages.append({"nature": kind, "details": detail, "fond": tr.substantive(kind, detail)})
    content = [m for m in messages if m["fond"]]
    if content:
        signature = json.dumps([m["details"] for m in content], sort_keys=True, default=str)[:300]
        if not _memory.due(f"contenu|{plane}|{signature}", AIRCRAFT_REPEAT_SEC):
            return False
        _memory.due(f"avion|{plane}", AIRCRAFT_REPEAT_SEC)
        chosen = content
    elif _memory.due(f"avion|{plane}", AIRCRAFT_REPEAT_SEC):
        chosen = list({m["nature"]: m for m in messages}.values())
    else:
        return False
    acars = next((a.get("acars") for a in frames_of if a.get("acars")), {}) or {}
    card = avions_info.card(hex_addr=plane, registration=(acars.get("reg") or "").lstrip(".") or None,
                            callsign=(acars.get("flight") or "").strip() or None)
    if content:
        text, fiche = tr.explain(card, chosen)
    else:
        text, fiche = tr.plain(card, [m["nature"] for m in chosen]), None
    vd.archive("vdl2_traduit", {"adresse": plane, "texte": text, "fiche": fiche or card})
    vd.announce(text, SOUND, "vdl2", "vdl2_watch")
    return True
