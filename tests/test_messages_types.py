"""Types de messages (explication unique, rejet nominatif), integrite, survie de la vitesse."""
import json
import os
import sys
import tempfile
import types

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("lib", "config", "watchers", "bridge"):
    sys.path.insert(0, os.path.join(RACINE, d))

import decodeurs
import messages_types as mt
import noise_signatures as ns
import reglages
import tempo

tmp = tempfile.mkdtemp()
mt.PATH = os.path.join(tmp, "types.json")
mt.ECRITURE = mt.PATH + ".v"
ns._signatures_path = lambda: os.path.join(tmp, "sig.json")
demandes = []
reponse = {"v": "Un message de test."}


def ask(prompt, **_):
    demandes.append(prompt)
    return (reponse["v"], None) if reponse["v"] else (None, "pas de reseau")


sys.modules["gemini"] = types.SimpleNamespace(ask=ask)
sys.modules["avions_info"] = types.SimpleNamespace(_langue=lambda: "français")

assert not decodeurs.meaningful("FMS: ed9d90f7b9f9 (9=Rotkreuz f=Thuringen Ort 0x9b=155 FZG 7f09) CRC INCORRECT")
assert decodeurs.meaningful("FMS: ed9d90f7b9f9 (9=Rotkreuz f=Thuringen Ort 0x9b=155 FZG 7f09) CRC CORRECT")
assert not decodeurs.meaningful("CLIPFSK: UNKNOWN Message type (0x3e) len=6 abcdef")
assert decodeurs.meaningful("POCSAG1200: Address: 123  Function: 0  Alpha: bonjour")

assert mt.cle_de("FMS: d8d557") == "FMS"
assert mt.cle_de("FLEETSYNC, flotte 12, poste 3 vers 45, position GPS", True) == "FLEETSYNC, flotte, poste vers, position GPS"
assert mt.cle_de("MPT1327: libération du canal 503", True) == "MPT1327: libération du canal"

f = mt.fiche("multimon", "FMS: 123456 test", 467_400_000)
reponse["v"] = None
assert mt.premiere_fois(f) is None and len(demandes) == 1, "sans IA : rien de retenu"
reponse["v"] = "Un message de test."
assert mt.premiere_fois(f) == "Un message de test.", "redemande apres un echec"
assert mt.premiere_fois(f) is None and len(demandes) == 2, "une seule explication par type"
assert mt.explication_connue("FMS") == "Un message de test."
assert "123456" not in demandes[1] and "######" in demandes[1], "chiffres masques"
mt.premiere_fois(mt.fiche("POCSAG", "POCSAG, message", 1, decoupe=True, prive=True, texte_original="Mme Durand, chambre 12"))
assert "Durand" not in demandes[-1] and "Exemple" not in demandes[-1], "le contenu prive ne part pas"

assert not mt.ecarte(f)
assert mt.rejeter(f, "uhf") == "FMS"
assert mt.ecarte(f)
assert mt.ecarte(dict(f, frequence_hz=467_402_000)), "meme frequence a 2 kHz pres"
assert not mt.ecarte(dict(f, frequence_hz=467_412_500)), "le canal d'a cote reste lu"
assert not mt.ecarte(dict(f, type_message="POCSAG1200")), "un autre type reste lu"
assert not mt.ecarte(dict(f, urgence=True)), "une detresse est toujours lue"
n = len(ns.load_signatures())
mt.rejeter(f)
assert len(ns.load_signatures()) == n, "pas de doublon"
ok, _ = ns.matches_known_noise(467_400_000, b"\x10\x20" * 24000, 48000, "s16le", "nfm")
assert not ok, "l'entree de type ne touche pas aux empreintes sonores"

import propositions
prise = os.path.join(tmp, "message.mp3")
open(prise, "w").close()
json.dump(f, open(prise[:-4] + ".json", "w"))
ns.capture_info = lambda _: None
assert propositions._juge_courant(prise)[:2] == ("FMS", 467_400_000), "V et fleche notent le type et la frequence"

# Vitesse : relecture apres redemarrage, NAS present puis absent ; ecriture refusee disque demonte.
reglages.SETTINGS_DIR = os.path.join(tmp, "nas", "server", "reglages")
reglages.CACHE_DIR = os.path.join(tmp, "cache")
os.makedirs(os.path.dirname(reglages.SETTINGS_DIR))
assert tempo.regler("pics") and tempo.courant(fresh=True) == "pics"
os.rename(os.path.join(tmp, "nas"), os.path.join(tmp, "nas_demonte"))
assert tempo.courant(fresh=True) == "pics", "disque absent au redemarrage : la copie locale"
assert not tempo.regler("lent"), "disque absent : refus annonce, pas d'ecriture sous le point de montage"
assert not os.path.exists(os.path.join(tmp, "nas"))
assert tempo.courant(fresh=True) == "pics"
os.rename(os.path.join(tmp, "nas_demonte"), os.path.join(tmp, "nas"))
assert tempo.courant(fresh=True) == "pics"
assert tempo.sonde(14) == max(2.0, 14 * 0.34), "pics raccourcit la sonde du banc"

import juges_radio
def compte(*lignes):
    st = {"clean": 0, "total": 0, "protocol": None}
    for l in lignes:
        juges_radio.compter_synchro(st, l)
    return st["total"], st["clean"]
assert compte("08:38 Sync: NXDN48  RTCH Data PF X/4", "08:38 Sync: NXDN96  RTCH Data RAN 07 PF X/4",
              "08:38 Sync: NXDN48  RDCH Data FACCH2 (CRC ERR)", "08:38 Sync: JPN DCR Voice SACCH (CRC ERR)",
              "08:38 Sync: IDAS D  RTCH2 Data SCCH FACCH3") == (5, 0), "le bruit reel ne prouve rien"
assert compte("08:38 Sync: NXDN48  RTCH Voice SACCH", "08:38 Sync: NXDN96  RTCH V1+F2 PF 2/4") == (2, 2)
assert compte("08:38 Sync: -EDACS  BCH FAIL ") == (1, 0), "EDACS : BCH faux"
assert compte("08:38 Sync: no sync") == (0, 0)
assert compte("08:38 Sync: +DMR  slot1 | Color Code=01 | IDLE") == (1, 1)
print("test_messages_types : tout est bon")
