import json
import os
import threading
import time

import requests

from speak import speak, speak_with_lead_sound
import audio_lock
from sound_paths import METEO_SCANER_DIR
import reglages
from phrases import Etiquettes, dire

# Miroir public (sans cle) des donnees officielles de vigilance
# Meteo-France, sur Opendatasoft. L'API officielle exige desormais un
# compte/cle -- ce miroir donne les memes donnees (phenomene, couleur,
# echeance) sans authentification. Teste et confirme en session.
METEO_URL = "https://public.opendatasoft.com/api/explore/v2.1/catalog/datasets/weatherref-france-vigilance-meteo-departement/records"

# Departement et position de la commune choisie par la commande "ville"
# de la console (lieu.json, voir reglages.py), lus au demarrage.
_LIEU = reglages.get("lieu")
DOMAIN_ID = str(_LIEU["departement"])
DEPARTMENT_NAME = _LIEU["departement_nom"]

# Position exacte pour l'alerte hyper-locale ci-dessous -- la vigilance
# departementale est trop large (tout le departement peut etre orange sans
# que ca touche precisement ici).
REF_LAT = float(_LIEU["lat"])
REF_LON = float(_LIEU["lon"])

# But du service : ALERTER quand un phenomene dangereux approche, pas
# fournir une prevision meteo routiniere. On ne regarde que l'echeance "J"
# (aujourd'hui) -- "J1" (demain) serait de la prevision.
ECHEANCE = "J"

COLOR_NAMES = Etiquettes("vigilance.couleur", {1: "vert", 2: "jaune", 3: "orange", 4: "rouge"}, "couleur de vigilance météo")

# Son court joue AVANT toute annonce "sans gravite" (ticker de vigilance,
# heads-up jaune local, retour au calme) -- distinct des sons vigilence_N,
# qui restent reserves aux niveaux d'alarme reels (orange/rouge). Fourni
# par l'utilisateur sur le NAS.
SIMPLE_MESSAGE_SOUND = "/media/nas/nd1/server/meteo_scaner/simple_message.mp3"


def _speak_lightly(text):
    """Joue le son 'message sans gravite' puis parle -- pour tout ce qui
    n'est pas une alarme reelle (orange/rouge)."""
    lock = audio_lock.acquire()
    try:
        speak_with_lead_sound(SIMPLE_MESSAGE_SOUND, text, label="meteo_watch")
    finally:
        audio_lock.release(lock)

# --- Vigilance departementale : simple indicateur de changement de statut ---
# Role redefini en session : la vigilance n'est plus le systeme d'alerte
# "temps reel" (ce role appartient desormais exclusivement a la veille
# hyper-locale ci-dessous) -- c'est juste un ticker qui signale les
# CHANGEMENTS de niveau officiel, peu frequent (le departement est trop
# large pour une alerte precise). Etat persiste dans un fichier JSON
# temporaire pour survivre a un redemarrage du service.
VIGILANCE_POLL_INTERVAL_SEC = 3600  # 1x/heure, demande explicite
VIGILANCE_STATE_FILE = "/tmp/meteo_watch_vigilance_state.json"


def fetch_vigilance():
    """Interroge le miroir Opendatasoft pour le departement/echeance
    surveilles. Retourne la liste des entrees (une par phenomene) ou None
    en cas d'echec."""
    params = {"where": f"domain_id='{DOMAIN_ID}' and echeance='{ECHEANCE}'", "limit": 20}
    try:
        resp = requests.get(METEO_URL, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("results") or []
    except Exception as exc:
        print(f"[meteo_watch] erreur API vigilance : {exc}")
        return None


def _load_vigilance_state():
    try:
        with open(VIGILANCE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_vigilance_state(state):
    try:
        with open(VIGILANCE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as exc:
        print(f"[meteo_watch] erreur ecriture etat vigilance : {exc}")


def _vigilance_watcher():
    """Fil de veille de la vigilance departementale officielle : annonce
    uniquement les CHANGEMENTS de niveau (montee ou descente), rien
    d'autre -- pas d'alarme en boucle, pas de conseils de securite (ce
    role est maintenant celui de la veille hyper-locale)."""
    print(f"[meteo_watch] demarrage de la veille vigilance (departement {DOMAIN_ID}, {DEPARTMENT_NAME})")
    known_colors = _load_vigilance_state()

    while True:
        results = fetch_vigilance()
        if results is not None:
            changed = False
            for entry in results:
                phenomenon = entry.get("phenomenon")
                color_id = entry.get("color_id")
                if phenomenon is None or color_id is None:
                    continue
                previous = known_colors.get(phenomenon)
                if previous == color_id:
                    continue
                if previous is not None:
                    _speak_lightly(dire("meteo_watch.niveau_monte" if color_id > previous else "meteo_watch.niveau_baisse",
                                        phenomene=PHENOMENES.get(phenomenon, phenomenon), departement=DEPARTMENT_NAME,
                                        avant=COLOR_NAMES[previous], apres=COLOR_NAMES[color_id]))
                known_colors[phenomenon] = color_id
                changed = True
            if changed:
                _save_vigilance_state(known_colors)

        time.sleep(VIGILANCE_POLL_INTERVAL_SEC)


# --- Alerte hyper-locale (nowcast) : le vrai systeme d'alerte temps reel ---
# La vigilance ci-dessus est a l'echelle du departement -- trop large pour
# savoir si une perturbation touche precisement Saint-Etienne. Utilise le
# modele AROME de Meteo-France (haute resolution, specialise convection/
# orages) via l'API Open-Meteo, gratuite et sans cle, resolution 15 min, a
# la position exacte. Teste et confirme en session avec des donnees reelles.
NOWCAST_URL = "https://api.open-meteo.com/v1/forecast"
NOWCAST_LOOKAHEAD_STEPS = 8  # 8 x 15 min = 2 heures d'anticipation

# Seuils de declenchement par niveau (jaune, orange, rouge), un par type de
# perturbation -- POINT DE DEPART a ajuster avec le retour utilisateur une
# fois de vrais evenements observes.
NOWCAST_THRESHOLDS = {
    "precipitation": (2.0, 5.0, 10.0),           # mm / 15 min
    "wind_gusts_10m": (50.0, 70.0, 100.0),       # km/h
    "cape": (500.0, 1500.0, 3000.0),             # J/kg -- instabilite/potentiel orageux
    "lightning_potential": (10.0, 50.0, 200.0),  # J/kg -- activite orageuse
}

# Message court et naturel par type de perturbation ("on speak simplement si
# de la pluie approche par exemple") -- pas de jargon, pas de boilerplate.
NOWCAST_APPROACH_MESSAGE = Etiquettes("meteo_watch.approche", {
    "precipitation": "De la pluie approche",
    "wind_gusts_10m": "Des rafales de vent approchent",
    "cape": "Un orage approche",
    "lightning_potential": "Un orage approche",
}, "début d'annonce : ce qui approche, suivi de quand")
NOWCAST_METRIC_PHENOMENON = {
    "precipitation": "pluie",
    "wind_gusts_10m": "vent",
    "cape": "orages",
    "lightning_potential": "orages",
}

# Conseils de securite -- lus seulement a partir du niveau orange (le
# simple heads-up jaune reste volontairement court, sans conseil).
SAFETY_ADVICE = Etiquettes("meteo_watch.conseil", {
    "orages": "Débranchez les appareils électriques sensibles. Fermez fenêtres et volets.",
    "vent": "Fermez et sécurisez volets et fenêtres. Rangez les objets extérieurs susceptibles d'être emportés.",
    "pluie": "Évitez les sous-sols et zones basses. Ne vous engagez pas sur une route inondée.",
}, "conseil de sécurité lu une fois, après une alerte orange ou rouge")
# Noms dits des phenomenes (les codes servent aux sons et aux conseils).
PHENOMENES = Etiquettes("meteo_watch.phenomene", {"orages": "orages", "vent": "vent", "pluie": "pluie", "crue": "crue",
                                                  "canicule": "canicule", "neige": "neige", "grand froid": "grand froid",
                                                  "avalanches": "avalanches", "vagues-submersion": "vagues-submersion",
                                                  "pluie-inondation": "pluie-inondation"},
                        "phénomène météo d'une alerte")

SOUND_EFFECTS_DIR = METEO_SCANER_DIR  # chemin de base centralise dans sound_paths.py (2026-08-31)
VIGILANCE_SOUNDS = {
    1: f"{SOUND_EFFECTS_DIR}/vigilence_1.mp3",
    2: f"{SOUND_EFFECTS_DIR}/vigilence_2.mp3",
    3: f"{SOUND_EFFECTS_DIR}/vigilence_3.mp3",
    4: f"{SOUND_EFFECTS_DIR}/vigilence_4.mp3",
}

NOWCAST_POLL_INTERVAL_SEC = 600  # 10 min entre deux sondages en temps calme
ALARM_REPEAT_SEC = 120  # frequence de sondage tant que orange/rouge (verification des etapes)
ALARM_LOOP_DURATION_SEC = 60  # duree de chaque boucle d'alarme/etape

# Alerte initiale + etapes de rapprochement (15/10 min) -- sons fournis par
# l'utilisateur sur le NAS, prefixe par niveau ("ao"=orange, "ar"=rouge) et
# par phenomene (orage/vent/cru). Repli sur le son de vigilance generique
# (VIGILANCE_SOUNDS) si aucun son dedie n'existe pour ce phenomene (ex:
# "pluie", pas encore fourni).
ALERT_LEVEL_PREFIX = {3: "ao", 4: "ar"}
PHENOMENON_SOUND_SUFFIX = {"orages": "orage", "vent": "vent", "crue": "cru"}
PROXIMITY_SOUNDS = {
    15: f"{SOUND_EFFECTS_DIR}/15min.mp3",
    10: f"{SOUND_EFFECTS_DIR}/10min.mp3",
}


# Contexte supplementaire (pas des seuils de declenchement, juste des infos
# annoncees en plus quand un seuil est franchi) : direction et vitesse du
# vent, comme proxy du sens d'approche de la perturbation -- pas un vrai
# suivi de cellule orageuse (l'API ne donne des donnees qu'a UN point, pas
# de traque multi-points), mais une approximation meteorologique standard
# ("le vent vient du nord-ouest" ~ "la perturbation arrive du nord-ouest").
# Pas de nom de lieu actuel de la perturbation -- non disponible avec cette
# source de donnees (point unique, pas de radar/suivi de cellule).
NOWCAST_CONTEXT_FIELDS = ["wind_speed_10m", "wind_direction_10m"]

CARDINAL_DIRECTIONS = [
    "nord", "nord-nord-est", "nord-est", "est-nord-est",
    "est", "est-sud-est", "sud-est", "sud-sud-est",
    "sud", "sud-sud-ouest", "sud-ouest", "ouest-sud-ouest",
    "ouest", "ouest-nord-ouest", "nord-ouest", "nord-nord-ouest",
]
DIRECTION_DE = Etiquettes("direction.de", {d: (f"de l'{d}" if d[0] in "eo" else f"du {d}") for d in CARDINAL_DIRECTIONS},
                          "d'où vient le vent ou la perturbation")


def _cardinal_direction(degrees):
    if degrees is None:
        return None
    idx = round(degrees / 22.5) % 16
    return CARDINAL_DIRECTIONS[idx]


def fetch_nowcast():
    """Interroge Open-Meteo (modele AROME) pour la position exacte.
    Retourne le dict "minutely_15" (series temporelles par metrique) ou
    None en cas d'echec."""
    params = {
        "latitude": REF_LAT,
        "longitude": REF_LON,
        "minutely_15": ",".join(list(NOWCAST_THRESHOLDS.keys()) + NOWCAST_CONTEXT_FIELDS),
        "forecast_minutely_15": NOWCAST_LOOKAHEAD_STEPS,
        "timezone": "Europe/Paris",
    }
    try:
        resp = requests.get(NOWCAST_URL, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("minutely_15")
    except Exception as exc:
        print(f"[meteo_watch] erreur API nowcast : {exc}")
        return None


def _severity_for_value(metric, value):
    if value is None:
        return 0
    jaune, orange, rouge = NOWCAST_THRESHOLDS[metric]
    if value >= rouge:
        return 4
    if value >= orange:
        return 3
    if value >= jaune:
        return 2
    return 0


def _analyze_nowcast(data):
    """Cherche, sur toute la fenetre d'anticipation, le niveau de severite
    le plus eleve toutes metriques confondues -- le type de perturbation ET
    le delai (proxy de distance : plus c'est loin dans le temps, plus c'est
    loin dans l'espace pour un systeme qui approche) determinent ensemble
    si/comment on alerte. Retourne (niveau, metrique, minutes_avant) ou
    (0, None, None) si rien de notable."""
    if not data or "time" not in data:
        return 0, None, None
    best_level, best_metric, best_index = 0, None, None
    for metric in NOWCAST_THRESHOLDS:
        for i, value in enumerate(data.get(metric) or []):
            level = _severity_for_value(metric, value)
            if level > best_level:
                best_level, best_metric, best_index = level, metric, i
    if best_level == 0:
        return 0, None, None
    return best_level, best_metric, best_index * 15


def _nowcast_announce(level, metric, minutes_ahead, data=None):
    """Heads-up leger, reserve au niveau jaune -- les niveaux orange/rouge
    passent desormais par _speak_initial_alert()/_speak_proximity_alert()
    (voir _nowcast_watcher), pas par cette fonction."""
    approach = NOWCAST_APPROACH_MESSAGE.get(metric) or dire("meteo_watch.perturbation_approche")
    timing = dire("meteo_watch.instant") if minutes_ahead <= 5 else dire("meteo_watch.dans_minutes", minutes=minutes_ahead)
    message = dire("meteo_watch.approche_quand", approche=approach, quand=timing)

    if data is not None:
        index = minutes_ahead // 15
        directions = data.get("wind_direction_10m") or []
        speeds = data.get("wind_speed_10m") or []
        cardinal = _cardinal_direction(directions[index]) if index < len(directions) else None
        speed = speeds[index] if index < len(speeds) else None
        if cardinal:
            # Elision : "de l'est", "de l'ouest-nord-ouest" mais "du nord", "du sud-est".
            message += " " + dire("meteo_watch.vient_de", origine=DIRECTION_DE[cardinal])
        if speed:
            message += " " + dire("meteo_watch.vent_vitesse", vitesse=f"{speed:.0f}")

    _speak_lightly(message)


def _alert_sound_path(level, phenomenon):
    prefix = ALERT_LEVEL_PREFIX.get(level)
    suffix = PHENOMENON_SOUND_SUFFIX.get(phenomenon)
    if prefix and suffix:
        return f"{SOUND_EFFECTS_DIR}/{prefix}_{suffix}.mp3"
    return VIGILANCE_SOUNDS.get(level)


def _loop_alarm_sound(sound_path, short_text, duration_sec=ALARM_LOOP_DURATION_SEC):
    """Boucle un son + répète un texte court pendant duration_sec. Le
    verrou audio doit déjà être tenu par l'appelant -- cette fonction ne
    le gère pas elle-même, pour permettre d'enchaîner ensuite un conseil
    de sécurité dans le même verrou sans coupure."""
    deadline = time.time() + duration_sec
    while time.time() < deadline:
        speak_with_lead_sound(sound_path, short_text, sound_timeout_sec=90, label="meteo_watch")


def _speak_initial_alert(level, phenomenon, advice_text=None):
    """Première alerte d'un épisode : boucle le son ao_/ar_ (ou repli
    générique si le phénomène n'a pas de son dédié) en répétant
    "Alerte {niveau} {phénomène} !", PUIS coupe et parle le conseil de
    sécurité une seule fois, au calme."""
    sound_path = _alert_sound_path(level, phenomenon)
    short_text = dire("meteo_watch.alerte", couleur=COLOR_NAMES[level], phenomene=PHENOMENES.get(phenomenon, phenomenon))
    lock = audio_lock.acquire()
    try:
        _loop_alarm_sound(sound_path, short_text)
        if advice_text:
            speak(advice_text)
    finally:
        audio_lock.release(lock)


def _speak_proximity_alert(level, phenomenon, minutes_threshold):
    """Étape de rapprochement (15 puis 10 minutes avant l'échéance) :
    boucle le son de compte à rebours correspondant en répétant juste
    "Alerte {niveau} {phénomène} !" -- pas de conseil répété ici, déjà
    donné à l'alerte initiale."""
    sound_path = PROXIMITY_SOUNDS[minutes_threshold]
    short_text = dire("meteo_watch.alerte", couleur=COLOR_NAMES[level], phenomene=PHENOMENES.get(phenomenon, phenomenon))
    lock = audio_lock.acquire()
    try:
        _loop_alarm_sound(sound_path, short_text)
    finally:
        audio_lock.release(lock)


def _nowcast_watcher():
    """Fil de veille hyper-locale : sonde régulièrement Open-Meteo à la
    position exacte. À partir du niveau orange/rouge, un épisode d'alerte
    se déroule en 3 étapes distinctes (chacune une seule fois par
    épisode) : alerte initiale (son ao_/ar_ + conseil de sécurité à la
    fin), puis étape "15 minutes" quand l'échéance approche, puis étape
    "10 minutes" en dessous -- demande explicite en session (2026-08-30).
    L'état des étapes est remis à zéro dès que le niveau repasse sous 3."""
    print(f"[meteo_watch] démarrage de la veille locale ({REF_LAT}, {REF_LON})")
    last_level = 0
    episode_active = False
    stage_15_done = False
    stage_10_done = False

    while True:
        data = fetch_nowcast()
        level, metric, minutes_ahead = _analyze_nowcast(data)
        phenomenon = NOWCAST_METRIC_PHENOMENON.get(metric, "orages")

        if level >= 3:
            if not episode_active:
                advice = SAFETY_ADVICE.get(phenomenon)
                _speak_initial_alert(level, phenomenon, advice)
                episode_active = True
                stage_15_done = False
                stage_10_done = False
            elif minutes_ahead is not None and minutes_ahead <= 10 and not stage_10_done:
                _speak_proximity_alert(level, phenomenon, 10)
                stage_10_done = True
                stage_15_done = True
            elif minutes_ahead is not None and minutes_ahead <= 15 and not stage_15_done:
                _speak_proximity_alert(level, phenomenon, 15)
                stage_15_done = True

            last_level = level
            time.sleep(ALARM_REPEAT_SEC)
            continue

        if level == 2 and last_level < 2:
            _nowcast_announce(level, metric, minutes_ahead, data)
        elif level < 3 <= last_level:
            _speak_lightly(dire("meteo_watch.alerte_retombee"))
            episode_active = False
            stage_15_done = False
            stage_10_done = False

        last_level = level
        time.sleep(NOWCAST_POLL_INTERVAL_SEC)


# --- Suivi des voisins et niveau de menace (lib/meteo_locale.py, 2026-10-01) ---
# Toutes les PRECURSOR_POLL_SEC : historique des releves voisins (stations
# APRS, capteurs), signes avant-coureurs, niveau de menace publie pour
# l'attention du scanner (qui ecoute alors davantage les stations meteo).
# Annonce les changements de niveau et chaque nouveau signe, avec la
# direction d'arrivee quand plusieurs stations ont reagi.
PRECURSOR_POLL_SEC = 300
FORECAST_REFRESH_SEC = 900
SIGN_REPEAT_SEC = 1800


def _precursor_watcher():
    import meteo_locale
    lieu = reglages.get("lieu")
    last_level, fc, fc_at = 0, None, 0.0
    announced = {}
    while True:
        try:
            history = meteo_locale.record_observations()
            if time.time() - fc_at >= FORECAST_REFRESH_SEC:
                fc = meteo_locale.forecast(lieu) or fc
                fc_at = time.time()
            signs = meteo_locale.precursors(lieu, history)
            level, reasons = meteo_locale.threat(lieu, fc, signs)
            meteo_locale.publish_context(level, reasons)
            now = time.time()
            fresh = [s for s in signs if now - announced.get(s["texte"], 0) > SIGN_REPEAT_SEC]
            for s in fresh:
                announced[s["texte"]] = now
            if level > last_level and level >= 2:
                text = dire("meteo_watch.danger_imminent" if level == 3 else "meteo_watch.perturbation_raisons",
                            raisons=", ".join(reasons))
                _speak_lightly(text)
            elif level < last_level and last_level >= 2 and level <= 1:
                _speak_lightly(dire("meteo_watch.calme"))
            if fresh:
                try:
                    import meteo_obs
                    meteo_obs.log_signs(fresh)  # pour juger leur fiabilite (bilan meteo)
                except Exception as exc:
                    print(f"[meteo_watch] journal des signes : {exc!r}")
                text = dire("meteo_watch.signe", signes=" ; ".join(s["texte"] for s in fresh[:3]))
                origin = meteo_locale.approach_direction(signs)
                if origin and len({s["direction"] for s in signs if s["direction"]}) > 1:
                    text += " " + dire("meteo_watch.arrive_de", origine=origin)
                _speak_lightly(text)
            announced = {k: t for k, t in announced.items() if now - t < 6 * 3600}
            last_level = level
        except Exception as exc:  # la veille principale ne doit jamais tomber pour ca
            print(f"[meteo_watch] suivi des voisins : {exc!r}")
        time.sleep(PRECURSOR_POLL_SEC)


# --- Donnees ouvertes (lib/donnees_ouvertes.py, 2026-10-01) : seismes
# notables, riviere qui monte vite ; orages signales par les aeroports
# (repris par le niveau de menace).
OPEN_DATA_POLL_SEC = 600


def _open_data_watcher():
    import donnees_ouvertes
    while True:
        try:
            for sentence in donnees_ouvertes.watch():
                _speak_lightly(sentence)
        except Exception as exc:
            print(f"[meteo_watch] donnees ouvertes : {exc!r}")
        time.sleep(OPEN_DATA_POLL_SEC)


def main():
    threading.Thread(target=_open_data_watcher, daemon=True).start()
    nowcast_thread = threading.Thread(target=_nowcast_watcher, daemon=True)
    nowcast_thread.start()
    threading.Thread(target=_precursor_watcher, daemon=True).start()
    import personnalite  # chaque nuit a minuit, le Pi reecrit ses phrases (personnalite choisie)
    threading.Thread(target=personnalite.watch, daemon=True).start()
    import bulletin_capteurs  # bulletin horaire des capteurs meteo, journal "meteo" (2026-10-19)
    threading.Thread(target=bulletin_capteurs.watch, daemon=True).start()
    import stats_locales  # releves de capteurs resumes sur la carte SD, bruts effaces apres 48 h
    threading.Thread(target=stats_locales.watch, daemon=True).start()
    import plantes_calendrier  # evenements de vie des plantes (journal "plantes")
    threading.Thread(target=plantes_calendrier.watch, daemon=True).start()
    _vigilance_watcher()


if __name__ == "__main__":
    main()
