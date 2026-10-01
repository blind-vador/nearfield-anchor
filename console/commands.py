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


def _fr_number(value, decimals=1):
    return f"{value:.{decimals}f}".replace(".", ",")


def pi_temp():
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                             text=True, timeout=5).stdout
        value = float(out.split("=")[1].split("'")[0])
    except Exception:
        return "Impossible de lire la température du Pi."
    return f"Le Pi est à {_fr_number(value)} degrés."


def heure():
    now = datetime.datetime.now()
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre"]
    minutes = f" {now.minute}" if now.minute else ""
    return (f"Il est {now.hour} heures{minutes}, "
            f"{jours[now.weekday()]} {now.day} {mois[now.month - 1]}.")


def printer_status():
    api_key = reglages.cle("octoprint")
    if not api_key:
        return ("Clé API OctoPrint absente : colle-la dans le fichier octoprint point txt "
                "du dossier cles des réglages.")
    headers = {"X-Api-Key": api_key}
    try:
        printer = requests.get(f"{OCTOPRINT_URL}/api/printer", headers=headers, timeout=5)
        if printer.status_code == 409:
            return "L'imprimante n'est pas connectée à OctoPrint."
        if printer.status_code in (401, 403):
            return "OctoPrint refuse la clé API. Vérifie-la."
        printer.raise_for_status()
        job = requests.get(f"{OCTOPRINT_URL}/api/job", headers=headers, timeout=5).json()
    except requests.RequestException:
        return "OctoPrint ne répond pas."

    data = printer.json()
    temps = data.get("temperature", {})
    parts = [f"Imprimante : {data.get('state', {}).get('text', 'état inconnu')}."]
    progress = (job.get("progress") or {})
    if progress.get("completion") is not None and job.get("state", "").startswith("Printing"):
        parts.append(f"Impression à {round(progress['completion'])} pour cent.")
        left = progress.get("printTimeLeft")
        if left:
            h, m = divmod(int(left) // 60, 60)
            parts.append(f"Il reste {h} heures {m} minutes." if h else f"Il reste {m} minutes.")
    for key, label in (("tool0", "Buse"), ("bed", "Plateau")):
        t = temps.get(key)
        if t and t.get("actual") is not None:
            parts.append(f"{label} {round(t['actual'])} degrés, cible {round(t.get('target') or 0)}.")
    return " ".join(parts)


def radio_scan():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        proc = subprocess.run(["rtl_power", "-f", FM_RANGE, "-i", "2", "-1", path],
                              capture_output=True, text=True, timeout=FM_SCAN_TIMEOUT_SEC)
        if "usb_claim_interface" in proc.stderr or "Failed to open" in proc.stderr:
            return "L'antenne est occupée par la veille radio. Réessaie plus tard."
        if "No supported devices" in proc.stderr:
            return "Aucune antenne radio détectée."
        best_freq, best_db = None, None
        with open(path, newline="") as f:
            for row in csv.reader(f):
                low, step = float(row[2]), float(row[4])
                for i, db in enumerate(row[6:]):
                    db = float(db)
                    if best_db is None or db > best_db:
                        best_freq, best_db = low + i * step, db
    except subprocess.TimeoutExpired:
        return "Le scan radio a pris trop de temps."
    except (OSError, ValueError, IndexError):
        return "Le scan radio a échoué."
    finally:
        os.unlink(path)
    if best_freq is None:
        return "Le scan radio n'a rien renvoyé."
    return f"Signal FM le plus fort : {_fr_number(best_freq / 1e6)} mégahertz."


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
        return "Le script radiono est introuvable."
    if _radiono_thread is not None and _radiono_thread.is_alive():
        return "Un son de radiono est déjà en cours."
    # Petit delai : laisser la console dire sa reponse avant le son.
    _radiono_thread = threading.Timer(3, _play_radiono)
    _radiono_thread.daemon = True
    _radiono_thread.start()
    return "Radiono, un son au hasard."


WATCHERS = {
    "nas-watch": "disques", "radio-watch": "radio", "meteo-watch": "météo",
    "crue-watch": "crues", "sncf-watch": "trains", "adsb-watch": "avions",
    "stas-watch": "bus et tram", "log-ship": "copie des journaux",
}
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
        return f"{days} jour{'s' if days > 1 else ''} et {hours} heures"
    if hours:
        return f"{hours} heures {mins}"
    return f"{mins} minutes"


def sante():
    """Bilan de sante : problemes d'abord, puis les mesures."""
    problems, details = [], []

    cpu = _cpu_percent()
    load1 = os.getloadavg()[0]
    details.append(f"Processeur à {round(cpu)} pour cent, charge moyenne {_fr_number(load1)}.")
    if cpu > 90:
        problems.append("processeur saturé")

    try:
        temp = float(_vcgencmd("measure_temp").split("=")[1].split("'")[0])
        details.append(f"Température {_fr_number(temp)} degrés.")
        if temp >= 80:
            problems.append("température critique, le Pi ralentit")
        elif temp >= 75:
            problems.append("température élevée")
    except (IndexError, ValueError, OSError):
        details.append("Température illisible.")

    # get_throttled : bits 0-3 = etat actuel, bits 16-19 = survenu depuis
    # le demarrage (0 sous-tension, 1 frequence bridee, 2 ralenti, 3 limite thermique).
    try:
        flags = int(_vcgencmd("get_throttled").split("=")[1], 16)
        volts = _vcgencmd("measure_volts", "core").split("=")[1].rstrip("V")
        if flags & 0x1:
            problems.append("sous-tension en ce moment, l'alimentation est trop faible")
            details.append("Alimentation : sous-tension en cours.")
        elif flags & 0x10000:
            problems.append("sous-tension survenue depuis le démarrage")
            details.append("Alimentation : correcte maintenant, mais une sous-tension a eu lieu depuis le démarrage.")
        else:
            details.append("Alimentation correcte, aucune sous-tension depuis le démarrage.")
        details.append(f"Tension du processeur {_fr_number(float(volts), 2)} volt.")
        if flags & 0x4:
            problems.append("processeur ralenti en ce moment")
        elif flags & 0x40000:
            details.append("Le processeur a été ralenti depuis le démarrage.")
    except (IndexError, ValueError, OSError):
        details.append("État de l'alimentation illisible.")

    with open("/proc/meminfo") as f:
        mem = {line.split(":")[0]: int(line.split()[1]) for line in f}
    mem_used = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])
    details.append(f"Mémoire utilisée {round(mem_used)} pour cent.")
    if mem_used > 90:
        problems.append("mémoire presque pleine")

    import shutil
    sd = shutil.disk_usage("/")
    sd_used = 100 * sd.used / sd.total
    details.append(f"Carte SD remplie à {round(sd_used)} pour cent.")
    if sd_used > 90:
        problems.append("carte SD presque pleine")

    mounted = sum(os.path.ismount(p) for p in NAS_MOUNTS)
    details.append(f"Disques du NAS : {mounted} sur {len(NAS_MOUNTS)} montés.")
    if mounted < len(NAS_MOUNTS):
        problems.append("disque du NAS manquant")

    states = subprocess.run(["systemctl", "is-active", *WATCHERS], capture_output=True,
                            text=True, timeout=10).stdout.split()
    down = [WATCHERS[u] for u, s in zip(WATCHERS, states) if s != "active"]
    if down:
        problems.append(f"veille arrêtée : {', '.join(down)}")
        details.append(f"{len(WATCHERS) - len(down)} veilles sur {len(WATCHERS)} actives.")
    else:
        details.append(f"Les {len(WATCHERS)} veilles sont actives.")

    details.append(f"Allumé depuis {_uptime_text()}.")
    summary = ("Le Pi va bien." if not problems
               else "Attention : " + ", ".join(problems) + ".")
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
        return "à l'instant"
    if minutes < 60:
        return f"il y a {minutes} minute{'s' if minutes > 1 else ''}"
    hours = minutes // 60
    return f"il y a {hours} heure{'s' if hours > 1 else ''}"


def parasite():
    """Derniere reception radio = faux positif : supprimee du journal,
    empreinte sonore apprise pour l'ignorer si elle revient (le scanner la
    compare en pre-ecoute silencieuse, voir lib/noise_signatures.py)."""
    import noise_signatures as ns
    capture = ns.last_capture()
    if not capture:
        return "Aucune réception radio récente à signaler."
    label = capture["label"]
    if capture.get("marked_noise"):
        return f"La dernière réception, {label}, est déjà marquée comme parasite."
    parts = [f"Réception {label}, {_ago(capture['time'])}, marquée comme parasite."]
    path = capture.get("log_path")
    if path and os.path.isfile(path):
        os.remove(path)
        parts.append("Supprimée du journal.")
    else:
        parts.append("Elle n'était pas, ou plus, dans le journal.")
    if capture.get("fingerprint"):
        ns.add_signature(capture)
        parts.append("J'ai retenu sa forme sonore : si elle revient sur cette fréquence, le scanner l'ignorera.")
    else:
        parts.append("Pas d'empreinte sonore pour cette réception, je ne peux pas l'apprendre.")
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
        parts.append(f"Station météo {vd.spell(call)}, {vd.where(st['lat'], st['lon'])} : "
                     f"{vd.fr_number(wx['temperature'])} degrés, il y a {age} minutes.")
    try:
        with open("/tmp/capteurs_releves.json", encoding="utf-8") as f:
            readings = json.load(f)
    except (OSError, ValueError):
        readings = {}
    for r in sorted(readings.values(), key=lambda r: -r["t"]):
        v = r.get("valeurs") or {}
        values = []
        if v.get("temperature_C") is not None:
            values.append(f"{vd.fr_number(v['temperature_C'], 1)} degrés")
        if v.get("humidity") is not None:
            values.append(f"humidité {v['humidity']} pour cent")
        age = int((time.time() - r["t"]) // 60)
        kinds = {"cuisson": "cuisson ou barbecue", "piscine": "piscine", "frigo": "réfrigérateur", "sol": "sonde de sol"}
        kind = f", {kinds[r['type']]}" if r.get("type") in kinds else ""
        parts.append(f"{r['nom']}{kind}" + (f" : {', '.join(values)}" if values else "") + f", il y a {age} minutes.")
    if not parts:
        return "Aucun capteur ni station météo capté ces dernières heures."
    return f"{len(parts)} source{'s' if len(parts) > 1 else ''}. " + " ".join(parts[:12])


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
            return f"Le véhicule numéro {vid} s'appelle maintenant {name}. Je dirai quand il passe."
        return f"Je ne connais pas de véhicule numéro {vid}."
    return voitures.summary()


def accroche():
    import noise_signatures as ns
    locked = ns.lock_request()
    if locked:
        return f"Le scanner est déjà verrouillé sur {locked['label']}. Tape décroche pour reprendre le balayage."
    capture = ns.last_capture()
    if not capture:
        return "Aucune réception récente sur laquelle se fixer."
    ns.request_lock(capture)
    return (f"Verrouillage demandé sur {capture['label']}, capté {_ago(capture['time'])}. "
            "Le scanner s'y fixe dans moins d'une demi-minute.")


def decroche():
    import noise_signatures as ns
    if not ns.lock_request() and not ns.manual_request():
        return "Le scanner n'est pas verrouillé, il balaye déjà."
    ns.request_unlock()
    return "Reprise du balayage demandée."


def help_text():
    lines = [f"{c.name} : {c.description}." for c in COMMANDS]
    return "Commandes disponibles. " + " ".join(lines)


def _power(action, done_text):
    def handler(announce):
        # Annonce AVANT d'agir : apres, le systeme coupe la voix en plein mot.
        announce(done_text)
        # sudo -n : echoue immediatement plutot que d'attendre un mot de
        # passe que personne ne peut taper sur cette console.
        proc = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", action],
                              capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            print(f"[console] systemctl {action} : {proc.stderr.strip()}", file=sys.stderr)
            return "Échec : la console n'a pas les droits pour le faire."
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
            _power("reboot", "Redémarrage du Pi. À tout de suite."),
            keywords=["redemarre", "redemarrer", "redemarrage", "reboot"],
            confirm="Confirmer le redémarrage du Pi ? Tape oui puis entrée. Toute autre réponse annule."),
    Command("éteindre", "éteint le Pi, après confirmation",
            _power("poweroff", "Extinction du Pi. Attends trente secondes avant de couper le courant."),
            keywords=["eteins", "eteindre", "extinction", "shutdown", "arrete le pi"],
            confirm="Confirmer l'extinction du Pi ? Il faudra couper et remettre le courant pour le rallumer. "
                    "Tape oui puis entrée. Toute autre réponse annule."),
    # Avant "aide" : "aide pratique" doit regler les conseils, pas lister les commandes.
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
    Command("capteurs", "derniers relevés des capteurs sans fil et stations météo radioamateur captés", capteurs,
            keywords=["capteurs", "capteur", "sondes", "stations meteo", "thermometres"],
            examples=["quelle température chez les voisins", "les relevés des capteurs"]),
    Command("flash", "flash d'info locale de la ville choisie ; flash détails pour les sujets par rubrique", None,
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
    Command("archives", "écouter les extraits radio gardés, sans rien supprimer", None,
            keywords=["archives", "archive", "extraits", "extraits interessants", "mes extraits"],
            examples=["écouter les archives", "les extraits gardés", "ce que j'ai gardé"]),
    Command("journal", "écouter et trier les enregistrements radio : garder ou rejeter", None,
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
