import json
import os
import time

import requests
from bs4 import BeautifulSoup

from sound_paths import TRAIN_SCANER_DIR
from phrases import dire, nombre

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


# --- veille de tous les reseaux actifs (2026-10-07) ---
# Les perturbations de chaque reseau actif de la commune (lib/transports.py :
# page STAS ci-dessus, alertes GTFS-RT, SIRI Lite) sont annoncees une fois,
# par la file centrale des annonces (prise vocale, journal "transports",
# phrases du fichier de langue). Silence de 22 h a 7 h : ce qui est apparu
# la nuit est dit le matin si c'est toujours d'actualite. Au premier passage
# sur un reseau, ses perturbations deja en cours sont notees sans etre dites.
SEEN_FILE = "/home/vador/autopilot/transports_vus.json"
NETWORKS_POLL_SEC = 600
QUIET_HOURS = (22, 7)


def _quiet():
    hour = time.localtime().tm_hour
    return hour >= QUIET_HOURS[0] or hour < QUIET_HOURS[1]


def _load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_seen(seen):
    try:
        with open(SEEN_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False)
        os.replace(SEEN_FILE + ".tmp", SEEN_FILE)
    except OSError as exc:
        print(f"[stas_watch] etat non ecrit : {exc}")


def _families_text(fams):
    import transports
    if "general" in fams or not fams:
        return dire("transport.tout_le_reseau")
    return ", ".join(transports.GROUPS[f] for f in transports.GROUPS if f in fams)


def announce_new(seen, announce=None):
    """Un passage sur les reseaux actifs. Rend les textes annonces."""
    import transports
    import veille_donnees as vd
    if announce is None:
        announce = lambda text: vd.announce(text, SOUND_NOTIF, "transport_perturbation", "stas_watch")  # noqa: E731
    said = []
    for n in transports.active():
        try:
            current = transports.items(n, transports.feed(n) if n.get("temps_reel") else None)
        except Exception as exc:  # un reseau en panne ne bloque pas les autres
            print(f"[stas_watch] {n['nom']} : {exc!r}")
            continue
        key = str(n["id"])
        first = key not in seen
        known = set(seen.get(key) or [])
        new = [i for i in current if i["id"] not in known]
        if new and not first and not _quiet():
            name = transports._short_name(n["nom"])
            if len(new) == 1:
                text = dire("transport.nouvelle_perturbation", reseau=name, familles=_families_text(new[0]["familles"]),
                            titre=new[0]["titre"])
            else:
                text = dire("transport.nouvelles_perturbations", reseau=name,
                            nombre=nombre(len(new), "perturbation"),
                            liste=" ; ".join(f"{_families_text(i['familles'])}, {i['titre']}" for i in new[:5]))
            announce(text)
            said.append(text)
        if first or not _quiet():  # la nuit, les nouvelles restent a dire au matin
            seen[key] = sorted({i["id"] for i in current})
        else:
            seen[key] = sorted(known & {i["id"] for i in current})
    _save_seen(seen)
    return said


def main():
    print("[stas_watch] demarrage de la veille des transports (page STAS + reseaux actifs de la commune)")
    known_titles = _load_state()
    seen = _load_seen()
    last_page = 0.0
    while True:
        if time.time() - last_page >= POLL_INTERVAL_SEC:
            last_page = time.time()
            disruptions = fetch_disruptions()
            if disruptions is not None:
                current_titles = {d["title"] for d in disruptions}
                if current_titles != known_titles:
                    known_titles = current_titles
                    _save_state(known_titles)  # lu par lib/transports.py
        try:
            announce_new(seen)
        except Exception as exc:
            print(f"[stas_watch] reseaux : {exc!r}")
        time.sleep(NETWORKS_POLL_SEC)


if __name__ == "__main__":
    main()
