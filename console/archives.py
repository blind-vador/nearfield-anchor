"""Ecoute des extraits gardes (dialogue "archives"), une archive par journal
(lib/journaux.py) : "archives" (radio, storage/radio/), "archives
capteurs", "archives satellites", "archives donnees".

Meme navigation que le journal, mais en lecture seule :
  Tab / Maj+Tab            jour suivant / precedent
  fleches haut / bas       extrait precedent / suivant, joue aussitot (un
                           releve de capteurs garde en texte est lu)
  Ctrl + fleche haut / bas premier / dernier extrait du jour
  fleches gauche / droite  volume de lecture - / + (temporaire, pour ce
                           dialogue seulement)
  Entree                   rejoue          Echap : ferme
Rien n'est jamais supprime ni deplace.

Date d'un extrait : celle de son nom quand il a ete garde depuis le journal
("mardi 29 septembre 2026 06h59m56s.mp3") ; sinon (extraits nommes a la
main, "belfort en approche.mp3") la date du fichier -- et dans ce cas le
titre est lu avant l'ecoute, puisque c'est lui qui dit de quoi il s'agit.
"""
import datetime
import os
import re
import subprocess

import journal
import journaux  # noqa: E402  (chemin ajoute par journal)
from phrases import dire, nombre  # noqa: E402
import espeak_out  # noqa: E402  (chemin ajoute par journal)
import conseils  # noqa: E402
import voix  # noqa: E402

ARCHIVE_DIR = journal.KEEP_DIR
AUDIO_EXT = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".txt")
RE_DATED = re.compile(r"^(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche) (\d{1,2}) ([a-zéû]+) "
                      r"(\d{4}) (\d{2})h(\d{2})m(\d{2})s\.")
VOLUME_STEP = 0.2
VOLUME_MIN, VOLUME_MAX = 0.2, 3.0

def scan_archives(folder=ARCHIVE_DIR):
    """{date: [(datetime, chemin, titre ou None), ...] tries}."""
    days = {}
    for entry in os.scandir(folder):
        if not entry.is_file() or not entry.name.lower().endswith(AUDIO_EXT):
            continue
        m = RE_DATED.match(entry.name)
        date = journal._date(m[2], m[3], m[4]) if m else None
        if date:
            stamp = datetime.datetime.combine(date, datetime.time(int(m[5]), int(m[6]), int(m[7])))
            title = None
        else:
            stamp = datetime.datetime.fromtimestamp(entry.stat().st_mtime)
            title = os.path.splitext(entry.name)[0]
        days.setdefault(stamp.date(), []).append((stamp, entry.path, title))
    for recs in days.values():
        recs.sort()
    return days


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def run(key_source, say, out, category="radio"):
    name = journaux.NAMES[category]
    folder = journaux.archive_dir(category)
    if not os.path.ismount("/media/nas/nd2"):
        return dire("archives.disque_absent")
    days = scan_archives(folder) if os.path.isdir(folder) else {}
    if not days:
        return dire("archives.vide", nom=name)
    title_ = dire("archives.titre", nom=name)
    consigne = dire("archives.consigne")
    order = sorted(days, reverse=True)
    day_i, rec_i = 0, -1  # -1 : "avant le premier"
    journal._journal_opened(True)
    player = journal.Player()

    def announce_day():
        n = len(days[order[day_i]])
        return dire("nav.jour", jour=journal.spoken_day(order[day_i]), elements=nombre(n, "extrait"))

    try:
        intro = conseils.intro("archives", title_, consigne)
        out(intro + "\n")
        say(intro + " " + announce_day())
        for key in key_source:
            if key == "\x1b":
                break
            recs = days[order[day_i]]
            text = None
            if key in ("\t", "\x1b[Z"):
                player.stop()
                day_i = (day_i + (1 if key == "\t" else -1)) % len(order)
                rec_i = -1
                text = announce_day()
            elif key in ("\x1b[A", "\x1b[B"):
                new_i = rec_i + (1 if key == "\x1b[B" else -1)
                if key == "\x1b[A" and rec_i < 0:
                    new_i = -1
                if journal.ctrl_held():
                    new_i = 0 if key == "\x1b[A" else len(recs) - 1
                    voix.say_blocking(dire("nav.premier_jour" if key == "\x1b[A" else "nav.dernier_jour"))
                if 0 <= new_i < len(recs):
                    rec_i = new_i
                    _, path, title = recs[rec_i]
                    if path.endswith(".txt"):  # releve de capteurs garde : lu
                        player.stop()
                        text = _read_text(path)
                    elif title:
                        # Titre choisi a la main : il dit de quoi il s'agit. Lu EN ENTIER
                        # avant l'extrait (espeak bloquant, pas la voix asynchrone).
                        player.stop()
                        say("")
                        espeak_out.say(title, rate=190, timeout=20)
                    if not path.endswith(".txt"):
                        player.play(path)
                        out(f"[{os.path.basename(path)}]\n")
                else:
                    bound = dire("nav.debut_journee" if new_i < 0 else "nav.fin_journee")
                    if player.playing() or rec_i < 0 or recs[rec_i][1].endswith(".txt"):
                        player.stop()
                        text = bound
                    else:
                        # Rien en lecture contre la borne : on rejoue l'extrait selectionne.
                        voix.say_blocking(bound)
                        player.play(recs[rec_i][1])
            elif key in ("\x1b[C", "\x1b[D"):
                step = VOLUME_STEP if key == "\x1b[C" else -VOLUME_STEP
                player.gain = round(min(max(player.gain + step, VOLUME_MIN), VOLUME_MAX), 1)
                text = dire("archives.volume", pourcent=round(player.gain * 100))
            elif key in ("\r", "\n"):
                if rec_i < 0:
                    text = dire("archives.choisir")
                else:
                    path = recs[rec_i][1]
                    if path.endswith(".txt"):
                        text = _read_text(path)
                    else:
                        player.play(path)
            elif conseils.is_help(key):
                text = consigne
            else:
                text = dire("archives.aide_courte")
            if text:
                out(f"[{text}]\n")
                say(text)
    finally:
        player.close()
        journal._journal_opened(False)
    return dire("archives.ferme", nom=name)
