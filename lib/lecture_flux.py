"""Lecture bornee du flux d'un programme radio (rtl_sdr, rtl_fm...).

Une cle RTL-SDR peut cesser d'envoyer des echantillons sans que le
programme ne s'arrete : il attend la cle, et un read() ordinaire attend le
programme pour toujours (2026-10-03 : scanner bloque 8 h 30 sur la fenetre
LoRa 869,525 MHz, plus aucune ecoute, capteurs compris). lire() rend ce qui
est arrive, ou b"" si plus rien n'arrive pendant `silence_s` secondes.
"""
import os
import select
import time


def lire(stream, n, silence_s=5.0):
    """Jusqu'a n octets ; moins (ou b"") si le flux se tait silence_s secondes
    ou se ferme. Ne bloque jamais plus longtemps que silence_s d'affilee."""
    fd = stream.fileno()
    chunks, got = [], 0
    last = time.time()
    while got < n:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if not ready:
            if time.time() - last >= silence_s:
                break
            continue
        data = os.read(fd, n - got)
        if not data:  # programme termine
            break
        chunks.append(data)
        got += len(data)
        last = time.time()
    return b"".join(chunks)


def arreter(proc, nom="programme radio", delai_s=5.0):
    """Arrete proprement un programme radio qui ecrit dans un tuyau (2026-10-07).
    Sans lecteur, le tuyau se remplit en quelques millisecondes : le programme
    reste bloque en ecriture, ne traite plus la demande d'arret, et il fallait
    le tuer en plein transfert USB -- le noyau reinitialisait alors la cle a
    l'ouverture suivante (toutes les ~27 min, au rythme des fenetres LoRa).
    On vide donc le tuyau pendant qu'il s'arrete ; tue seulement en dernier
    recours, et c'est dit. Rend True si l'arret a ete propre."""
    import subprocess
    if proc.poll() is None:
        proc.terminate()
    end = time.time() + delai_s
    try:  # stdout deja ferme cote parent (chaines rtl_fm | decodeur ou il est confie au decodeur)
        fd = proc.stdout.fileno() if proc.stdout and not proc.stdout.closed else None
    except (ValueError, OSError):
        fd = None
    while proc.poll() is None and time.time() < end and fd is not None:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                if not os.read(fd, 1 << 16):
                    break
            except BlockingIOError:
                continue
            except OSError:
                break
    try:
        proc.wait(timeout=max(0.1, end - time.time()))
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        print(f"[lecture_flux] {nom} ne s'arretait pas : tue (la cle sera reinitialisee)")
        return False
