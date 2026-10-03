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
