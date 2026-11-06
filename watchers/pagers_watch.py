"""Veille des messageries de pagers en 466 MHz (reseau e*Message :
alertes d'equipes, dont des pompiers volontaires). Messages non chiffres,
POCSAG ou FLEX, decodes par multimon-ng.

Une frequence du reseau par passage, en tourniquet (rtl_fm ne tient qu'une
frequence a la fois). Messages alphanumeriques lisibles annonces (anti-
repetition), tout est archive.
"""
import os
import re
import flux_tickets
import messages_types
import subprocess
import threading
import time

import veille_donnees as vd
from sound_paths import RADIO_SCANER_DIR
from phrases import dire

FREQUENCIES_HZ = [466_025_000, 466_050_000, 466_075_000, 466_175_000, 466_206_250, 466_231_250]
SAMPLE_RATE = 22050
import sons
SOUND = sons.chemin("bipeur", os.path.join(RADIO_SCANER_DIR, "decoding_morse.mp3"))  # son propre (2026-10-23, sons.py)
STATE_PATH = "/tmp/pagers_watch_state.json"
REPEAT_SEC = 300          # retransmission d'un meme appel (reseau qui repete), pas un nouvel appel
MIN_ALNUM = 2             # en dessous : trame de service ou bruit, pas un message
# Caracteres attendus dans un message en clair (lettres/chiffres accentues
# compris via isalnum, espace, ponctuation courante). Le reste = bruit de
# decodage (octets parasites d'une trame mal recue).
_TEXTE_PONCT = " .,:;!?/\\-'\"()[]°+%&@*#=<>"
# POCSAG : "POCSAG1200: Address: 123456  Function: 3  Alpha:   texte"
# FLEX   : "FLEX|date|1600/2/K/A|12.345|000123456|ALN|texte"
ALPHA = re.compile(r"Alpha:\s*(.*)$|\|ALN\|(.*)$")
NUMERIC = re.compile(r"Numeric:\s*(.*)$|\|NUM\|(.*)$")
ADDRESS = re.compile(r"Address:\s*(\d+)|\|0*(\d+)\|(?:ALN|NUM|TON)")

_memory = vd.Memory(STATE_PATH)
_next = 0


def _readable(text):
    """Un message en clair est LU tel quel (comme une voix sur un talkie) --
    on ne rejette QUE le bruit de decodage. 2026-10-09 : avant, un seuil de
    6 lettres ET >=50 % de lettres ecartait a tort les messages riches en
    chiffres (adresses, heures, codes), pourtant parfaitement en clair, qui
    etaient alors retrogrades en "appel sans texte". Desormais on compte
    l'alphanumerique (chiffres inclus) et on ne jette que ce qui est
    majoritairement du symbole/octet parasite."""
    text = re.sub(r"<[A-Z]{2,4}>", " ", text)  # <NUL>, <EOT>... de multimon-ng
    text = "".join(c if c.isprintable() else " " for c in text)  # octets parasites
    text = " ".join(text.split())
    if not text:
        return ""
    alnum = sum(c.isalnum() for c in text)
    texte = sum(1 for c in text if c.isalnum() or c in _TEXTE_PONCT)
    # assez de contenu alphanumerique, et pas majoritairement du symbole
    if alnum >= MIN_ALNUM and texte >= len(text) * 0.6:
        return text
    return ""


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
    if not digits:
        # Bip sans texte (2026-10-23, demande utilisateur) : ni annonce ni prise a
        # part ; il est archive (pagers_AAAA-MM-JJ.jsonl) et compte dans le resume
        # horaire du journal "donnees" (lib/trafic_heure.py).
        return False
    if not _memory.due(f"pager_adresse|{address}|{digits}", REPEAT_SEC):
        return False
    if digits:
        text = dire("pager.numerique", chiffres=digits, adresse=address, frequence=mhz)
    else:
        text = dire("pager.appel_sans_texte", adresse=address, frequence=mhz)
    vd.announce(text, SOUND, "message_pager", "pagers_watch",
                fiche=messages_types.fiche("POCSAG", "POCSAG, appel sans texte" if not digits else "POCSAG, message numérique",
                                           freq, decoupe=True, prive=True, source=f"adresse {address}"))
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
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        print(f"[pagers_watch] outil manquant : {exc}")
        return False
    copie = flux_tickets.Copie(rtl.stdout, mm.stdin, SAMPLE_RATE)
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
                if text:  # un pager peut donner une frequence, un relais
                    try:
                        import relais
                        relais.depuis_texte(text, None, "pager")
                    except Exception as exc:
                        print(f"[pagers_watch] texte non lu pour les relais : {exc!r}")
                if text and _memory.due(f"pager|{text[:80]}", REPEAT_SEC):
                    vd.announce(dire("pager.message_pager", text=text), SOUND, "message_pager", "pagers_watch",
                                fiche=dict(messages_types.fiche("POCSAG ou FLEX", "POCSAG ou FLEX, message texte", freq,
                                                                decoupe=True, prive=True), texte_original=text))
                    announced = True
                elif not text and _other_page(line, freq):  # appel sans texte ou numerique (2026-10-03)
                    announced = True
    finally:
        # Arret propre (2026-10-07) : decodeur d'abord, puis rtl_fm en vidant
        # le tuyau (voir aprs_watch) -- evite la reinitialisation de la cle.
        from lecture_flux import arreter
        if mm.poll() is None:
            mm.terminate()
        try:
            mm.wait(timeout=3)
        except subprocess.TimeoutExpired:
            mm.kill()
        arreter(rtl, "rtl_fm (pagers)")
        copie.deposer(freq, "pagers", "pagers")
        _memory.save()
    if count:
        print(f"[pagers_watch] {count} trame(s) sur {freq / 1e6:.5f} MHz")
    return announced
