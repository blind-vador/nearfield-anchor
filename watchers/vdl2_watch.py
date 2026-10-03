"""Veille VDL2 (VHF Data Link mode 2, 136,725 a 136,975 MHz) : successeur de
l'ACARS classique, beaucoup plus bavard en Europe.

dumpvdl2 (compile depuis les sources, comme acarsdec) ecoute les quatre
frequences europeennes d'un seul coup (moins de 250 kHz d'ecart) et sort
une ligne JSON par trame. Toutes les trames sont archivees.

Annonces au fil de l'eau (2026-10-03) : chaque trame est rattachee a son
avion (emise par lui ou par une station au sol qui lui parle) et dite des
qu'elle arrive, en langage humain (lib/acars_traduction.py), comme une
prise vocale du journal "messages" (lib/prise_vocale.py). Un vrai contenu
(ACARS, passage de relais avec destination, controle aerien, position) est
dit a chaque fois, sauf une retransmission identique (REPEAT_SAME_SEC) ;
le simple entretien de la liaison, une fois par avion et par passage.
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
REPEAT_SAME_SEC = 300        # meme contenu du meme avion : retransmission, pas redit
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
    linked = set()     # avions dont l'entretien de liaison est deja dit dans ce passage
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
                    try:
                        if _on_frame(plane, avlc, (frame.get("vdl2") or {}).get("freq"), linked):
                            announced = True
                    except Exception as exc:  # une annonce ratee n'arrete pas la veille
                        print(f"[vdl2_watch] annonce {plane} : {exc!r}")
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if count:
        print(f"[vdl2_watch] {count} trame(s) VDL2 en {dwell_sec:.0f} s")
    _memory.save()
    return announced


def _on_frame(plane, avlc, freq, linked):
    """Une trame : annonce mise en file si elle dit quelque chose de neuf.
    Rend True si c'est un vrai contenu (piqure de la fenetre)."""
    kind, detail = tr.frame_kind(avlc)
    content = tr.substantive(kind, detail)
    if content:
        signature = json.dumps(detail, sort_keys=True, default=str)[:300]
        if not _memory.due(f"contenu|{plane}|{signature}", REPEAT_SAME_SEC):
            return False
    elif plane in linked:
        return False
    linked.add(plane)
    acars = avlc.get("acars") or {}
    fiche = {"protocole": "VDL2", "frequence_hz": freq, "source": plane, "nature": kind, "details": detail}

    def text():  # dans la file d'annonces : base d'avions et Gemini sans bloquer l'ecoute
        card = avions_info.card(hex_addr=plane, registration=(acars.get("reg") or "").lstrip(".") or None,
                                callsign=(acars.get("flight") or "").strip() or None)
        fiche["source"] = " ".join(x for x in (card.get("modele"), card.get("immatriculation"), card.get("indicatif")) if x) or plane
        fiche["appareil"] = card
        if content:
            said, _ = tr.explain(card, [{"nature": kind, "details": detail}], "VDL2")
        else:
            said = tr.plain(card, [kind], "VDL2")
        vd.archive("vdl2_traduit", {"adresse": plane, "texte": said, "fiche": fiche})
        return said

    vd.announce(text, SOUND, f"message_avion_{plane}", "vdl2_watch", fiche=fiche)
    return content
