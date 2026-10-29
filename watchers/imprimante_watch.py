"""Annonces vocales des evenements de l'imprimante 3D (2026-10-11).

Complement du pilotage vocal (console/imprimante_cmd.py) : tout ce qui se
passe sur l'imprimante est dit, que la console soit ouverte ou non, que
l'ordre vienne du Pi, de l'interface web d'OctoPrint ou du panneau de
l'imprimante :
  - temperature cible atteinte (buse, plateau) ;
  - impression lancee (fichier, duree estimee), a 25 / 50 / 75 %,
    en pause, reprise, terminee (duree reelle), annulee ;
  - erreur de l'imprimante (liaison perdue, arret du firmware...) ;
  - chauffe allumee sans impression : rappel toutes les
    IDLE_HEAT_REMINDER_SEC (une buse oubliee a 200 degres).
La reconnexion apres rebranchement reste a usb_watch.py.
Sondage REST toutes les POLL_SEC : OctoPrint ne pousse ses evenements que
par websocket, et 5 s suffisent pour une voix.
"""
import chemins
import os
import re
import time

import audio_lock
import octoprint as op
from phrases import dire, nombre
from speak import speak_cloud
import sons_imprimante as sons

POLL_SEC = 5
SERIAL_LOG = chemins.home(".octoprint/logs/serial.log")
# Messages Marlin (runout / M600) : host actions desactivees sur ce firmware,
# donc OctoPrint reste en "Printing" alors que la machine attend le fil.
FILAMENT_MARKERS = (
    ("insert filament", "manque"),
    ("press button to heat nozzle", "bouton"),
    ("too long extrusion prevented", "extrusion"),
    ("filament runout", "manque"),
)
FILAMENT_COOLDOWN_SEC = 90
# Secondes sans busy pause/processing avant d'annoncer "c'est bon"
FILAMENT_CLEAR_SEC = 18
AUTO_M108_SEC = 10  # simule le bouton sans intervention
ECHO_VOCAL_COOLDOWN = 8
REACHED_MARGIN_C = 3
MILESTONES = (25, 50, 75)
IDLE_HEAT_REMINDER_SEC = 15 * 60
ERROR_STATES = ("Error", "Offline after error")
# Bruitages (lib/sons_imprimante.py, 2026-10-18)
WARN, FINI, PROGRES = "printer_warning", "printer_job_finished", "printer_progression_notification"
CHAUFFE_BOUCLE = "printer_waiting_temperature_loop"
CHAUFFE_BOUCLE_MAX_SEC = 20 * 60


def _speak(text, son=None):
    """son : bruitage d'Epidemic Sound joue avant la phrase (lib/sons_imprimante.py)."""
    print(f"[imprimante_watch] annonce : {text}")
    lock = audio_lock.acquire()
    try:
        if son and not lock.dropped:
            sons.jouer(son, attendre=True)
        speak_cloud(text)
    except Exception as exc:
        print(f"[imprimante_watch] voix indisponible : {exc}")
    finally:
        audio_lock.release(lock)


def _duree(seconds):
    h, m = divmod(int(seconds or 0) // 60, 60)
    if h:
        return dire("imprimante.duree_h", heures=nombre(h, "heure"), m=m)
    return dire("imprimante.duree_m", minutes=nombre(max(m, 1), "minute"))


def _file_name(job):
    f = (job.get("job") or {}).get("file") or {}
    return f.get("display") or f.get("name") or "?"


class Watch:
    def __init__(self):
        self.prev = None          # etat precedent (None : premier passage, on ne dit rien)
        self.reached = {}         # "buse"/"plateau" -> cible deja annoncee comme atteinte
        self.milestone = 0
        self.cancelling = False
        self.heat_since = None
        self.last_reminder = 0.0
        self._serial_pos = 0
        self._serial_inode = None
        self._filament_announced_at = 0.0
        self._filament_paused = False
        self._filament_pause_since = 0.0
        self._filament_phase = None  # attente|action|ok
        self._filament_clear_since = 0.0
        self._filament_ok_announced = False
        self._filament_action_at = 0.0
        self._filament_boucle_at = 0.0
        self._auto_m108_at = 0.0
        self._echo_pos = 0
        self._echo_inode = None
        self._echo_last = {}
        self._ecran_texte = ""
        self._ecran_annonce_at = 0.0
        self._chauffe_jeton = None   # boucle "en chauffe" en cours
        self._chauffe_depuis = 0.0

    def cycle(self):
        try:
            e = op.etat()
        except op.Erreur as exc:
            if exc.code != "injoignable":
                print(f"[imprimante_watch] OctoPrint : {exc}")
            # Serial filament / echo meme si REST foire
            self._filament_serial()
            self._marlin_echo_vocal()
            return
        prev, self.prev = self.prev, e
        if prev is None:
            self._remember_reached(e)
            self._filament_serial()
            self._marlin_echo_vocal()
            return
        self._connection(prev, e)
        # Toujours suivre le serial : pause Marlin meme si OctoPrint "Connecting"
        self._filament_serial()
        self._marlin_echo_vocal()
        if not e["connecte"]:
            self.reached.clear()
            self.heat_since = None
            try:  # dernier ecran d'une imprimante qui n'est plus la : perime
                os.remove("/tmp/imprimante_ecran.json")
            except OSError:
                pass
            if self._chauffe_jeton:
                sons.arreter_boucle(self._chauffe_jeton)
                self._chauffe_jeton, self._chauffe_depuis = None, 0.0
            return
        self._job(prev, e)
        self._temperatures(e)
        self._idle_heat(e)
        self._ecran_virtuel(e)

    def _connection(self, prev, e):
        if prev["connecte"] and not e["connecte"] and e["texte"].startswith(ERROR_STATES):
            _speak(dire("imprimante.evt_erreur", erreur=op.liaison_en_clair(e["texte"])), WARN)
        elif e["connecte"] and e["erreur"] and e["erreur"] != prev.get("erreur"):
            _speak(dire("imprimante.evt_erreur", erreur=op.message_en_clair(e["erreur"])), WARN)

    def _job(self, prev, e):
        was, now = prev.get("flags") or {}, e["flags"]
        job, progress = e["job"], (e["job"].get("progress") or {})
        name = _file_name(job)
        if now.get("cancelling"):
            self.cancelling = True
        if now.get("printing") and not (was.get("printing") or was.get("paused") or was.get("resuming")):
            self.milestone, self.cancelling = 0, False
            estimated = (job.get("job") or {}).get("estimatedPrintTime")
            _speak(dire("imprimante.evt_lancee", fichier=name) +
                   (" " + dire("imprimante.evt_estimee", duree=_duree(estimated)) if estimated else ""), PROGRES)
        elif now.get("paused") and not was.get("paused"):
            _speak(dire("imprimante.evt_pause", fichier=name))
        elif now.get("printing") and (was.get("paused") or was.get("resuming")):
            _speak(dire("imprimante.evt_reprise"))
        if now.get("printing"):
            done = progress.get("completion") or 0
            passed = [m for m in MILESTONES if m > self.milestone and done >= m]
            if passed:
                self.milestone = passed[-1]
                left = progress.get("printTimeLeft")
                _speak(dire("imprimante.evt_progression", pourcent=self.milestone) +
                       (" " + dire("imprimante.reste", duree=_duree(left)) if left else ""), PROGRES)
        # "resuming" : entre la pause et la reprise, printing peut etre faux un instant.
        active = ("printing", "paused", "pausing", "resuming", "cancelling", "finishing")
        if any(was.get(f) for f in active) and not any(now.get(f) for f in active):
            if self.cancelling:
                _speak(dire("imprimante.evt_annulee", fichier=name), WARN)
            else:
                _speak(dire("imprimante.evt_terminee", fichier=name,
                            duree=_duree(progress.get("printTime") or 0)), FINI)
            self.cancelling = False
            self.milestone = 0

    def _remember_reached(self, e):
        """Au demarrage de la veille : ce qui est deja chaud n'est pas annonce."""
        if not e["connecte"]:
            return
        for key in ("buse", "plateau"):
            actual, target = e[key]
            if target and actual is not None and abs(actual - target) <= REACHED_MARGIN_C:
                self.reached[key] = target

    def _temperatures(self, e):
        for key, label in (("buse", "imprimante.buse"), ("plateau", "imprimante.plateau")):
            actual, target = e[key]
            if not target:
                self.reached.pop(key, None)
                continue
            if self.reached.get(key) == target or actual is None:
                continue
            if abs(actual - target) <= REACHED_MARGIN_C:
                self.reached[key] = target
                _speak(dire("imprimante.evt_temperature_atteinte", nom=dire(label), cible=round(target)), PROGRES)
        self._boucle_chauffe(e)

    def _boucle_chauffe(self, e):
        """Boucle "en chauffe" tant qu'une temperature demandee n'est pas
        atteinte (au plus CHAUFFE_BOUCLE_MAX_SEC)."""
        attente = any(e[k][1] and e[k][0] is not None and self.reached.get(k) != e[k][1]
                      and abs(e[k][0] - e[k][1]) > REACHED_MARGIN_C for k in ("buse", "plateau"))
        now = time.time()
        if attente and self._chauffe_jeton is None and now - self._chauffe_depuis > CHAUFFE_BOUCLE_MAX_SEC:
            self._chauffe_jeton, self._chauffe_depuis = sons.demarrer_boucle(CHAUFFE_BOUCLE), now
        elif self._chauffe_jeton and (not attente or now - self._chauffe_depuis > CHAUFFE_BOUCLE_MAX_SEC):
            sons.arreter_boucle(self._chauffe_jeton)
            self._chauffe_jeton = None
            if not attente:
                self._chauffe_depuis = 0.0

    def _idle_heat(self, e):
        flags = e["flags"]
        heating = any(e[k][1] for k in ("buse", "plateau"))
        printing = any(flags.get(f) for f in ("printing", "paused", "pausing", "cancelling"))
        if not heating or printing:
            self.heat_since = None
            return
        now = time.time()
        if self.heat_since is None:
            self.heat_since, self.last_reminder = now, now
            return
        if now - self.last_reminder >= IDLE_HEAT_REMINDER_SEC:
            self.last_reminder = now
            minutes = round((now - self.heat_since) / 60)
            _speak(dire("imprimante.evt_chauffe_oubliee", minutes=minutes,
                        buse=round(e["buse"][1] or 0), plateau=round(e["plateau"][1] or 0)), WARN)


    def _filament_serial(self):
        """Dialogue filament Marlin = ecran Ender (ADVANCED_PAUSE).

        Suite typique :
          1. busy: paused for user + "Insert filament and press button"
          2. bouton → busy: processing (ejection puis chargement : tic / moteur)
          3. éventuellement re-pause (attendre le fil / un autre clic)
          4. reprise réelle quand plus aucun busy pause/processing

        On vocalise chaque étape. "C'est bon" seulement après FILAMENT_CLEAR_SEC
        sans busy (pas dès le premier trou pendant processing).
        """
        try:
            st = os.stat(SERIAL_LOG)
        except OSError:
            return
        inode = getattr(st, "st_ino", None)
        if inode != self._serial_inode:
            self._serial_inode = inode
            self._serial_pos = max(0, st.st_size - 8192)
        if st.st_size < self._serial_pos:
            self._serial_pos = 0
        chunk = ""
        if st.st_size != self._serial_pos:
            try:
                with open(SERIAL_LOG, "r", errors="replace") as f:
                    f.seek(self._serial_pos)
                    chunk = f.read()
                    self._serial_pos = f.tell()
            except OSError:
                return

        low = chunk.lower()
        now = time.time()
        marker_kind = None
        for needle, k in FILAMENT_MARKERS:
            if needle in low:
                marker_kind = k
                break

        try:
            from octoprint import etat_filament_serial
            etat = etat_filament_serial()
        except Exception:
            etat = {"phase": "attente" if "busy: paused for user" in low else (
                "action" if "busy: processing" in low else "inconnu"
            ), "kind": marker_kind, "erreur": True, "ok": False, "ecran": "inconnu"}

        phase = etat.get("phase") or "inconnu"
        prev_phase = self._filament_phase

        # Entrée / maintien du dialogue
        if phase in ("attente", "action"):
            if not self._filament_paused:
                self._filament_paused = True
                self._filament_pause_since = now
                self._filament_ok_announced = False
                self._filament_clear_since = 0.0
                if now - self._filament_announced_at >= 5:
                    self._filament_announced_at = now
                    if marker_kind == "extrusion" or etat.get("kind") == "extrusion":
                        _speak(dire("imprimante.evt_filament_extrusion_longue"), WARN)
                    elif marker_kind == "bouton" or etat.get("ecran") == "bouton_chauffe":
                        _speak(dire("imprimante.evt_filament_bouton"))
                    else:
                        _speak(dire("imprimante.evt_filament_ecran_attente"))
                    _speak(dire("imprimante.evt_filament_guide"))

            # Transitions vocales (écran)
            if phase != prev_phase and prev_phase is not None:
                if phase == "action":
                    self._filament_action_at = now
                    _speak(dire("imprimante.evt_filament_ecran_action"))
                    self._filament_announced_at = now
                elif phase == "attente" and prev_phase == "action":
                    # Charge puis retour pause rapide = capteur runout pas vu
                    duree = now - getattr(self, "_filament_action_at", 0)
                    if 0 < duree <= 90 and now - getattr(self, "_filament_boucle_at", 0) >= 60:
                        self._filament_boucle_at = now
                        _speak(dire("imprimante.evt_filament_boucle"), WARN)
                    else:
                        _speak(dire("imprimante.evt_filament_ecran_reattente"))
                    self._filament_announced_at = now
            elif marker_kind == "manque" and "insert filament" in low:
                if now - self._filament_announced_at >= 45:
                    self._filament_announced_at = now
                    _speak(dire("imprimante.evt_filament_ecran_attente"))

            self._filament_phase = phase
            self._filament_clear_since = 0.0
            self._publish_filament(True, etat.get("kind") or marker_kind or phase)
            # Sans bouton : M108 auto (force serie) + coupe runout
            # force_commande DECONNECTE OctoPrint : pendant une impression
            # envoyee par OctoPrint (fichier local), la deconnexion annule la
            # piece. Seulement hors impression ou pour une impression depuis
            # la carte SD (2026-10-14).
            if phase == "attente" and self._impression_hote():
                if now - self._filament_announced_at >= 90:
                    self._filament_announced_at = now
                    _speak(dire("imprimante.evt_filament_pause_active"), WARN)
            elif phase == "attente" and now - getattr(self, "_auto_m108_at", 0) >= AUTO_M108_SEC:
                self._auto_m108_at = now
                if now - self._filament_announced_at >= 8:
                    self._filament_announced_at = now
                    _speak(dire("imprimante.evt_filament_auto_bouton"))
                try:
                    op.force_commande("M412 S0", "M108", "M108")
                except Exception as exc:
                    print(f"[imprimante_watch] auto-M108 : {exc!r}")
            elif phase == "attente" and now - self._filament_announced_at >= 90:
                self._filament_announced_at = now
                _speak(dire("imprimante.evt_filament_pause_active"), WARN)
            return

        # Hors busy pause/processing
        if self._filament_paused:
            if self._filament_clear_since <= 0:
                self._filament_clear_since = now
            elapsed = now - self._filament_clear_since
            self._filament_phase = "clearing"
            self._publish_filament(True, "clearing")
            if elapsed < FILAMENT_CLEAR_SEC:
                return
            # Reprise stable
            self._filament_paused = False
            self._filament_phase = "ok"
            self._publish_filament(False)
            if not self._filament_ok_announced:
                self._filament_ok_announced = True
                self._filament_announced_at = now
                _speak(dire("imprimante.evt_filament_ok"), "printer_filament_in")
            return

        self._filament_phase = "ok"
        if time.time() - getattr(self, "_filament_pub_at", 0) >= 5:
            self._publish_filament(False)



    def _ecran_virtuel(self, e):
        """Reconstitue la ligne de statut type ecran LCD Marlin.

        Sans HOST_STATUS_NOTIFICATIONS, M117/status LCD ne remontent PAS
        sur le serial. On reconstruit l'equivalent a partir d'OctoPrint
        (etat, fichier, %, temperatures, phase filament) et on le vocalise
        a chaque changement significatif.
        """
        flags = e.get("flags") or {}
        job = e.get("job") or {}
        progress = job.get("progress") or {}
        name = _file_name(job) or ""
        pct = progress.get("completion")
        buse = e.get("buse") or (None, None)
        plateau = e.get("plateau") or (None, None)
        try:
            from octoprint import etat_filament_serial
            fil = etat_filament_serial()
        except Exception:
            fil = {"phase": "ok", "ecran": "ok"}

        # Ligne style ecran
        if fil.get("phase") == "attente":
            ligne = dire("imprimante.ecran_ligne_filament_attente")
        elif fil.get("phase") == "action":
            ligne = dire("imprimante.ecran_ligne_filament_action")
        elif flags.get("printing"):
            p = round(pct or 0)
            ligne = dire(
                "imprimante.ecran_ligne_impression",
                fichier=name or "?",
                pourcent=p,
                buse=round(buse[0] or 0),
                plateau=round(plateau[0] or 0),
            )
        elif flags.get("paused") or flags.get("pausing"):
            ligne = dire("imprimante.ecran_ligne_pause", fichier=name or "?")
        elif (buse[1] or 0) > 0 or (plateau[1] or 0) > 0:
            ligne = dire(
                "imprimante.ecran_ligne_chauffe",
                buse=round(buse[0] or 0),
                buse_cible=round(buse[1] or 0),
                plateau=round(plateau[0] or 0),
                plateau_cible=round(plateau[1] or 0),
            )
        else:
            ligne = dire(
                "imprimante.ecran_ligne_pret",
                buse=round(buse[0] or 0),
                plateau=round(plateau[0] or 0),
            )

        # Publier toujours (releve technique)
        try:
            import json
            path = "/tmp/imprimante_ecran.json"
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "texte": ligne,
                    "phase_filament": fil.get("phase"),
                    "etat": e.get("texte"),
                    "pourcent": pct,
                    "fichier": name,
                    "buse": buse,
                    "plateau": plateau,
                    "t": time.time(),
                }, f)
            os.replace(tmp, path)
        except OSError:
            pass

        # Vocaliser si la "page ecran" change (pas a chaque degre)
        cle = ligne
        # Pendant impression : n'annoncer que tous les 10% via milestones deja ;
        # ici on annonce changement d'ecran (pret↔chauffe↔print↔filament)
        ecran_id = (
            fil.get("phase"),
            "print" if flags.get("printing") else (
                "pause" if flags.get("paused") or flags.get("pausing") else (
                    "heat" if (buse[1] or 0) > 0 or (plateau[1] or 0) > 0 else "idle"
                )
            ),
            (round((pct or 0) / 25) * 25) if flags.get("printing") else None,
        )
        prev = getattr(self, "_ecran_id", None)
        now = time.time()
        if ecran_id != prev and now - self._ecran_annonce_at >= 6:
            self._ecran_id = ecran_id
            self._ecran_texte = ligne
            self._ecran_annonce_at = now
            # Eviter double avec evt_lancee / temperatures deja parles
            if prev is not None:
                _speak(dire("imprimante.ecran_affiche", texte=ligne))

    def _marlin_echo_vocal(self):
        """Vocalise tout message Marlin echo: destine a l'ecran / l'utilisateur.

        Le firmware n'a pas HOST_ACTION_COMMANDS : l'ecran LCD ne pousse pas
        ses menus vers le host. On lit donc chaque `echo:` utile du serial
        (insert filament, runout, SD, etc.) et on le dit en francais.
        """
        # Libelles connus (sous-chaines, minuscules)
        VOCAL = (
            ("insert filament and press button", "imprimante.echo_insert_filament"),
            ("press button to heat nozzle", "imprimante.echo_press_heat"),
            ("filament runout on", "imprimante.echo_runout_on"),
            ("filament runout off", "imprimante.echo_runout_off"),
            ("no sd card", "imprimante.echo_no_sd"),
            ("sd card ok", "imprimante.echo_sd_ok"),
            ("sd card released", "imprimante.echo_sd_released"),
            ("too long extrusion prevented", "imprimante.echo_extrusion"),
            ("printer halted", "imprimante.echo_halted"),
            ("settings stored", "imprimante.echo_settings_stored"),
            ("unknown command", None),  # silence : souvent nos probes M420 etc.
            ("z probe past bed", "imprimante.evt_probe_hors_plateau"),
            ("busy: paused for user", None),  # gere par _filament_serial
            ("busy: processing", None),
            ("busy:", None),
        )
        try:
            st = os.stat(SERIAL_LOG)
        except OSError:
            return
        inode = getattr(st, "st_ino", None)
        if inode != self._echo_inode:
            self._echo_inode = inode
            self._echo_pos = max(0, st.st_size - 4096)
        if st.st_size < self._echo_pos:
            self._echo_pos = 0
        if st.st_size == self._echo_pos:
            return
        try:
            with open(SERIAL_LOG, "r", errors="replace") as f:
                f.seek(self._echo_pos)
                chunk = f.read()
                self._echo_pos = f.tell()
        except OSError:
            return
        now = time.time()
        for line in chunk.splitlines():
            if "Recv:" not in line:
                continue
            payload = line.split("Recv:", 1)[-1].strip()
            low = payload.lower()
            # ignorer trafic bruit
            if low.startswith("t:") or low.startswith("ok t:") or low in ("ok",):
                continue
            if low.startswith("x:") and "count" in low:
                continue
            if low.startswith("cap:"):
                continue

            msg = payload
            ml = low
            if low.startswith("echo:"):
                msg = payload.split(":", 1)[-1].strip()
                ml = msg.lower()
            elif low.startswith("//"):
                msg = payload.lstrip("/").strip()
                ml = msg.lower()

            phrase = None
            skip = False
            # Releves des fins de course (M119) : demandes par le Pi lui-meme
            # (detection de la machine, calibrage qui les lit chaque seconde et
            # les dit en direct). Les dire ici doublait tout, en retard de 5 s.
            if ml.startswith(("filament:", "z_min:", "z_probe:", "x_min:", "y_min:")) \
                    or "reporting endstop status" in ml:
                continue
            # Reponses techniques aux questions du Pi (M851, M211, M115...)
            if re.match(r"(;|m\d+\b|min:|bed x:|cap:|reporting )", ml):
                continue
            else:
                for needle, key in VOCAL:
                    if needle in ml:
                        if key is None:
                            skip = True
                        else:
                            phrase = dire(key)
                        break
            if skip:
                continue
            if phrase is None:
                # Uniquement les messages "humains" (lettres), pas le bruit binaire
                if not re.search(r"[A-Za-z]{3,}", msg):
                    continue
                if any(ml.startswith(p) for p in ("send:", "wait", "w:", "count ")):
                    continue
                # Eviter de vocaliser chaque ok/firmware spam
                if ml.startswith("firmware_name") or ml.startswith("ok "):
                    continue
                phrase = op.message_en_clair(msg)
            last = self._echo_last.get(phrase, 0)
            if now - last < ECHO_VOCAL_COOLDOWN:
                continue
            self._echo_last[phrase] = now
            _speak(phrase)

    def _impression_hote(self):
        """True si OctoPrint envoie lui-meme une piece (fichier local)."""
        e = self.prev or {}
        flags = e.get("flags") or {}
        if not e.get("connecte") or not any(flags.get(f) for f in ("printing", "paused", "pausing")):
            return False
        origine = (((e.get("job") or {}).get("job") or {}).get("file") or {}).get("origin")
        return origine != "sdcard"

    def _filament_check_recovery(self):
        """Compat : la logique est dans _filament_serial."""
        self._filament_serial()

    def _publish_filament(self, en_pause, kind=None):
        path = "/tmp/imprimante_filament.json"
        self._filament_pub_at = time.time()
        try:
            import json
            try:
                from octoprint import etat_filament_serial
                etat = etat_filament_serial()
            except Exception:
                etat = {
                    "pause": en_pause,
                    "erreur": bool(en_pause),
                    "ok": not en_pause,
                    "kind": kind,
                    "phase": "attente" if en_pause else "ok",
                    "ecran": "inconnu",
                }
            payload = {
                "pause": bool(etat.get("pause", en_pause)),
                "processing": bool(etat.get("processing")),
                "phase": etat.get("phase") or ("attente" if en_pause else "ok"),
                "ecran": etat.get("ecran"),
                "erreur": bool(etat.get("erreur", en_pause)),
                "ok": bool(etat.get("ok", not en_pause)),
                "kind": etat.get("kind") or kind,
                "demande_fil": bool(etat.get("demande_fil")),
                "chauffe_bloquee": bool(etat.get("chauffe_bloquee")),
                "extrusion_refusee": bool(etat.get("extrusion_refusee")),
                "runout_firmware": bool(etat.get("runout_firmware")),
                "t": time.time(),
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except OSError:
            pass



def main():
    print("[imprimante_watch] demarrage des annonces de l'imprimante")
    watch = Watch()
    while True:
        try:
            watch.cycle()
        except Exception as exc:  # une reponse inattendue ne doit jamais arreter la veille
            print(f"[imprimante_watch] erreur pendant le passage : {exc!r}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
