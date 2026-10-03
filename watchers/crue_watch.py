import json
import time

import requests

from speak import speak, speak_with_lead_sound
import audio_lock
from sound_paths import METEO_SCANER_DIR
from phrases import Etiquettes, dire

# Vigicrues (service public gratuit, sans clé) -- réutilise telle quelle
# l'architecture d'alerte de meteo_watch.py : même échelle de couleurs 1-4,
# mêmes sons (simple_message pour un changement léger, vigilence_N pour une
# alarme réelle orange/rouge), même logique "on ne parle que sur un
# CHANGEMENT de niveau", même persistance d'état dans un fichier JSON
# temporaire pour survivre à un redémarrage.
VIGICRUE_URL = "https://www.vigicrues.gouv.fr/services/1/InfoVigiCru.geojson"

# Tronçon Vigicrues "Loire forézienne", code CdEntCru=LC126. Couvre la Loire
# à travers la plaine du Forez ET le Furan (station de référence à
# Andrézieux, juste en aval de Saint-Étienne) -- le Furan n'a pas de tronçon
# de vigilance dédié à Saint-Étienne même, c'est le tronçon officiel le plus
# proche. Confirmé par inspection directe du geojson national en session
# (2026-08-30) : propriétés {"CdEntCru":"LC126","lbentcru":"Loire
# forézienne","cdensup_1":"30", ...}.
TRONCON_CODE = "LC126"
TRONCON_NAME = "Loire forézienne"

COLOR_NAMES = Etiquettes("vigilance.couleur", {1: "vert", 2: "jaune", 3: "orange", 4: "rouge"}, "couleur de vigilance météo")

SOUND_EFFECTS_DIR = METEO_SCANER_DIR  # chemin de base centralise dans sound_paths.py (2026-08-31)
SIMPLE_MESSAGE_SOUND = f"{SOUND_EFFECTS_DIR}/simple_message.mp3"
VIGILANCE_SOUNDS = {
    1: f"{SOUND_EFFECTS_DIR}/vigilence_1.mp3",
    2: f"{SOUND_EFFECTS_DIR}/vigilence_2.mp3",
    3: f"{SOUND_EFFECTS_DIR}/vigilence_3.mp3",
    4: f"{SOUND_EFFECTS_DIR}/vigilence_4.mp3",
}


# Vigicrues publie a 10h et 16h, ou plus souvent si la situation l'exige --
# un sondage horaire (meme cadence que la vigilance meteo) est deja plus
# reactif que necessaire.
POLL_INTERVAL_SEC = 3600
STATE_FILE = "/tmp/crue_watch_vigilance_state.json"
ALARM_LOOP_DURATION_SEC = 60  # duree de la boucle alarme avant de couper et parler le conseil

# Alerte initiale -- sons dedies fournis par l'utilisateur sur le NAS
# (ao_cru.mp3/ar_cru.mp3, meme dossier et convention que meteo_watch.py).
# Pas d'etape de rapprochement 15/10 min ici : Vigicrues ne donne qu'un
# niveau de couleur, pas d'echeance/heure d'arrivee comme le nowcast
# meteo -- cette notion n'a pas d'equivalent pour les crues.
ALERT_LEVEL_PREFIX = {3: "ao", 4: "ar"}
PHENOMENON_LABEL = "crue"
ALERT_SOUND_SUFFIX = "cru"


def _speak_lightly(text):
    lock = audio_lock.acquire()
    try:
        speak_with_lead_sound(SIMPLE_MESSAGE_SOUND, text, label="crue_watch")
    finally:
        audio_lock.release(lock)


def _alert_sound_path(level):
    prefix = ALERT_LEVEL_PREFIX.get(level)
    if prefix:
        return f"{SOUND_EFFECTS_DIR}/{prefix}_{ALERT_SOUND_SUFFIX}.mp3"
    return VIGILANCE_SOUNDS.get(level)


def _speak_initial_alert(level, advice_text=None):
    """Boucle le son ao_cru/ar_cru en répétant "Alerte {niveau} crue !"
    pendant ALARM_LOOP_DURATION_SEC, PUIS coupe et parle le conseil de
    sécurité une seule fois, au calme."""
    sound_path = _alert_sound_path(level)
    short_text = dire("crue.alerte", v1=COLOR_NAMES[level], PHENOMENON_LABEL=PHENOMENON_LABEL)
    lock = audio_lock.acquire()
    try:
        deadline = time.time() + ALARM_LOOP_DURATION_SEC
        while time.time() < deadline:
            speak_with_lead_sound(sound_path, short_text, sound_timeout_sec=90, label="crue_watch")
        if advice_text:
            speak(advice_text)
    finally:
        audio_lock.release(lock)


def fetch_vigilance_level():
    """Interroge le geojson national Vigicrues et retourne le niveau de
    vigilance (1-4) du tronçon surveillé, ou None en cas d'échec ou si le
    tronçon est introuvable dans la réponse."""
    try:
        resp = requests.get(VIGICRUE_URL, timeout=20)
        resp.raise_for_status()
        for feature in resp.json().get("features", []):
            props = feature.get("properties", {})
            if props.get("CdEntCru") == TRONCON_CODE:
                return props.get("NivInfViCr")
        print(f"[crue_watch] tronçon {TRONCON_CODE} introuvable dans la réponse")
        return None
    except Exception as exc:
        print(f"[crue_watch] erreur API vigicrues : {exc}")
        return None


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as exc:
        print(f"[crue_watch] erreur écriture état : {exc}")


def main():
    print(f"[crue_watch] démarrage de la veille crue ({TRONCON_NAME}, code {TRONCON_CODE})")
    state = _load_state()
    previous = state.get(TRONCON_CODE)

    while True:
        level = fetch_vigilance_level()
        if level is not None and level != previous:
            if previous is not None:
                if level >= 3:
                    _speak_initial_alert(level, dire("crue.safety_advice"))
                else:
                    message = dire("crue.niveau_monte" if level > previous else "crue.niveau_baisse",
                                   troncon=TRONCON_NAME, avant=COLOR_NAMES[previous], apres=COLOR_NAMES[level])
                    _speak_lightly(message)
            previous = level
            state[TRONCON_CODE] = level
            _save_state(state)

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
