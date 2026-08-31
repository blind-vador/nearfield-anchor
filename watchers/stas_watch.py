import json
import time

import requests
from bs4 import BeautifulSoup

from speak import speak_with_lead_sound
import audio_lock
from sound_paths import TRAIN_SCANER_DIR

# Page publique "infos trafic" de l'exploitant STAS (Cityway). Pas d'API
# JSON publique documentée trouvée pour les perturbations (contrairement
# au GTFS-RT, qui ne couvre que les retards de trajet, pas les travaux/
# déviations) -- la liste des perturbations est rendue côté serveur dans
# cette page, confirmé par un simple curl (pas besoin de JavaScript).
STAS_URL = "https://tsi.stas3.cityway.fr/fr/infos-trafic/17/Disruption"

SOUND_DIR = TRAIN_SCANER_DIR  # chemin de base centralise dans sound_paths.py (2026-08-31)
SOUND_NOTIF = f"{SOUND_DIR}/simple_notif.mp3"  # fourni par l'utilisateur

# "Régulièrement, pas trop souvent" -- les perturbations de ce réseau sont
# des travaux/déviations qui durent des jours voire des mois, pas des
# événements minute par minute.
POLL_INTERVAL_SEC = 1800  # 30 min
STATE_FILE = "/tmp/stas_watch_state.json"


def fetch_disruptions():
    """Récupère et parse la page infos-trafic. Retourne la liste des
    perturbations actives (title, content) ou None en cas d'échec."""
    try:
        resp = requests.get(STAS_URL, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[stas_watch] erreur récupération page STAS : {exc}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    disruptions = []
    for block in soup.select("div.disruption.clearfix"):
        title_el = block.select_one(".disruption-title")
        content_el = block.select_one(".disruption-content")
        title = title_el.get_text(" ", strip=True) if title_el else None
        content = content_el.get_text(" ", strip=True) if content_el else ""
        if title:
            disruptions.append({"title": title, "content": content})
    return disruptions


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_state(known_titles):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(known_titles), f)
    except Exception as exc:
        print(f"[stas_watch] erreur écriture état : {exc}")


def _speak_new_disruptions(new_disruptions):
    if not new_disruptions:
        return
    if len(new_disruptions) == 1:
        intro = "Nouvelle perturbation sur le réseau de bus et tram de Saint-Étienne."
    else:
        intro = f"{len(new_disruptions)} nouvelles perturbations sur le réseau de bus et tram de Saint-Étienne."
    lines = [intro] + [d["title"] for d in new_disruptions]
    message = " ".join(lines)

    lock = audio_lock.acquire()
    try:
        speak_with_lead_sound(SOUND_NOTIF, message, label="stas_watch")
    finally:
        audio_lock.release(lock)


def main():
    print("[stas_watch] démarrage de la veille perturbations STAS (Saint-Étienne)")
    known_titles = _load_state()

    while True:
        disruptions = fetch_disruptions()
        if disruptions is not None:
            current_titles = {d["title"] for d in disruptions}
            new_titles = current_titles - known_titles
            new_disruptions = [d for d in disruptions if d["title"] in new_titles]

            if new_disruptions:
                _speak_new_disruptions(new_disruptions)

            if current_titles != known_titles:
                known_titles = current_titles
                _save_state(known_titles)

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
