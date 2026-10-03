"""Veille des capteurs sans fil 433,92 et 868,3 MHz (rtl_433) : stations
meteo et thermometres, detecteurs de fumee, sonnettes, telecommandes...
tout ce qui emet en clair a portee du Pi.

Une frequence par passage, en alternance. Annonces :
- alarmes (fumee, eau, intrusion...) : a chaque evenement (anti-repetition
  ALARM_EVERY_SEC) ;
- sonnettes et boutons : a chaque appui (BUTTON_EVERY_SEC) ;
- capteur jamais vu depuis NEW_EVERY_SEC : presente une fois, avec ses
  valeurs ;
- releves (demande utilisateur 2026-10-03 : "on doit etre informe vocalement
  quand le Pi capte quelque chose") : a chaque passage, une phrase groupee
  des capteurs meteo entendus ; un capteur n'est redit qu'apres
  READING_REPEAT_SEC, ou plus tot si sa valeur bouge (TEMP_STEP_C,
  HUMIDITY_STEP) ; capteurs atypiques (cuisson, piscine, frigo, sol...) :
  meme rythme, chacun avec une petite blague de Gemini sur ses donnees ;
- capteurs de pneus (voitures qui passent) : jamais annonces, archives.
Les derniers releves sont publies (READINGS_PATH) pour la console et le
flash ; tout est archive.
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime

import veille_donnees as vd
import voitures
from sound_paths import RADIO_SCANER_DIR
from phrases import Etiquettes, dire

FREQUENCIES = ["433.92M", "868.3M"]
SOUND = os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3")
STATE_PATH = "/tmp/capteurs_watch_state.json"
READINGS_PATH = "/tmp/capteurs_releves.json"
NEW_EVERY_SEC = 7 * 86400
ALARM_EVERY_SEC = 120
BUTTON_EVERY_SEC = 60
READINGS_KEEP_SEC = 6 * 3600
READING_REPEAT_SEC = 1800
TEMP_STEP_C = 1.0
HUMIDITY_STEP = 5

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
_heard = {}   # releves du passage en cours, annonces en fin de passage : {cle: (evenement, genre)}
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


def _process(event, freq, tpms, at):
    if _double_decode(event, at):
        vd.archive("capteurs", dict(event, bande=freq, doublon=True))
        return False
    vd.archive("capteurs", dict(event, bande=freq))
    key = _key(event)
    kind = _kind(event)
    if kind == "pneu":
        # Pas d'annonce directe : apprentissage des vehicules (lib/voitures.py).
        tpms.append((at, key, _pressure_kpa(event)))
        return False
    _publish(key, event, kind)
    values = _values(event)
    if kind == "alarme" and _memory.due(f"alarme|{key}", ALARM_EVERY_SEC):
        vd.announce(dire("capteur.alarme_capteur_sans_fil", v1=_name(event)) + (f" {values}." if values else ""),
                    SOUND, f"capteur_alarme_{key}", "capteurs_watch")
        return True
    if _memory.due(f"nouveau|{key}", NEW_EVERY_SEC):
        what = KIND_NAMES.get(kind) or dire("capteur.type_inconnu")
        freq_txt = "433" if freq.startswith("433") else "868"
        number = dire("capteur.numero", id=event['id']) if event.get("id") is not None else ""
        plain = (dire("capteur.nouveau_sans_fil_megahertz", what=what, freq_txt=freq_txt, v1=_name(event), number=number)
                 + (f", {values}" if values else "") + ".")
        text = plain if kind == "meteo" else (_fun_explanation(event, what, freq_txt) or plain)
        _memory.due(f"bouton|{key}", BUTTON_EVERY_SEC)  # le premier appui est deja raconte
        vd.announce(text, SOUND, f"capteur_nouveau_{key}", "capteurs_watch")
        _said(key, event)
        return True
    if kind not in ("alarme", "bouton", "pneu"):
        _heard[key] = (event, kind)
    if kind == "bouton" and _memory.due(f"bouton|{key}", BUTTON_EVERY_SEC):
        vd.announce(dire("capteur.appui_sonnette_telecommande_sans", v1=_name(event)), SOUND,
                    f"capteur_bouton_{key}", "capteurs_watch")
        return True
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


def _worth_saying(key, event):
    """Releve a dire ? Jamais dit, dit il y a plus de READING_REPEAT_SEC, ou
    valeur qui a bouge depuis."""
    last = _memory.data.get("_dits", {}).get(key)
    if not last or time.time() - last["t"] >= READING_REPEAT_SEC:
        return True
    temp, hum = event.get("temperature_C"), event.get("humidity")
    return ((temp is not None and last.get("temp") is not None and abs(temp - last["temp"]) >= TEMP_STEP_C)
            or (hum is not None and last.get("hum") is not None and abs(hum - last["hum"]) >= HUMIDITY_STEP))


FUN_AGAIN_PROMPT = """Tu es la voix d'un récepteur radio domestique, au ton amusé et bienveillant.
Il capte de nouveau un appareil sans fil chez un voisin : {what}, modèle {model}. Dis à une
personne aveugle ce que disent ses données maintenant, en 1 ou 2 phrases courtes à lire à voix
haute, avec une petite blague liée au contexte (heure : {heure}, valeurs ci-dessous). N'invente
aucune donnée, ne porte aucun jugement sur le voisin. Pas de markdown, pas d'emoji, pas de liste.

Données décodées (format rtl_433) :
{data}
"""


def _announce_readings(freq):
    """Fin de passage : capteurs meteo groupes en une phrase, atypiques un par un avec une blague."""
    heard, weather, said = dict(_heard), [], False
    _heard.clear()
    for key, (event, kind) in heard.items():
        if not _worth_saying(key, event):
            continue
        values = _values(event)
        if kind == "meteo":
            if values:
                weather.append(dire("capteur.releve", nom=_name(event), valeurs=values))
                _said(key, event)
            continue
        what = KIND_NAMES.get(kind) or dire("capteur.type_inconnu")
        plain = dire("capteur.releve_atypique", what=what, nom=_name(event), valeurs=values or dire("capteur.sans_valeur"))
        text = _fun_again(event, what) or plain
        vd.announce(text, SOUND, f"capteur_{key}", "capteurs_watch")
        _said(key, event)
        said = True
    if weather:
        vd.announce(dire("capteur.releves_meteo", releves=" ; ".join(weather)), SOUND, "capteurs_meteo",
                    "capteurs_watch")
    return said or bool(weather)


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
            vd.announce(sentence, SOUND, "voiture", "capteurs_watch")
            announced = True
    return announced


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
        try:
            if _announce_readings(freq):
                announced = True
        except Exception as exc:  # une annonce ratee n'arrete pas la veille
            print(f"[capteurs_watch] annonce des releves : {exc!r}")
        _memory.save()
    if count:
        print(f"[capteurs_watch] {count} trame(s) capteurs sur {freq}")
    return announced
