"""Commande "appairage" : relie une machine compagne (lib/compagnon.py) qui
fera les calculs lourds (images de satellites meteo...) a la place du Pi.

En meme temps, sur la machine : Nearfield Bridge, "Appairer avec un Pi".
Les deux machines echangent leurs cles et disent le meme code a 4 chiffres
(tire des deux cles) ; Entree l'accepte ici, Echap annule. La machine
demande aussi de confirmer le code de son cote.
"""
import sys
import threading
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import compagnon  # noqa: E402
import conseils  # noqa: E402

TITLE = "Appairage d'une machine compagne."
CONSIGNE = ("Sur l'autre machine, ouvre Nearfield Bridge et choisis appairer avec un Pi. "
            "Quand elle répond, vérifie qu'elle dit le même code, puis Entrée pour accepter. Échap : annuler. "
            "F1 redit ces consignes.")


def _spoken_code(code):
    return " ".join(code)


def run(key_source, say, out):
    compagnon.start_pairing()
    found = {}
    stop = threading.Event()

    def tell(text):
        out(f"[{text}]\n")
        say(text)

    def wait_answer():
        deadline = time.time() + compagnon.PAIRING_SEC
        while not stop.is_set() and time.time() < deadline:
            answer = compagnon.pairing_answer()
            if answer:
                found["answer"] = answer
                caps = ", ".join(answer.get("capacites") or []) or "rien pour l'instant"
                tell(f"La machine {answer.get('nom', 'inconnue')} répond. Code : {_spoken_code(answer['code'])}. "
                     f"Elle sait faire : {caps}. Si elle dit le même code, Entrée pour accepter ; sinon Échap.")
                return
            stop.wait(2)
        if not stop.is_set():
            found["timeout"] = True
            tell("Aucune machine n'a répondu en trois minutes. Échap pour fermer.")

    tell(conseils.intro("appairage", TITLE, CONSIGNE) + " J'attends la machine.")
    threading.Thread(target=wait_answer, daemon=True).start()
    try:
        for key in key_source:
            if key == "\x1b":
                return "Appairage annulé."
            if key in ("\r", "\n"):
                answer = found.get("answer")
                if answer and compagnon.refused(answer["id"]):
                    return "La machine a refusé : les codes ne correspondaient pas. Rien n'est enregistré."
                if answer:
                    compagnon.accept(answer)
                    return (f"Machine {answer.get('nom')} appairée : nos clés sont échangées. Elle se connectera "
                            "au Pi et calculera pour lui ; dis compagnon pour son état.")
                text = "Pas encore de réponse." if not found.get("timeout") else "Personne n'a répondu. Échap."
            elif conseils.is_help(key):
                text = CONSIGNE
            else:
                text = "Entrée pour accepter quand la machine a répondu, Échap pour annuler."
            tell(text)
        return "Appairage fermé."
    finally:
        stop.set()
        compagnon.stop_pairing()
