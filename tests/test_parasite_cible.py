"""\"parasite\" vise la prise qu on vient d entendre, et la veille DMR n ouvre
pas la FM sur du charabia de decodeur de donnees (2026-11-23)."""
import os
import sys
import tempfile
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "config", "bridge")]

import noise_signatures as ns
import prise_unique

TMP = tempfile.mkdtemp()
ns.LAST_CAPTURE_PATH = os.path.join(TMP, "derniere.json")
ns.LIVE_CAPTURE_PATH = os.path.join(TMP, "en_cours.json")
ns.PARASITE_PENDING_PATH = os.path.join(TMP, "parasite.json")
ns.background_similarity = lambda *a: None
ns._remember_capture = lambda *a, **k: None
import frequences_actives
frequences_actives.depuis_capture = lambda *a, **k: None
import mesures_scanner
mesures_scanner.noter = lambda *a, **k: None

# Une prise vraiment entendue devient la derniere reception.
ns.record_capture("dmr", 467_237_500, "nfm", "veille DMR", "dmr", None, None)
assert ns.last_capture()["label"] == "veille DMR"

# Un decodage differe (juges_watch) ou un repechage ne la remplace pas.
ns.record_capture("juges", 463_224_800, "-fs", "vieille prise", "pro", None, None, derniere=False)
assert ns.last_capture()["label"] == "veille DMR", "une prise rejugee apres coup n est pas la derniere entendue"

# Un "parasite" demande pendant l ecoute n est pas consomme par une prise differee.
ns.capture_started("dmr", 467_237_500, "dmr", "veille DMR", "dmr")
ns.request_parasite_live(ns.live_capture())
ns.record_capture("juges", 467_237_500, "-fs", "vieille prise", "dmr", None, None, derniere=False)
assert ns._parasite_en_attente(), "la demande attend toujours l ecoute en cours"

# Ecoute jetee sans enregistrement : la demande ne vise pas une autre prise plus tard.
ns.capture_ended()
assert ns._parasite_en_attente() is None
assert ns.live_capture() is None

# Arbitre : un juge retenu ici fait attendre la FM tant qu il n a pas parle.
a = prise_unique.Arbitre()
assert not a.pref_attendu()
a.pref, a.pref_jusqua = "-fs", time.time() + 8
assert a.pref_attendu()
a.prendre("-fs", "DMR")
assert not a.pref_attendu(), "deja parle"
b = prise_unique.Arbitre()
b.pref, b.pref_jusqua = "-fs", time.time() + 8
b.ceder("-fs")
assert not b.pref_attendu(), "cede par juge suivant"

print("OK")
