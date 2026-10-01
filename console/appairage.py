"""Commande "appairage" : relie une machine compagne (lib/compagnon.py) qui
fera les calculs lourds (images de satellites meteo...) a la place du Pi.

Lancer en meme temps, sur la machine compagne :
  python3 ~/.nearfield-bridge/nearfield_bridge.py appairage
Les deux machines disent le meme code a 4 chiffres ; Entree l'accepte,
Echap annule.
"""
import sys
import threading
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import compagnon  # noqa: E402
import conseils  # noqa: E402

TITLE = "Appairage d'une machine compagne."
CONSIGNE = ("Lance maintenant le mode appairage du module compagnon sur l'autre machine. "
            "Quand elle répond, vérifie qu'elle dit le même code, puis Entrée pour accepter. Échap : annuler. "
            "F1 redit ces consignes.")


def _spoken_code(code):
    return " ".join(code)


def run(key_source, say, out):
    code = compagnon.start_pairing()
    found = {}
    stop = threading.Event()

    def wait_answer():
        deadline = time.time() + compagnon.PAIRING_SEC
        while not stop.is_set() and time.time() < deadline:
            answer = compagnon.pairing_answer(code)
            if answer:
                found["answer"] = answer
                caps = ", ".join(answer.get("capacites") or []) or "rien pour l'instant"
                text = (f"La machine {answer.get('nom', 'inconnue')} répond avec le code {_spoken_code(code)}. "
                        f"Elle sait faire : {caps}. Entrée pour accepter, Échap pour refuser.")
                out(f"[{text}]\n")
                say(text)
                return
            stop.wait(2)
        if not stop.is_set():
            found["timeout"] = True
            text = "Aucune machine n'a répondu en trois minutes. Échap pour fermer."
            out(f"[{text}]\n")
            say(text)

    intro = conseils.intro("appairage", TITLE, CONSIGNE)
    text = f"{intro} Code : {_spoken_code(code)}."
    out(text + "\n")
    say(text)
    threading.Thread(target=wait_answer, daemon=True).start()
    try:
        for key in key_source:
            if key == "\x1b":
                return "Appairage annulé."
            if key in ("\r", "\n"):
                answer = found.get("answer")
                if answer:
                    compagnon.accept(answer)
                    return (f"Machine {answer.get('nom')} appairée. Elle calculera pour le Pi tant que son "
                            "service tourne ; dis compagnon pour son état.")
                text = "Pas encore de réponse." if not found.get("timeout") else "Personne n'a répondu. Échap."
            elif conseils.is_help(key):
                text = f"{CONSIGNE} Code : {_spoken_code(code)}."
            else:
                text = "Entrée pour accepter quand la machine a répondu, Échap pour annuler."
            out(f"[{text}]\n")
            say(text)
        return "Appairage fermé."
    finally:
        stop.set()
        compagnon.stop_pairing()
