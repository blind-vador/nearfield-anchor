"""Le meme message de donnees n'est dit qu'une fois (2026-11-05) : le direct et le
passage differe par les tickets peuvent decoder le meme son."""
import fcntl
import json
import time

PATH = "/tmp/messages_dits.json"
LOCK = "/tmp/messages_dits.lock"
FENETRE_SEC = 3600


def deja_dit(texte):
    """Vrai si ce texte a deja ete dit dans l'heure ; sinon le note et rend Faux."""
    cle = " ".join((texte or "").lower().split())[:200]
    with open(LOCK, "w") as verrou:
        fcntl.flock(verrou, fcntl.LOCK_EX)
        try:
            with open(PATH, encoding="utf-8") as f:
                vus = json.load(f)
        except (OSError, ValueError):
            vus = {}
        maintenant = time.time()
        vus = {k: t for k, t in vus.items() if maintenant - t < FENETRE_SEC}
        deja = cle in vus
        vus[cle] = maintenant
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(vus, f, ensure_ascii=False)
    return deja
