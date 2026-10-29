"""Veille des capteurs sans fil 433,92 et 868,3 MHz (rtl_433) : stations
meteo et thermometres, detecteurs de fumee, sonnettes, telecommandes...
tout ce qui emet en clair a portee du Pi.

Une frequence par passage, en alternance. Annonces :
- alarmes (fumee, eau, intrusion...) : a chaque evenement (anti-repetition
  ALARM_EVERY_SEC) ;
- sonnettes et boutons : a chaque appui (BUTTON_EVERY_SEC) ;
- capteur jamais entendu (memoire persistante, lib/capteurs_connus.py,
  2026-10-14 : plus de redecouverte a chaque redemarrage) : presente une
  fois, avec ses valeurs ;
- releves meteo (thermometres, stations) : plus dits un par un
  (2026-10-19, des centaines d'entrees dans le journal capteurs) : gardes
  pour le bulletin horaire (lib/bulletin_capteurs.py, journal "meteo") ;
  les thermometres d'interieur probables (lib/capteurs_interieurs.py) y
  sont a part. Capteurs atypiques (cuisson, piscine, frigo, sol...) :
  chaque releve avec une petite blague de Gemini ;
- capteurs de pneus (voitures qui passent) : jamais annonces, archives.
Les derniers releves sont publies (READINGS_PATH) pour la console et le
flash ; tout est archive.
"""
import chemins
import json
import os
import subprocess
import threading
import time
from datetime import datetime

import capteurs_connus as connus
import veille_donnees as vd
import voitures
from sound_paths import RADIO_SCANER_DIR
from phrases import Etiquettes, dire

FREQUENCIES = ["433.92M", "868.3M"]
SOUND = os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3")
import sons
VOITURE_SOUND = sons.chemin("voiture", SOUND)  # son propre (2026-10-23, sons.py)
NOUVEAU_SOUND = sons.chemin("nouveau_capteur", SOUND)
# Carte SD, pas /tmp (vide a chaque demarrage) : valeurs deja dites, anti-repetitions.
STATE_PATH = chemins.base("capteurs_watch_state.json")
READINGS_PATH = "/tmp/capteurs_releves.json"
ALARM_EVERY_SEC = 120
BUTTON_EVERY_SEC = 60
READINGS_KEEP_SEC = 6 * 3600

# Thermometres qui ne mesurent PAS l'air exterieur (2026-10-01 : un
# thermometre de barbecue voisin, "GT-TMBBQ05", a 75 degres, s'etait glisse
# dans le bulletin meteo). Reconnus par le nom de modele rtl_433.
NOT_WEATHER = [
    ("cuisson", ("bbq", "meat", "maverick", "grill", "burnhard", "01185m", "thermopro-tp08", "thermopro-tp11",
                 "thermopro-tp12", "thermopro-tp20", "smoker")),
    ("piscine", ("pool", "marbella")),
    ("frigo", ("refrigerator", "freezer", "acurite-986")),
    ("sol", ("soil", "wh51", "springfield", "rainpoint", "xt300")),
]
KIND_NAMES = Etiquettes("capteur.type", {"meteo": "thermomètre ou station météo", "alarme": "détecteur", "bouton": "bouton",
              "cuisson": "thermomètre de cuisson ou de barbecue", "piscine": "thermomètre de piscine",
              "frigo": "thermomètre de réfrigérateur ou congélateur", "sol": "sonde de sol"},
                        "type de capteur sans fil")

ALARM_WORDS = ("smoke", "fumee", "alarm", "water", "leak", "flood", "tamper", "intrusion", "gas", "co_")
BUTTON_WORDS = ("doorbell", "bell", "button", "remote", "switch", "keyfob")
IGNORED_KEYS = {"time", "model", "id", "channel", "mic", "mod", "freq", "freq1", "freq2", "rssi",
                "snr", "noise", "protocol", "subtype", "flags", "raw", "code", "cmd", "rbt"}

_memory = vd.Memory(STATE_PATH)
_next = 0
# Une meme emission peut etre reconnue par plusieurs decodeurs de rtl_433
# (2026-10-01 : "Rubicson-Temperature" et "Solight-TE44", meme identifiant,
# canal et temperature, a la meme seconde : UN thermometre annonce deux
# fois). Une trame identique d'un autre modele dans DOUBLE_DECODE_SEC est
# ignoree.
DOUBLE_DECODE_SEC = 3
_recent_frames = {}


def _key(event):
    return "|".join(str(event.get(k, "")) for k in ("model", "id", "channel"))


def _values(event):
    parts = []
    if event.get("temperature_C") is not None:
        parts.append(dire("capteur.degres", fr_number=vd.fr_number(event['temperature_C'], 1)))
    if event.get("humidity") is not None:
        parts.append(dire("capteur.humidite_cent", humidity=event['humidity']))
    if event.get("wind_avg_km_h") is not None:
        parts.append(dire("capteur.vent_kilometres_heure", round=round(event['wind_avg_km_h'])))
    if event.get("rain_mm") is not None:
        parts.append(dire("capteur.pluie_cumulee_millimetres", fr_number=vd.fr_number(event['rain_mm'], 1)))
    if event.get("battery_ok") == 0:
        parts.append(dire("capteurs.pile_faible"))
    return ", ".join(parts)


def _kind(event):
    model = str(event.get("model", "")).lower()
    for kind, words in NOT_WEATHER:
        if any(w in model for w in words):
            return kind
    blob = json.dumps(event).lower()
    if "tpms" in blob or "pressure_kpa" in blob or "pressure_psi" in blob:
        return "pneu"
    if any(w in blob for w in ALARM_WORDS) and event.get("alarm", 1) not in (0, "0", False):
        return "alarme"
    if any(w in blob for w in BUTTON_WORDS):
        return "bouton"
    if "temperature_c" in blob or "humidity" in blob:
        return "meteo"
    return "autre"


def _name(event):
    return str(event.get("model", "capteur")).replace("-", " ").replace("_", " ")


def _publish(key, event, kind="meteo"):
    try:
        with open(READINGS_PATH, encoding="utf-8") as f:
            readings = json.load(f)
    except (OSError, ValueError):
        readings = {}
    now = time.time()
    readings[key] = {"nom": _name(event), "type": kind,
                     "valeurs": {k: v for k, v in event.items() if k not in IGNORED_KEYS}, "t": now}
    readings = {k: v for k, v in readings.items() if now - v.get("t", 0) < READINGS_KEEP_SEC}
    try:
        tmp = READINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(readings, f, ensure_ascii=False)
        os.replace(tmp, READINGS_PATH)
    except OSError:
        pass


def _pressure_kpa(event):
    if event.get("pressure_kPa") is not None:
        return float(event["pressure_kPa"])
    if event.get("pressure_PSI") is not None:
        return float(event["pressure_PSI"]) * 6.895
    return None


def _received_at(event, at):
    """Heure de reception notee par rtl_433 (identique pour les decodages
    multiples d'une meme emission), sinon l'heure de lecture."""
    try:
        return datetime.strptime(event["time"], "%Y-%m-%d %H:%M:%S").timestamp()
    except (KeyError, TypeError, ValueError):
        return at


def _double_decode(event, at):
    at = _received_at(event, at)
    values = tuple(event.get(k) for k in ("id", "channel", "temperature_C", "humidity", "pressure_kPa"))
    if values[0] is None:
        return False
    for key in [k for k, (t, _) in _recent_frames.items() if at - t > 60]:
        del _recent_frames[key]
    previous = _recent_frames.get(values)
    _recent_frames[values] = (at, event.get("model"))
    return previous is not None and at - previous[0] <= DOUBLE_DECODE_SEC and previous[1] != event.get("model")


# Capteurs de l'utilisateur (lib/capteurs_miens.py, 2026-10-07) : relus sur
# le NAS au plus toutes les MINE_CACHE_SEC ; annonces sous leur nom ; une
# sonde de sol ne dit pas chaque releve (lib/plantes_alertes.py s'en sert) ;
# pile faible dite une fois par semaine.
MINE_CACHE_SEC = 600
BATTERY_EVERY_SEC = 7 * 86400
_mine = {"t": 0, "items": {}}


def _my_sensor(key):
    if time.time() - _mine["t"] > MINE_CACHE_SEC:
        try:
            import capteurs_miens
            _mine["items"] = {c["cle"]: c for c in capteurs_miens.load()}
        except Exception as exc:
            print(f"[capteurs_watch] mes capteurs : {exc!r}")
        _mine["t"] = time.time()
    return _mine["items"].get(key)


def _process(event, freq, tpms, at):
    if _double_decode(event, at):
        vd.archive("capteurs", dict(event, bande=freq, doublon=True))
        return False
    vd.archive("capteurs", dict(event, bande=freq))
    key = _key(event)
    own = _my_sensor(key)
    if own:
        connus.seen(key, own["nom"], own.get("genre") or "mien", freq[:3], mine=True)
        _publish(key, event, "mien")
        if event.get("battery_ok") == 0 and _memory.due(f"pile|{key}", BATTERY_EVERY_SEC):
            vd.announce(dire("capteur.pile_faible_mien", nom=own["nom"]), SOUND, f"capteur_pile_{key}", "capteurs_watch")
        if own.get("genre") == "sol":
            return False
        return _reading(key, dict(event, model=own["nom"]), "meteo", freq)
    kind = _kind(event)
    if kind == "pneu":
        # Pas d'annonce directe : apprentissage des vehicules (lib/voitures.py).
        tpms.append((at, key, _pressure_kpa(event)))
        return False
    _publish(key, event, kind)
    first = connus.seen(key, _name(event), kind, freq[:3])
    values = _values(event)
    if kind == "alarme" and _memory.due(f"alarme|{key}", ALARM_EVERY_SEC):
        plain = dire("capteur.alarme_capteur_sans_fil", v1=_name(event)) + (f" {values}." if values else "")
        vd.announce(lambda: plain + (" " + (_alarm_explanation(event) or "")).rstrip(),
                    SOUND, f"capteur_alarme_{key}", "capteurs_watch")
        return True
    if first:
        what = KIND_NAMES.get(kind) or dire("capteur.type_inconnu")
        freq_txt = "433" if freq.startswith("433") else "868"
        number = dire("capteur.numero", id=event['id']) if event.get("id") is not None else ""
        plain = (dire("capteur.nouveau_sans_fil_megahertz", what=what, freq_txt=freq_txt, v1=_name(event), number=number)
                 + (f", {values}" if values else "") + ".")
        text = plain if kind == "meteo" else (lambda: _fun_explanation(event, what, freq_txt) or plain)
        _memory.due(f"bouton|{key}", BUTTON_EVERY_SEC)  # le premier appui est deja raconte
        vd.announce(text, NOUVEAU_SOUND, f"capteur_nouveau_{key}", "capteurs_watch",
                    fiche={"protocole": "rtl_433", "frequence_hz": _hz(freq), "source": _name(event)})
        _said(key, event)
        return True
    if kind == "bouton" and _memory.due(f"bouton|{key}", BUTTON_EVERY_SEC):
        vd.announce(dire("capteur.appui_sonnette_telecommande_sans", v1=_name(event)), SOUND,
                    f"capteur_bouton_{key}", "capteurs_watch")
        return True
    if kind not in ("alarme", "bouton", "pneu"):
        return _reading(key, event, kind, freq)
    return False


# Appareil nouveau qui n'est pas une station meteo (cuisson, piscine,
# sonnette, inconnu...) : Gemini explique ses donnees avec une petite
# blague (demande utilisateur 2026-10-01 : "on veut rien manquer"). Jamais
# pour les alarmes, qui restent sobres. Repli sur l'annonce simple.
FUN_PROMPT = """Tu es la voix d'un récepteur radio domestique, au ton amusé et bienveillant.
Il vient de capter, pour la première fois, un appareil sans fil chez un voisin. Explique en
français, à une personne aveugle, ce qu'est cet appareil et ce que disent ses données, en
2 ou 3 phrases courtes à lire à voix haute, avec une petite blague. N'invente aucune donnée,
ne porte aucun jugement sur le voisin. Pas de markdown, pas d'emoji, pas de liste.
Commence par : Nouveau {what} capté en {freq} mégahertz.

Données décodées (format rtl_433) :
{data}
"""


def _said(key, event):
    _memory.data.setdefault("_dits", {})[key] = {"t": time.time(), "temp": event.get("temperature_C"),
                                                "hum": event.get("humidity")}


def _hz(freq):
    return float(freq.rstrip("M")) * 1e6 if freq.rstrip("M").replace(".", "").isdigit() else None


def _reading(key, event, kind, freq):
    """Releve capte. Meteo : garde pour le bulletin horaire, jamais dit seul
    (2026-10-19). Capteur atypique : dit tout de suite (prise vocale), sauf
    s'il redit exactement les valeurs deja annoncees (2026-10-03).
    Rend False : la routine d'un capteur ne pique pas l'attention du scanner
    (seuls un nouveau capteur, une alarme, une sonnette le font)."""
    values = _values(event)
    last = _memory.data.get("_dits", {}).get(key)
    if kind == "meteo" and values:
        import bulletin_capteurs  # chaque releve compte pour l'evolution de l'heure
        import capteurs_interieurs
        bulletin_capteurs.noter(key, _name(event), event, capteurs_interieurs.is_indoor(key))
        _said(key, event)
        _memory.data["_dits"][key]["valeurs"] = values
        return False
    if last and last.get("valeurs") == values:
        return False
    _said(key, event)
    _memory.data["_dits"][key]["valeurs"] = values
    fiche = {"protocole": "rtl_433", "frequence_hz": _hz(freq), "source": _name(event), "valeurs": values}
    if kind == "meteo":
        return False  # releve meteo sans valeur lisible
    what = KIND_NAMES.get(kind) or dire("capteur.type_inconnu")
    plain = dire("capteur.releve_atypique", what=what, nom=_name(event), valeurs=values or dire("capteur.sans_valeur"))
    vd.announce(lambda: _fun_again(event, what) or plain, SOUND, f"capteur_{key}", "capteurs_watch", fiche=fiche)
    return False


FUN_AGAIN_PROMPT = """Tu es la voix d'un récepteur radio domestique, au ton amusé et bienveillant.
Il capte de nouveau un appareil sans fil chez un voisin : {what}, modèle {model}. Dis à une
personne aveugle ce que disent ses données maintenant, en 1 ou 2 phrases courtes à lire à voix
haute, avec une petite blague liée au contexte (heure : {heure}, valeurs ci-dessous). N'invente
aucune donnée, ne porte aucun jugement sur le voisin. Pas de markdown, pas d'emoji, pas de liste.

Données décodées (format rtl_433) :
{data}
"""


def _fun_again(event, what):
    try:
        import gemini
        data = json.dumps({k: v for k, v in event.items() if k not in ("time", "rssi", "snr", "noise", "freq",
                                                                    "freq1", "freq2", "mod")},
                          ensure_ascii=False)
        text, problem = gemini.ask(FUN_AGAIN_PROMPT.format(what=what, model=_name(event), data=data,
                                                           heure=datetime.now().strftime("%H:%M")))
    except Exception as exc:
        text, problem = None, repr(exc)
    if problem:
        print(f"[capteurs_watch] blague Gemini indisponible : {problem}")
        return None
    vd.archive("capteurs_gemini", {"modele": event.get("model"), "texte": text})
    return text


def _fun_explanation(event, what, freq_txt):
    try:
        import gemini
        data = json.dumps({k: v for k, v in event.items() if k not in ("time", "rssi", "snr", "noise", "freq",
                                                                    "freq1", "freq2", "mod")},
                          ensure_ascii=False)
        text, problem = gemini.ask(FUN_PROMPT.format(what=what, freq=freq_txt, data=data))
    except Exception as exc:
        text, problem = None, repr(exc)
    if problem:
        print(f"[capteurs_watch] explication Gemini indisponible : {problem}")
    else:
        vd.archive("capteurs_gemini", {"modele": event.get("model"), "texte": text})
    return text


# Alarme (2026-10-14, demande utilisateur) : une phrase sobre de Gemini dit de
# quelle NATURE d'alarme il s'agit (fumee, fuite, cuve, ouverture...) et si
# la trame ressemble a un faux decodage. Gardee par modele et contenu
# (ALARM_EXPLAINED_SEC) : une alarme repetee ne rappelle pas Gemini.
ALARM_PROMPT = """Tu es la voix d'un récepteur radio domestique. Un capteur sans fil proche vient
d'émettre une trame marquée "alarme". En français, pour une personne aveugle, en 2 phrases
courtes à lire à voix haute, sans blague :
1. D'abord ce qu'est l'appareil d'après son nom de modèle (par exemple "Oil-SonicStd" est une
   jauge à ultrasons de cuve à fioul) et à quoi il sert.
2. Puis la nature de l'alarme (fumée, fuite d'eau, niveau bas de cuve, ouverture, pile,
   sabotage...) ; si les données ressemblent à un faux décodage (identifiant 65535 ou 0,
   valeurs toutes à zéro, champs incohérents), dis plutôt que c'est probablement une fausse
   alarme due à un décodage erroné.
L'appareil est chez un voisin, pas chez l'auditeur : ne dis jamais "votre". N'invente aucune
donnée. Ne commence pas par "Alerte". Pas de markdown, pas de liste.

Données décodées (format rtl_433) :
{data}
"""
ALARM_EXPLAINED_SEC = 30 * 86400


def _alarm_explanation(event):
    fields = {k: v for k, v in event.items() if k not in ("time", "rssi", "snr", "noise", "freq", "freq1", "freq2",
                                                          "mod", "bande", "t")}
    cache_key = json.dumps({k: v for k, v in fields.items() if k != "id"}, sort_keys=True, ensure_ascii=False)
    cache = _memory.data.setdefault("_alarmes_expliquees", {})
    hit = cache.get(cache_key)
    if hit and time.time() - hit["t"] < ALARM_EXPLAINED_SEC:
        return hit["texte"]
    try:
        import gemini
        text, problem = gemini.ask(ALARM_PROMPT.format(data=json.dumps(fields, ensure_ascii=False)))
    except Exception as exc:
        text, problem = None, repr(exc)
    if problem or not text:
        print(f"[capteurs_watch] explication d'alarme indisponible : {problem}")
        return None
    cache[cache_key] = {"t": time.time(), "texte": text}
    vd.archive("capteurs_gemini", {"modele": event.get("model"), "alarme": True, "texte": text})
    return text


def _reader(proc, events):
    for raw in proc.stdout:
        try:
            events.append((time.time(), json.loads(raw)))
        except ValueError:
            continue


def _cars(tpms, freq, listened_sec):
    """Apprentissage et annonces des vehicules (pneus) apres une passe."""
    if not freq.startswith("433"):
        return False
    try:
        voitures.record_listening(listened_sec)
        sentences = voitures.learn(tpms)
    except Exception as exc:  # jamais bloquant pour le reste de la veille
        print(f"[capteurs_watch] voitures : {exc!r}")
        return False
    announced = False
    for sentence in sentences:
        if _memory.due(f"voiture|{sentence[:40]}", 600):
            vd.announce(sentence, VOITURE_SOUND, "voiture", "capteurs_watch")
            announced = True
    return announced


def pairing_session():
    """Appairage demande par la console (lib/capteurs_miens.py) : rtl_433 sur
    433,92 et 868,3 MHz en alternance, chaque trame ecrite pour la console,
    jusqu'a la fin de la demande. Appele par la boucle de radio_watch."""
    import capteurs_miens as cm
    until = cm.requested()
    if not until:
        return False
    with open(cm.EVENTS_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_debut": time.time()}) + "\n")
    try:
        proc = subprocess.Popen(["rtl_433", "-d", "0", "-f", "433.92M", "-f", "868.3M", "-H", "20", "-F", "json",
                                 "-M", "level", "-C", "si"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("[capteurs_watch] rtl_433 introuvable")
        return False
    events = []
    reader = threading.Thread(target=_reader, args=(proc, events), daemon=True)
    reader.start()
    print("[capteurs_watch] appairage : ecoute 433 et 868 MHz")
    try:
        while cm.requested() and proc.poll() is None:
            time.sleep(0.3)
            with open(cm.EVENTS_PATH, "a", encoding="utf-8") as f:
                while events:
                    at, event = events.pop(0)
                    f.write(json.dumps(dict(event, _t=at), ensure_ascii=False) + "\n")
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        reader.join(timeout=2)
        print("[capteurs_watch] appairage termine")
    return True


FENETRES_VIDES = chemins.base("capteurs_fenetres_vides")


def _compter_fenetre_vide(vide):
    """Ecoutes des capteurs faites de suite sans aucune trame (2026-10-24) :
    l'alarme antenne muette ne compte que des ecoutes reellement faites (le
    mode restreint les sautait, et elle sonnait a tort)."""
    try:
        with open(FENETRES_VIDES) as f:
            n = int(f.read().strip() or 0)
    except (OSError, ValueError):
        n = 0
    try:
        with open(FENETRES_VIDES, "w") as f:
            f.write(str(n + 1 if vide else 0))
    except OSError:
        pass


def scan_window(dwell_sec=30):
    """Une passe rtl_433, sur la frequence suivante. Rend True si annonce."""
    global _next
    freq = FREQUENCIES[_next % len(FREQUENCIES)]
    _next += 1
    try:
        proc = subprocess.Popen(["rtl_433", "-d", "0", "-f", freq, "-F", "json", "-M", "level",
                                 "-C", "si", "-T", str(int(dwell_sec))],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("[capteurs_watch] rtl_433 introuvable")
        return False
    events, tpms = [], []
    reader = threading.Thread(target=_reader, args=(proc, events), daemon=True)
    reader.start()
    announced = False
    count = 0
    started = time.time()
    try:
        deadline = time.time() + dwell_sec + 5
        while time.time() < deadline and (proc.poll() is None or events):
            time.sleep(0.2)
            while events:
                count += 1
                at, event = events.pop(0)
                if _process(event, freq, tpms, at):
                    announced = True
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        reader.join(timeout=2)
        while events:
            count += 1
            at, event = events.pop(0)
            if _process(event, freq, tpms, at):
                announced = True
        if _cars(tpms, freq, min(dwell_sec, time.time() - started)):
            announced = True

        _memory.save()
        connus.save()
    if count:
        print(f"[capteurs_watch] {count} trame(s) capteurs sur {freq}")
        try:  # antenne vivante (veille_systeme : antenne muette)
            with open(chemins.base("capteurs_derniere_trame"), "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
        if not announced:  # captees sans rien a dire : un son en fin de fenetre (2026-10-23)
            import audio_lock
            from sound_player import play_sound_effect
            lock = audio_lock.acquire()
            try:
                play_sound_effect(sons.chemin("trame_non_dite", SOUND), label="capteurs_watch")
            finally:
                audio_lock.release(lock)
    _compter_fenetre_vide(not count)
    return announced
