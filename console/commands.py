"""Registre des commandes de la console vocale.

Pour ajouter une commande : ecrire un handler qui retourne la phrase a
dire, puis l'ajouter a COMMANDS avec des mots-cles (correspondance
certaine) et des phrases d'exemple (repli flou, voir matcher.py).
"""
import chemins
import csv
import datetime
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import requests

import reglages  # noqa: E402
from phrases import Etiquettes, _code, dire, dire_ou, nombre  # noqa: E402

FM_RANGE = "88M:108M:125k"
FM_SCAN_TIMEOUT_SEC = 40


@dataclass
class Command:
    name: str
    description: str
    handler: object
    keywords: list = field(default_factory=list)
    examples: list = field(default_factory=list)
    confirm: str = ""  # si renseigne : question posee, "oui" exige avant d'agir
    busy: object = None  # si renseigne : rend l'etat parle d'une action deja en cours, ou ""

    # Description et question de confirmation passent par les phrases
    # (commande.<nom>.description / .confirmation) ; le texte ci-dessus sert
    # de repli. Les noms et mots-cles restent ceux de la reconnaissance.
    def texte_description(self):
        return dire_ou(f"commande.{_code(self.name)}.description", self.description)

    def question(self):
        return self.confirm and dire_ou(f"commande.{_code(self.name)}.confirmation", self.confirm)


JOURS = Etiquettes("jour", dict(enumerate(["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"])),
                   "jour de la semaine, 0 = lundi")
MOIS = Etiquettes("mois", dict(enumerate(["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
                                          "septembre", "octobre", "novembre", "décembre"], 1)), "nom du mois")


def _fr_number(value, decimals=1):
    return f"{value:.{decimals}f}".replace(".", ",")


def pi_temp():
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                             text=True, timeout=5).stdout
        value = float(out.split("=")[1].split("'")[0])
    except Exception:
        return dire("commandes.impossible_lire_temperature_pi")
    return dire("commandes.pi_degres", v1=_fr_number(value))


def heure():
    now = datetime.datetime.now()
    return dire("commandes.heure_minutes" if now.minute else "commandes.heure_pile", heures=now.hour,
                minutes=now.minute, jour=JOURS[now.weekday()], date=now.day, mois=MOIS[now.month])


def printer_status():
    # Etat complet (fichier, pause, erreur, temperatures) : console/imprimante_cmd.py (2026-10-11).
    import imprimante_cmd
    return imprimante_cmd.etat_texte()


def radio_scan():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        proc = subprocess.run(["rtl_power", "-f", FM_RANGE, "-i", "2", "-1", path],
                              capture_output=True, text=True, timeout=FM_SCAN_TIMEOUT_SEC)
        if "usb_claim_interface" in proc.stderr or "Failed to open" in proc.stderr:
            return dire("commandes.antenne_occupee_veille_radio")
        if "No supported devices" in proc.stderr:
            return dire("commandes.aucune_antenne_radio_detectee")
        best_freq, best_db = None, None
        with open(path, newline="") as f:
            for row in csv.reader(f):
                low, step = float(row[2]), float(row[4])
                for i, db in enumerate(row[6:]):
                    db = float(db)
                    if best_db is None or db > best_db:
                        best_freq, best_db = low + i * step, db
    except subprocess.TimeoutExpired:
        return dire("commandes.scan_radio_pris_trop")
    except (OSError, ValueError, IndexError):
        return dire("commandes.scan_radio_echoue")
    finally:
        os.unlink(path)
    if best_freq is None:
        return dire("commandes.scan_radio_rien_renvoye")
    return dire("commandes.signal_fm_plus_fort", v1=_fr_number(best_freq / 1e6))


RADIONO_SCRIPT = chemins.base("radiono.py")
_radiono_thread = None


def _play_radiono():
    import audio_lock
    lock = audio_lock.acquire("console")  # attend la fin d'une annonce en cours (priorites)
    try:
        if not lock.dropped:
            subprocess.run(["python3", RADIONO_SCRIPT], cwd=os.path.dirname(RADIONO_SCRIPT),
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
    finally:
        audio_lock.release(lock)


def rire():
    """Lance radiono (un son au hasard dans les capsules du NAS), en
    arriere-plan : un clip peut durer plusieurs minutes."""
    global _radiono_thread
    import threading
    if not os.path.isfile(RADIONO_SCRIPT):
        return dire("commandes.script_radiono_introuvable")
    if _radiono_thread is not None and _radiono_thread.is_alive():
        return dire("commandes.son_radiono_deja_cours")
    # Petit delai : laisser la console dire sa reponse avant le son.
    _radiono_thread = threading.Timer(3, _play_radiono)
    _radiono_thread.daemon = True
    _radiono_thread.start()
    return dire("commandes.radiono_son_au_hasard")


WATCHERS = Etiquettes("veille", {
    "nas-watch": "disques", "radio-watch": "radio", "meteo-watch": "météo",
    "crue-watch": "crues", "sncf-watch": "trains", "adsb-watch": "avions",
    "stas-watch": "bus et tram", "log-ship": "copie des journaux", "usb-watch": "appareils USB",
    "imprimante-watch": "annonces de l'imprimante",
}, "nom d'une veille (service du Pi)")
NAS_MOUNTS = ["/media/nas/nd1", "/media/nas/nd2"]


def _cpu_percent(interval=0.5):
    def sample():
        with open("/proc/stat") as f:
            values = [int(v) for v in f.readline().split()[1:]]
        idle = values[3] + values[4]  # idle + iowait
        return idle, sum(values)
    idle1, total1 = sample()
    import time
    time.sleep(interval)
    idle2, total2 = sample()
    return 100 * (1 - (idle2 - idle1) / max(1, total2 - total1))


def _vcgencmd(*args):
    return subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=5).stdout.strip()


def _uptime_text():
    with open("/proc/uptime") as f:
        minutes = int(float(f.read().split()[0]) // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return dire("commandes.duree_jours", jours=nombre(days, "jour"), heures=hours)
    if hours:
        return dire("commandes.duree_heures", heures=hours, minutes=mins)
    return dire("commandes.duree_minutes", minutes=mins)


def sante():
    """Bilan de sante : problemes d'abord, puis les mesures."""
    problems, details = [], []

    cpu = _cpu_percent()
    load1 = os.getloadavg()[0]
    details.append(dire("commandes.processeur_cent_charge_moyenne", round=round(cpu), v1=_fr_number(load1)))
    if cpu > 90:
        problems.append(dire("commandes.processeur_sature"))

    try:
        temp = float(_vcgencmd("measure_temp").split("=")[1].split("'")[0])
        details.append(dire("commandes.temperature_degres", v1=_fr_number(temp)))
        if temp >= 80:
            problems.append(dire("commandes.temperature_critique_pi_ralentit"))
        elif temp >= 75:
            problems.append(dire("commandes.temperature_elevee"))
    except (IndexError, ValueError, OSError):
        details.append(dire("commandes.temperature_illisible"))

    # get_throttled : bits 0-3 = etat actuel, bits 16-19 = survenu depuis
    # le demarrage (0 sous-tension, 1 frequence bridee, 2 ralenti, 3 limite thermique).
    try:
        flags = int(_vcgencmd("get_throttled").split("=")[1], 16)
        volts = _vcgencmd("measure_volts", "core").split("=")[1].rstrip("V")
        if flags & 0x1:
            problems.append(dire("commandes.sous_tension_moment_alimentation"))
            details.append(dire("commandes.alimentation_sous_tension_cours"))
        elif flags & 0x10000:
            problems.append(dire("commandes.sous_tension_survenue_depuis"))
            details.append(dire("commandes.alimentation_correcte_maintenant_mais"))
        else:
            details.append(dire("commandes.alimentation_correcte_aucune_sous"))
        details.append(dire("commandes.tension_processeur_volt", v1=_fr_number(float(volts), 2)))
        if flags & 0x4:
            problems.append(dire("commandes.processeur_ralenti_moment"))
        elif flags & 0x40000:
            details.append(dire("commandes.processeur_ete_ralenti_depuis"))
    except (IndexError, ValueError, OSError):
        details.append(dire("commandes.etat_alimentation_illisible"))

    with open("/proc/meminfo") as f:
        mem = {line.split(":")[0]: int(line.split()[1]) for line in f}
    mem_used = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])
    details.append(dire("commandes.memoire_utilisee_cent", round=round(mem_used)))
    if mem_used > 90:
        problems.append(dire("commandes.memoire_presque_pleine"))

    import shutil
    sd = shutil.disk_usage("/")
    sd_used = 100 * sd.used / sd.total
    details.append(dire("commandes.carte_sd_remplie_cent", round=round(sd_used)))
    if sd_used > 90:
        problems.append("carte SD presque pleine")

    mounted = sum(os.path.ismount(p) for p in NAS_MOUNTS)
    details.append(dire("commandes.disques_nas_montes", mounted=mounted, len=len(NAS_MOUNTS)))
    if mounted < len(NAS_MOUNTS):
        problems.append(dire("commandes.disque_nas_manquant"))

    states = subprocess.run(["systemctl", "is-active", *WATCHERS], capture_output=True,
                            text=True, timeout=10).stdout.split()
    down = [WATCHERS[u] for u, s in zip(WATCHERS, states) if s != "active"]
    if down:
        problems.append(dire("commandes.veille_arretee", join=', '.join(down)))
        details.append(dire("commandes.veilles_actives", v1=len(WATCHERS) - len(down), len=len(WATCHERS)))
    else:
        details.append(dire("commandes.veilles_sont_actives", len=len(WATCHERS)))

    details.append(dire("commandes.allume_depuis", v1=_uptime_text()))
    summary = (dire("commandes.pi_va_bien") if not problems
               else (dire("commandes.attention") + " ") + ", ".join(problems) + ".")
    return " ".join([summary] + details)


def mise_a_jour(announce):
    import maj
    import voix
    # Les etapes sont dites par maj.py lui-meme, dans l'ordre (say_and_wait).
    maj.start(voix.say_and_wait)
    return ""


def _ago(timestamp):
    minutes = int((time.time() - timestamp) // 60)
    if minutes < 1:
        return dire("commandes.instant")
    if minutes < 60:
        return dire("commandes.il_y_a", duree=nombre(minutes, "minute"))
    hours = minutes // 60
    return dire("commandes.il_y_a", duree=nombre(hours, "heure"))


def parasite():
    """Derniere reception radio = faux positif : supprimee du journal,
    empreinte sonore apprise pour l'ignorer si elle revient (le scanner la
    compare en pre-ecoute silencieuse, voir lib/noise_signatures.py)."""
    import noise_signatures as ns
    capture = ns.last_capture()
    if not capture:
        return dire("commandes.aucune_reception_radio_recente")
    label = capture["label"]
    if capture.get("marked_noise"):
        return dire("commandes.derniere_reception_deja_marquee", label=label)
    parts = [dire("commandes.reception_marquee_comme_parasite", label=label, v1=_ago(capture['time']))]
    path = capture.get("log_path")
    if path and os.path.isfile(path):
        os.remove(path)
        parts.append(dire("commandes.supprimee_journal"))
    else:
        parts.append(dire("commandes.elle_etait_plus_journal"))
    if capture.get("fingerprint"):
        ns.add_signature(capture)
        parts.append(dire("commandes.ai_retenu_sa_forme"))
    else:
        parts.append(dire("commandes.empreinte_sonore_cette_reception"))
    ns.mark_last_capture_noise()
    try:  # un parasite ne doit pas attirer l'attention du scanner
        import attention
        attention.stimulate(capture.get("target"), attention.PARASITE_POINTS)
    except Exception:
        pass
    return " ".join(parts)


def attention_status():
    """Ou le scanner concentre son ecoute, et pourquoi (lib/attention.py)."""
    import attention
    import noise_signatures as ns
    labels = {p["target"]: p["label"] for p in ns.read_plages()}
    labels.update(attention.DATA_LABELS)
    return attention.describe(labels)


def capteurs():
    """Derniers releves des capteurs sans fil (rtl_433) et des stations meteo
    APRS captes par le scanner (watchers/capteurs_watch.py, aprs_watch.py)."""
    import json
    import time
    import veille_donnees as vd
    parts = []
    try:
        with open("/tmp/meteo_stations_aprs.json", encoding="utf-8") as f:
            stations = json.load(f)
    except (OSError, ValueError):
        stations = {}
    for call, st in sorted(stations.items(), key=lambda p: vd.distance_km(p[1]["lat"], p[1]["lon"])):
        wx = st.get("meteo") or {}
        if wx.get("temperature") is None:
            continue
        age = int((time.time() - st["t"]) // 60)
        parts.append(dire("commandes.station_meteo_degres_minutes", spell=vd.spell(call), where=vd.where(st['lat'], st['lon']), fr_number=vd.fr_number(wx['temperature']), age=age))
    try:
        with open("/tmp/capteurs_releves.json", encoding="utf-8") as f:
            readings = json.load(f)
    except (OSError, ValueError):
        readings = {}
    for r in sorted(readings.values(), key=lambda r: -r["t"]):
        v = r.get("valeurs") or {}
        values = []
        if v.get("temperature_C") is not None:
            values.append(dire("commandes.degres", fr_number=vd.fr_number(v['temperature_C'], 1)))
        if v.get("humidity") is not None:
            values.append(dire("commandes.humidite_cent", humidity=v['humidity']))
        age = int((time.time() - r["t"]) // 60)
        kinds = {"cuisson": dire("commandes.cuisson_barbecue"), "piscine": "piscine", "frigo": "réfrigérateur", "sol": dire("commandes.sonde_sol")}
        kind = f", {kinds[r['type']]}" if r.get("type") in kinds else ""
        parts.append(f"{r['nom']}{kind}" + (f" : {', '.join(values)}" if values else "") + dire("commandes.minutes", age=age))
    if not parts:
        return dire("commandes.aucun_capteur_ni_station")
    return dire("commandes.sources", sources=nombre(len(parts), "source")) + " " + " ".join(parts[:12])


def voitures_cmd(line):
    """Statistiques de la rue (capteurs de pneus, lib/voitures.py), ou
    "nomme la voiture 3 facteur"."""
    import re
    import matcher
    import voitures
    m = re.search(r"(?:vehicule|voiture)\s+(?:numero\s+)?(\d+)\s+(?:s appelle\s+|en\s+|comme\s+)?(.+)$",
                  matcher.normalize(line))
    if m and re.search(r"\bnomm|\bappel|\brenomm", matcher.normalize(line)):
        vid, name = m.group(1), m.group(2).strip()
        if voitures.rename(vid, name):
            return dire("commandes.vehicule_numero_appelle_maintenant", vid=vid, name=name)
        return dire("commandes.connais_vehicule_numero", vid=vid)
    return voitures.summary()


def terrain_cmd(line=""):
    """Derniers messages de la veille terrain (watchers/veille_terrain.py) ;
    "terrain mots" : ce qu'elle surveille en ce moment."""
    import matcher
    import veille_terrain
    if {"mots", "mot", "surveille", "hashtags", "diese"} & set(matcher.normalize(line).split()):
        return veille_terrain.words_text()
    return veille_terrain.latest()


def accroche():
    import noise_signatures as ns
    import veille_donnees as vd
    locked = ns.lock_request()
    if locked:
        return dire("commandes.scanner_deja_verrouille_tape", label=locked['label'])
    # D'abord ce qu'on entend en ce moment (2026-10-17) : la derniere capture
    # n'est ecrite qu'en fin d'ecoute et designait sinon la precedente.
    live = ns.live_capture()
    if live:
        ns.request_lock(live)
        return dire("commandes.verrouillage_demande_en_cours", label=live['label'],
                    mhz=vd.fr_number(live['freq_hz'] / 1e6, 4))
    capture = ns.last_capture()
    if not capture:
        return dire("commandes.aucune_reception_recente_laquelle")
    ns.request_lock(capture)
    return (dire("commandes.verrouillage_demande_capte_scanner", label=capture['label'], v1=_ago(capture['time'])))


def securite(line=""):
    """Commande "securite" (2026-10-23) : dernier controle de securite du Pi ;
    "securite pip" met a jour les paquets pip signales."""
    import securite_cmd
    return securite_cmd.run(line)


def relais_bilan():
    """Commande "relais" (2026-10-23) : relais appris par l'APRS et ce qu'on en entend."""
    import relais_cmd
    return relais_cmd.texte()


def zappe():
    """Commande "zapper" (2026-10-22) : l'ecoute en cours s'arrete, le scanner
    passe a la cible suivante ; un verrouillage est leve (sinon il y revient)."""
    import maintien
    import noise_signatures as ns
    maintien.demander_zap()
    if ns.lock_request():
        ns.request_unlock()
        return dire("commandes.zapper_deverrouille")
    return dire("commandes.zapper_fait")


def decroche():
    import noise_signatures as ns
    if not ns.lock_request() and not ns.manual_request():
        return dire("commandes.scanner_verrouille_balaye_deja")
    ns.request_unlock()
    return dire("commandes.reprise_balayage_demandee")


def help_text():
    lines = [dire("commandes.aide_ligne", nom=c.name, description=c.texte_description()) for c in COMMANDS]
    return (dire("commandes.commandes_disponibles") + " ") + " ".join(lines)


def _power(action, done_key):
    def handler(announce):
        # Annonce AVANT d'agir : apres, le systeme coupe la voix en plein mot.
        announce(dire(done_key))
        # sudo -n : echoue immediatement plutot que d'attendre un mot de
        # passe que personne ne peut taper sur cette console.
        proc = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", action],
                              capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            print(f"[console] systemctl {action} : {proc.stderr.strip()}", file=sys.stderr)
            return dire("commandes.echec_console_droits_faire")
        return ""
    return handler


# "touches" et "répète" sont interceptes par main.py (ils agissent sur la
# boucle clavier elle-meme) ; ils figurent ici pour l'aide et la
# reconnaissance.
#
# redémarrer / éteindre : en tete de liste pour que toute ligne qui les
# mentionne passe par la confirmation, et SANS phrases d'exemple : jamais
# declenches par le repli flou, uniquement par mot-cle explicite.
COMMANDS = [
    # En tete (2026-10-19) : "simulation imprimante", "simulation meteo"... restent
    # des simulations, et ne sont jamais prises par la commande de leur sujet.
    Command("simulation", "simule les annonces : train, avion, météo, crue, bus, radio, disque, imprimante, ou tout", None,
            keywords=["simulation", "simulations", "simule", "simuler"],
            examples=["simulation"]),
    Command("redémarrer", "redémarre le Pi, après confirmation",
            _power("reboot", "commandes.redemarrage_pi"),
            keywords=["redemarre", "redemarrer", "redemarrage", "reboot"],
            confirm="Confirmer le redémarrage du Pi ? Tape oui puis entrée. Toute autre réponse annule."),
    Command("éteindre", "éteint le Pi, après confirmation",
            _power("poweroff", "commandes.extinction_pi"),
            keywords=["eteins", "eteindre", "extinction", "shutdown", "arrete le pi"],
            confirm="Confirmer l'extinction du Pi ? Il faudra couper et remettre le courant pour le rallumer. "
                    "Tape oui puis entrée. Toute autre réponse annule."),
    # Avant "aide" : "aide pratique" doit regler les conseils, pas lister les commandes.
    # Reglages audio appris des notes (console/bilan.py) : avant "réglages"
    # (cles API) et "bilan", dont ils partagent des mots.
    Command("applique", "appliquer les réglages audio proposés par le bilan des notes", None,
            keywords=["applique les reglages", "applique les propositions", "appliquer les reglages", "applique le bilan"],
            examples=["applique les réglages proposés"]),
    Command("réglage précédent", "revenir à la version précédente des réglages audio", None,
            keywords=["reglage precedent", "reglages precedents", "annule le reglage", "annule les reglages",
                      "version precedente"],
            examples=["reviens au réglage d'avant"]),
    Command("réglages automatiques", "réglages audio appliqués tout seuls d'après tes notes : réglages automatiques oui ou non", None,
            keywords=["reglages automatiques", "reglage automatique", "mode automatique"],
            examples=["active les réglages automatiques", "arrête les réglages automatiques"]),
    # Avant "réglages audio" (qui lit seulement) : regler soi-meme les couches de filtres.
    Command("réglages son", "régler à la main, bande par bande, le type de contenu (musique, voix, données) "
            "et chaque couche de filtres", None,
            keywords=["reglages son", "reglage son", "regler le son", "filtres audio", "filtres du son",
                      "couches de filtres", "type de contenu", "reglages des filtres"],
            examples=["coupe l'anti-sifflement sur la FM", "mets la bande FM en musique"]),
    Command("réglages audio", "réglages audio en place, bande par bande", None,
            keywords=["reglages audio", "reglage audio", "reglages du son"],
            examples=["quels réglages audio"]),
    Command("réapprendre", "réglages audio d'origine, empreintes de bruit et notes mises de côté : on réapprend de zéro", None,
            keywords=["reapprendre de zero", "reapprendre", "remise a zero des reglages", "reset des reglages"],
            examples=["recommence l'apprentissage à zéro"],
            confirm="Remettre les réglages audio d'origine et repartir de zéro ? Les notes et empreintes de bruit "
                    "seront mises de côté dans des archives. Tape oui puis entrée. Toute autre réponse annule."),
    # Avant "personnalité" (qui ouvre le reglage) : "quelle est ta personnalite" demande seulement qui parle.
    Command("qui es-tu", "le personnage du jour, choisi par Gemini, avec lequel le Pi parle ; qui es-tu en détail pour sa fiche",
            None, keywords=["qui es tu", "tu es qui", "quelle est ta personnalite", "c est quoi ta personnalite",
                            "ta personnalite du jour", "personnalite du jour", "presente toi", "quel personnage",
                            "qui parle", "qui est la", "t es qui"],
            examples=["qui me parle aujourd'hui", "quel est ton personnage", "comment es-tu aujourd'hui"]),
    Command("personnalité", "personnalité du Pi : décris-la, il réécrit ses phrases chaque nuit ; neutre ou surprise", None,
            keywords=["personnalite", "personnalité", "caractere du pi", "ton du pi", "change ta facon de parler",
                      "change ton caractere"],
            examples=["change ta personnalité", "parle autrement"]),
    Command("priorités", "ordre des sources sur les haut-parleurs et ce qu'elles font quand une plus importante parle", None,
            keywords=["priorites", "priorite", "simultane", "simultanes", "haut parleurs", "ordre des annonces"],
            examples=["qui parle en premier", "règle les priorités"]),
    Command("conseils", "niveau d'aide : consignes à chaque fois, ou seulement à la première utilisation", None,
            keywords=["conseils", "conseil", "consignes", "mode guide", "mode expert", "aide pratique",
                      "niveau d aide"],
            examples=["arrête de m'expliquer les touches", "explique moi toujours les touches"]),
    Command("aide", "liste des commandes", help_text,
            keywords=["aide", "help", "commandes"],
            examples=["aide", "qu'est-ce que tu sais faire", "liste des commandes"]),
    # Avant les autres : un nom de commune tape apres "ville" ne doit pas
    # declencher une autre commande par ses mots ; "tableau gare" doit
    # ouvrir le tableau, pas le choix de la gare suivie.
    # Avant "voix" : "voix locales" contient le mot-cle "voix" (2026-10-18).
    Command("capture", "enregistre tout ce qui sort du haut-parleur ; capture démarre, capture stop", None,
            keywords=["capture", "capturer", "enregistre le son", "enregistrer le son", "enregistrement du son"],
            examples=["capture démarre", "capture stop", "enregistre ce qui sort du haut-parleur"]),
    # Tutoriel (ecouter) AVANT "sons" (regler) : sur "apprendre les sons" les deux
    # matchent a 100, le premier gagne -> le tutoriel (2026-10-30).
    Command("son", "tutoriel des sons : parcourir le catalogue et écouter chaque son pour les apprendre",
            None, keywords=["son", "apprendre les sons", "apprendre le son", "tutoriel des sons",
                            "tutoriel son", "tuto des sons", "tuto son", "tuto sons", "tutoriel", "tuto"],
            examples=["apprends-moi les sons", "tutoriel des sons", "fais-moi écouter les sons un par un"]),
    Command("sons", "le son de chaque événement : l'écouter, et choisir son et phrase, son seul, ou rien",
            None, keywords=["sons", "les sons", "reglage des sons", "regler les sons", "sons des evenements",
                            "ecouter les sons", "sons de notification"],
            examples=["règle les sons des alertes", "fais-moi écouter les sons"]),
    Command("volume", "volume général, puis un curseur par veille et par sorte de son : annonces, trains, "
            "ambiance de la gare, écoutes radio... ; volume 60 règle directement le général", None,
            keywords=["volume", "volumes", "son plus fort", "son moins fort", "monte le son", "baisse le son",
                      "niveau sonore", "curseurs de volume", "volume general"],
            examples=["règle les volumes", "mets le volume à 60"]),
    Command("antenne", "mesure l'antenne : stations FM, bruit de fond, écart au seuil de détection par bande, capteurs entendus, puis audiographe ; antenne détail ; antenne son pour réentendre l audiographe", None,
            keywords=["antenne", "mesure antenne", "mesurer l'antenne", "test antenne", "tester l'antenne", "cable antenne",
                      "puissance du signal", "niveau du signal", "force du signal", "audiographe", "antenne son"],
            examples=["est-ce que l'antenne est bien branchée", "quelle est la puissance du signal reçu"]),
    Command("phrases", "phrases gardées en cache par les voix : écouter, recalculer une phrase bafouillée, valider, effacer", None,
            keywords=["phrases", "phrases en cache", "cache des voix", "cache de voix", "cache voix", "phrases des voix",
                      "phrases bafouillees", "recalculer une phrase", "valider les phrases"],
            examples=["une phrase est mal prononcée", "Piper bafouille sur une phrase", "nettoyer le cache des voix"]),
    Command("voix locales", "voix neuronales locales Piper : écouter, installer depuis le dépôt officiel, dans toutes les langues", None,
            keywords=["voix locales", "voix locale", "voix piper", "piper", "telecharger des voix", "telecharger une voix",
                      "installer une voix", "installer des voix", "autres voix", "voix neuronales"],
            examples=["je veux d'autres voix", "ajouter une voix dans une autre langue"]),
    Command("voix", "choisir la voix de la console et son débit, ou la voix locale espeak", None,
            keywords=["voix", "changer de voix", "change de voix", "choisir la voix", "debit de la voix"],
            examples=["parle plus vite", "change ta voix"]),
    Command("voitures", "statistiques de la rue par les capteurs de pneus ; nomme la voiture 3 facteur", None,
            keywords=["voitures", "voiture", "vehicules", "vehicule", "rue", "circulation", "trafic de la rue"],
            examples=["combien de voitures passent", "qui passe dans la rue"]),
    # Apres "voitures" : "trafic de la rue" reste aux voitures, "trafic" seul aux routes.
    Command("trafic", "incidents et travaux sur les grandes routes autour de chez toi, d'après Bison Futé", None,
            keywords=["trafic", "routes", "route", "bouchons", "bouchon", "bison fute", "accident de la route"],
            examples=["comment ça roule", "y a-t-il des bouchons"]),
    Command("environnement", "rivières, séismes, qualité de l'air et pollens, météo des aéroports", None,
            keywords=["environnement", "rivieres", "riviere", "seisme", "seismes", "tremblement de terre",
                      "qualite de l air", "pollution", "pollen", "pollens", "meteo des aeroports", "crue de la riviere"],
            examples=["la rivière monte-t-elle", "y a-t-il eu un séisme", "comment est l'air"]),
    # Avant "satellites" : "machine compagne" ne doit pas lister les passages.
    Command("juge", "trier les décodeurs à la volée sur ce qu'on entend : « juge » ou « juge suivant » "
            "passe au décodeur suivant ; « juge ok » confirme que c'est le bon",
            None, keywords=["juge", "juges", "juge suivant", "juge ok", "juge okay", "juge bon", "décodeur suivant"],
            examples=["juge", "juge suivant", "juge ok", "décodeur suivant", "change de juge"]),
    # Avant "dmr" : "exploration dmr" contient le mot-cle "dmr" (2026-10-17).
    Command("exploration dmr", "écouter les fréquences DMR groupe d'appel par groupe d'appel, ignorer ou mettre en favori, "
            "chercher de nouvelles fréquences", None,
            keywords=["exploration dmr", "explorer dmr", "explore dmr", "explorer le dmr", "dmr exploration",
                      "dmr explore", "cibler dmr", "cibler le dmr", "ciblage dmr", "groupes dmr"],
            examples=["je veux choisir les groupes d'appel", "explorer la bande numérique"]),
    Command("restreint", "mode restreint : cocher les bandes que le scanner a le droit d'écouter ; restreint arrêt pour tout reprendre",
            None, keywords=["mode restreint", "restreint", "restreindre", "restriction", "cocher les bandes"],
            examples=["n'écoute que certaines bandes", "je veux choisir les bandes à écouter"]),
    Command("dmr", "ce que le scanner sait des fréquences DMR captées : relais, groupes d'appel, émetteurs ; dmr explications pour les mots",
            None, keywords=["dmr", "radio numerique pro", "groupes d appel", "groupe d appel", "talkgroup", "talkgroups",
                            "frequences dmr"],
            examples=["qu'est-ce qu'on a capté en numérique", "les relais numériques"]),
    # Registre TETRA (lib/tetra.py, 2026-10-27), comme "dmr".
    Command("tetra", "ce que le scanner sait des réseaux TETRA captés : postes actifs, messages en clair, appels, porteuses",
            None, keywords=["tetra", "teta", "reseau tetra", "reseaux tetra", "radio des bus", "postes tetra"],
            examples=["qu'est-ce qu'on a capté en tetra", "les réseaux tetra"]),
    # Transports en commun de la commune choisie (lib/transports.py, 2026-10-07).
    Command("bus", "comment roulent les bus de ta ville : retards en temps réel, lignes à la traîne, perturbations", None,
            keywords=["bus", "les bus", "autobus", "trolleybus", "comment roulent les bus", "comment vont les bus",
                      "comment ca roule pour les bus", "comment ca roule en bus", "etat des bus", "trafic des bus",
                      "les bus roulent", "infos bus"],
            examples=["les bus ont du retard ?", "comment ça va côté bus", "est-ce que les bus roulent bien"]),
    Command("tram", "comment roule le tram de ta ville", None,
            keywords=["tram", "trams", "tramway", "tramways", "le tram", "comment roule le tram", "comment va le tram",
                      "comment ca roule pour le tram", "etat du tram", "infos tram"],
            examples=["le tram a du retard ?", "comment ça va côté tram"]),
    Command("métro", "comment roule le métro de ta ville, s'il y en a un", None,
            keywords=["metro", "le metro", "comment roule le metro", "comment va le metro", "etat du metro", "infos metro"],
            examples=["le métro a du retard ?"]),
    Command("transports", "transports en commun de ta ville : toutes les familles ; transports réseaux pour choisir les réseaux",
            None, keywords=["transports", "transport", "transports en commun", "transport en commun", "reseaux de transport",
                            "reseau de transport", "funiculaire", "telepherique", "telecabine", "navette fluviale",
                            "comment ca roule en ville"],
            examples=["comment circulent les transports", "les transports publics"]),
    # Avant "appairage" (machine compagne) et "capteurs" (releves entendus) : capteurs a soi.
    Command("mes capteurs", "tes capteurs sans fil : sondes de sol, pluviomètre, station météo ; nouveau capteur pour en appairer un", None,
            keywords=["mes capteurs", "mes sondes", "nouveau capteur", "nouvelle sonde", "appairer un capteur",
                      "appairer une sonde", "appairage capteur", "appairage sonde", "appairage d un capteur",
                      "appairage d une sonde", "ajouter un capteur", "ajouter une sonde"],
            examples=["j'ai acheté une sonde", "brancher mon pluviomètre", "mes appareils de mesure"]),
    Command("appairage", "relier une machine compagne qui calcule à la place du Pi, les images de satellites météo par exemple", None,
            keywords=["appairage", "appairer", "jumelage", "jumeler", "nouvelle machine compagne", "ajoute une machine"],
            examples=["appaire mon mac", "ajoute une machine compagne"]),
    Command("compagnon", "état des machines compagnes : présentes, ce qu'elles savent faire, travaux en attente", None,
            keywords=["compagnon", "compagne", "compagnons", "machine compagne", "machines compagnes", "pont", "bridge"],
            examples=["le mac est-il là", "état du pont"]),
    Command("stations", "stations probables : fréquences notées parfaites, recentrées et revisitées, avec leurs habitudes", None,
            keywords=["stations probables", "station probable", "mes stations", "stations pirates", "pirates"],
            examples=["quelles stations tu surveilles", "les pirates"]),
    Command("satellites", "prochains passages de satellites écoutés : météo, station spatiale, radioamateurs", None,
            keywords=["satellites", "satellite", "passages", "station spatiale", "iss", "meteor"],
            examples=["quand passe l'iss", "prochain satellite"]),
    # Avant "capteurs" : la memoire des capteurs deja entendus (lib/capteurs_connus.py).
    Command("capteurs connus", "capteurs sans fil déjà entendus : combien, depuis quand, combien de fois, les tiens",
            lambda: __import__("capteurs_connus").summary(),
            keywords=["capteurs connus", "capteurs deja vus", "capteurs deja entendus", "memoire des capteurs",
                      "combien de capteurs", "historique des capteurs"],
            examples=["quels capteurs tu connais", "depuis quand tu entends les capteurs"]),
    Command("capteurs", "derniers relevés des capteurs sans fil et stations météo radioamateur captés", capteurs,
            keywords=["capteurs", "capteur", "sondes", "stations meteo", "thermometres"],
            examples=["quelle température chez les voisins", "les relevés des capteurs"]),
    Command("flash", "flash d'info complet de la ville choisie : presse, météo mesurée autour de toi, environnement, routes, trains et bus ; flash détails pour les sujets par rubrique", None,
            keywords=["flash", "flash info", "flashinfo", "flash infos", "info locale", "infos locales",
                      "information locale", "informations locales", "quoi de neuf", "actualites",
                      "actualite", "actus", "actu", "nouvelles", "infos", "info"],
            examples=["qu'est-ce qui se passe dans le coin", "les nouvelles du jour", "donne moi les infos"]),
    Command("tableau", "départs et arrivées d'une gare ou d'un aéroport ; tableau gare ou tableau aéroport", None,
            keywords=["tableau", "tableaux", "departs", "arrivees", "horaires"],
            examples=["prochains trains", "prochains vols", "tableau des départs"]),
    Command("ville", "choisir sa commune : météo et position de référence ; ville suivi du nom", None,
            keywords=["ville", "commune", "ma ville", "ma commune"],
            examples=["change de ville", "je suis à"]),
    Command("gare", "choisir la gare suivie, dans une arborescence alphabétique", None,
            keywords=["gare", "gares", "choisir la gare"],
            examples=["change de gare", "quelle gare surveiller"]),
    Command("aéroport", "choisir l'aéroport proche ou le grand aéroport surveillés", None,
            keywords=["aeroport", "aeroports", "aerodrome", "aerodromes"],
            examples=["change d'aéroport", "quel aéroport surveiller"]),
    Command("réglages", "réglages en place et clés API présentes ou manquantes", None,
            keywords=["reglages", "cles", "cle", "cles api", "parametres", "configuration"],
            examples=["qu'est-ce qui est réglé", "où en sont les clés"]),
    Command("décris", "photo de l'imprimante décrite par une intelligence artificielle", None,
            keywords=["decris", "decrire", "description", "qu est ce que tu vois", "que vois tu",
                      "tu vois quoi", "photo", "camera", "webcam"],
            examples=["décris moi l'imprimante", "montre moi ce qui se passe", "qu'est-ce qu'il y a dans le caisson"],
            busy=lambda: __import__("vision").busy()),
    # Pilotage de l'imprimante (console/imprimante_cmd.py, 2026-10-11) : avant
    # "imprimante", qui garde "impression" seul. "pause" et "reprends" seuls
    # restent aux veilles et au scanner : ici, toujours suivis de l'impression.
    Command("annule impression", "annule l'impression en cours, après confirmation",
            lambda announce: __import__("imprimante_cmd").annuler(announce),
            keywords=["annule l impression", "annuler l impression", "annule impression", "arrete l impression",
                      "stoppe l impression", "annulation de l impression"],
            examples=["arrête d'imprimer", "annule le print"],
            confirm="Annuler l'impression en cours ? La pièce sera perdue. Tape oui puis entrée. "
                    "Toute autre réponse annule."),
    Command("pause impression", "met l'impression en pause", None,
            keywords=["pause impression", "pause l impression", "pause de l impression", "mets en pause",
                      "met en pause", "mets l impression en pause", "suspends l impression"],
            examples=["mets l'impression en pause"]),
    Command("reprends impression", "reprend l'impression en pause", None,
            keywords=["reprends l impression", "reprendre l impression", "reprends impression",
                      "reprise de l impression", "relance l impression", "continue l impression"],
            examples=["reprends l'impression"]),
    Command("refroidis", "coupe la chauffe de la buse et du plateau", None,
            keywords=["refroidis", "refroidir", "refroidissement", "coupe la chauffe", "arrete la chauffe",
                      "eteins la chauffe", "stop chauffe"],
            examples=["arrête de chauffer"]),
    # Dialogue guide (console/imprimante_calibrage.py, 2026-10-14) : verifications,
    # test des capteurs en direct, origines, reglage du plateau, hauteur de buse.
    Command("calibre plateau", "calibrage guidé de l'imprimante : capteurs, origines, plateau, hauteur de la buse", None,
            keywords=["calibre plateau", "calibrer plateau", "calibrage imprimante", "calibre imprimante",
                      "calibrer imprimante", "calibrage plateau", "procedure origine", "procédure origine",
                      "nivellement", "niveler", "nivelle", "niveler plateau", "nivelle plateau",
                      "calibrage de l imprimante", "calibre l imprimante", "calibrer l imprimante",
                      "calibrage"],
            examples=["calibre plateau", "calibrage de l'imprimante", "niveler le plateau",
                      "remettre la tête à l'origine"]),
    # Dialogue guide (console/imprimante_nettoyage.py, 2026-10-16) : catalogue de
    # methodes (plateau, buse a chaud, purge, tirage a froid), materiel decrit.
    Command("nettoie imprimante", "nettoyage guidé : plateau, buse à chaud, purge, tirage à froid", None,
            keywords=["nettoie imprimante", "nettoyer imprimante", "nettoyage imprimante", "nettoyage",
                      "nettoie buse", "nettoyer buse", "nettoyage buse", "nettoie plateau", "nettoyer plateau",
                      "nettoyage plateau", "nettoyage de l imprimante", "nettoie l imprimante",
                      "tirage a froid", "tirage à froid", "cold pull", "purge buse",
                      "nettoyer le plateau", "nettoie le plateau", "nettoyage du plateau", "nettoyer la buse",
                      "nettoie la buse", "nettoyage de la buse", "nettoyer l imprimante", "purger la buse",
                      "purge la buse"],
            examples=["nettoie l'imprimante", "nettoyage de la buse", "nettoyer le plateau"]),
    # Dialogue guide (console/imprimante_chargement.py, 2026-10-16) : materiau,
    # retrait de l'ancien filament, chargement, ecoulement, debit facultatif.
    Command("charge filament", "chargement guidé du filament avant une impression : matériau, écoulement, débit", None,
            keywords=["charge filament", "charger filament", "charge le filament", "charger le filament",
                      "chargement filament", "chargement du filament", "change filament", "changer filament",
                      "change de filament", "changer de filament", "changement de filament", "prepare l imprimante",
                      "preparer l imprimante", "prepare imprimante", "preparer imprimante", "mettre du filament",
                      "mets du filament", "insere le filament", "inserer le filament"],
            examples=["charge le filament", "prépare l'imprimante", "change de filament"]),
    Command("reconnecte imprimante", "rétabli la liaison USB OctoPrint après un arrêt d'urgence", None,
            keywords=["reconnecte imprimante", "reconnecter imprimante", "relie imprimante", "usb imprimante"],
            examples=["reconnecte imprimante", "relie l'imprimante"]),
    Command("filament prêt", "après une pause filament : confirmer le rechargement et reprendre", None,
            keywords=["filament pret", "filament prêt", "fil pret", "fil prêt", "reprends filament",
                      "reprise filament", "filament recharge", "filament rechargé"],
            examples=["filament prêt", "fil prêt", "reprends filament"]),
    Command("imprimante chargée", "déclarer le filament en place : P.L.A., A.B.S., P.E.T.G. ou T.P.U.", None,
            keywords=["imprimante chargee", "imprimante chargée", "filament charge", "filament chargé",
                      "chargee en pla", "chargée en pla", "materiau charge", "matériau chargé",
                      "filament pla", "filament abs", "filament petg", "filament tpu"],
            examples=["imprimante chargée en pla", "filament chargé abs", "matériau PETG"]),
    Command("éjecte filament", "retire le filament même buse froide (utile si le fil a cassé dans l'extrudeur)", None,
            keywords=["ejecte filament", "éjecte filament", "ejecter filament", "retire filament",
                      "retirer filament", "decharge filament", "décharge filament", "sort filament"],
            examples=["éjecte le filament", "retire le filament à froid"]),
    Command("fichiers préparés", "explorer les fichiers pièces téléchargés : descriptions Gemini, ouvrir, imprimer, supprimer", None,
            keywords=["fichiers prepares", "fichiers préparés", "explorateur pieces", "explorateur pièces",
                      "pieces telechargees", "pièces téléchargées", "mes pieces", "mes pièces",
                      "fichiers pieces", "fichiers pièces", "stl telecharges", "préparations impression"],
            examples=["mes fichiers préparés", "ouvre les pièces téléchargées", "explorateur de pièces"]),

    Command("chauffe", "chauffe l'imprimante : chauffe pla, chauffe abs, chauffe buse 200, chauffe plateau 60", None,
            keywords=["chauffe", "chauffer", "prechauffe", "prechauffer", "prechauffage"],
            examples=["préchauffe pour le pla"]),
    Command("arrêt d'urgence", "arrêt d'urgence de l'imprimante : tout se fige, il faudra la redémarrer", None,
            keywords=["arret d urgence", "arret urgence", "urgence"]),
    Command("pilote imprimante", "piloter l'imprimante au clavier : axes, origine, position, filament, réglage du plateau",
            None,
            keywords=["pilote imprimante", "pilote l imprimante", "piloter l imprimante", "controle imprimante",
                      "controle l imprimante", "bouge les axes", "bouger les axes", "deplace la tete", "axes"],
            examples=["je veux bouger la tête de l'imprimante", "réglage du plateau", "mise à niveau du plateau"]),
    Command("imprime", "choisir un fichier et lancer une impression, après confirmation", None,
            keywords=["imprime", "imprimer", "lance une impression", "lancer une impression",
                      "fichiers d impression", "fichiers a imprimer", "fichiers"],
            examples=["qu'est-ce que je peux imprimer", "liste des fichiers"]),
    Command("ventilateur", "ventilateur de la pièce : ventilateur, ventilateur 50, ventilateur arrêt", None,
            keywords=["ventilateur", "ventilo", "ventilation"],
            examples=["coupe le ventilo"]),
    Command("moteurs", "coupe les moteurs de l'imprimante : les axes se bougent à la main", None,
            keywords=["coupe les moteurs", "moteurs", "moteur", "libere les axes"]),
    Command("connexion imprimante", "connecte ou déconnecte l'imprimante d'OctoPrint", None,
            keywords=["connecte l imprimante", "connecte imprimante", "connecter l imprimante",
                      "reconnecte l imprimante", "deconnecte l imprimante", "deconnecte imprimante",
                      "deconnecter l imprimante"]),
    Command("vitesse d'impression", "vitesse de l'impression en cours, en pour cent : vitesse d'impression 120", None,
            keywords=["vitesse d impression", "vitesse impression", "vitesse de l impression"]),
    Command("débit", "débit de filament en pour cent : débit 95", None,
            keywords=["debit", "debit de filament", "flux de filament"]),
    Command("imprimante", "état de l'impression et températures", printer_status,
            keywords=["imprimante", "impression", "octoprint", "buse", "plateau"],
            examples=["elle en est où l'impression", "état de l'imprimante",
                      "ça imprime", "où en est le print"]),
    Command("pièces", "chercher un fichier à imprimer sur Printables : liste, fiche, Valider pour une description fine", None,
            keywords=["pieces", "piece", "cherche piece", "chercher piece", "cherche pieces",
                      "modele imprimable", "modeles imprimables", "fichier a imprimer",
                      "fichiers a imprimer", "printables", "recherche impression",
                      "fichier 3d", "modele 3d", "stl"],
            examples=["pièces station spatiale", "cherche pièce tiroir", "modèle imprimable crochet",
                      "pièces ISS", "pièce crochet"]),

    # Apres "décris" (qui garde "camera"/"webcam") et "imprimante".
    Command("usb", "état des appareils USB : clavier, son, radio, caméra, imprimante, et réparations automatiques",
            lambda: __import__("usb_cmd").usb_status(),
            keywords=["usb", "peripheriques", "peripherique", "appareils branches", "branchements"],
            examples=["est-ce que tout est bien branché", "état des périphériques"]),
    Command("température", "température du Pi", pi_temp,
            keywords=["temperature du pi", "temperature pi", "chaleur", "temp"],
            examples=["quelle température fait le pi", "le pi chauffe",
                      "température processeur"]),
    Command("mise à jour", "mise à jour complète du système et nettoyage, après confirmation",
            mise_a_jour,
            keywords=["mise a jour", "mises a jour", "mets a jour", "mettre a jour", "update", "upgrade"],
            confirm="Lancer la mise à jour complète du système ? Évite pendant une impression. "
                    "Ça peut durer un quart d'heure, je te préviens à chaque étape. "
                    "Tape oui puis entrée. Toute autre réponse annule.",
            busy=lambda: __import__("maj").busy()),
    Command("santé", "bilan de santé du Pi : processeur, alimentation, mémoire, disques, veilles", sante,
            keywords=["sante", "comment tu vas", "comment vas tu", "comment va le pi",
                      "ca va", "tu vas bien", "etat du pi", "bilan de sante", "diagnostic"],
            examples=["comment tu vas", "comment va le pi", "tout va bien",
                      "le pi va bien", "donne moi des nouvelles du pi"]),
    Command("rire", "un son de radiono au hasard", rire,
            keywords=["rire", "rigoler", "marrer", "blague", "drole", "radiono"],
            examples=["fais moi rire", "fait moi rire", "raconte une blague",
                      "un truc drôle", "amuse moi"]),
    Command("heure", "heure et date", heure,
            keywords=["heure", "date", "quel jour"],
            examples=["il est quelle heure", "on est quel jour"]),
    # Avant "radio" : "scan verrouiller" contient le mot-cle "scan".
    Command("parasite", "la dernière réception radio était un parasite : l'effacer et l'apprendre", parasite,
            keywords=["parasite", "parasites", "faux positif", "c est du bruit", "que du bruit",
                      "bruit parasite", "juste du bruit"],
            examples=["c'était du bruit", "c'était un parasite", "fausse alerte radio"]),
    Command("veilles", "état des veilles ; arrête ou reprends suivi d'un nom de veille", None,
            keywords=["veilles", "veille", "liste des veilles", "services", "status", "statut",
                      "etat des veilles"],
            examples=["quelles veilles tournent", "état des veilles"]),
    Command("bilan", "bilan des notes radio par bande, avec propositions de réglages", None,
            keywords=["bilan", "bilan des notes", "notes radio", "statistiques radio"],
            examples=["qu'est-ce que donnent mes notes", "résumé des notes"]),
    Command("archives", "écouter les extraits gardés, sans rien supprimer ; archives messages, capteurs, satellites, données", None,
            keywords=["archives", "archive", "extraits", "extraits interessants", "mes extraits"],
            examples=["écouter les archives", "les extraits gardés", "ce que j'ai gardé"]),
    Command("journal", "écouter et trier les enregistrements radio : garder ou rejeter ; journal alarmes, messages, "
            "capteurs, satellites ou données pour les autres", None,
            keywords=["journal", "journal radio", "journal alarmes", "journal des alarmes", "enregistrements", "enregistrement", "captures",
                      "log", "logs radio"],
            examples=["écouter les enregistrements", "ce que la radio a capté", "les réceptions de la nuit"]),
    Command("compris", "arrête les alarmes qui sonnent en boucle, et fait taire les alertes en cours pour tes "
            "plantes (gel, vent, chaleur, soif)", None,
            keywords=["compris", "ok", "okay", "bien recu", "bien pris", "c est note", "d accord", "j ai compris"],
            examples=["c'est bon j'ai compris", "message reçu"]),
    Command("plantes", "tes plantes : les ajouter, noter leur stade, l'arrosage, si elles sont rentrées ; "
            "plante basilic arrosée, par exemple", None,
            keywords=["plantes", "plante", "mes plantes", "botanique", "jardinage", "plante photo", "plantes photo",
                      "plante photos", "photos des plantes", "photo de plante"],
            examples=["ajouter une plante", "j'ai arrosé le basilic", "mes plantes vertes"]),
    Command("liste noire", "stations DAB interdites : la parcourir et en effacer, ou liste noire éditer pour en ajouter", None,
            keywords=["liste noire", "blacklist", "black list", "stations interdites", "radios interdites",
                      "liste des interdites", "interdites"],
            examples=["les stations que j'ai bannies", "ajouter une radio à la liste noire", "débannir une station"]),
    Command("filtres", "parasites appris : les parcourir et activer ou désactiver chacun, ou en supprimer ; utile si une bande est bloquée par trop de filtres", None,
            keywords=["filtres", "filtre", "filtres parasites", "gérer les parasites",
                      "gérer les filtres", "activer un filtre", "désactiver un filtre", "débloquer une bande"],
            examples=["montre-moi les filtres parasites", "désactiver le filtre sur le 446", "débloquer la bande 446"]),
    Command("rejets", "journal des candidats écartés en silence : les écouter et trier, n si c'était un vrai signal, r si c'était du bruit", None,
            keywords=["rejets", "journal des rejets", "les rejets", "trier les rejets", "candidats rejetés",
                      "faux rejets", "rejetés"],
            examples=["ouvre le journal des rejets", "écouter ce qui a été rejeté", "trier les rejets"]),
    Command("proposition", "journal des propositions : les réglages suggérés par l'analyse du Mac mini, à écouter et confirmer (c) ou refuser (r) le matin", None,
            keywords=["proposition", "propositions", "journal proposition", "journal des propositions",
                      "suggestions de réglages", "propositions du matin"],
            examples=["journal proposition", "voir les propositions de réglage", "qu'est-ce que le mac propose"]),
    # Avant "attention" (dont un mot-cle est "priorites") : centres d'interet de l'utilisateur.
    Command("intérêts", "tes centres d'intérêt : ce qui doit attirer le scanner en priorité ; intérêt avions fort, par exemple", None,
            keywords=["interets", "interet", "centres d interet", "centre d interet", "mes interets"],
            examples=["je m'intéresse surtout aux avions", "moins de radio fm", "ce qui m'intéresse"]),
    Command("notifications", "combien de nouvelles entrées, pas encore écoutées, dans chaque journal", None,
            keywords=["notification", "notifications", "nouveautes", "du nouveau"],
            examples=["qu'est-ce qu'il y a de nouveau", "des nouvelles entrées ?"]),
    Command("attention", "où le scanner concentre son écoute, et pourquoi", attention_status,
            keywords=["attention", "piqure", "piqures", "priorites", "ou ecoutes tu", "ou tu ecoutes"],
            examples=["qu'est-ce qui t'intéresse en ce moment", "où tu écoutes le plus"]),
    Command("manuel", "mode manuel : choisir soi-même la plage et la fréquence aux flèches", None,
            keywords=["manuel", "manuelle", "mode manuel", "reglage manuel", "accord manuel"],
            examples=["je veux régler moi-même", "passe en manuel", "je choisis la fréquence"]),
    Command("telegram", "canaux Telegram publics suivis par la veille terrain : s'abonner, suggestions pour la ville", None,
            keywords=["telegram", "telegramme", "canaux telegram", "canal telegram", "abonnements"],
            examples=["je veux suivre un canal", "ajouter un canal"]),
    Command("terrain", "derniers messages de la veille terrain : sites militants, Mastodon et canaux Telegram publics",
            terrain_cmd, keywords=["terrain", "veille terrain", "infos terrain", "ca se passe ou", "quoi de neuf dehors"],
            examples=["qu'est-ce qui se passe dehors", "les dernières infos de la manif"]),
    Command("accroche", "le scanner se fixe sur la fréquence de la dernière réception", accroche,
            keywords=["accroche", "accrocher", "verrouille", "verrouiller", "verrouillage",
                      "scan verrouiller", "bloque le scan", "reste dessus", "reste sur cette frequence"],
            examples=["fixe toi sur cette fréquence", "garde cette fréquence", "reste sur ce canal"]),
    Command("sécurité", "dernier contrôle de sécurité du Pi : mises à jour système en attente et failles des "
            "paquets Python ; sécurité pip met ces paquets à jour, mise à jour applique le reste", securite,
            keywords=["securite", "controle de securite", "failles", "vulnerabilites", "alertes de securite",
                      "securite pip"],
            examples=["y a-t-il des failles de sécurité", "le Pi est-il à jour"]),
    Command("relais", "les relais radioamateurs appris par l'APRS : fréquence, tonalité, distance, "
            "et combien de fois on les entend vraiment", relais_bilan,
            keywords=["relais", "les relais", "relais radioamateurs", "repeteurs", "repeaters", "relais appris",
                      "relais audibles", "tonalite d acces"],
            examples=["quels relais j'entends", "les relais du coin"]),
    Command("zapper", "le scanner arrête l'écoute en cours et passe à la suivante : bande, fenêtre ou appel DMR",
            zappe, keywords=["zapper", "zappe", "zap", "suivant", "passe", "passe a la suite", "change de cible",
                             "ecoute suivante", "appel suivant", "bande suivante"],
            examples=["passe à autre chose", "change de fréquence"]),
    Command("ignorer", "mettre de côté ce que l'on entend : la fréquence, ou en DMR le groupe ou l'appelant, "
            "de 5 minutes à toujours ; ignorer liste ; ignorer effacer", None,
            keywords=["ignorer", "ignore", "ignores", "ignorees", "mets de cote", "liste des ignores",
                      "ne plus ignorer", "blacklist", "masquer cette frequence"],
            examples=["ignore cette fréquence pendant une heure", "ignore ce groupe"]),
    Command("décroche", "le scanner reprend son balayage normal", decroche,
            keywords=["decroche", "decrocher", "deverrouille", "deverrouiller", "scan reprise",
                      "reprend", "reprends", "reprise du scan", "relache"],
            examples=["reprends le balayage", "lâche cette fréquence", "reprends le scan"]),
    Command("radio", "scan de la bande FM", radio_scan,
            keywords=["radio", "scan", "fm"],
            examples=["scanne la bande fm", "cherche une station"]),
    # Apres "simulation" : "simulation meteo" reste une simulation.
    # Avant "meteo" : "orage au loin" est une observation, "orage" seul une question.
    Command("observation", "dire au Pi le temps qu'il fait : il pleut fort, tonnerre au loin, ou le dialogue complet", None,
            keywords=["observation", "observer", "il pleut", "pleut fort", "tonnerre", "gronde", "il grele",
                      "il neige", "orage au loin", "orage proche", "orage tout pres", "il fait beau", "bruine",
                      "pleuviote", "vent fort", "tempete", "ca s arrete", "s est arrete de pleuvoir"],
            examples=["j'entends le tonnerre", "il commence à pleuvoir", "ça tombe fort"]),
    Command("météo", "bulletin météo local, signes avant-coureurs, et balayage des stations météo voisines", None,
            keywords=["meteo", "quel temps", "temps qu il fait", "previsions", "prevision", "orage", "orages",
                      "pluie", "il va pleuvoir"],
            examples=["quel temps fait il", "va-t-il pleuvoir", "quelle météo demain"]),
    Command("touches", "repérage des touches du clavier", None,
            keywords=["touches", "touche", "clavier", "reperage"],
            examples=["test du clavier", "trouver les touches"]),
    Command("répète", "répète la dernière réponse", None,
            keywords=["repete", "repeter", "encore"],
            examples=["tu peux répéter", "redis"]),
]
