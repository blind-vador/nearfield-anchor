"""Console vocale du Pi : tourne sur tty1 au demarrage (pi-console.service),
pilotee au clavier USB branche sur le Pi, sans ecran.

Le tty est en mode brut : chaque touche est lue des qu'elle est pressee,
ce qui permet l'echo vocal de la frappe (l'ancienne version lisait avec
input(), muette pendant la saisie -- voir ROADMAP). Toute touche coupe la
parole en cours.

Touches de la ligne de commande :
  Entree            valide la ligne
  Retour arriere    efface le dernier caractere (et le dit)
  Echap             efface toute la ligne
  Tab / fleche haut relit la ligne en cours
"""
import codecs
import os
import select
import signal
import sys
import termios
import tty

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import commands  # noqa: E402
import keys  # noqa: E402
import matcher  # noqa: E402
import archives  # noqa: E402
import bilan  # noqa: E402
import journal  # noqa: E402
import manuel  # noqa: E402
import reglages_cmd  # noqa: E402
import tableaux  # noqa: E402
import simulation  # noqa: E402
import veilles  # noqa: E402
import vision  # noqa: E402
import voix  # noqa: E402

WELCOME = "Console vocale du Pi prête. Tape aide pour la liste des commandes."
ESCAPE_TIMEOUT_SEC = 0.05
KEYTEST_EXIT_ESCAPES = 3


def out(text):
    sys.stdout.write(text.replace("\n", "\r\n"))
    sys.stdout.flush()


def read_keys(fd=0):
    """Genere une saisie par touche : caractere, controle ou sequence
    d'echappement complete (fleches, F1-F12, Alt+lettre...)."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf = ""
    while True:
        if not buf:
            data = os.read(fd, 64)
            if not data:
                return
            buf += decoder.decode(data)
            continue
        if buf[0] != "\x1b":
            ch, buf = buf[0], buf[1:]
            yield ch
            continue
        if len(buf) == 1:
            # Echap seul, ou debut de sequence pas encore arrive ?
            ready, _, _ = select.select([fd], [], [], ESCAPE_TIMEOUT_SEC)
            if not ready:
                buf = ""
                yield "\x1b"
                continue
            buf += decoder.decode(os.read(fd, 64))
            continue
        if buf[1] != "[":
            seq, buf = buf[:2], buf[2:]
            yield seq
            continue
        # CSI : "\x1b[" puis parametres, termine par un octet 0x40-0x7E.
        # Cas particulier console Linux : F1-F5 = "\x1b[[A".."\x1b[[E".
        start = 3 if len(buf) > 2 and buf[2] == "[" else 2
        end = next((i for i in range(start, len(buf)) if "@" <= buf[i] <= "~"), None)
        if end is None:
            ready, _, _ = select.select([fd], [], [], ESCAPE_TIMEOUT_SEC)
            if not ready:
                seq, buf = buf, ""
                yield seq
                continue
            buf += decoder.decode(os.read(fd, 64))
            continue
        seq, buf = buf[:end + 1], buf[end + 1:]
        yield seq


def keytest(key_source):
    """Mode reperage : annonce chaque touche sans rien executer."""
    intro = (f"Mode repérage des touches. Chaque touche est annoncée, rien n'est exécuté. "
             f"Appuie {KEYTEST_EXIT_ESCAPES} fois sur échap pour quitter.")
    out(intro + "\n")
    voix.say(intro)
    announcer = keys.ModifierAnnouncer(voix.say)
    announcer.start()
    escapes = 0
    try:
        for key in key_source:
            if key == "\x1b":
                escapes += 1
                if escapes >= KEYTEST_EXIT_ESCAPES:
                    break
            else:
                escapes = 0
            name = keys.sequence_name(key)
            out(f"[{name}]\n")
            voix.say(name)
    finally:
        announcer.stop()
    return "Fin du repérage des touches."


def read_line(key_source):
    """Lit une ligne avec echo vocal de la frappe ; rend la ligne a
    l'appui sur Entree (eventuellement vide)."""
    line = ""
    for key in key_source:
        voix.stop()
        if key in ("\r", "\n"):
            out("\n")
            return line
        if key in ("\x7f", "\x08"):
            if line:
                removed, line = line[-1], line[:-1]
                out("\b \b")
                voix.say(f"efface {keys.char_name(removed)}")
            else:
                voix.say("ligne vide")
        elif key == "\x1b":
            if line:
                out("\r\x1b[K> ")
                line = ""
            voix.say("ligne effacée")
        elif key in ("\t", "\x1b[A"):
            voix.say(line if line.strip() else "ligne vide")
        elif len(key) == 1 and key.isprintable():
            line += key
            out(key)
            voix.say(keys.char_name(key))
        else:
            voix.say(keys.sequence_name(key))
    raise EOFError


def run_command(line, key_source, last_response):
    # Veilles d'abord : "arrete radio", "redemarre radio"... (voir veilles.py).
    request = veilles.parse(line)
    if request:
        return veilles.handle(*request)
    cmd, score = matcher.match(line, commands.COMMANDS)
    if cmd is None:
        return f"Commande non reconnue : {line}."
    if cmd.busy:
        state = cmd.busy()
        if state:
            return state
    if cmd.confirm:
        out(f"{cmd.confirm}\n? ")
        voix.say(cmd.confirm, lock=True)
        if matcher.normalize(read_line(key_source)) != "oui":
            return "Annulé."
        def announce(text):
            out(f"{text}\n")
            voix.say_and_wait(text)
        return cmd.handler(announce)
    if cmd.name == "décris":
        # Tout ce qui n'est pas un simple mot declencheur devient une question
        # posee en plus a l'IA ("photo, la piece est-elle decollee ?").
        question = "" if matcher.normalize(line) in map(matcher.normalize, cmd.keywords) else line
        vision.start(question, voix.say_and_wait)
        return ""
    if cmd.name == "veilles":
        return veilles.status_text()
    if cmd.name == "bilan":
        return bilan.summary()
    if cmd.name == "archives":
        return archives.run(key_source, voix.say, out)
    if cmd.name == "journal":
        return journal.run(key_source, voix.say, out)
    if cmd.name == "manuel":
        return manuel.run(key_source, voix.say, out)
    if cmd.name == "tableau":
        return tableaux.run(line, key_source, voix.say, out)
    if cmd.name == "réglages":
        return reglages_cmd.status()
    if cmd.name == "ville":
        return reglages_cmd.ville(line, key_source, voix.say, out, read_line)
    if cmd.name == "gare":
        return reglages_cmd.gare(key_source, voix.say, out)
    if cmd.name == "aéroport":
        return reglages_cmd.aeroport(line, key_source, voix.say, out)
    if cmd.name == "simulation":
        return simulation.handle(line)
    if cmd.name == "touches":
        return keytest(key_source)
    if cmd.name == "répète":
        return last_response
    if cmd.name == "radio":
        voix.say_and_wait("Scan de la bande FM, environ dix secondes.")
    try:
        return cmd.handler()
    except Exception as exc:  # une commande qui plante ne doit pas tuer la console
        print(f"[console] erreur dans {cmd.name} : {exc!r}", file=sys.stderr)
        return f"Erreur pendant la commande {cmd.name}."


def main():
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    tty.setraw(fd)
    try:
        out(f"\n{WELCOME}\n> ")
        voix.say_and_wait(WELCOME)
        last_response = WELCOME
        key_source = read_keys(fd)
        while True:
            line = read_line(key_source)
            if not line.strip():
                voix.say("ligne vide")
                out("> ")
                continue
            response = run_command(line, key_source, last_response)
            # Reponse vide = la commande parle elle-meme en arriere-plan
            # (description, mise a jour) : rien a memoriser pour "repete".
            if response:
                last_response = response
            out(f"{response}\n> ")
            voix.say(response, lock=True)
    except EOFError:
        pass
    finally:
        voix.stop()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    main()
