"""Veille systeme (2026-10-23) : les evenements du Pi lui-meme, jusque-la
silencieux, chacun avec son son (lib/sons.py), et une phrase quand le son
seul ne suffit pas a agir.

  noyau (journalctl -k)  surintensite USB : alarme electrique en boucle avec
                         la voix ; appareil USB branche / debranche ; sous-tension
  toutes les 30 s        Pi bride par la chaleur ou en sous-tension (vcgencmd),
                         horloge non synchronisee, internet perdu / revenu (avec
                         la cause : wifi, box, internet derriere la box, DNS),
                         carte SD ou NAS presque pleins, veille qui redemarre en
                         boucle, copie des journaux arretee, camera arretee,
                         fin d'une mise de cote ("ignorer"), antenne muette
                         (plus aucune trame de capteur depuis 2 heures)

Le NAS perdu / retrouve reste a nas_watch (il le dit deja, avec ce son)."""
import os
import re
import shutil
import socket
import subprocess
import threading
import time

import alarmes
import audio_lock
import sons
from phrases import dire, nombre
from sound_player import play_sound_effect
from speak import speak_with_lead_sound

PERIODE_SEC = 30
ECHECS_RESEAU = 2               # verifications ratees de suite avant de dire la panne
HORLOGE_MAX_SEC = 15 * 60       # horloge non synchronisee plus longtemps : son
PLEIN_POURCENT = 90
PLEIN_RAPPEL_SEC = 24 * 3600
BOUCLE_FENETRE_SEC = 15 * 60
BOUCLE_REDEMARRAGES = 3
RAPPEL_SEC = 15 * 60            # veille en boucle : alarme redite de temps en temps
ALARME_ECART_SEC = 10 * 60      # une seule alarme electrique par episode
USB_REGROUPE_SEC = 2.0          # un hub qui revient = une rafale d'evenements, un seul son
CHALEUR_ECART_SEC = 30 * 60
DISQUES = [("/", "systeme_veille.carte_sd"), ("/media/nas/nd1", "systeme_veille.nas")]
VEILLES = ["radio-watch", "sncf-watch", "stas-watch", "meteo-watch", "crue-watch", "adsb-watch", "terrain-watch",
           "imprimante-watch", "usb-watch", "nas-watch", "compagnon-watch", "log-ship", "pi-console",
           "voix-preparation"]

_derniers = {}


def _log(texte):
    print(f"[veille_systeme] {texte}", flush=True)


def _une_fois(cle, ecart):
    now = time.time()
    if now - _derniers.get(cle, 0) < ecart:
        return False
    _derniers[cle] = now
    return True


def _jouer(evenement, texte=None):
    chemin = sons.chemin(evenement)
    _log(f"{evenement}" + (f" : {texte}" if texte else ""))
    lock = audio_lock.acquire()
    try:
        if texte:
            speak_with_lead_sound(chemin, texte, sound_timeout_sec=30, label="veille_systeme", source="systeme")
        elif chemin:
            play_sound_effect(chemin, timeout_sec=30, label="veille_systeme")
    finally:
        audio_lock.release(lock)


def _plus_tard(fonction, *args):
    threading.Thread(target=fonction, args=args, daemon=True).start()


# --- noyau --------------------------------------------------------------------

def _alarme_electrique(texte):
    alarmes.declencher("alarme_electrique", texte, source="systeme")  # en boucle jusqu'au "compris" (2026-10-24)


class _Usb:
    """Regroupe les rafales d'evenements USB : un son par sens."""

    def __init__(self):
        self._garde = threading.Lock()
        self._en_attente = set()
        self._minuteur = None

    def note(self, sens):
        with self._garde:
            self._en_attente.add(sens)
            if self._minuteur is None:
                self._minuteur = threading.Timer(USB_REGROUPE_SEC, self._vider)
                self._minuteur.daemon = True
                self._minuteur.start()

    def _vider(self):
        with self._garde:
            sens, self._en_attente, self._minuteur = self._en_attente, set(), None
        if "debranche" in sens:
            _jouer("peripherique_decroche")
        if "branche" in sens:
            _jouer("peripherique_branche")


def _noyau():
    usb = _Usb()
    while True:
        try:
            proc = subprocess.Popen(["journalctl", "-k", "-f", "-n", "0", "-o", "cat"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
            for ligne in proc.stdout:
                bas = ligne.lower()
                if "over-current" in bas or "overcurrent" in bas:
                    m = re.search(r"port (\d+)", ligne)
                    if _une_fois("surintensite", ALARME_ECART_SEC):
                        texte = dire("systeme_veille.surintensite_port", port=m.group(1)) if m else \
                            dire("systeme_veille.surintensite")
                        _plus_tard(_alarme_electrique, texte)
                elif "undervoltage" in bas or "under-voltage" in bas:
                    if _une_fois("sous_tension", CHALEUR_ECART_SEC):
                        _sous_tension()
                elif "new usb device found" in bas:
                    usb.note("branche")
                elif "usb disconnect" in bas:
                    usb.note("debranche")
        except Exception as exc:
            _log(f"journal du noyau : {exc!r}")
        time.sleep(10)


# --- verifications periodiques -----------------------------------------------

def _sortie(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _alimentation():
    m = re.search(r"0x([0-9a-f]+)", _sortie(["vcgencmd", "get_throttled"]))
    if not m:
        return
    bits = int(m.group(1), 16)
    if bits & 0x1 and _une_fois("sous_tension", CHALEUR_ECART_SEC):
        _sous_tension()
    if bits & 0xC and _une_fois("chaleur", CHALEUR_ECART_SEC):   # bride ou limite de temperature, maintenant
        # un seul tour, sonnerie et message : une alarme en boucle le ferait chauffer davantage (2026-10-24)
        alarmes.declencher("pi_bride_chaleur", dire("systeme_veille.chaleur_alarme"), source="systeme",
                           une_fois=True)


def _sous_tension():
    """Alarme en boucle jusqu'au "compris" (2026-10-24, "oui absolument")."""
    alarmes.declencher("sous_tension", dire("systeme_veille.sous_tension_alarme"), source="systeme")


class _Horloge:
    depuis = None
    dit = False

    def verifier(self):
        if _sortie(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]) != "no":
            self.depuis, self.dit = None, False
            return
        self.depuis = self.depuis or time.time()
        if not self.dit and time.time() - self.depuis >= HORLOGE_MAX_SEC:
            self.dit = True
            _jouer("horloge_perdue")


def _ping(hote):
    return subprocess.run(["ping", "-c", "1", "-W", "2", hote], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def _passerelle():
    m = re.search(r"default via (\S+)", _sortie(["ip", "route"]))
    return m.group(1) if m else None


def _diagnostic_reseau():
    """None si internet marche, sinon la cle de la phrase qui dit pourquoi."""
    if _ping("1.1.1.1") or _ping("8.8.8.8"):
        try:
            socket.setdefaulttimeout(5)
            socket.gethostbyname("www.google.com")
            return None
        except OSError:
            return "systeme_veille.reseau_dns"
    if "Not connected" in _sortie(["iw", "dev", "wlan0", "link"]):
        return "systeme_veille.reseau_wifi"
    passerelle = _passerelle()
    if not passerelle:
        return "systeme_veille.reseau_sans_route"
    if not _ping(passerelle):
        return "systeme_veille.reseau_box"
    return "systeme_veille.reseau_internet"


class _Reseau:
    echecs = 0
    panne = None      # (cle, debut)

    def verifier(self):
        cause = _diagnostic_reseau()
        if cause is None:
            self.echecs = 0
            if self.panne:
                minutes = max(1, round((time.time() - self.panne[1]) / 60))
                self.panne = None
                _jouer(None, dire("systeme_veille.reseau_revenu", minutes=nombre(minutes, "minute")))
            return
        self.echecs += 1
        if self.echecs >= ECHECS_RESEAU and (self.panne is None or self.panne[0] != cause):
            debut = self.panne[1] if self.panne else time.time()
            self.panne = (cause, debut)
            _jouer(None, dire(cause))


def _disques():
    for chemin, cle in DISQUES:
        if not os.path.ismount(chemin) and chemin != "/":
            continue
        try:
            u = shutil.disk_usage(chemin)
        except OSError:
            continue
        pourcent = round(100 * u.used / u.total)
        if pourcent >= PLEIN_POURCENT and _une_fois(f"plein_{chemin}", PLEIN_RAPPEL_SEC):
            _jouer(None, dire("systeme_veille.disque_plein", disque=dire(cle), pourcent=pourcent,
                              reste=max(1, round(u.free / 1e9))))


class _Services:
    def __init__(self):
        self.redemarrages = {}   # veille -> [(t, NRestarts)]
        self.etats = {}

    def _etat(self, unite):
        return _sortie(["systemctl", "is-active", unite])

    def _encore(self, veille):
        """Pour le rappel : vrai tant que la veille a encore redemarre depuis la derniere fois."""
        vu = [self.redemarrages[veille][-1][1]]

        def encore():
            n = _sortie(["systemctl", "show", "-p", "NRestarts", "--value", veille])
            if not n.isdigit():
                return False
            plus, vu[0] = int(n) > vu[0], int(n)
            return plus or self._etat(veille) != "active"
        return encore

    def verifier(self):
        for veille in VEILLES:
            n = _sortie(["systemctl", "show", "-p", "NRestarts", "--value", veille])
            if not n.isdigit():
                continue
            now = time.time()
            hist = [(t, v) for t, v in self.redemarrages.get(veille, []) if now - t <= BOUCLE_FENETRE_SEC]
            hist.append((now, int(n)))
            self.redemarrages[veille] = hist
            if hist[-1][1] - hist[0][1] >= BOUCLE_REDEMARRAGES and _une_fois(f"boucle_{veille}", BOUCLE_FENETRE_SEC):
                # rappel de temps en temps, jamais en continu, tant qu'elle redemarre (2026-10-24)
                alarmes.declencher("veille_plantee", dire("systeme_veille.veille_en_boucle",
                                                          veille=veille.replace("-", " ")),
                                   source="systeme", rappel_sec=RAPPEL_SEC, encore=self._encore(veille))
        for unite, evenement in (("log-ship", "journal_non_sauve"), ("webcamd", "camera_coupee")):
            etat = self._etat(unite)
            avant = self.etats.get(unite)
            self.etats[unite] = etat
            if avant == "active" and etat != "active":
                _jouer(evenement)


class _Ignores:
    """Fin d'une mise de cote faite avec "ignorer" (lib/ignores.py) : la cible
    revient dans l'ecoute, on le fait savoir."""

    def __init__(self):
        self.vues = {}   # (type, valeur) -> entree encore en vigueur au dernier tour

    def verifier(self):
        import ignores
        now = time.time()
        liste = ignores._lire_fichier()
        en_vigueur = {(e.get("type"), str(e.get("valeur"))): e for e in liste
                      if e.get("jusqu_a") is not None and e["jusqu_a"] > now}
        for cle, e in self.vues.items():
            if cle not in en_vigueur and e["jusqu_a"] <= now:  # retiree a la main avant l'heure : rien
                _jouer("fin_ignore", dire("systeme_veille.fin_ignore", libelle=e.get("libelle") or cle[1]))
        self.vues = en_vigueur


DERNIERE_TRAME = "/home/vador/autopilot/capteurs_derniere_trame"   # touche par capteurs_watch.scan_window
ANTENNE_MUETTE_SEC = 2 * 3600    # d'habitude une trame toutes les 10 minutes environ
ANTENNE_RAPPEL_SEC = 3 * 3600
ANTENNE_FENETRES_MIN = 6         # au moins 6 ecoutes des capteurs de suite, toutes sans trame
FENETRES_VIDES = "/home/vador/autopilot/capteurs_fenetres_vides"


def _antenne():
    if _sortie(["systemctl", "is-active", "radio-watch"]) != "active":
        return
    try:
        derniere = os.path.getmtime(DERNIERE_TRAME)
    except OSError:
        return  # pas encore de trame notee depuis l'installation
    if _derniers.get("antenne_compris") == derniere:
        return  # "compris" dit : plus d'alarme tant qu'aucune trame n'est revenue
    try:  # ecoutes des capteurs vraiment faites, sans trame (mode restreint : aucune, 2026-10-24)
        with open(FENETRES_VIDES) as f:
            if int(f.read().strip() or 0) < ANTENNE_FENETRES_MIN:
                return
    except (OSError, ValueError):
        return
    age = time.time() - derniere
    actif_depuis = _sortie(["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", "radio-watch"])
    try:
        actif_sec = time.monotonic() - int(actif_depuis) / 1e6
    except ValueError:
        actif_sec = ANTENNE_MUETTE_SEC
    if age >= ANTENNE_MUETTE_SEC and actif_sec >= ANTENNE_MUETTE_SEC and _une_fois("antenne", ANTENNE_RAPPEL_SEC):
        def fini(resultat):
            if resultat == "compris":
                _derniers["antenne_compris"] = derniere
        _log("antenne_muette")
        alarmes.declencher("antenne_muette", dire("systeme_veille.antenne_muette",
                                                  heures=nombre(round(age / 3600), "heure")),
                           source="systeme", quand_fini=fini)


def _sonde_sol():
    """Arrosage et mesure de la sonde de sol apres stabilisation (lib/plantes_sonde.py)."""
    import plantes_sonde
    plantes_sonde.verifier(lambda evenement, phrase: _plus_tard(_jouer, evenement, phrase))


def main():
    _log("demarrage")
    threading.Thread(target=_noyau, daemon=True).start()
    horloge, reseau, services, ignores_ = _Horloge(), _Reseau(), _Services(), _Ignores()
    while True:
        for verif in (_alimentation, horloge.verifier, reseau.verifier, _disques, services.verifier,
                      ignores_.verifier, _antenne, _sonde_sol):
            try:
                verif()
            except Exception as exc:
                _log(f"{verif.__qualname__} : {exc!r}")
        time.sleep(PERIODE_SEC)


if __name__ == "__main__":
    main()
