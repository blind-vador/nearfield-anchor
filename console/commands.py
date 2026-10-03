"""Registre des commandes de la console vocale.

Pour ajouter une commande : ecrire un handler qui retourne la phrase a
dire, puis l'ajouter a COMMANDS avec des mots-cles (correspondance
certaine) et des phrases d'exemple (repli flou, voir matcher.py).
"""
import csv
import datetime
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import requests

sys.path.insert(0, "/home/vador/autopilot/pi_console")
import reglages  # noqa: E402
from phrases import Etiquettes, _code, dire, dire_ou, nombre  # noqa: E402

OCTOPRINT_URL = "http://127.0.0.1:5000"
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
    api_key = reglages.cle("octoprint")
    if not api_key:
        return (dire("commandes.cle_api_octoprint_absente"))
    headers = {"X-Api-Key": api_key}
    try:
        printer = requests.get(f"{OCTOPRINT_URL}/api/printer", headers=headers, timeout=5)
        if printer.status_code == 409:
            return dire("commandes.imprimante_connectee_octoprint")
        if printer.status_code in (401, 403):
            return dire("commandes.octoprint_refuse_cle_api")
        printer.raise_for_status()
        job = requests.get(f"{OCTOPRINT_URL}/api/job", headers=headers, timeout=5).json()
    except requests.RequestException:
        return dire("commandes.octoprint_repond")

    data = printer.json()
    temps = data.get("temperature", {})
    parts = [dire("commandes.imprimante", get=data.get('state', {}).get('text') or dire("commandes.etat_inconnu"))]
    progress = (job.get("progress") or {})
    if progress.get("completion") is not None and job.get("state", "").startswith("Printing"):
        parts.append(dire("commandes.impression_cent", round=round(progress['completion'])))
        left = progress.get("printTimeLeft")
        if left:
            h, m = divmod(int(left) // 60, 60)
            parts.append(dire("commandes.reste_heures_minutes", h=h, m=m) if h else dire("commandes.reste_minutes", m=m))
    for key, label in (("tool0", "Buse"), ("bed", "Plateau")):
        t = temps.get(key)
        if t and t.get("actual") is not None:
            parts.append(dire("commandes.degres_cible", label=label, round=round(t['actual']), round2=round(t.get('target') or 0)))
    return " ".join(parts)


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


RADIONO_SCRIPT = "/home/vador/autopilot/radiono.py"
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
    "stas-watch": "bus et tram", "log-ship": "copie des journaux",
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


def accroche():
    import noise_signatures as ns
    locked = ns.lock_request()
    if locked:
        return dire("commandes.scanner_deja_verrouille_tape", label=locked['label'])
    capture = ns.last_capture()
    if not capture:
        return dire("commandes.aucune_reception_recente_laquelle")
    ns.request_lock(capture)
    return (dire("commandes.verrouillage_demande_capte_scanner", label=capture['label'], v1=_ago(capture['time'])))


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
    Command("réglages audio", "réglages audio en place, bande par bande", None,
            keywords=["reglages audio", "reglage audio", "reglages du son"],
            examples=["quels réglages audio"]),
    Command("réapprendre", "réglages audio d'origine, empreintes de bruit et notes mises de côté : on réapprend de zéro", None,
            keywords=["reapprendre de zero", "reapprendre", "remise a zero des reglages", "reset des reglages"],
            examples=["recommence l'apprentissage à zéro"],
            confirm="Remettre les réglages audio d'origine et repartir de zéro ? Les notes et empreintes de bruit "
                    "seront mises de côté dans des archives. Tape oui puis entrée. Toute autre réponse annule."),
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
    Command("imprimante", "état de l'impression et températures", printer_status,
            keywords=["imprimante", "impression", "octoprint", "buse", "plateau"],
            examples=["elle en est où l'impression", "état de l'imprimante",
                      "ça imprime", "où en est le print"]),
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
    Command("archives", "écouter les extraits gardés, sans rien supprimer ; archives capteurs, satellites, données", None,
            keywords=["archives", "archive", "extraits", "extraits interessants", "mes extraits"],
            examples=["écouter les archives", "les extraits gardés", "ce que j'ai gardé"]),
    Command("journal", "écouter et trier les enregistrements radio : garder ou rejeter ; journal capteurs, "
            "satellites ou données pour les autres", None,
            keywords=["journal", "journal radio", "enregistrements", "enregistrement", "captures",
                      "log", "logs radio"],
            examples=["écouter les enregistrements", "ce que la radio a capté", "les réceptions de la nuit"]),
    Command("attention", "où le scanner concentre son écoute, et pourquoi", attention_status,
            keywords=["attention", "piqure", "piqures", "priorites", "ou ecoutes tu", "ou tu ecoutes"],
            examples=["qu'est-ce qui t'intéresse en ce moment", "où tu écoutes le plus"]),
    Command("manuel", "mode manuel : choisir soi-même la plage et la fréquence aux flèches", None,
            keywords=["manuel", "manuelle", "mode manuel", "reglage manuel", "accord manuel"],
            examples=["je veux régler moi-même", "passe en manuel", "je choisis la fréquence"]),
    Command("accroche", "le scanner se fixe sur la fréquence de la dernière réception", accroche,
            keywords=["accroche", "accrocher", "verrouille", "verrouiller", "verrouillage",
                      "scan verrouiller", "bloque le scan", "reste dessus", "reste sur cette frequence"],
            examples=["fixe toi sur cette fréquence", "garde cette fréquence", "reste sur ce canal"]),
    Command("décroche", "le scanner reprend son balayage normal", decroche,
            keywords=["decroche", "decrocher", "deverrouille", "deverrouiller", "scan reprise",
                      "reprend", "reprends", "reprise du scan", "relache"],
            examples=["reprends le balayage", "lâche cette fréquence", "reprends le scan"]),
    Command("radio", "scan de la bande FM", radio_scan,
            keywords=["radio", "scan", "fm"],
            examples=["scanne la bande fm", "cherche une station"]),
    Command("simulation", "simule les annonces : train, avion, météo, crue, bus, radio, disque, ou tout", None,
            keywords=["simulation", "simulations", "simule", "simuler"],
            examples=["simulation"]),
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
