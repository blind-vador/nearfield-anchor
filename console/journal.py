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
  fleche droite            garde le rendu entendu : copie dans le dossier du
                           jour des archives (lib/journaux.py)
  fleche gauche            rejette le rendu en cours : ce decodeur descend sur
                           cette frequence et retient son empreinte de bruit,
                           pour lui seul et cette frequence seule. S'il reste
                           d'autres rendus, le suivant est lu.
                           Un message DAB+ rejete interdit sa station.
  J                        proposition suivante d'un autre juge, meme prise
  I                        informations : frequence, mode, et pour la CB et
                           le 10 m le verdict "vraie emission ou artefact"
  B                        ecoute le son brut, avant nettoyage (journal
                           radio : <nom>.brut.mp3, garde 48 h comme le reste)
  R                        renvoie le flux non decode a tous les juges
                           (<nom>.flux.wav, sur le NAS, 48 h)
  Ctrl+R                   le meme flux, a un seul decodeur choisi.
                           Sa sortie est gardee, meme si elle semble mauvaise.
  Entree                   rejoue          Echap : ferme

Sources : server/radio_log/ (48 dernieres heures, date dans le nom) et
ses dossiers d'archives par jour ("jeudi_3_septembre_2026/", heure seule
dans le nom).

Le clavier n'attend jamais (2026-11-06) : une phrase, l'annonce d'un rendu et
le son suivant s'enchainent dans un fil a part, et toute touche de navigation
annule ce qui etait en route. Avant, ignorer un rendu figeait tout jusqu'a 20 s.

UNE seule sortie son pour tout le dialogue (aplay ouvert en continu,
alimente par lib/decodage.py) : passer vite d'un enregistrement a l'autre ne
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
import chemins
import hashlib
import threading
import time


import decodage  # noqa: E402
import journaux  # noqa: E402
import tmp_ram  # noqa: E402
from phrases import Etiquettes, dire, nombre  # noqa: E402
import keys  # noqa: E402
import noise_signatures as ns  # noqa: E402
import notation  # noqa: E402
import propositions  # noqa: E402
import conseils  # noqa: E402
import voix  # noqa: E402

LOG_DIR = "/media/nas/nd1/server/radio_log"
UPDATE_FLAG = "/tmp/journal_maj"  # touche par radio_log a chaque captation enregistree
LOCK_WAIT_SEC = 10
RATE = 48000
AVANCE_MAX_SEC = 0.35  # un stop s'entend tout de suite : pas de son d'avance dans le tuyau

JOURS, MOIS = journaux.JOURS, journaux.MOIS
_DAY = r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)_(\d{1,2})_([a-zéû]+)_(\d{4})"
_TIME = r"(\d{2})h(\d{2})m(\d{2})s\.mp3$"
RE_ROOT_FILE = re.compile("_" + _DAY + "_" + _TIME)
RE_DAY_DIR = re.compile("^" + _DAY + "$")
RE_TIME_ONLY = re.compile("_" + _TIME)

# Phrases dites : lib/phrases.py (fichier phrases/<langue>.ini, personnalite du jour).

MODES = Etiquettes("mode", {"nfm": "FM étroite", "wfm": "FM large", "am": "AM", "usb": "BLU haute",
                             "lsb": "BLU basse"}, "mode de réception radio")


def _date(day, month_name, year):
    return journaux.date_de(day, month_name, year)


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


class Player:
    """aplay ouvert en continu ; chaque lecture = un decodeur qui l'alimente."""

    def __init__(self):
        self._aplay = self._open_aplay()
        self._flux = None
        self._thread = None
        self.gain = 1.0  # volume de lecture (dialogue "archives" : fleches gauche/droite)
        self._generation = 0
        self._guard = threading.Lock()
        self._sortie = threading.Lock()
        self._ticket = None  # sortie son prise seulement pendant une ecoute

    @staticmethod
    def _open_aplay():
        return subprocess.Popen(
            ["aplay", "-q", "-D", "default", "-r", str(RATE), "-f", "S16_LE", "-c", "1", "-B", "300000"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _aplay_ok(self):
        """Relance la sortie son si elle est tombee (2026-10-19 : un aplay mort
        rendait tout le journal muet, sans aucun message)."""
        with self._sortie:
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

    def play(self, path, avant=(), apres=()):
        """avant, apres : sons joues autour de path, sans modifier le fichier."""
        self.stop()
        self._aplay_ok()
        self._hold()
        self._thread = threading.Thread(target=self._relay, args=([*avant, path, *apres], self._generation),
                                        daemon=True)
        self._thread.start()

    def playing(self):
        """Vrai tant que l'enregistrement est en cours de lecture."""
        return self._thread is not None and self._thread.is_alive()

    def _relay(self, fichiers, generation):
        debut, envoye = time.time(), 0
        for fichier in fichiers:
            try:
                flux = decodage.Flux(fichier, RATE)
            except OSError as exc:
                print(f"[journal] lecture impossible : {exc!r}", file=sys.stderr)
                continue
            try:
                with self._guard:
                    if generation != self._generation:
                        return
                    self._flux = flux
                while True:
                    data = flux.lire(RATE // 5 * 2)  # 200 ms
                    if not data:
                        break
                    if self.gain != 1.0:
                        data = audioop.mul(data, 2, self.gain)  # ecretage automatique au-dela du maximum
                    with self._guard:
                        if generation != self._generation:
                            return
                    # Hors du verrou : une carte son bloquee ne fige pas le clavier.
                    if not self._ecrire(data):
                        return
                    envoye += len(data)
                    avance = envoye / (2 * RATE) - (time.time() - debut)
                    if avance > AVANCE_MAX_SEC:
                        time.sleep(avance - AVANCE_MAX_SEC)
            finally:
                flux.fermer()
        with self._guard:
            if generation == self._generation:
                self._flux = None
                self._let_go()  # fin de l'ecoute : les annonces peuvent passer

    def _ecrire(self, data):
        for essai in (1, 2):
            try:
                self._aplay.stdin.write(data)
                self._aplay.stdin.flush()
                return True
            except (BrokenPipeError, ValueError, OSError):
                if essai == 2:
                    with self._guard:
                        self._let_go()
                    return False
                self._aplay_ok()  # sortie tombee en cours de lecture : relancee, on reprend
        return False

    def stop(self):
        with self._guard:
            self._generation += 1
            self._let_go()
            flux, self._flux = self._flux, None
            self._thread = None
        if flux is not None:
            flux.arreter()

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
    """Copie le rendu entendu dans le dossier du jour de ce journal, cree au
    premier archivage ; un releve lu (capteurs) y est garde en texte, relu par
    "archives capteurs"."""
    if not os.path.isdir(journaux.KEEP_DIR):
        return dire("archives.disque_absent")
    journaux.ranger_archives()
    folder = journaux.dossier_du_jour(stamp.date())
    os.makedirs(folder, exist_ok=True)
    detail = ""
    source = path
    if text is None:
        source = propositions.fichier_courant(path)
        if source != path:  # un autre juge que la prise : gardes l'un a cote de l'autre
            detail = propositions.nom((propositions.rendu_courant(path) or {}).get("juge"))
    nom = journaux.nom_garde(stamp, category, detail)
    dest = os.path.join(folder, nom + (".txt" if text is not None else ".mp3"))
    if os.path.exists(dest):
        return dire("journal.deja_garde")
    if text is not None:
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        shutil.copy2(source, dest)
    return dire("journal.garde")


def info_text(path):
    """Touche I : ce que le systeme sait de cet enregistrement."""
    info = ns.capture_info(path) or {}
    if not info.get("freq_hz"):
        fiche = _message_card(path)
        if fiche:
            parts = [dire("journal.info_protocole", protocole=fiche["protocole"])] if fiche.get("protocole") else []
            if fiche.get("type_message") and fiche["type_message"] != fiche.get("protocole"):
                parts.append(fiche["type_message"] + ".")
            if fiche.get("source"):
                parts.append(dire("journal.info_source", source=fiche["source"]))
            if fiche.get("frequence_hz"):
                parts.append(dire("journal.info_frequence", frequence=f"{fiche['frequence_hz'] / 1e6:.4f}".rstrip("0").replace(".", ",")))
            if fiche.get("type_message"):
                import messages_types
                explication = fiche.get("explication") or messages_types.explication_connue(fiche["type_message"])
                if explication:
                    parts.append(explication)
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


def _annonce_gardee(ceremonie, phrase):
    """Annonce d'un rendu annexe : calculee une fois (3 a 6 s), puis gardee sur disque."""
    import annonce_morceaux as morceaux
    if not phrase:
        return None
    dossier = chemins.base("voix_cache/ceremonies_journal")
    chemin = os.path.join(dossier, hashlib.sha1(phrase.encode("utf-8")).hexdigest() + ".wav")
    if os.path.exists(chemin):
        return chemin
    # La voix des annonces du scanner : le rendu annexe sonne comme la prise.
    faite = ceremonie._fichier_annonce(phrase, source="radio")
    if not faite:
        return None
    if os.path.basename(faite).startswith(morceaux.PREFIXE_DIRECT):
        return faite  # dernier recours : pour cette ecoute, jamais garde pour la suite
    os.makedirs(dossier, exist_ok=True)
    shutil.copyfile(faite, chemin)
    ceremonie.jeter(faite)
    return chemin


def _reject(path, rendu=None):
    """rendu : dernier rendu d'une fiche, deja note et appris par propositions.rejeter ;
    l'empreinte de la capture est celle du premier juge, elle ne doit pas prendre sa place."""
    info = ns.capture_info(path)
    fiche = _message_card(path)
    # Deja partie (purge, autre rejet) : le rejet continue, il ne doit pas fermer le journal.
    for suffixe in (".mp3", ".brut.mp3", ".flux.wav") + ((".json",) if fiche is not None else ()):
        try:
            os.remove(path[:-4] + suffixe)
        except OSError:
            pass
    if fiche is not None:
        if fiche.get("protocole") == "DAB+":
            return _ban_dab_station(fiche)
        import messages_types
        type_appris = messages_types.rejeter(fiche)
        if type_appris:
            ns.forget_capture(path)
            return dire("journal.rejete_type", type=type_appris)
    ns.forget_capture(path)
    last = ns.last_capture()
    if last and last.get("log_path") == path:
        ns.mark_last_capture_noise()
    try:  # un parasite ne doit pas attirer l'attention du scanner
        import attention
        attention.stimulate((info or {}).get("target"), attention.PARASITE_POINTS)
    except Exception:
        pass
    if rendu is not None:
        if rendu.get("empreinte"):
            return dire("journal.rejete_appris", juge=propositions.nom(rendu.get("juge")))
        return dire("journal.rejete_supprime")
    if info and info.get("fingerprint"):
        juge = ns.juge_de(info)
        ns.add_signature(dict(info, juge=juge))
        return dire("journal.rejete_appris", juge=propositions.nom(juge))
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


def marquer_lu(category):
    """"journal messages lu" (2026-11-07) : tout ce journal est declare ecoute,
    ses notifications se taisent ; rien n'est supprime ni note."""
    import journal_vus
    name = journaux.NAMES[category]
    if not os.path.ismount("/media/nas/nd1"):
        return dire("journal.disque_absent", nom=name)
    nouveaux = [path for recs in scan_recordings(category).values() for _, path, text in recs
                if path and text is None and journal_vus.is_new(path)]
    if not nouveaux:
        return dire("journal.lu_rien", nom=name)
    journal_vus.mark_many(nouveaux)
    return dire("journal.lu_fait", nom=name,
                entrees=nombre(len(nouveaux), "entrée", "entrées"))


def notifications():
    if not os.path.ismount("/media/nas/nd1"):
        return dire("notifications.disque_absent")
    counts = unheard_counts()
    if not counts:
        return dire("notifications.rien")
    parts = [dire("notifications.journal", entrees=nombre(n, "nouvelle entrée", "nouvelles entrées"),
                  nom=journaux.NAMES[c]) for c, n in counts.items()]
    return dire("notifications.liste", liste=" ; ".join(parts))


DECODEURS = ("-fs", "-fm", "-fi", "-fn", "-f1", "-f2", "-fd", "-fy", "-fz",
             "-fx", "-fp", "-fh", "-fe", "-fa")


def _envoyer_decodeur(path, key_source):
    """Contrôle+R : on choisit un décodeur, le flux part chez lui seul."""
    import voix
    import propositions
    i = 0
    noms = [propositions.nom(d) for d in DECODEURS]
    voix.prechauffer(
        [dire("journal.decodeur_choisir", nom=n) for n in noms] + noms
        + [dire("journal.decodeur_aide"), dire("journal.decodeur_annule")],
        echo=False)
    voix.say_blocking(dire("journal.decodeur_choisir", nom=noms[0]))
    for key in key_source:
        if key in ("\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"):
            i = (i + (1 if key in ("\x1b[B", "\x1b[C") else -1)) % len(DECODEURS)
            voix.say_blocking(propositions.nom(DECODEURS[i]))
        elif key in ("\r", "\n"):
            return propositions.renvoyer_decodeur(path, DECODEURS[i])
        elif key == "\x1b":
            return dire("journal.decodeur_annule")
        else:
            voix.say_blocking(dire("journal.decodeur_aide"))
    return dire("journal.decodeur_annule")


def run(key_source, say, out, category="radio"):
    name = journaux.NAMES[category]
    if not os.path.ismount("/media/nas/nd1"):
        return dire("journal.disque_absent", nom=name)
    days = scan_recordings(category)
    if not days:
        return dire("journal.vide", nom=name)
    title = dire("journal.titre", nom=name)
    consigne = dire("journal.consigne_capteurs" if category == "capteurs" else "journal.consigne")
    import voix
    voix.prechauffer([
        title, consigne, dire("journal.plus_de_non_notee"), dire("journal.choisir"),
        dire("journal.releve"), dire("journal.son_brut"), dire("journal.pas_son_brut"),
        dire("journal.releve_non_notable"), dire("journal.plus_rien"),
        dire("nav.premier_jour"), dire("nav.dernier_jour"),
        dire("nav.debut_journee"), dire("nav.fin_journee"),
    ])
    order = sorted(days, reverse=True)  # du plus recent au plus ancien
    # rec_i = -1 : "avant le premier" -- la premiere fleche bas joue le 1er enregistrement.
    day_i, rec_i = 0, -1
    _journal_opened(True)
    player = Player()
    seen_update = _update_stamp()
    def habillage(path):
        """Sons autour d'un rendu annexe : la meme ceremonie que les autres prises."""
        rendu = propositions.habillage_courant(path)
        if not rendu:
            return (), ()
        import ceremonie_radio as ceremonie
        freq, bande, juge = rendu
        try:
            import sonneries
            bande_id = sonneries.bande_pour(freq) if freq else None
            arrivee, ouverture = ceremonie._fichiers(bande_id, None)
            annonce = _annonce_gardee(ceremonie, ceremonie.texte(bande, ceremonie.modulation_dite(juge)))
            milieu = [annonce] if annonce else []
            return (arrivee, *milieu, ouverture), (ceremonie.son_fin(freq, bande_id),)
        except Exception as exc:
            print(f"[journal] ceremonie du rendu : {exc!r}", file=sys.stderr)
            return (), ()

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

    suite_lock = threading.Lock()
    suite = {"tour": 0, "fil": None}
    explications = {"en_cours": set()}

    def expliquer(path):
        """Touche E des journaux messages et donnees : l'IA explique le message.
        Rend la main tout de suite ; la reponse est dite si l'on n'a pas
        bouge depuis (elle coupe alors la lecture du message), sinon E la
        redit aussitot."""
        import explication_messages as em
        fiche = em.fiche_de(path)
        if not fiche:
            return dire("journal.explication_sans_fiche")
        if em.deja(fiche):
            return em.deja(fiche)
        if path in explications["en_cours"]:
            return dire("journal.explication_en_cours")
        explications["en_cours"].add(path)
        tour = suite["tour"]

        def fil():
            try:
                texte, raison = em.expliquer(category, path)
            except Exception as exc:
                print(f"[journal] explication : {exc!r}", file=sys.stderr)
                texte, raison = None, None
            finally:
                explications["en_cours"].discard(path)
            phrase = texte or (raison if raison and raison not in ("ia", "fiche") else dire("journal.explication_echec"))
            with suite_lock:
                ici = (suite["tour"] == tour and order and 0 <= rec_i < len(days.get(order[day_i], ()))
                       and days[order[day_i]][rec_i][1] == path)
                if ici:
                    player.stop()
            if ici:
                out(f"[{phrase}]\n")
                say(phrase)
        threading.Thread(target=fil, daemon=True).start()
        return dire("journal.explication_demandee")

    def couper():
        """Toute touche de navigation : le son, la phrase et ce qui etait en route."""
        with suite_lock:
            suite["tour"] += 1
            player.stop()
        voix.stop()

    def en_route():
        return player.playing() or (suite["fil"] is not None and suite["fil"].is_alive())

    def enchainer(tour, phrase, path, brut):
        """Dans son fil : la phrase, l'habillage du rendu, puis le son."""
        def encore():
            return suite["tour"] == tour
        try:
            if phrase:
                voix.say_blocking(phrase, encore=encore)
            if not encore():
                return
            avant, apres = ((), ()) if brut else habillage(path)
            fichier = brut or propositions.fichier_courant(path)
            with suite_lock:
                if not encore():
                    return
                player.play(fichier, avant, apres)
            out(f"[{os.path.basename(path)}]\n")
            import journal_vus
            journal_vus.mark(path)  # n'est plus nouveau (commande "notifications")
        except Exception as exc:
            print(f"[journal] lecture : {exc!r}", file=sys.stderr)

    def play(i, phrase=None, brut=None):
        """Joue l'enregistrement, ou lit le releve (journal capteurs). Rend la
        main tout de suite : phrase, annonce et son partent dans un fil."""
        _, path, text = days[order[day_i]][i]
        couper()
        if text is not None:
            say(" ".join(p for p in (phrase, text) if p))
            out(f"[{text}]\n")
            return
        fil = threading.Thread(target=enchainer, args=(suite["tour"], phrase, path, brut), daemon=True)
        suite["fil"] = fil
        fil.start()

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
                couper()
                day_i = (day_i + (1 if key == "\t" else -1)) % len(order)
                rec_i = -1
                text = announce_day()
            elif key == keys.ALT_UP or (key in ("\x1b[A", "\x1b[B") and alt_held()):
                # Alt + fleche : prochaine (ou precedente) entree pas encore notee.
                found = next_unrated(1 if key == "\x1b[B" else -1)
                if found is None:
                    text = dire("journal.plus_de_non_notee")
                else:
                    phrase = None
                    if found[0] != day_i:
                        day_i = found[0]
                        phrase = announce_day()
                    rec_i = found[1]
                    play(rec_i, phrase)
            elif key in ("\x1b[A", "\x1b[B") and ctrl_held():
                # Ctrl + fleche : premier ou dernier du jour, joue aussitot.
                rec_i = 0 if key == "\x1b[A" else len(recs) - 1
                play(rec_i, dire("nav.premier_jour" if key == "\x1b[A" else "nav.dernier_jour"))
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
                    day_i, rec_i = day_i + 1, 0
                    play(rec_i, announce_day())
                elif key == "\x1b[A" and new_i < 0 and rec_i >= 0 and day_i > 0:
                    day_i = day_i - 1
                    rec_i = len(days[order[day_i]]) - 1
                    play(rec_i, announce_day())
                else:
                    bound = dire("nav.debut_journee" if new_i < 0 else "nav.fin_journee")
                    if en_route() or rec_i < 0 or recs[rec_i][2] is not None:
                        couper()
                        text = bound
                    else:
                        # Rien en lecture contre la borne : on relance l'enregistrement
                        # selectionne (sinon on croit l'avoir ecoute alors que non).
                        play(rec_i, bound)
            elif rec_i < 0 and key in ("\r", "\n", "\x1b[C", "\x1b[D", "n", "N", "v", "V", "c", "C", "b", "B", "i", "I", "e", "E", "j", "J", "r", "R", "\x12"):
                text = dire("journal.choisir")
            elif key in ("\r", "\n"):
                play(rec_i)
            elif key in ("i", "I"):
                text = info_text(recs[rec_i][1]) if recs[rec_i][1] else dire("journal.releve")
            elif key in ("b", "B"):
                brut = raw_path(recs[rec_i][1])
                if brut:  # le son brut ecoute compte aussi (commande "notifications")
                    play(rec_i, dire("journal.son_brut"), brut=brut)
                else:
                    text = dire("journal.pas_son_brut")
            elif recs[rec_i][2] is not None and key in ("\x1b[D", "n", "N", "v", "V", "c", "C"):
                text = dire("journal.releve_non_notable")
            elif key in ("\x1b[C", "\x1b[D"):
                couper()  # le son s'arrete d'abord : la touche s'entend tout de suite
                stamp, path, spoken = recs[rec_i]
                if key == "\x1b[C":
                    text = _keep(stamp, path, spoken, category)
                else:
                    fiche = propositions.a_fiche(path)
                    rendu = propositions.rendu_courant(path)
                    reste = propositions.rejeter(path)
                    if reste:
                        # D'autres juges ont un rendu : on le lit, la prise reste.
                        play(rec_i, reste)
                    else:
                        if not fiche:
                            propositions.noter_rejet_simple(path)
                        text = _reject(path, rendu=rendu)
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
                            play(rec_i, text)
                            text = None
            elif key in ("j", "J"):
                annonce = propositions.avancer(recs[rec_i][1])
                if not annonce:
                    text = dire("journal.une_seule")
                else:
                    play(rec_i, annonce)
            elif key in ("v", "V", "c", "C"):
                # La lecture continue : on valide sans interrompre l'ecoute ;
                # C : bonne prise mais coupee trop tot (2026-10-14).
                stamp, path, _ = recs[rec_i]
                text = notation.validate(path, stamp, cut=key in ("c", "C"))
            elif key in ("n", "N"):
                couper()
                stamp, path, _ = recs[rec_i]
                text = notation.rate(key_source, say, out, path, stamp)
            elif key == "\x12" or (key in ("r", "R") and ctrl_held()):
                text = _envoyer_decodeur(recs[rec_i][1], key_source)
            elif key in ("r", "R"):
                text = propositions.renvoyer_flux(recs[rec_i][1])
            elif key in ("e", "E") and category in ("messages", "donnees"):
                text = expliquer(recs[rec_i][1])
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
        couper()
        player.close()
        _journal_opened(False)
    return dire("journal.ferme", nom=name)
