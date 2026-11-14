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
  disques                (2026-11-12) bilan quotidien de sante (lib/sante_disques.py) :
                         alarmes propres -- disque defaillant (en boucle), secteurs
                         abimes, erreurs d'entree-sortie (rappels espaces) ; en
                         direct, erreurs du noyau et chaleur des disques
  puce Wi-Fi calee       (2026-11-01) erreurs SDIO de brcmfmac et plus de reseau :
                         pilote recharge, puis bus SDIO reinitialise, puis
                         redemarrage du Pi (au plus un par heure), sinon alarme

Le NAS perdu / retrouve reste a nas_watch (il le dit deja, avec ce son)."""
import chemins
import os
import re
import shutil
import socket
import subprocess
import threading
import time

import alarmes
import audio_lock
import sante_disques
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
WIFI_ERREURS = 5                # erreurs SDIO de la puce Wi-Fi en WIFI_FENETRE_SEC : elle est calee
WIFI_FENETRE_SEC = 60
WIFI_ATTENTE_SEC = (60, 90)     # temps laisse au reseau pour revenir apres chaque geste
WIFI_REDEMARRAGE_SEC = 3600     # au plus un redemarrage automatique par heure (pas de boucle)
WIFI_REDEMARRAGE = chemins.base("wifi_redemarrage")   # carte SD : survit au redemarrage
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
                elif "brcmf" in bas and ("-110" in bas or "sdio error" in bas or "txfail" in bas):
                    _WIFI.erreur()
                elif sante_disques.disque_de_ligne(ligne):
                    _DISQUES.ligne_noyau(ligne)
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


class _Wifi:
    """Puce Wi-Fi calee (2026-11-01) : sous emission soutenue (flux rtl_tcp vers
    une machine compagne), le bus SDIO de la puce brcmfmac cale et ne revient
    jamais seul -- 77 min sans reseau le 30/10, jusqu'au redemarrage. Gestes de
    plus en plus forts (tools/wifi-repare), chacun suivi d'une attente du
    reseau ; en dernier recours un redemarrage, au plus un par heure."""

    def __init__(self):
        self._erreurs = []
        self._garde = threading.Lock()
        self._en_cours = False
        self.passerelle = None      # derniere passerelle joignable (la route peut rester sans reseau)

    def erreur(self):
        with self._garde:
            now = time.time()
            self._erreurs = [t for t in self._erreurs if now - t < WIFI_FENETRE_SEC] + [now]

    def verifier(self):
        passerelle = _passerelle()
        if passerelle and _ping(passerelle):
            self.passerelle = passerelle
        with self._garde:
            calee = len([t for t in self._erreurs if time.time() - t < WIFI_FENETRE_SEC]) >= WIFI_ERREURS
            if not calee or self._en_cours or self._reseau_ok():
                return
            self._en_cours = True
        _plus_tard(self._reparer)

    def _reseau_ok(self):
        cible = self.passerelle or _passerelle()
        return bool(cible) and _ping(cible)

    def _attendre(self, secondes):
        fin = time.time() + secondes
        while time.time() < fin:
            time.sleep(5)
            if self._reseau_ok():
                return True
        return False

    def _reparer(self):
        try:
            _jouer(None, dire("systeme_veille.wifi_decroche"))
            for geste, attente in zip(("pilote", "bus"), WIFI_ATTENTE_SEC):
                _log(f"puce Wi-Fi calee : wifi-repare {geste}")
                sortie = subprocess.run(["sudo", "-n", "/usr/local/sbin/wifi-repare", geste],
                                        capture_output=True, text=True, timeout=60)
                if sortie.returncode:
                    _log(f"wifi-repare {geste} : {sortie.stderr.strip()[-200:]}")
                with self._garde:
                    self._erreurs = []
                if self._attendre(attente):
                    _jouer(None, dire("systeme_veille.wifi_revenu"))
                    return
            try:
                dernier = os.path.getmtime(WIFI_REDEMARRAGE)
            except OSError:
                dernier = 0
            if time.time() - dernier >= WIFI_REDEMARRAGE_SEC:
                with open(WIFI_REDEMARRAGE, "w") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
                _jouer(None, dire("systeme_veille.wifi_redemarrage"))
                subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "reboot"], timeout=30)
            else:
                alarmes.declencher("wifi_perdu", dire("systeme_veille.wifi_echec"), source="systeme", une_fois=True)
        except Exception as exc:
            _log(f"reparation Wi-Fi : {exc!r}")
        finally:
            with self._garde:
                self._en_cours = False


_WIFI = _Wifi()


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


# --- sante des disques (2026-11-12) --------------------------------------------
# Le bilan du matin (sante-disques.timer) dit ce qui ne va pas ; la veille en
# tire les alarmes, une par sorte d'evenement, et guette en direct le noyau
# (erreurs d'entree-sortie, lecture seule) et la chaleur des disques.
DISQUE_RAPPEL_SEC = {sante_disques.DEFAILLANT: None,          # en boucle jusqu'au "compris"
                     sante_disques.SECTEURS: 2 * 3600,
                     sante_disques.ERREURS: 3600,
                     sante_disques.CHAUD: 30 * 60}
DISQUE_ERREURS_MIN = 3            # erreurs d'E/S en DISQUE_FENETRE_SEC (une seule : un hub qui retombe)
DISQUE_FENETRE_SEC = 10 * 60
DISQUE_CALME_SEC = 10             # le disque doit etre encore la apres la rafale (pas debranche)
DISQUE_REALARME_SEC = 3 * 3600    # apres un "compris" : pas de nouvelle alarme directe avant
DISQUE_TEMP_SEC = 30 * 60         # lecture de la chaleur des disques
DISQUE_TEMP_CHAUD_SEC = 5 * 60    # ... plus souvent quand ils chauffent
DISQUE_REBILAN_SEC = 3600
DISQUE_ACQUIT = chemins.base("sante_disques_compris.json")


class _SanteDisques:
    def __init__(self):
        self._garde = threading.Lock()
        self._erreurs = {}          # disque -> [t]
        self._graves = set()        # disques avec une ligne grave (lecture seule, erreur ext4, secteur illisible)
        self._rapport_vu = None
        self._temp_t = 0
        self._chauds = set()
        self._derniere = {}         # disque -> derniere erreur du noyau
        self._compris_direct = {}   # disque -> "compris" d'une alarme directe (le bilan qui suit ne la redit pas)

    # noyau (fil _noyau)
    def ligne_noyau(self, ligne):
        dev = sante_disques.disque_de_ligne(ligne)
        if not dev or not os.path.exists(f"/sys/block/{dev}"):   # mmc1 : la puce Wi-Fi, pas un disque
            return
        bas = ligne.lower()
        with self._garde:
            now = time.time()
            self._erreurs[dev] = [t for t in self._erreurs.get(dev, []) if now - t < DISQUE_FENETRE_SEC] + [now]
            self._derniere[dev] = now
            if "read-only" in bas or "ext4-fs error" in bas or "medium error" in bas:
                self._graves.add(dev)

    def verifier(self):
        self._noyau()
        self._rapport()
        self._chaleur()

    @staticmethod
    def _disque(dev):
        return next((d for d in sante_disques.peripheriques() if d["id"] == dev), None)

    def _noyau(self):
        now = time.time()
        with self._garde:
            prets = []
            for dev, ts in list(self._erreurs.items()):
                ts = [t for t in ts if now - t < DISQUE_FENETRE_SEC]
                self._erreurs[dev] = ts
                if ts and (dev in self._graves or len(ts) >= DISQUE_ERREURS_MIN) and now - ts[-1] >= DISQUE_CALME_SEC:
                    prets.append((dev, len(ts)))
                    self._erreurs[dev] = []
                    self._graves.discard(dev)
                elif not ts:
                    self._graves.discard(dev)
        for dev, n in prets:
            disque = self._disque(dev)
            if not disque:            # parti avec la rafale : c'est un debranchement (usb_watch, nas_watch)
                _log(f"erreurs d'E/S sur {dev}, disparu ensuite : pas d'alarme disque")
                continue
            self._rebilan()
            if not _une_fois(f"disque_noyau_{dev}", DISQUE_REALARME_SEC):
                continue
            detail = dire("sante_disques.erreurs_direct", nombre=nombre(n, "erreur"))
            self._alarme(sante_disques.ERREURS, [sante_disques.nom(disque)], detail,
                         encore=self._encore_noyau(dev), compris=lambda: self._compris_direct.__setitem__(dev, time.time()))

    def _encore_noyau(self, dev):
        """Rappel tant que le disque a encore eu des erreurs dans l'heure, ou reste en lecture seule."""
        def encore():
            disque = self._disque(dev)
            if not disque:
                return False
            with self._garde:
                recent = time.time() - self._derniere.get(dev, 0) < 3600
            return recent or bool(sante_disques.mesures_fs(disque)["lecture_seule"])
        return encore

    def _rebilan(self):
        """Erreur en direct : le bilan est refait (la commande "sante" le dit)."""
        if _une_fois("disque_rebilan", DISQUE_REBILAN_SEC):
            _plus_tard(lambda: sante_disques.bilan())

    # bilan du matin
    def _acquits(self):
        try:
            import json
            with open(DISQUE_ACQUIT, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _acquitter(self, signature):
        import json
        data = self._acquits()
        data[signature] = time.time()
        data = dict(sorted(data.items(), key=lambda kv: kv[1])[-200:])
        tmp = f"{DISQUE_ACQUIT}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, DISQUE_ACQUIT)

    @staticmethod
    def _signature(evenement, d):
        m = d["mesures"]
        if evenement == sante_disques.DEFAILLANT:
            cle = [m.get("verdict"), m.get("attributs_en_echec"), (m.get("dernier_test") or {}).get("heures")]
        elif evenement == sante_disques.SECTEURS:
            cle = [m.get(k) for k in ("en_attente", "illisibles", "realloues", "incorrigibles_signales")]
        else:
            cle = [m.get("erreurs_fs"), m.get("lecture_seule"), m.get("erreurs_noyau")]
        return f"{d['serie']}:{evenement}:{cle}"

    def _rapport(self):
        try:
            mtime = os.path.getmtime(sante_disques.RAPPORT)
        except OSError:
            return
        if mtime == self._rapport_vu:
            return
        self._rapport_vu = mtime
        rapport = sante_disques.dernier() or {}
        for d in rapport.get("disques", []):   # erreurs deja dites en direct, et comprises
            if sante_disques.ERREURS in d.get("alarmes", []) and \
                    self._compris_direct.get(d["id"], 0) > rapport.get("t", 0) - 86400:
                self._acquitter(self._signature(sante_disques.ERREURS, d))
        acquits = self._acquits()
        for evenement in (sante_disques.DEFAILLANT, sante_disques.SECTEURS, sante_disques.ERREURS):
            touches = [d for d in rapport.get("disques", []) if evenement in d.get("alarmes", [])]
            nouveaux = [d for d in touches if self._signature(evenement, d) not in acquits]
            if not nouveaux:
                continue
            if evenement in alarmes._en_cours:   # la meme sorte sonne deja (alarme directe) : on revient apres
                self._rapport_vu = None
                continue
            signatures = [self._signature(evenement, d) for d in nouveaux]
            detail = ". ".join(f"{d['nom']} : " + "; ".join(c.rstrip(".") for c in d["constats"])
                               for d in nouveaux)
            series = {d["serie"] for d in nouveaux}
            self._alarme(evenement, [d["nom"] for d in nouveaux], detail, signatures=signatures,
                         encore=self._encore_rapport(evenement, series))

    @staticmethod
    def _encore_rapport(evenement, series):
        def encore():
            r = sante_disques.dernier() or {}
            return any(d["serie"] in series and evenement in d.get("alarmes", []) for d in r.get("disques", []))
        return encore

    # chaleur
    def _chaleur(self):
        now = time.time()
        if now - self._temp_t < (DISQUE_TEMP_CHAUD_SEC if self._chauds else DISQUE_TEMP_SEC):
            return
        self._temp_t = now
        for d in sante_disques.peripheriques():
            if d["type"] == "sd":
                continue
            t = sante_disques.temperature(d["id"])
            if t is None:
                continue
            if t < sante_disques.CHAUD_C - 3:
                self._chauds.discard(d["id"])
                continue
            if t >= sante_disques.CHAUD_C and d["id"] not in self._chauds:
                self._chauds.add(d["id"])
                detail = dire("sante_disques.chaud", degres=t)
                self._alarme(sante_disques.CHAUD, [sante_disques.nom(d)], detail, encore=self._encore_chaud(d["id"]))

    def _encore_chaud(self, dev):
        lu = [0, True]

        def encore():
            if time.time() - lu[0] >= DISQUE_TEMP_CHAUD_SEC:
                t = sante_disques.temperature(dev)
                lu[0], lu[1] = time.time(), t is None or t >= sante_disques.CHAUD_C - 3
                if not lu[1]:
                    self._chauds.discard(dev)
            return lu[1]
        return encore

    def _alarme(self, evenement, noms, detail, signatures=(), encore=None, compris=None):
        texte = dire(f"sante_disques.alarme_{evenement}", disques=", ".join(noms), detail=detail)

        def fini(resultat):
            if resultat == "compris":
                for s in signatures:
                    self._acquitter(s)
                if compris:
                    compris()
        _log(f"{evenement} : {texte}")
        alarmes.declencher(evenement, texte, source="systeme", quand_fini=fini,
                           rappel_sec=DISQUE_RAPPEL_SEC[evenement], encore=encore)


_DISQUES = _SanteDisques()


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


DERNIERE_TRAME = chemins.base("capteurs_derniere_trame")   # touche par capteurs_watch.scan_window
ANTENNE_MUETTE_SEC = 2 * 3600    # d'habitude une trame toutes les 10 minutes environ
ANTENNE_RAPPEL_SEC = 3 * 3600
ANTENNE_FENETRES_MIN = 6         # au moins 6 ecoutes des capteurs de suite, toutes sans trame
FENETRES_VIDES = chemins.base("capteurs_fenetres_vides")
# rtl_433 qui s'arrete aussitot (2026-11-14) : ce n'est pas l'antenne, on le dit
# comme tel (capteurs_watch tient le compte des ecoutes ratees de suite).
FENETRES_RATEES = chemins.base("capteurs_fenetres_ratees")
RATEES_MIN = 4
RATEES_RAPPEL_SEC = 6 * 3600


DONGLE_REPETE = 3                 # reinitialisations du dongle en une heure : il ou son alimentation lache
DONGLE_REPETE_SEC = 3600


class _Dongle:
    """Dongle radio fige puis reinitialise par le scanner (lib/dongle_fige.py,
    2026-11-14) : on le dit ; s'il se fige encore et encore, alarme."""

    def __init__(self):
        self.vu = time.time()

    def verifier(self):
        import dongle_fige
        nouvelles = dongle_fige.reinits_depuis(self.vu)
        if not nouvelles:
            return
        self.vu = max(x["t"] for x in nouvelles)
        recentes = dongle_fige.reinits_depuis(time.time() - DONGLE_REPETE_SEC)
        if len(recentes) >= DONGLE_REPETE and _une_fois("dongle_repete", DONGLE_REPETE_SEC):
            alarmes.declencher("veille_plantee", dire("systeme_veille.dongle_repete", n=nombre(len(recentes), "fois")),
                               source="systeme")
            return
        derniere = nouvelles[-1]
        _jouer("peripherique_branche", dire("systeme_veille.dongle_reinit" if derniere.get("ok")
                                            else "systeme_veille.dongle_reinit_echec"))


def _capteurs_ratees():
    try:
        with open(FENETRES_RATEES) as f:
            n = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return
    if n >= RATEES_MIN and _une_fois("capteurs_ratees", RATEES_RAPPEL_SEC):
        _jouer("veille_plantee", dire("systeme_veille.capteurs_ratees", n=nombre(n, "écoute")))


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
    dongle = _Dongle()
    while True:
        for verif in (_alimentation, horloge.verifier, reseau.verifier, _WIFI.verifier, _disques, _DISQUES.verifier,
                      services.verifier,
                      ignores_.verifier, _antenne, _capteurs_ratees, dongle.verifier, _sonde_sol):
            try:
                verif()
            except Exception as exc:
                _log(f"{verif.__qualname__} : {exc!r}")
        time.sleep(PERIODE_SEC)


if __name__ == "__main__":
    main()
