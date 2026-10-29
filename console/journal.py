"""Ecoute et tri des enregistrements (dialogue "journal"), un journal par
famille (lib/journaux.py) : radio (par defaut), donnees, capteurs,
satellites -- "journal capteurs", "journal satellites"...

  Tab / Maj+Tab            jour suivant / precedent (annonce le jour)
  fleches haut / bas       enregistrement precedent / suivant, joue aussitot ;
                           au bout de la journee, continue dans le jour d a cote (2026-10-23)
                           (pas d'annonce : l'enregistrement commence par
                           l'annonce de la bande, enregistree avec)
  Ctrl + fleche haut / bas premier / dernier enregistrement du jour
                           (Ctrl lu sur le clavier par evdev : la console
                           envoie la meme sequence qu'une fleche seule)
  C                        bonne prise, mais coupee trop tot (comme V, fin
                           coupee : le bilan fera ecouter plus longtemps) ;
                           sur une prise deja notee, corrige seulement la fin
  fleche droite            garde : copie dans les extraits interessants
                           (storage/radio), nommee par sa date de capture
  fleche gauche            rejette : meme procedure que "parasite"
                           (supprime + apprend l'empreinte si elle existe) ;
                           un message DAB+ rejete interdit sa station
                           (reglage dab, "interdites", lu par dab_watch)
  I                        informations : frequence, mode, et pour la CB et
                           le 10 m le verdict "vraie emission ou artefact"
  B                        ecoute le son brut, avant nettoyage (journal
                           radio : <nom>.brut.mp3, garde 48 h comme le reste)
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
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time


import journaux  # noqa: E402
from phrases import Etiquettes, dire, nombre  # noqa: E402
import keys  # noqa: E402
import noise_signatures as ns  # noqa: E402
import notation  # noqa: E402
import conseils  # noqa: E402
import voix  # noqa: E402

LOG_DIR = "/media/nas/nd1/server/radio_log"
KEEP_DIR = "/media/nas/nd2/storage/radio"
UPDATE_FLAG = "/tmp/journal_maj"  # touche par radio_log a chaque captation enregistree
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

# Phrases dites : lib/phrases.py (fichier phrases/<langue>.ini, personnalite du jour).

MODES = Etiquettes("mode", {"nfm": "FM étroite", "wfm": "FM large", "am": "AM", "usb": "BLU haute",
                             "lsb": "BLU basse"}, "mode de réception radio")


def _date(day, month_name, year):
    month = _MOIS_IDX.get(month_name)
    return datetime.date(int(year), month, int(day)) if month else None


def scan_recordings(category="radio"):
    """{date: [(datetime, chemin ou None, texte ou None), ...] tries} : les
    enregistrements de ce journal ; pour les capteurs, plus les releves lus
    a voix haute (chemin None)."""
    days = {}

    def add(date, hh, mm, ss, path):
        name = os.path.basename(path)
        if not date or journaux.category_of(name) != category:
            return
        stamp = datetime.datetime.combine(date, datetime.time(int(hh), int(mm), int(ss)))
        days.setdefault(date, []).append((stamp, path, None))

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
    # (2026-10-03) les releves de capteurs sont des prises vocales enregistrees
    # au fil de l'eau : plus de releves lus par la voix de la console ici.
    for recs in days.values():
        recs.sort(key=lambda r: r[0])
    return days


def spoken_day(date):
    return f"{JOURS[date.weekday()]} {date.day} {MOIS[date.month - 1]}"


def keep_name(stamp):
    return (f"{JOURS[stamp.weekday()]} {stamp.day} {MOIS[stamp.month - 1]} {stamp.year} "
            f"{stamp.hour:02d}h{stamp.minute:02d}m{stamp.second:02d}s.mp3")


class Player:
    """aplay ouvert en continu ; chaque lecture = un ffmpeg qui l'alimente."""

    def __init__(self):
        self._aplay = self._open_aplay()
        self._ffmpeg = None
        self._thread = None
        self.gain = 1.0  # volume de lecture (dialogue "archives" : fleches gauche/droite)
        self._generation = 0
        self._guard = threading.Lock()
        self._ticket = None  # sortie son prise seulement pendant une ecoute

    @staticmethod
    def _open_aplay():
        return subprocess.Popen(
            ["aplay", "-q", "-D", "default", "-r", str(RATE), "-f", "S16_LE", "-c", "1", "-B", "500000"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _aplay_ok(self):
        """Relance la sortie son si elle est tombee (2026-10-19 : un aplay mort
        rendait tout le journal muet, sans aucun message)."""
        if self._aplay.poll() is not None:
            print(f"[journal] sortie son tombee (code {self._aplay.returncode}) : relancee", file=sys.stderr)
            self._aplay = self._open_aplay()

    def _hold(self):
        """Ecouter est une action directe : le son part tout de suite, sans
        attendre son tour (l'attente bloquait le clavier jusqu'a 10 s,
        2026-10-19) ; la phrase de la console en cours s'efface devant lui."""
        import audio_lock
        if self._ticket is None:
            voix.stop()
            self._ticket = audio_lock.prendre_sans_attendre("console")

    def _let_go(self):
        import audio_lock
        ticket, self._ticket = self._ticket, None
        if ticket is not None:
            audio_lock.release(ticket)

    def play(self, path):
        self.stop()
        self._aplay_ok()
        self._hold()
        self._ffmpeg = subprocess.Popen(
            ["ffmpeg", "-v", "quiet", "-nostdin", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
            stdout=subprocess.PIPE)
        self._thread = threading.Thread(target=self._relay, args=(self._ffmpeg, self._generation), daemon=True)
        self._thread.start()

    def playing(self):
        """Vrai tant que l'enregistrement est en cours de lecture."""
        return self._thread is not None and self._thread.is_alive()

    def _relay(self, proc, generation):
        while True:
            data = proc.stdout.read(RATE // 5 * 2)  # 200 ms
            if not data:
                with self._guard:
                    if generation == self._generation:
                        self._let_go()  # fin de l'ecoute : les annonces peuvent passer
                return
            if self.gain != 1.0:
                data = audioop.mul(data, 2, self.gain)  # ecretage automatique au-dela du maximum
            with self._guard:
                if generation != self._generation:
                    return
                for essai in (1, 2):
                    try:
                        self._aplay.stdin.write(data)
                        self._aplay.stdin.flush()
                        break
                    except (BrokenPipeError, ValueError, OSError):
                        if essai == 2:
                            self._let_go()
                            return
                        self._aplay_ok()  # sortie tombee en cours de lecture : relancee, on reprend

    def stop(self):
        with self._guard:
            self._generation += 1
            self._let_go()
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


def _journal_opened(is_open):
    """Pendant que le journal (ou les archives) est ouvert, les captations
    radio s'enregistrent en silence (lib/audio_lock.py, SILENT_WHEN_JOURNAL)."""
    import audio_lock
    audio_lock.set_journal_open(is_open)


def _update_stamp():
    try:
        return os.path.getmtime(UPDATE_FLAG)
    except OSError:
        return 0.0


def _keep(stamp, path, text, category):
    """Copie dans les archives de ce journal ; un releve lu (capteurs) y est
    garde en texte, relu par "archives capteurs"."""
    folder = journaux.archive_dir(category)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, keep_name(stamp))
    if text is not None:
        dest = dest[:-4] + ".txt"
    if os.path.exists(dest):
        return dire("journal.deja_garde")
    if text is not None:
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        shutil.copy2(path, dest)
    return dire("journal.garde")


def info_text(path):
    """Touche I : ce que le systeme sait de cet enregistrement."""
    info = ns.capture_info(path) or {}
    if not info.get("freq_hz"):
        fiche = _message_card(path)
        if fiche:
            parts = [dire("journal.info_protocole", protocole=fiche["protocole"])] if fiche.get("protocole") else []
            if fiche.get("source"):
                parts.append(dire("journal.info_source", source=fiche["source"]))
            if fiche.get("frequence_hz"):
                parts.append(dire("journal.info_frequence", frequence=f"{fiche['frequence_hz'] / 1e6:.4f}".rstrip("0").replace(".", ",")))
            return " ".join(parts) or dire("journal.pas_info")
        return dire("journal.pas_info")
    mhz = f"{info['freq_hz'] / 1e6:.4f}".rstrip("0").replace(".", ",")
    text = dire("journal.info", frequence=mhz, mode=MODES.get(info.get("mode"), info.get("mode") or dire("journal.mode_inconnu")),
                libelle=info.get("label") or dire("journal.bande_inconnue"))
    analysis = (info.get("metrics") or {}).get("analyse")
    if analysis:
        import stations  # verdict dit dans la langue du moment (le code est garde depuis le 2026-10-03)
        verdict = stations.VERDICTS[analysis["code"]] if analysis.get("code") in stations.VERDICTS else analysis["verdict"]
        text += " " + dire("journal.info_analyse", largeur=str(analysis["largeur_khz"]).replace(".", ","),
                           baisse=str(analysis["baisse_db"]).replace(".", ","), verdict=verdict)
    return text


def _message_card(path):
    """Fiche d'une prise vocale (lib/prise_vocale.py) : <nom>.json, ou None."""
    try:
        with open(path[:-4] + ".json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return None


def raw_path(path):
    """Son brut garde a cote d'un enregistrement radio, s'il existe."""
    candidate = path[:-4] + ".brut.mp3" if path else None
    return candidate if candidate and os.path.exists(candidate) else None


def _ban_dab_station(fiche):
    """Message DAB+ rejete : sa station rejoint les interdites (2026-10-06,
    demande utilisateur : ecarter une radio commerciale depuis le journal).
    Rend la phrase a dire."""
    import dab_liste
    station = (fiche.get("station") or fiche.get("source", "").rsplit(", ", 1)[0]).strip()
    if not station:
        return dire("journal.rejete_supprime")
    status, _ = dab_liste.add(station)
    if status == "deja":
        return dire("journal.dab_deja_interdite", station=station)
    if status == "nas":
        return dire("journal.dab_interdite_echec", station=station)
    return dire("journal.dab_interdite", station=station)


def _reject(path):
    info = ns.capture_info(path)
    fiche = _message_card(path)
    os.remove(path)
    if raw_path(path):
        os.remove(raw_path(path))
    if fiche is not None:
        try:
            os.remove(path[:-4] + ".json")
        except OSError:
            pass
        if fiche.get("protocole") == "DAB+":
            return _ban_dab_station(fiche)
    ns.forget_capture(path)
    last = ns.last_capture()
    if last and last.get("log_path") == path:
        ns.mark_last_capture_noise()
    try:  # un parasite ne doit pas attirer l'attention du scanner
        import attention
        attention.stimulate((info or {}).get("target"), attention.PARASITE_POINTS)
    except Exception:
        pass
    if info and info.get("fingerprint"):
        ns.add_signature(info)
        return dire("journal.rejete_appris")
    return dire("journal.rejete_supprime")


def ctrl_held():
    return keys.modifier_held("ctrl")


def alt_held():
    return keys.modifier_held("alt")


def unheard_counts():
    """{journal: entrees pas encore ecoutees} (commande "notifications")."""
    import journal_vus
    counts = {}
    for category in journaux.ORDER:
        n = sum(1 for recs in scan_recordings(category).values() for _, path, text in recs
                if path and text is None and journal_vus.is_new(path))
        if n:
            counts[category] = n
    return counts


def notifications():
    if not os.path.ismount("/media/nas/nd1"):
        return dire("notifications.disque_absent")
    counts = unheard_counts()
    if not counts:
        return dire("notifications.rien")
    parts = [dire("notifications.journal", entrees=nombre(n, "nouvelle entrée", "nouvelles entrées"),
                  nom=journaux.NAMES[c]) for c, n in counts.items()]
    return dire("notifications.liste", liste=" ; ".join(parts))


def run(key_source, say, out, category="radio"):
    name = journaux.NAMES[category]
    if not os.path.ismount("/media/nas/nd1"):
        return dire("journal.disque_absent", nom=name)
    days = scan_recordings(category)
    if not days:
        return dire("journal.vide", nom=name)
    title = dire("journal.titre", nom=name)
    consigne = dire("journal.consigne_capteurs" if category == "capteurs" else "journal.consigne")
    order = sorted(days, reverse=True)  # du plus recent au plus ancien
    # rec_i = -1 : "avant le premier" -- la premiere fleche bas joue le 1er enregistrement.
    day_i, rec_i = 0, -1
    _journal_opened(True)
    player = Player()
    seen_update = _update_stamp()

    def refresh():
        """Nouvelle captation enregistree pendant que le journal est ouvert :
        on la reprend dans la liste, sans perdre la position courante."""
        nonlocal days, order, day_i, rec_i
        current_day = order[day_i] if order else None
        current = days[current_day][rec_i][:2] if current_day and rec_i >= 0 else None
        fresh = scan_recordings(category)
        if not fresh:
            return
        days, order = fresh, sorted(fresh, reverse=True)
        day_i = order.index(current_day) if current_day in order else 0
        keys_ = [r[:2] for r in days[order[day_i]]]
        rec_i = keys_.index(current) if current in keys_ else min(rec_i, len(keys_) - 1)

    def announce_day():
        date = order[day_i]
        n = len(days[date])
        what = "élément" if category == "capteurs" else "enregistrement"
        return dire("nav.jour", jour=spoken_day(date), elements=nombre(n, what))

    def play(i):
        """Joue l'enregistrement, ou lit le releve (journal capteurs)."""
        _, path, text = days[order[day_i]][i]
        player.stop()
        if text is not None:
            say(text)
            out(f"[{text}]\n")
        else:
            player.play(path)
            out(f"[{os.path.basename(path)}]\n")
            try:
                import journal_vus
                journal_vus.mark(path)  # n'est plus nouveau (commande "notifications")
            except Exception as exc:
                print(f"[journal] ecoute non notee : {exc!r}")

    def next_unrated(step):
        """(jour, rang) de la prochaine entree sans note dans le sens step, ou None."""
        rated = {n.get("fichier") for n in notation.load_notes()}
        d, i = day_i, rec_i
        for _ in range(sum(len(v) for v in days.values()) + 1):
            i += step
            if not 0 <= i < len(days[order[d]]):
                d += 1 if step > 0 else -1  # jour suivant dans l'ordre de Tab (plus ancien)
                if not 0 <= d < len(order):
                    return None
                i = 0 if step > 0 else len(days[order[d]]) - 1
            _, path, text = days[order[d]][i]
            if path and text is None and os.path.basename(path) not in rated:
                return d, i
        return None

    try:
        intro = conseils.intro("journal", title, consigne)
        out(intro + "\n")
        say(intro + " " + announce_day())
        for key in key_source:
            if key == "\x1b":
                break
            stamp_now = _update_stamp()
            if stamp_now != seen_update:
                seen_update = stamp_now
                refresh()
            recs = days[order[day_i]]
            text = None
            if key in ("\t", "\x1b[Z"):
                player.stop()
                day_i = (day_i + (1 if key == "\t" else -1)) % len(order)
                rec_i = -1
                text = announce_day()
            elif key == keys.ALT_UP or (key in ("\x1b[A", "\x1b[B") and alt_held()):
                # Alt + fleche : prochaine (ou precedente) entree pas encore notee.
                found = next_unrated(1 if key == "\x1b[B" else -1)
                if found is None:
                    text = dire("journal.plus_de_non_notee")
                else:
                    if found[0] != day_i:
                        day_i = found[0]
                        voix.say_blocking(announce_day())
                    rec_i = found[1]
                    play(rec_i)
            elif key in ("\x1b[A", "\x1b[B") and ctrl_held():
                # Ctrl + fleche : premier ou dernier du jour, joue aussitot.
                rec_i = 0 if key == "\x1b[A" else len(recs) - 1
                voix.say_blocking(dire("nav.premier_jour" if key == "\x1b[A" else "nav.dernier_jour"))
                play(rec_i)
            elif key in ("\x1b[A", "\x1b[B"):
                new_i = rec_i + (1 if key == "\x1b[B" else -1)
                if key == "\x1b[A" and rec_i < 0:
                    new_i = -1  # deja avant le premier
                if 0 <= new_i < len(recs):
                    rec_i = new_i
                    play(rec_i)
                elif key == "\x1b[B" and new_i >= len(recs) and day_i + 1 < len(order):
                    # Les fleches traversent les jours (2026-10-23) : au bout de la
                    # journee, on continue dans la precedente -- tout le journal se
                    # parcourt aux fleches, et ce qui est ecoute n'est plus "nouveau".
                    player.stop()
                    day_i, rec_i = day_i + 1, 0
                    voix.say_blocking(announce_day())
                    play(rec_i)
                elif key == "\x1b[A" and new_i < 0 and rec_i >= 0 and day_i > 0:
                    player.stop()
                    day_i = day_i - 1
                    rec_i = len(days[order[day_i]]) - 1
                    voix.say_blocking(announce_day())
                    play(rec_i)
                else:
                    bound = dire("nav.debut_journee" if new_i < 0 else "nav.fin_journee")
                    if player.playing() or rec_i < 0 or recs[rec_i][2] is not None:
                        player.stop()
                        text = bound
                    else:
                        # Rien en lecture contre la borne : on relance l'enregistrement
                        # selectionne (sinon on croit l'avoir ecoute alors que non).
                        voix.say_blocking(bound)
                        play(rec_i)
            elif rec_i < 0 and key in ("\r", "\n", "\x1b[C", "\x1b[D", "n", "N", "v", "V", "c", "C", "b", "B", "i", "I", "e", "E"):
                text = dire("journal.choisir")
            elif key in ("\r", "\n"):
                play(rec_i)
            elif key in ("i", "I"):
                text = info_text(recs[rec_i][1]) if recs[rec_i][1] else dire("journal.releve")
            elif key in ("b", "B"):
                brut = raw_path(recs[rec_i][1])
                if brut:
                    player.stop()
                    voix.say_blocking(dire("journal.son_brut"))
                    player.play(brut)
                    try:  # le son brut ecoute compte aussi (commande "notifications")
                        import journal_vus
                        journal_vus.mark(recs[rec_i][1])
                    except Exception as exc:
                        print(f"[journal] ecoute non notee : {exc!r}")
                else:
                    text = dire("journal.pas_son_brut")
            elif recs[rec_i][2] is not None and key in ("\x1b[D", "n", "N", "v", "V", "c", "C"):
                text = dire("journal.releve_non_notable")
            elif key in ("\x1b[C", "\x1b[D"):
                player.stop()
                stamp, path, spoken = recs[rec_i]
                if key == "\x1b[C":
                    text = _keep(stamp, path, spoken, category)
                else:
                    text = _reject(path)
                    del recs[rec_i]
                    if not recs:
                        del days[order[day_i]]
                        order.pop(day_i)
                        if not order:
                            text += " " + dire("journal.plus_rien")
                            say(text)
                            break
                        day_i %= len(order)
                        rec_i = -1
                        text += " " + dire("journal.journee_vide") + " " + announce_day()
                    else:
                        # Le suivant prend la place du rejete : on le joue tout de
                        # suite, sinon fleche bas le sauterait.
                        rec_i = min(rec_i, len(recs) - 1)
                        voix.say_blocking(text)
                        text = None
                        play(rec_i)
            elif key in ("v", "V", "c", "C"):
                # La lecture continue : on valide sans interrompre l'ecoute ;
                # C : bonne prise mais coupee trop tot (2026-10-14).
                stamp, path, _ = recs[rec_i]
                text = notation.validate(path, stamp, cut=key in ("c", "C"))
            elif key in ("n", "N"):
                player.stop()
                stamp, path, _ = recs[rec_i]
                text = notation.rate(key_source, say, out, path, stamp)
            elif key in ("e", "E"):
                # Envoi a l'examen IA du Mac mini (en differe) : il reprendra le
                # brut, tentera plusieurs nettoyages, et deposera une proposition.
                import examen
                path = recs[rec_i][1]
                text = (dire("journal.examen_demande") if examen.demander(os.path.basename(path))
                        else dire("journal.examen_deja"))
            elif conseils.is_help(key):
                text = consigne
            else:
                text = dire("journal.aide_courte")
            if text:
                out(f"[{text}]\n")
                say(text)
    finally:
        player.close()
        _journal_opened(False)
    return dire("journal.ferme", nom=name)
