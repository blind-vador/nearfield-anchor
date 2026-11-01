"""Veille des donnees marines et fluviales (2026-11-01) : AIS des navires et
bateaux (161,975 / 162,025 MHz) et appels selectifs numeriques ASN du canal 70
(156,525 MHz : detresse, securite, appels individuels).

Ce sont des salves tres courtes (26 ms pour une trame AIS, moins d'une seconde
pour un appel ASN) : le balayage (rtl_power integre sur 1 s) ne les voit
presque jamais. Cette fenetre ecoute donc chaque canal a tour de role :
  - AIS : rtl_fm 48 kHz -> Dire Wolf (-B AIS, paquet Debian "direwolf") ;
  - ASN : rtl_fm 48 kHz -> lib/asn_dsc.py.
Tout est archive ; un navire est annonce a sa premiere apparition (puis au
plus toutes les NAVIRE_REDIT_SEC), un appel ASN a chaque fois, une detresse
avec le son d'alarme. Utile en mer comme sur les voies navigables : l'AIS
fluvial (Rhone, Saone, Rhin...) emet sur les memes canaux."""
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import veille_donnees as vd
from phrases import dire
from sound_paths import RADIO_SCANER_DIR
import sons

AIS_HZ = (161_975_000, 162_025_000)
ASN_HZ = 156_525_000
TAUX = 48000
NAVIRE_REDIT_SEC = 2 * 3600
LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
DIREWOLF_CONF = os.path.join(os.path.dirname(LIB), "config", "direwolf_donnees.conf")
SOUND = sons.chemin("marine", os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3"))
ALARME_SOUND = sons.chemin("marine_detresse", os.path.join(RADIO_SCANER_DIR, "scaner_error.mp3"))
_memory = vd.Memory("/tmp/marine_watch_state.json")
_AIS_LIGNE = re.compile(r"^AIS (\d+): (\d{9})")
_tour = [0]


def _ecouter(freq_hz, secondes, decodeur):
    """rtl_fm sur freq_hz pendant secondes, son passe a decodeur (liste de
    commande) ; rend les lignes que le decodeur a ecrites."""
    fm = subprocess.Popen(["rtl_fm", "-f", str(freq_hz), "-M", "fm", "-s", str(TAUX), "-l", "0", "-E", "dc"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    dec = subprocess.Popen(decodeur, stdin=fm.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    fm.stdout.close()
    lignes = []

    def lire():
        for brut in dec.stdout:
            ligne = brut.decode(errors="replace").strip()
            if ligne:
                lignes.append(ligne)
    fil = threading.Thread(target=lire, daemon=True)
    fil.start()
    fin = time.time() + secondes
    while time.time() < fin and fm.poll() is None:
        time.sleep(0.2)
    fm.terminate()
    try:
        fm.wait(timeout=3)
    except subprocess.TimeoutExpired:
        fm.kill()
    try:
        dec.wait(timeout=5)       # fin du flux : le decodeur finit sa derniere trame
    except subprocess.TimeoutExpired:
        dec.kill()
    fil.join(timeout=2)
    return lignes


def _ais(freq_hz, secondes):
    """Rend True si un navire a ete annonce."""
    if not shutil.which("direwolf"):
        print("[marine_watch] direwolf introuvable (paquet direwolf)")
        return False
    lignes = _ecouter(freq_hz, secondes, ["direwolf", "-c", DIREWOLF_CONF, "-B", "AIS", "-r", str(TAUX), "-n", "1",
                                           "-b", "16", "-t", "0", "-a", "0", "-q", "hd", "-"])
    annonce = False
    for ligne in lignes:
        m = _AIS_LIGNE.match(ligne)
        if not m:
            continue
        mmsi = m.group(2)
        vd.archive("ais", {"freq_hz": freq_hz, "mmsi": mmsi, "texte": ligne})
        if _memory.due(f"navire|{mmsi}", NAVIRE_REDIT_SEC):
            vd.announce(dire("marine.navire_ais", mmsi=" ".join(mmsi), details=ligne.split(":", 1)[1].strip()),
                        SOUND, f"message_ais_{mmsi}", "marine_watch",
                        fiche={"protocole": "AIS", "frequence_hz": freq_hz, "source": mmsi})
            annonce = True
    if lignes:
        print(f"[marine_watch] AIS {freq_hz / 1e6:.3f} MHz : {sum(bool(_AIS_LIGNE.match(l)) for l in lignes)} trame(s)")
    return annonce


def _asn(secondes):
    lignes = _ecouter(ASN_HZ, secondes, [sys.executable, "-u", os.path.join(LIB, "asn_dsc.py"), str(TAUX)])
    annonce = False
    for ligne in lignes:
        if not ligne.startswith("ASN: "):
            continue
        texte = ligne[5:]
        vd.archive("asn", {"freq_hz": ASN_HZ, "texte": texte})
        detresse = "DÉTRESSE" in texte or "détresse" in texte
        vd.announce(dire("marine.appel_asn", texte=texte), ALARME_SOUND if detresse else SOUND,
                    "message_asn", "marine_watch", fiche={"protocole": "ASN", "frequence_hz": ASN_HZ})
        annonce = True
    return annonce


def scan_window(dwell_sec=30):
    """Une passe : les deux canaux AIS puis le canal 70 (ordre tournant, pour
    que chacun ait sa part meme si la fenetre est raccourcie)."""
    etapes = [lambda s: _ais(AIS_HZ[0], s), lambda s: _ais(AIS_HZ[1], s), _asn]
    debut = _tour[0] % len(etapes)
    _tour[0] += 1
    part = max(5.0, dwell_sec / len(etapes))
    annonce = False
    for i in range(len(etapes)):
        try:
            annonce = etapes[(debut + i) % len(etapes)](part) or annonce
        except Exception as exc:   # une etape ratee n'arrete pas la veille
            print(f"[marine_watch] {exc!r}")
    _memory.save()
    return annonce
