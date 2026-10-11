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
import time

import audio_lock
import octoprint as op
from phrases import dire, nombre
from speak import speak_cloud

POLL_SEC = 5
REACHED_MARGIN_C = 3
MILESTONES = (25, 50, 75)
IDLE_HEAT_REMINDER_SEC = 15 * 60
ERROR_STATES = ("Error", "Offline after error")


def _speak(text):
    print(f"[imprimante_watch] annonce : {text}")
    lock = audio_lock.acquire()
    try:
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

    def cycle(self):
        try:
            e = op.etat()
        except op.Erreur as exc:
            if exc.code != "injoignable":
                print(f"[imprimante_watch] OctoPrint : {exc}")
            return  # OctoPrint redemarre ou cle absente : on reessaie au passage suivant
        prev, self.prev = self.prev, e
        if prev is None:
            self._remember_reached(e)
            return
        self._connection(prev, e)
        if not e["connecte"]:
            self.reached.clear()
            self.heat_since = None
            return
        self._job(prev, e)
        self._temperatures(e)
        self._idle_heat(e)

    def _connection(self, prev, e):
        if prev["connecte"] and not e["connecte"] and e["texte"].startswith(ERROR_STATES):
            _speak(dire("imprimante.evt_erreur", erreur=e["texte"]))
        elif e["connecte"] and e["erreur"] and e["erreur"] != prev.get("erreur"):
            _speak(dire("imprimante.evt_erreur", erreur=e["erreur"]))

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
                   (" " + dire("imprimante.evt_estimee", duree=_duree(estimated)) if estimated else ""))
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
                       (" " + dire("imprimante.reste", duree=_duree(left)) if left else ""))
        # "resuming" : entre la pause et la reprise, printing peut etre faux un instant.
        active = ("printing", "paused", "pausing", "resuming", "cancelling", "finishing")
        if any(was.get(f) for f in active) and not any(now.get(f) for f in active):
            if self.cancelling:
                _speak(dire("imprimante.evt_annulee", fichier=name))
            else:
                _speak(dire("imprimante.evt_terminee", fichier=name,
                            duree=_duree(progress.get("printTime") or 0)))
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
                _speak(dire("imprimante.evt_temperature_atteinte", nom=dire(label), cible=round(target)))

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
                        buse=round(e["buse"][1] or 0), plateau=round(e["plateau"][1] or 0)))


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
