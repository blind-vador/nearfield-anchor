"""Ecoute et tri des enregistrements du scanner radio (dialogue "journal").

  Tab / Maj+Tab            jour suivant / precedent (annonce le jour)
  fleches haut / bas       enregistrement precedent / suivant, joue aussitot
                           (pas d'annonce : l'enregistrement commence par
                           l'annonce de la bande, enregistree avec)
  fleche droite            garde : copie dans les extraits interessants
                           (storage/radio), nommee par sa date de capture
  fleche gauche            rejette : meme procedure que "parasite"
                           (supprime + apprend l'empreinte si elle existe)
  Entree                   rejoue          Echap : ferme

Sources : server/radio_log/ (48 dernieres heures, date dans le nom) et
ses dossiers d'archives par jour ("jeudi_3_septembre_2026/", heure seule
dans le nom).

UNE seule sortie son pour tout le dialogue (aplay ouvert en continu,
alimente par ffmpeg) : passer vite d'un enregistrement a l'autre ne
referme pas la carte son a chaque fois -- c'est ce qui faisait planter le
pilote du jack du Pi (voir lib/manual_tuner.py).
"""
import audioop
import datetime
import fcntl
import os
import re
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import noise_signatures as ns  # noqa: E402
import notation  # noqa: E402

LOG_DIR = "/media/nas/nd1/server/radio_log"
KEEP_DIR = "/media/nas/nd2/storage/radio"
AUDIO_LOCK_PATH = "/tmp/radio_watch_audio.lock"
LOCK_WAIT_SEC = 10
RATE = 48000

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
_MOIS_IDX = {m: i + 1 for i, m in enumerate(MOIS)}
_MOIS_IDX.update({"fevrier": 2, "aout": 8, "decembre": 12})
_DAY = r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)_(\d{1,2})_([a-zéû]+)_(\d{4})"
_TIME = r"(\d{2})h(\d{2})m(\d{2})s\.mp3$"
RE_ROOT_FILE = re.compile("_" + _DAY + "_" + _TIME)
RE_DAY_DIR = re.compile("^" + _DAY + "$")
RE_TIME_ONLY = re.compile("_" + _TIME)

INTRO = ("Journal radio. Tab et majuscule tab : changer de jour. Flèches haut et bas : "
         "écouter les enregistrements. Flèche droite : garder. Flèche gauche : rejeter comme "
         "parasite. N : noter. Entrée : rejouer. Échap : fermer.")


def _date(day, month_name, year):
    month = _MOIS_IDX.get(month_name)
    return datetime.date(int(year), month, int(day)) if month else None


def scan_recordings():
    """{date: [(datetime, chemin), ...] tries}."""
    days = {}

    def add(date, hh, mm, ss, path):
        if date:
            stamp = datetime.datetime.combine(date, datetime.time(int(hh), int(mm), int(ss)))
            days.setdefault(date, []).append((stamp, path))

    for entry in os.scandir(LOG_DIR):
        if entry.is_file():
            m = RE_ROOT_FILE.search(entry.name)
            if m:
                add(_date(m[2], m[3], m[4]), m[5], m[6], m[7], entry.path)
        elif entry.is_dir():
            d = RE_DAY_DIR.match(entry.name)
            if d:
                for f in os.scandir(entry.path):
                    t = RE_TIME_ONLY.search(f.name)
                    if t and f.is_file():
                        add(_date(d[2], d[3], d[4]), t[1], t[2], t[3], f.path)
    for recs in days.values():
        recs.sort()
    return days


def spoken_day(date):
    return f"{JOURS[date.weekday()]} {date.day} {MOIS[date.month - 1]}"


def keep_name(stamp):
    return (f"{JOURS[stamp.weekday()]} {stamp.day} {MOIS[stamp.month - 1]} {stamp.year} "
            f"{stamp.hour:02d}h{stamp.minute:02d}m{stamp.second:02d}s.mp3")


class Player:
    """aplay ouvert en continu ; chaque lecture = un ffmpeg qui l'alimente."""

    def __init__(self):
        self._aplay = subprocess.Popen(
            ["aplay", "-q", "-D", "default", "-r", str(RATE), "-f", "S16_LE", "-c", "1", "-B", "500000"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._ffmpeg = None
        self.gain = 1.0  # volume de lecture (dialogue "archives" : fleches gauche/droite)
        self._generation = 0
        self._guard = threading.Lock()

    def play(self, path):
        self.stop()
        self._ffmpeg = subprocess.Popen(
            ["ffmpeg", "-v", "quiet", "-nostdin", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
            stdout=subprocess.PIPE)
        threading.Thread(target=self._relay, args=(self._ffmpeg, self._generation), daemon=True).start()

    def _relay(self, proc, generation):
        while True:
            data = proc.stdout.read(RATE // 5 * 2)  # 200 ms
            if not data:
                return
            if self.gain != 1.0:
                data = audioop.mul(data, 2, self.gain)  # ecretage automatique au-dela du maximum
            with self._guard:
                if generation != self._generation:
                    return
                try:
                    self._aplay.stdin.write(data)
                    self._aplay.stdin.flush()
                except (BrokenPipeError, ValueError, OSError):
                    return

    def stop(self):
        with self._guard:
            self._generation += 1
        if self._ffmpeg is not None and self._ffmpeg.poll() is None:
            self._ffmpeg.terminate()
            try:
                self._ffmpeg.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._ffmpeg.kill()
        self._ffmpeg = None

    def close(self):
        self.stop()
        try:
            self._aplay.stdin.close()  # fin de flux : la carte son se ferme une seule fois
            self._aplay.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self._aplay.kill()


def _acquire_audio_lock():
    f = open(AUDIO_LOCK_PATH, "w")
    deadline = time.time() + LOCK_WAIT_SEC
    while time.time() < deadline:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            time.sleep(0.2)
    return f


def _keep(stamp, path):
    os.makedirs(KEEP_DIR, exist_ok=True)
    dest = os.path.join(KEEP_DIR, keep_name(stamp))
    if os.path.exists(dest):
        return "Déjà gardé."
    shutil.copy2(path, dest)
    return "Gardé."


def _reject(path):
    info = ns.capture_info(path)
    os.remove(path)
    ns.forget_capture(path)
    last = ns.last_capture()
    if last and last.get("log_path") == path:
        ns.mark_last_capture_noise()
    if info and info.get("fingerprint"):
        ns.add_signature(info)
        return "Rejeté, empreinte apprise."
    return "Rejeté et supprimé. Pas d'empreinte sonore pour celui-ci, je ne peux pas l'apprendre."


def run(key_source, say, out):
    if not os.path.ismount("/media/nas/nd1"):
        return "Le disque server n'est pas accessible, pas de journal radio."
    days = scan_recordings()
    if not days:
        return "Aucun enregistrement radio dans le journal."
    order = sorted(days, reverse=True)  # du plus recent au plus ancien
    # rec_i = -1 : "avant le premier" -- la premiere fleche bas joue le 1er enregistrement.
    day_i, rec_i = 0, -1
    lock = _acquire_audio_lock()
    player = Player()

    def announce_day():
        date = order[day_i]
        n = len(days[date])
        return f"{spoken_day(date)}, {n} enregistrement{'s' if n > 1 else ''}."

    try:
        out(INTRO + "\n")
        say(INTRO + " " + announce_day())
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
                    new_i = -1  # deja avant le premier
                if 0 <= new_i < len(recs):
                    rec_i = new_i
                    player.play(recs[rec_i][1])
                    out(f"[{os.path.basename(recs[rec_i][1])}]\n")
                else:
                    player.stop()
                    text = "Début de la journée." if new_i < 0 else "Fin de la journée."
            elif rec_i < 0 and key in ("\r", "\n", "\x1b[C", "\x1b[D", "n", "N"):
                text = "Choisis d'abord un enregistrement avec la flèche bas."
            elif key in ("\r", "\n"):
                player.play(recs[rec_i][1])
            elif key in ("\x1b[C", "\x1b[D"):
                player.stop()
                stamp, path = recs[rec_i]
                if key == "\x1b[C":
                    text = _keep(stamp, path)
                else:
                    text = _reject(path)
                    del recs[rec_i]
                    if not recs:
                        del days[order[day_i]]
                        order.pop(day_i)
                        if not order:
                            text += " Plus aucun enregistrement."
                            say(text)
                            break
                        day_i %= len(order)
                        rec_i = -1
                        text += " Journée vide. " + announce_day()
                    else:
                        rec_i = min(rec_i, len(recs) - 1)
            elif key in ("n", "N"):
                player.stop()
                stamp, path = recs[rec_i]
                text = notation.rate(key_source, say, out, path, stamp)
            else:
                text = "Tab, flèches, N, entrée, ou échap pour fermer."
            if text:
                out(f"[{text}]\n")
                say(text)
    finally:
        player.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return "Journal radio fermé."
