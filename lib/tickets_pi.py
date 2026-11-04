"""Cote Pi des tickets partages (bridge/tickets_partages.py), commun a
juges_watch (sons bruts, renvois) et radio_watch (BLU du banc)."""
import os
import shutil
import time

import compagnon
import pont_protocole as proto
import tickets_partages as tp

SIGNAL = "/tmp/tickets_signal"     # lu par compagnon_serveur, qui previent les machines par la liaison
RACINE = compagnon.BRIDGE_DIR
RACINE_DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _chemin(*noms):
    for nom in noms:
        c = shutil.which(nom) or (os.path.join(os.path.expanduser("~/autopilot/bin"), nom))
        if os.access(c, os.X_OK):
            return c
    return None


def outils():
    return {"dsd": _chemin("dsd-fme"), "multimon": _chemin("multimon-ng"), "direwolf": _chemin("direwolf"),
            "direwolf_conf": os.path.join(RACINE_DEPOT, "config", "direwolf_donnees.conf"),
            "python_sstv": "/usr/bin/python3", "sstv_script": os.path.join(RACINE_DEPOT, "lib", "sstv_veille.py"),
            "freedv": _chemin("freedv_rx"), "jt9": _chemin("jt9")}


def moi():
    return compagnon.pi_id()


def cle():
    return compagnon.key()


def cles():
    found = {cid: c["cle"] for cid, c in compagnon.trusted().items() if c.get("cle")}
    found[moi()] = proto.public_b64(cle())
    return found


def machines():
    """Machines reliees en direct qui travaillent sur les tickets."""
    try:
        return [c for c in compagnon.rank("tickets") if c.get("directe")]
    except Exception as exc:
        print(f"[tickets] machines compagnes illisibles : {exc!r}")
        return []


def sait(c, epreuve):
    return "ticket_sans_" + epreuve not in (c.get("capacites") or [])


def signaler(tid):
    """Reveille les machines : du neuf sur ce ticket. On leur donne son nom : le
    client SMB du Mac garde la liste du dossier en cache une trentaine de secondes,
    un ticket neuf n'y apparait pas tout de suite."""
    try:
        with open(SIGNAL, "a") as f:
            f.write(tid + "\n")
    except OSError:
        pass


def ouvrir(sons, infos, epreuves):
    tid = tp.ouvrir(RACINE, cle(), moi(), sons, infos, epreuves)
    signaler(tid)
    return tid


def contre_possible(mach):
    """contre_possible(epreuve, auteur) de tp.conclure : une AUTRE machine reliee
    (ou le Pi s'il sait la faire, fut-ce apres une pause de chaleur) peut-elle
    encore la repasser ?"""
    ici = outils()

    def possible(ep, auteur):
        if auteur != moi() and tp.savoir(ep, ici):
            return True
        return any(c["id"] != auteur and sait(c, ep) for c in mach)
    return possible


def attendre(tid, secondes, utiles=None, pas=1.0):
    """Attend la conclusion d'un ticket : (ticket, avis, conclusion). utiles(epreuve) :
    seules celles-la comptent. La conclusion dit "attendu" : epreuves utiles encore
    en attente quand le temps a manque."""
    fin = time.time() + secondes
    while True:
        ticket, avis = tp.lire(RACINE, tid, cles())
        c = tp.conclure(ticket, avis, contre_possible(machines())) if ticket else None
        if c:
            c["attendu"] = [e for e in c["manque"] if utiles is None or utiles(e)]
        if (c and not c["attendu"]) or time.time() >= fin or not ticket:
            return ticket, avis, c
        time.sleep(pas)
