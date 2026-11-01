"""Client de la prise NUMERIQUE_SOCKET (tenue par compagnon_serveur) : demande
a la machine compagne un decodage numerique en direct (DMR, P25... -- tout sauf
TETRA) du flux rtl_tcp que le Pi sert deja. Pendant du TETRA lib/tetra.decoder,
pour le decodage que le Pi SAIT faire lui-meme mais DELEGUE quand il chauffe
(lib/offload.py). Ne leve pas."""
import json
import socket

NUMERIQUE_SOCKET = "/tmp/numerique_pont.sock"   # prise tenue par compagnon_serveur (compagnon_watch)


def decoder(freq_hz, gain, secondes, host=None, port=1234, ppm=0, delai=None, max_secondes=None, mode="-fa",
            banc=False):
    """Demande a compagnon_serveur un decodage numerique en direct de freq_hz.
    Le Pi doit DEJA servir rtl_tcp sur host:port, et avoir tune le CENTRE
    RECENTRE (sinon FEC ERR). `mode` = flag dsd-neo impose (-fs pour un relais
    DMR connu, -fa auto sinon). Rend le dict resultat (voir bridge/numerique_neo),
    ou {"ok": False, "erreur": ...} si pas de compagne / pas de reponse."""
    max_secondes = max(int(secondes), int(max_secondes or secondes))
    req = {"freq_hz": int(freq_hz), "gain": gain, "secondes": int(secondes), "max_secondes": max_secondes,
           "port": int(port), "ppm": ppm, "mode": mode, "banc": bool(banc)}
    if host:
        req["host"] = host
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(delai or (max_secondes + 60))
    try:
        s.connect(NUMERIQUE_SOCKET)
    except OSError as exc:
        return {"ok": False, "erreur": "prise numerique injoignable (%r)" % exc}
    try:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            bloc = s.recv(65536)
            if not bloc:
                break
            buf += bloc
        if not buf.strip():
            return {"ok": False, "erreur": "pas de reponse"}
        return json.loads(buf.split(b"\n", 1)[0])
    except (OSError, ValueError) as exc:
        return {"ok": False, "erreur": repr(exc)[:200]}
    finally:
        s.close()
