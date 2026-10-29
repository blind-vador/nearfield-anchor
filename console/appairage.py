"""Commande "appairage" : relie une machine compagne (lib/compagnon.py) qui
fera les calculs lourds (images de satellites meteo...) a la place du Pi.

En meme temps, sur la machine : Nearfield Bridge, "Appairer avec un Pi".
Les deux machines echangent leurs cles et disent le meme code a 4 chiffres
(tire des deux cles). Il suffit de valider d'un seul cote : Entree ici, ou
Oui sur la machine ; l'autre suit tout seul. Echap annule.
"""
import sys
import threading
import time


import compagnon  # noqa: E402
import conseils  # noqa: E402
import keys  # noqa: E402
from phrases import dire  # noqa: E402

def _spoken_code(code):
    return " ".join(code)


def _done_text(answer):
    return dire("appairage.reussi", nom=answer.get("nom"))


def run(key_source, say, out):
    compagnon.start_pairing()
    keys.drain_wake()
    found = {}
    stop = threading.Event()

    def tell(text):
        out(f"[{text}]\n")
        say(text)

    def wait_answer():
        deadline = time.time() + compagnon.PAIRING_SEC
        while not stop.is_set() and time.time() < deadline:
            answer = found.get("answer") or compagnon.pairing_answer()
            if answer and "answer" not in found:
                found["answer"] = answer
                caps = ", ".join(answer.get("capacites") or []) or dire("appairage.aucune_capacite")
                tell(dire("appairage.reponse", nom=answer.get("nom", "inconnue"), code=_spoken_code(answer["code"]),
                          capacites=caps))
                return
            if answer and compagnon.refused(answer["id"]):
                found["fin"] = "refus"
                keys.wake()
                return
            stop.wait(1)
        if not stop.is_set():
            found["timeout"] = True
            tell(dire("appairage.delai"))

    consigne = dire("appairage.consigne")
    tell(conseils.intro("appairage", dire("appairage.titre"), consigne) + " " + dire("appairage.attente"))
    threading.Thread(target=wait_answer, daemon=True).start()
    try:
        for key in key_source:
            if key == keys.WAKE:
                if found.get("fin") == "confirme":
                    compagnon.accept(found["answer"])
                    return dire("appairage.valide_ailleurs") + " " + _done_text(found["answer"])
                if found.get("fin") == "refus":
                    return dire("appairage.refus")
                continue
            if key == "\x1b":
                return dire("appairage.annule")
            if key in ("\r", "\n"):
                answer = found.get("answer")
                if answer:
                    compagnon.accept(answer)
                    return _done_text(answer)
                text = dire("appairage.pas_reponse" if not found.get("timeout") else "appairage.personne")
            elif conseils.is_help(key):
                text = consigne
            else:
                text = dire("appairage.aide_courte")
            tell(text)
        return dire("appairage.ferme")
    finally:
        stop.set()
        compagnon.stop_pairing()
