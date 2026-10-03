import math
import os
import json
import time

import requests

from speak import speak, speak_with_lead_sound
import audio_lock
from sound_paths import FLIGHT_SCANER_DIR
import reglages
import adsb_local_watch
import ogn_watch
from phrases import Etiquettes, dire
from phrases import dire

# API OpenSky Network. Compte authentifie cree en session (username
# blind_vador) -- l'API exige desormais OAuth2 (client_credentials).
OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

# Identifiants du compte dans nearfield-anchor-settings/cles/opensky_id.txt
# et opensky_secret.txt, relus a chaque demande de jeton (voir reglages.py) -- recuperes via le tableau de bord OpenSky
# (client secret obtenu en session par l'utilisateur via une manip NVDA
# apres un blocage serveur 503 persistant sur le bouton "Reset Credential"
# -- voir ROADMAP.md pour le detail complet de cette peripetie).

_access_token = None
_token_expiry = 0.0  # timestamp Unix


def _get_access_token():
    """Echange client_id/client_secret contre un jeton d'acces (flow OAuth2
    client_credentials), avec mise en cache -- les jetons expirent apres
    ~30 min, on en redemande un nouveau un peu avant l'expiration plutot
    que d'attendre un echec. Retourne None si l'authentification echoue
    (le service continue alors en anonyme, cf fetch_aircraft)."""
    global _access_token, _token_expiry
    if _access_token and time.time() < _token_expiry - 60:
        return _access_token
    if not reglages.cle("opensky_id"):
        return None  # pas de cle : anonyme, sans erreur a chaque sondage
    try:
        resp = requests.post(
            OPENSKY_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": reglages.cle("opensky_id"),
                "client_secret": reglages.cle("opensky_secret"),
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _access_token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 1800)
        return _access_token
    except Exception as exc:
        print(f"[adsb_watch] erreur authentification OpenSky : {exc}")
        _access_token = None
        return None


# Commune de reference (commande "ville") et aeroports surveilles (commande
# "aeroport") : lieu.json et avions.json, voir reglages.py. Lus au demarrage.
_LIEU = reglages.get("lieu")
_AVIONS = reglages.get("avions")
REF_LAT = float(_LIEU["lat"])
REF_LON = float(_LIEU["lon"])
AIRPORT_NEAR = (float(_AVIONS["proche"]["lat"]), float(_AVIONS["proche"]["lon"]))
AIRPORT_BIG = (float(_AVIONS["grand"]["lat"]), float(_AVIONS["grand"]["lon"]))

# Boite de recherche : la commune et les deux aeroports, plus BBOX_MARGIN_DEG
# de marge (approches et departs). Sert uniquement a limiter la requete API
# -- le vrai filtre des annonces est le rayon MAX_RADIUS_KM ci-dessous. Au
# dela de BBOX_MAX_SQ_DEG (grand aeroport tres loin), on se limite a la
# commune : sous 25 sq deg, une requete coute 1 credit (voir doc API), dans
# le budget de 4000 credits/jour du compte authentifie.
BBOX_MARGIN_DEG = 0.5
BBOX_MAX_SQ_DEG = 20.0


def _bbox():
    lats = [REF_LAT, AIRPORT_NEAR[0], AIRPORT_BIG[0]]
    lons = [REF_LON, AIRPORT_NEAR[1], AIRPORT_BIG[1]]
    box = (min(lats) - BBOX_MARGIN_DEG, max(lats) + BBOX_MARGIN_DEG,
           min(lons) - BBOX_MARGIN_DEG, max(lons) + BBOX_MARGIN_DEG)
    if (box[1] - box[0]) * (box[3] - box[2]) > BBOX_MAX_SQ_DEG:
        return REF_LAT - 1, REF_LAT + 1, REF_LON - 1, REF_LON + 1
    return box


LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = _bbox()

# Rayon de veille reel -- resserre en session (2026-08-30) : 50km -> 20km,
# puis borne a la distance reelle de l'aeroport de l'utilisateur (15.9km,
# Saint-Etienne -> Bouthéon ; a revoir si la commune change beaucoup).
MAX_RADIUS_KM = 15.9

# Rayon "juste au-dessus de moi" -- au-dela de la simple presence dans la
# zone, declenche le son de survol dedie.
OVERHEAD_RADIUS_KM = 3.0

# Son joue quand un avion passe directement au-dessus (pas juste dans la
# zone de veille) -- fourni par l'utilisateur sur le NAS.
FLYBY_SOUND = f"{FLIGHT_SCANER_DIR}/flyer_by.mp3"  # chemin de base centralise dans sound_paths.py (2026-08-31)

# Son court joue AVANT l'annonce d'un avion detecte dans la zone de veille
# (pas un survol direct, qui a deja son propre son FLYBY_SOUND).
SIMPLE_CONTACT_SOUND = f"{FLIGHT_SCANER_DIR}/simple_contact.mp3"

# Intervalle entre deux interrogations. Relache volontairement en session
# (2026-08-30) : 30s jugee trop frequente/gourmande en CPU pour une veille
# que l'utilisateur decrit comme "un peu decorum" (pas besoin de precision
# fine sur l'instant exact du survol) -- 90s reduit la charge (~960
# requetes/jour au lieu de ~2880, tres large marge sous le quota de 4000/jour
# du compte authentifie) tout en restant assez frequent pour capter un avion
# qui traverse la zone (30 kilometres de diametre a 15.9km de rayon : un
# avion de ligne a 900 km/h la traverse en ~2min, un avion leger/GA plus
# lentement encore -- au moins un sondage pendant le passage dans la
# plupart des cas).
POLL_INTERVAL_SEC = 90

# Indices du tableau "states" retourne par l'API OpenSky (voir sa doc) :
IDX_ICAO24 = 0
IDX_CALLSIGN = 1
IDX_LONGITUDE = 5
IDX_LATITUDE = 6
IDX_BARO_ALTITUDE = 7
IDX_ON_GROUND = 8
IDX_CATEGORY = 17
IDX_VERTICAL_RATE = 11

# Contexte aerien pour le systeme d'attention du scanner radio (2026-10-01) :
# calcule sur TOUS les avions de la zone interrogee (qui couvre les deux aeroports),
# alors que les annonces vocales restent limitees a MAX_RADIUS_KM. Piqures par
# fenetre radio, d'apres position, altitude, taux de montee et sol/vol.
AIR_CONTEXT_PATH = "/tmp/contexte_aerien.json"
AIR_TARGET_CAP = 3.0

# Categorie ADS-B (norme DO-260B) -- l'API OpenSky ne documente PAS de
# lookup de modele d'appareil precis (ex: "Airbus A320"), meme authentifie
# (confirme en session par lecture de la doc officielle) -- ceci est
# l'information la plus precise disponible sur le TYPE d'appareil.
CATEGORY_NAMES = Etiquettes("avion.categorie", {
    0: None,  # aucune info -- on ne dit rien plutot que "categorie inconnue"
    1: None,
    2: "avion léger",
    3: "avion de petite taille",
    4: "avion de ligne",
    5: "gros avion à forte turbulence de sillage",
    6: "avion gros porteur",
    7: "avion à hautes performances",
    8: "hélicoptère",
    9: "planeur",
    10: "aéronef plus léger que l'air",
    11: "parachutiste",
    12: "ULM ou parapente",
    14: "drone",
    15: "véhicule spatial",
}, "type d'appareil annoncé par un avion")

EARTH_RADIUS_KM = 6371.0


def _distance_km(lat1, lon1, lat2, lon2):
    """Distance a vol d'oiseau (formule de haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _publish_air_context(states):
    targets = {}

    def add(target, points):
        targets[target] = min(AIR_TARGET_CAP, targets.get(target, 0.0) + points)

    count = 0
    for st in states:
        lat, lon = st[IDX_LATITUDE], st[IDX_LONGITUDE]
        if lat is None or lon is None:
            continue
        count += 1
        alt = st[IDX_BARO_ALTITUDE] or 0
        vrate = abs(st[IDX_VERTICAL_RATE] or 0) if len(st) > IDX_VERTICAL_RATE else 0
        moving = 0.5 if vrate > 2 else 0.0          # monte ou descend : on lui parle
        d_ste = _distance_km(AIRPORT_NEAR[0], AIRPORT_NEAR[1], lat, lon)
        d_lyon = _distance_km(AIRPORT_BIG[0], AIRPORT_BIG[1], lat, lon)
        if st[IDX_ON_GROUND]:
            if d_lyon < 5:
                add("aviation_sol", 0.3)          # faible : a 63 km, au ras du sol
            continue
        if d_ste < 25 and alt < 2000:
            add("aviation_st_etienne", 1.0 + moving)
        if d_lyon < 20 and alt < 1500:
            add("aviation_tour", 1.0)
        elif 20 <= d_lyon < 60 and 1500 <= alt < 6000:
            add("aviation_approche", 0.5 + moving)
        if alt >= 6000:                            # croisiere : ACARS, controle en route
            add("acars", 0.3)
            add("vdl2", 0.3)
            add("aviation", 0.2)
    try:
        tmp = AIR_CONTEXT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"cibles": targets, "avions": count, "updated": time.time()}, f)
        os.replace(tmp, AIR_CONTEXT_PATH)
    except OSError as exc:
        print(f"[adsb_watch] contexte aerien non publie : {exc}")


def fetch_aircraft():
    """Interroge l'API OpenSky pour la zone surveillee (authentifie si
    possible, retombe sur l'anonyme sinon). Retourne la liste des "states"
    (un par appareil) ou None en cas d'echec (reseau, API indisponible,
    quota depasse...)."""
    params = {"lamin": LAT_MIN, "lomin": LON_MIN, "lamax": LAT_MAX, "lomax": LON_MAX}
    headers = {}
    token = _get_access_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(OPENSKY_URL, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        remaining = resp.headers.get("X-Rate-Limit-Remaining")
        if remaining is not None:
            print(f"[adsb_watch] credits restants aujourd'hui : {remaining}")
        data = resp.json()
        return data.get("states") or []
    except Exception as exc:
        print(f"[adsb_watch] erreur API OpenSky : {exc}")
        return None


def _speak_coordinated(text):
    """Attend que radio_watch.py ait fini son cycle en cours (annonce +
    diffusion + son de fin) avant de parler, pour ne jamais chevaucher le
    scan radio -- verrou de fichier partage entre les deux services
    (processus separes, voir audio_lock.py)."""
    lock = audio_lock.acquire()
    try:
        speak(text)
    finally:
        audio_lock.release(lock)


def _announce_flyby(callsign):
    lock = audio_lock.acquire()
    try:
        speak_with_lead_sound(FLYBY_SOUND, dire("avion.passe_juste_au_dessus", callsign=callsign), sound_timeout_sec=60, label="adsb_watch")
    finally:
        audio_lock.release(lock)


# Un appareil disparu moins longtemps que ca n'est pas re-annonce : les
# donnees de l'antenne (fenetre du scanner) arrivent par a-coups.
FORGET_SEC = 600


def _merge(remote, local):
    """OpenSky + reception directe (adsb_local_watch). Rend (etats, icao
    captes seulement par l'antenne)."""
    seen = {s[0] for s in remote}
    local_only = {s[0] for s in local if s[0] not in seen}
    return remote + [s for s in local if s[0] in local_only], local_only


def main():
    print("[adsb_watch] demarrage de la veille ADS-B (API OpenSky + antenne)")
    _speak_coordinated(dire("avion.veille_ads_b_demarree"))
    ogn_watch.start()

    # Buffer des appareils deja annonces (icao24 -> indicatif) : sert
    # uniquement a decider si un appareil est "nouveau" depuis le dernier
    # passage, pour ne jamais repeter la meme info en boucle -- un appareil
    # deja vu et toujours present reste muet tant qu'il ne disparait pas et
    # ne revient pas.
    known_aircraft = {}  # icao24 -> dernier instant vu
    # Appareils pour lesquels le son de survol a deja ete joue -- persiste
    # entre les passages (contrairement a known_aircraft) pour ne jouer le
    # son qu'une fois par passage au-dessus, meme sur plusieurs sondages
    # consecutifs ou l'avion resterait dans le rayon de survol.
    overhead_announced = set()

    while True:
        remote = fetch_aircraft()
        local = adsb_local_watch.as_states()
        if remote is None and not local:
            time.sleep(POLL_INTERVAL_SEC)
            continue
        states, local_only = _merge(remote or [], local)
        now = time.time()

        _publish_air_context(states)
        current_aircraft = {}
        for state in states:
            if state[IDX_ON_GROUND]:
                continue  # pas interessant pour une veille "dans le ciel"
            lon = state[IDX_LONGITUDE]
            lat = state[IDX_LATITUDE]
            if lat is None or lon is None:
                continue
            dist = _distance_km(REF_LAT, REF_LON, lat, lon)
            if dist > MAX_RADIUS_KM:
                continue  # hors du rayon de veille reel

            icao24 = state[IDX_ICAO24]
            callsign = (state[IDX_CALLSIGN] or "").strip() or "indicatif inconnu"
            altitude_m = state[IDX_BARO_ALTITUDE]
            category = state[IDX_CATEGORY] if len(state) > IDX_CATEGORY else None
            current_aircraft[icao24] = callsign

            if now - known_aircraft.get(icao24, 0) > FORGET_SEC:
                parts = [dire("avion.avion_detecte", callsign=callsign)]
                if icao24 in local_only:
                    parts.append(dire("avion.capte_direct_antenne"))
                category_name = CATEGORY_NAMES.get(category)
                if category_name:
                    parts.append(f"{category_name.capitalize()}.")
                if altitude_m:
                    parts.append(dire("avion.altitude_metres", altitude_m=format(altitude_m, ".0f")))
                parts.append(dire("avion.distance_kilometres", dist=format(dist, ".0f")))
                lock = audio_lock.acquire()
                try:
                    speak_with_lead_sound(SIMPLE_CONTACT_SOUND, " ".join(parts), label="adsb_watch")
                finally:
                    audio_lock.release(lock)
                print(f"[adsb_watch] nouvel appareil : {callsign} ({icao24}), {dist:.0f} km")

            if dist <= OVERHEAD_RADIUS_KM:
                if icao24 not in overhead_announced:
                    _announce_flyby(callsign)
                    overhead_announced.add(icao24)
                    print(f"[adsb_watch] survol direct : {callsign} ({icao24}), {dist:.1f} km")
            else:
                overhead_announced.discard(icao24)

        for icao24 in current_aircraft:
            known_aircraft[icao24] = now
        known_aircraft = {k: t for k, t in known_aircraft.items() if now - t <= FORGET_SEC}
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
