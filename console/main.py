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
import time
import termios
import tty

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import commands  # noqa: E402
import keys  # noqa: E402
import matcher  # noqa: E402
import archives  # noqa: E402
import bilan  # noqa: E402
import journal  # noqa: E402
import journaux  # noqa: E402
import manuel  # noqa: E402
import reglages_cmd  # noqa: E402
import tableaux  # noqa: E402
import flash  # noqa: E402
import voix_cmd  # noqa: E402
import conseils  # noqa: E402
import priorites  # noqa: E402
import observation  # noqa: E402
import appairage  # noqa: E402
import simulation  # noqa: E402
import veilles  # noqa: E402
import vision  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

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
            ready, _, _ = select.select([fd, keys.WAKE_R], [], [])
            if keys.WAKE_R in ready:
                os.read(keys.WAKE_R, 64)
                yield keys.WAKE
                continue
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
    intro = conseils.intro("touches", dire("console.mode_reperage_touches"),
                           dire("console.chaque_touche_annoncee_rien", KEYTEST_EXIT_ESCAPES=KEYTEST_EXIT_ESCAPES))
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
    return dire("console.fin_reperage_touches")


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
            voix.say(dire("console.ligne_effacee"))
        elif key in ("\t", "\x1b[A"):
            voix.say(line if line.strip() else "ligne vide")
        elif len(key) == 1 and key.isprintable():
            line += key
            out(key)
            voix.say(keys.char_name(key))
        else:
            voix.say(keys.sequence_name(key))
    raise EOFError


# Reponse donnee plus tard, en arriere-plan (bulletin meteo) : "repete" la
# relit si elle est plus recente que la derniere reponse directe.
_async_response = {"text": "", "t": 0.0}
_last_direct = {"t": 0.0}


def _scan_then(produce, busy_key, wait_key, fail_key):
    """Balayage du panel radio meteo local par le scanner, PUIS produce(scan,
    scan_failed) en arriere-plan : le clavier reste libre, la reponse est
    dite quand elle est prete (et "repete" la relit)."""
    import threading
    import meteo_locale
    asked = meteo_locale.request_scan()
    seconds = meteo_locale.scan_duration()

    busy = meteo_locale.scanner_busy()

    def run():
        scan = None if busy else meteo_locale.wait_scan(asked, timeout=seconds + 180)
        reason = busy or meteo_locale.scanner_busy()
        try:
            text = produce(scan, (reason or True) if scan is None else None)
        except Exception as exc:  # une source au format inattendu ne doit pas tuer la console
            print(f"[console] {fail_key} : {exc!r}", file=sys.stderr)
            text = dire(fail_key)
        _async_response.update(text=text, t=time.time())
        out(f"{text}\n> ")
        voix.say_and_wait(text)

    threading.Thread(target=run, daemon=True).start()
    if busy:
        return dire(busy_key, busy=busy)
    # Le scanner finit d'abord son etape en cours (balayage d'une bande large,
    # captation) : mesure reelle 2026-10-01, 3 min d'attente + 2 min 30 de balayage.
    low, high = max(3, round(seconds / 60) + 1), max(5, round(seconds / 60) + 3)
    return dire(wait_key, low=low, high=high)


def meteo_cmd(line):
    """Bulletin complet bati sur l'agregat de toutes les mesures, apres un
    balayage frais du panel radio meteo (demande utilisateur 2026-10-01)."""
    import meteo_locale
    return _scan_then(lambda scan, failed: meteo_locale.bulletin(scan=scan, scan_failed=failed),
                      "console.prepare_bulletin_meteo_balayage", "console.balaie_abord_panel_radio",
                      "console.bulletin_meteo_echoue_erreur")


def flash_cmd(line):
    """Flash complet (demande utilisateur 2026-10-03) : on prend le temps de
    tout rassembler -- balayage meteo, agregat, bulletin, routes, trains,
    bus, presse -- puis on met en forme."""
    return _scan_then(flash.texte_complet, "flash.prepare_complet_sans_balayage", "flash.prepare_complet",
                      "flash.flash_info_echoue_erreur")


def _match(line):
    """Commande tapee. "journal ..." et "archives ..." passent avant tout :
    "journal capteurs" ou "archives satellites" ne doivent pas ouvrir la
    commande capteurs ou satellites (lib/journaux.py)."""
    first = (matcher.normalize(line).split() or [""])[0]
    if first in ("journal", "journaux", "archives", "archive"):
        name = "journal" if first.startswith("journ") else "archives"
        return next(c for c in commands.COMMANDS if c.name == name), 100
    if first in ("plante", "plantes"):  # "plante photo" : pas la commande decris
        return next(c for c in commands.COMMANDS if c.name == "plantes"), 100
    return matcher.match(line, commands.COMMANDS)


def cmd_is_not_repeat(line):
    cmd, _ = _match(line)
    return cmd is None or cmd.name != "répète"


def run_command(line, key_source, last_response):
    # Veilles d'abord : "arrete radio", "redemarre radio"... (voir veilles.py).
    request = veilles.parse(line)
    if request:
        return veilles.handle(*request)
    cmd, score = _match(line)
    if cmd is None:
        return dire("console.commande_non_reconnue", commande=line)
    if cmd.busy:
        state = cmd.busy()
        if state:
            return state
    if cmd.confirm:
        question = cmd.question()
        out(f"{question}\n? ")
        voix.say(question, lock=True)
        if matcher.normalize(read_line(key_source)) != "oui":
            return dire("commun.annule")
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
    if cmd.name == "bilan" and "meteo" in matcher.normalize(line).split():
        import meteo_obs
        return meteo_obs.summary()
    if cmd.name == "bilan":
        return bilan.summary()
    if cmd.name == "personnalité":
        import personnalite_cmd
        return personnalite_cmd.run(key_source, voix.say, out, read_line)
    if cmd.name == "applique":
        return bilan.apply_all()
    if cmd.name == "réglage précédent":
        import reglages_audio
        what = reglages_audio.previous()
        return dire("console.revenu_version_avant_annule", what=what) if what else dire("console.aucun_reglage_annuler_tout")
    if cmd.name == "réglages automatiques":
        import reglages_audio
        words = set(matcher.normalize(line).split())
        on = not (words & {"non", "arrete", "arreter", "desactive", "desactiver", "stop", "coupe"})
        reglages_audio.set_auto(on)
        return (dire("console.reglages_automatiques_actives_apres") if on else dire("console.reglages_automatiques_desactives_bilan"))
    if cmd.name == "réglages audio":
        return bilan.settings_text()
    if cmd.name == "réapprendre":
        return bilan.reset_all()
    if cmd.name == "archives":
        return archives.run(key_source, voix.say, out, journaux.from_words(matcher.normalize(line)))
    if cmd.name == "journal":
        return journal.run(key_source, voix.say, out, journaux.from_words(matcher.normalize(line)))
    if cmd.name == "manuel":
        return manuel.run(key_source, voix.say, out)
    if cmd.name == "priorités":
        return priorites.run(key_source, voix.say, out)
    if cmd.name == "conseils":
        return conseils.command(line)
    if cmd.name == "voitures":
        return commands.voitures_cmd(line)
    if cmd.name == "voix":
        return voix_cmd.run(key_source, voix.say, out)
    if cmd.name == "appairage":
        return appairage.run(key_source, voix.say, out)
    if cmd.name == "compagnon":
        import compagnon
        return compagnon.status()
    if cmd.name == "stations":
        import stations
        return stations.spoken_list()
    if cmd.name == "satellites":
        import satellites
        return satellites.spoken_list()
    if cmd.name == "trafic":
        import trafic
        voix.say(dire("console.regarde_etat_routes"))
        return trafic.summary()
    if cmd.name == "environnement":
        import donnees_ouvertes
        voix.say(dire("console.rassemble_donnees"))
        return donnees_ouvertes.summary()
    if cmd.name == "observation":
        if matcher.normalize(line) in ("observation", "observer", "observation meteo"):
            return observation.run(line, key_source, voix.say, out)
        return observation.quick(line) or observation.run(line, key_source, voix.say, out)
    if cmd.name == "notifications":
        return journal.notifications()
    if cmd.name == "mes capteurs":
        import capteurs_cmd
        return capteurs_cmd.run(line, key_source, voix.say, out)
    if cmd.name == "compris":
        import plantes_alertes
        silenced = plantes_alertes.ack()
        if not silenced:
            return dire("plantes.compris_rien")
        return dire("plantes.compris", nombre=nombre(len(silenced), "alerte"))
    if cmd.name == "plantes":
        import plantes_cmd
        return plantes_cmd.run(line, key_source, voix.say, out)
    if cmd.name == "liste noire":
        import liste_noire_cmd
        return liste_noire_cmd.run(line, key_source, voix.say, out)
    if cmd.name == "intérêts":
        import interets_cmd
        return interets_cmd.run(line, key_source, voix.say, out)
    if cmd.name == "météo":
        return meteo_cmd(line)
    if cmd.name == "flash":
        if set(matcher.normalize(line).split()) & flash.DETAIL_WORDS:
            return flash.run(line, key_source, voix.say, out)
        return flash_cmd(line)
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
        if _async_response["t"] > _last_direct["t"]:
            return _async_response["text"]
        return last_response
    if cmd.name == "radio":
        voix.say_and_wait(dire("console.scan_bande_fm_environ"))
    try:
        return cmd.handler()
    except Exception as exc:  # une commande qui plante ne doit pas tuer la console
        print(f"[console] erreur dans {cmd.name} : {exc!r}", file=sys.stderr)
        return dire("console.erreur_pendant_commande", name=cmd.name)


def main():
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    tty.setraw(fd)
    try:
        welcome = conseils.intro("accueil", dire("console.accueil"), dire("console.accueil_consigne"))
        out(f"\n{welcome}\n> ")
        voix.say_and_wait(welcome)
        # Noms de touches dans la voix choisie, prepares en fond (espeak en attendant).
        voix.prune_cache()
        voix.prewarm(voix_cmd.echo_texts())
        last_response = welcome
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
            if response and cmd_is_not_repeat(line):
                last_response = response
                _last_direct["t"] = time.time()
            out(f"{response}\n> ")
            voix.say(response, lock=True)
    except EOFError:
        pass
    finally:
        voix.stop()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    main()
