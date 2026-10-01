"""Ecoute des extraits radio gardes (dialogue "archives") : storage/radio/.

Meme navigation que le journal, mais en lecture seule :
  Tab / Maj+Tab            jour suivant / precedent
  fleches haut / bas       extrait precedent / suivant, joue aussitot
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
import fcntl
import os
import re
import subprocess

import journal
import espeak_out  # noqa: E402  (chemin ajoute par journal)
import conseils  # noqa: E402

ARCHIVE_DIR = journal.KEEP_DIR
AUDIO_EXT = (".mp3", ".wav", ".ogg", ".m4a", ".flac")
RE_DATED = re.compile(r"^(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche) (\d{1,2}) ([a-zéû]+) "
                      r"(\d{4}) (\d{2})h(\d{2})m(\d{2})s\.")
VOLUME_STEP = 0.2
VOLUME_MIN, VOLUME_MAX = 0.2, 3.0

TITLE = "Archives radio, lecture seule."
CONSIGNE = ("Tab et majuscule tab : changer de jour. Flèches haut et bas : "
            "écouter. Gauche et droite : volume. Entrée : rejouer. Échap : fermer. F1 redit ces touches.")


def scan_archives():
    """{date: [(datetime, chemin, titre ou None), ...] tries}."""
    days = {}
    for entry in os.scandir(ARCHIVE_DIR):
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


def run(key_source, say, out):
    if not os.path.ismount("/media/nas/nd2"):
        return "Le disque de stockage n'est pas accessible, pas d'archives."
    if not os.path.isdir(ARCHIVE_DIR):
        return "Aucune archive radio pour l'instant."
    days = scan_archives()
    if not days:
        return "Aucune archive radio pour l'instant."
    order = sorted(days, reverse=True)
    day_i, rec_i = 0, -1  # -1 : "avant le premier"
    lock = journal._acquire_audio_lock()
    player = journal.Player()

    def announce_day():
        n = len(days[order[day_i]])
        return f"{journal.spoken_day(order[day_i])}, {n} extrait{'s' if n > 1 else ''}."

    try:
        intro = conseils.intro("archives", TITLE, CONSIGNE)
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
                if 0 <= new_i < len(recs):
                    rec_i = new_i
                    _, path, title = recs[rec_i]
                    if title:
                        # Titre choisi a la main : il dit de quoi il s'agit. Lu EN ENTIER
                        # avant l'extrait (espeak bloquant, pas la voix asynchrone).
                        player.stop()
                        say("")
                        espeak_out.say(title, rate=190, timeout=20)
                    player.play(path)
                    out(f"[{os.path.basename(path)}]\n")
                else:
                    player.stop()
                    text = "Début de la journée." if new_i < 0 else "Fin de la journée."
            elif key in ("\x1b[C", "\x1b[D"):
                step = VOLUME_STEP if key == "\x1b[C" else -VOLUME_STEP
                player.gain = round(min(max(player.gain + step, VOLUME_MIN), VOLUME_MAX), 1)
                text = f"Volume {round(player.gain * 100)} pour cent."
            elif key in ("\r", "\n"):
                if rec_i < 0:
                    text = "Choisis d'abord un extrait avec la flèche bas."
                else:
                    player.play(recs[rec_i][1])
            elif conseils.is_help(key):
                text = CONSIGNE
            else:
                text = "Tab, flèches, entrée, ou échap pour fermer. F1 pour l'aide."
            if text:
                out(f"[{text}]\n")
                say(text)
    finally:
        player.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return "Archives radio fermées."
