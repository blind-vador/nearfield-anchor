"""Dongle radio fige (2026-11-14) : reconnu par le systeme, mais il ne rend plus
aucun echantillon.

Constat du 14/11 : de 5 h 19 a 15 h 43, toute la veille radio etait sourde sans le
dire. rtl_433 s'arretait au bout de 4 s ("Async read stalled, exiting!"), rtl_sdr
et rtl_fm ne lisaient plus rien, les balayages rtl_power ne rendaient rien, et
seule l'alarme "antenne muette" le laissait deviner, chaque matin, en accusant
l'antenne. Un debranchement physique n'avait tenu qu'une heure. Une
reinitialisation USB (usbreset) l'a rendu aussitot. usb_watch ne le voyait pas :
il ne repare que les ports ou l'appareil n'est jamais reconnu.

Ici : les lecteurs du dongle signalent ce qu'ils voient.
  signaler(source, detail, sur=True)   preuve nette (message de rtl_433) :
                                       reinitialisation tout de suite ;
  signaler(source, detail, sur=False)  indice (passe rtl_power vide) : il en faut
                                       SOUPCONS_MIN en SOUPCONS_FENETRE_SEC.
Au plus une reinitialisation par REINIT_ECART_SEC. Toujours appele par le
proprietaire du dongle (radio_watch et ses fenetres) entre deux lectures, quand
plus rien ne le tient ouvert. Etat dans ETAT : veille_systeme l'annonce, et passe
a l'alarme si les reinitialisations se repetent (le dongle ou son alimentation
lache).
"""
import json
import os
import subprocess
import time

import chemins

ETAT = chemins.base("dongle_fige.json")
HELPER = ["sudo", "-n", "/usr/local/sbin/usb-repare", "reinit"]
RTL_IDS = {("0bda", "2838"), ("0bda", "2832")}
SYS = "/sys/bus/usb/devices"
REINIT_ECART_SEC = 300
SOUPCONS_MIN = 2
SOUPCONS_FENETRE_SEC = 600
APRES_REINIT_SEC = 3
HISTORIQUE_MAX = 20


def _lire():
    try:
        with open(ETAT, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ecrire(etat):
    tmp = f"{ETAT}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(etat, f)
        os.replace(tmp, ETAT)
    except OSError:
        pass


def trouver(sys_dir=None):
    """Nom sysfs du dongle radio (ex. "1-1.1.1"), ou None."""
    sys_dir = sys_dir or SYS
    try:
        noms = os.listdir(sys_dir)
    except OSError:
        return None
    for nom in sorted(noms):
        if ":" in nom:
            continue
        try:
            with open(os.path.join(sys_dir, nom, "idVendor")) as f:
                vid = f.read().strip()
            with open(os.path.join(sys_dir, nom, "idProduct")) as f:
                pid = f.read().strip()
        except OSError:
            continue
        if (vid, pid) in RTL_IDS:
            return nom
    return None


def _contexte(dev):
    """Ce qui peut expliquer un gel (2026-11-14 : cause encore inconnue -- clef
    reconnue, plus aucun echantillon, aucune erreur du noyau) : chaleur du Pi,
    bridage et sous-tension depuis le demarrage, ce qui tournait sur la clef,
    depuis quand elle marchait. Garde avec chaque reinitialisation."""
    ctx = {}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            ctx["temp_c"] = round(int(f.read()) / 1000, 1)
    except (OSError, ValueError):
        pass
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5)
        ctx["bride"] = r.stdout.strip().split("=")[-1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        r = subprocess.run(["pgrep", "-a", "-f", "rtl_|rtl_airband|dump1090|welle-cli|dec406|acarsdec|dumpvdl2"],
                           capture_output=True, text=True, timeout=5)
        ctx["outils"] = [l.split(None, 1)[-1][:80] for l in r.stdout.splitlines()][:5]
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        with open(os.path.join(SYS, dev, "devnum")) as f:
            ctx["devnum"] = int(f.read())
    except (OSError, ValueError):
        pass
    return ctx


def signaler(source, detail="", sur=True):
    """Un lecteur du dongle n'a rien recu. Rend True si on vient de le reinitialiser."""
    now = time.time()
    etat = _lire()
    if not sur:
        soupcons = [t for t in etat.get("soupcons", []) if now - t < SOUPCONS_FENETRE_SEC] + [now]
        etat["soupcons"] = soupcons
        if len(soupcons) < SOUPCONS_MIN:
            _ecrire(etat)
            return False
    if now - etat.get("derniere", 0) < REINIT_ECART_SEC:
        _ecrire(etat)
        return False
    dev = trouver()
    if not dev:
        print(f"[dongle_fige] {source} : dongle introuvable, rien a reinitialiser", flush=True)
        return False
    ctx = _contexte(dev)
    try:
        r = subprocess.run(HELPER + [dev], capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0
        message = (r.stdout + r.stderr).strip()[-200:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, message = False, repr(exc)
    print(f"[dongle_fige] {source} : {detail[:160]} -> reinitialisation USB de {dev} : "
          f"{'faite' if ok else 'echec'} ({message}) ; contexte {ctx}", flush=True)
    precedente = etat.get("derniere")
    etat.update(derniere=now, soupcons=[])
    etat["reinits"] = (etat.get("reinits") or [])[-(HISTORIQUE_MAX - 1):] + [
        {"t": now, "source": source, "detail": detail[:200], "ok": ok, "contexte": ctx,
         "depuis_reinit_s": round(now - precedente) if precedente else None}]
    _ecrire(etat)
    if ok:
        time.sleep(APRES_REINIT_SEC)   # le temps que le noyau le remette en service
    return ok


def remis(source):
    """Le dongle rend des echantillons : les soupcons s'effacent."""
    etat = _lire()
    if etat.get("soupcons"):
        etat["soupcons"] = []
        _ecrire(etat)


def reinits_depuis(t):
    return [r for r in (_lire().get("reinits") or []) if r.get("t", 0) > t]
