"""Veille des messageries de pagers en 466 MHz (reseau e*Message :
alertes d'equipes, dont des pompiers volontaires). Messages non chiffres,
POCSAG ou FLEX, decodes par multimon-ng.

Une frequence du reseau par passage, en tourniquet (rtl_fm ne tient qu'une
frequence a la fois). Messages alphanumeriques lisibles annonces (anti-
repetition), tout est archive.
"""
import os
import re
import subprocess
import threading
import time

import veille_donnees as vd
from sound_paths import RADIO_SCANER_DIR
from phrases import dire

FREQUENCIES_HZ = [466_025_000, 466_050_000, 466_075_000, 466_175_000, 466_206_250, 466_231_250]
SAMPLE_RATE = 22050
SOUND = os.path.join(RADIO_SCANER_DIR, "decoding_morse.mp3")
STATE_PATH = "/tmp/pagers_watch_state.json"
REPEAT_SEC = 1800
MIN_LETTERS = 6            # en dessous : numerique, bruit ou trame de service
# POCSAG : "POCSAG1200: Address: 123456  Function: 3  Alpha:   texte"
# FLEX   : "FLEX|date|1600/2/K/A|12.345|000123456|ALN|texte"
ALPHA = re.compile(r"Alpha:\s*(.*)$|\|ALN\|(.*)$")
NUMERIC = re.compile(r"Numeric:\s*(.*)$|\|NUM\|(.*)$")
ADDRESS = re.compile(r"Address:\s*(\d+)|\|0*(\d+)\|(?:ALN|NUM|TON)")

_memory = vd.Memory(STATE_PATH)
_next = 0


def _readable(text):
    text = re.sub(r"<[A-Z]{2,4}>", " ", text)  # <NUL>, <EOT>... de multimon-ng
    text = " ".join(text.split())
    letters = sum(c.isalpha() for c in text)
    return text if letters >= MIN_LETTERS and letters >= len(text) * 0.5 else ""


def _other_page(line, freq):
    """Appel sans texte (bip) ou message numerique : dit aussi, une fois par
    adresse et par REPEAT_SEC (demande utilisateur : tout ce qui est capte)."""
    m = ADDRESS.search(line)
    if not m:
        return False
    address = (m.group(1) or m.group(2)).lstrip("0") or "0"
    mhz = f"{freq / 1e6:.4f}".rstrip("0").replace(".", ",")
    n = NUMERIC.search(line)
    digits = " ".join((n.group(1) or n.group(2) or "").split()) if n else ""
    if not _memory.due(f"pager_adresse|{address}|{digits}", REPEAT_SEC):
        return False
    if digits:
        text = dire("pager.numerique", chiffres=digits, adresse=address, frequence=mhz)
    else:
        text = dire("pager.appel_sans_texte", adresse=address, frequence=mhz)
    vd.announce(text, SOUND, "pager", "pagers_watch")
    return True


def _reader(proc, lines):
    for raw in proc.stdout:
        lines.append(raw.decode("utf-8", errors="replace").strip())


def scan_window(dwell_sec=30):
    global _next
    freq = FREQUENCIES_HZ[_next % len(FREQUENCIES_HZ)]
    _next += 1
    try:
        rtl = subprocess.Popen(["rtl_fm", "-f", str(freq), "-M", "fm", "-s", str(SAMPLE_RATE), "-l", "0", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        mm = subprocess.Popen(["multimon-ng", "-a", "POCSAG512", "-a", "POCSAG1200", "-a", "POCSAG2400",
                               "-a", "FLEX", "-u", "-t", "raw", "-"],
                              stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        print(f"[pagers_watch] outil manquant : {exc}")
        return False
    rtl.stdout.close()
    lines = []
    threading.Thread(target=_reader, args=(mm, lines), daemon=True).start()
    announced, count = False, 0
    try:
        deadline = time.time() + dwell_sec
        while time.time() < deadline:
            time.sleep(0.2)
            while lines:
                line = lines.pop(0)
                if not line or line.startswith(("multimon", "Enabled demodulators")):
                    continue
                count += 1
                vd.archive("pagers", {"freq": freq, "ligne": line})
                m = ALPHA.search(line)
                text = _readable(m.group(1) or m.group(2) or "") if m else ""
                if text and _memory.due(f"pager|{text[:80]}", REPEAT_SEC):
                    vd.announce(dire("pager.message_pager", text=text), SOUND, "pager", "pagers_watch")
                    announced = True
                elif not text and _other_page(line, freq):  # appel sans texte ou numerique (2026-10-03)
                    announced = True
    finally:
        for p in (rtl, mm):
            if p.poll() is None:
                p.terminate()
        for p in (rtl, mm):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        _memory.save()
    if count:
        print(f"[pagers_watch] {count} trame(s) sur {freq / 1e6:.5f} MHz")
    return announced
