import os
import random
import subprocess
import threading
import time
from datetime import datetime

import requests

from speak import speak_with_lead_sound as _speak_with_lead_sound
import audio_lock
import annonce_morceaux as morceaux
from sound_player import play_sound_effect as _play_sound_effect


# Tout le son de la GARE passe par un effet "gare", sans toucher chaque appel
# ni impacter les autres veilles (radio, alertes), qui gardent le son sec.
# Deux profils (2026-10-09) : les annonces et jingles sortent d'un haut-parleur
# de quai -> "pa" (bande limitee + grand hall) ; les sons d'ambiance (train qui
# passe, klaxon, bruitages d'arrivee/depart via _play_file) sont seulement
# poses dans la salle reverberante, pleine bande -> "hall".
def play_sound_effect(*args, reverb="hall", **kwargs):
    return _play_sound_effect(*args, reverb=reverb, **kwargs)


def speak_with_lead_sound(*args, reverb="pa", **kwargs):
    return _speak_with_lead_sound(*args, reverb=reverb, **kwargs)
from sound_paths import TRAIN_SCANER_DIR
import reglages
from phrases import dire, nombre

# API SNCF (moteur Navitia), gratuite avec compte -- 150 000 requetes/mois
# (5000/jour). Jeton dans nearfield-anchor-settings/cles/sncf.txt, relu a
# chaque requete (voir reglages.py). Gare choisie par la commande "gare" de
# la console (trains.json), lue au demarrage.
_TRAINS = reglages.get("trains")
STATION_UIC = str(_TRAINS["uic"])
STATION_NAME = _TRAINS["gare"]
STATION_BASE_URL = f"https://api.sncf.com/v1/coverage/sncf/stop_areas/stop_area:SNCF:{STATION_UIC}"
DEPARTURES_URL = f"{STATION_BASE_URL}/departures"
ARRIVALS_URL = f"{STATION_BASE_URL}/arrivals"

# Fenêtre de l'annonce légère : tout train dont le départ/arrivée
# (théorique) tombe dans l'heure qui vient. Le paramètre "duration" de
# Navitia filtre déjà côté serveur -- pas besoin de count énorme.
UPCOMING_WINDOW_SEC = 3600

SOUND_DIR = TRAIN_SCANER_DIR  # chemin de base centralise dans sound_paths.py (2026-08-31)
SOUND_MODERNE = f"{SOUND_DIR}/sncf_moderne.wav"  # annonce légère (bulletin départs/arrivées)
SOUND_RETRO = f"{SOUND_DIR}/sncf_retro.mp3"      # perturbation/suppression temps réel

# Seuil en dessous duquel un écart théorique/réel n'est pas annoncé (bruit
# de mesure, pas un vrai retard).
DELAY_THRESHOLD_MIN = 2

# Delai avant l'heure REELLE (retard deja inclus) auquel l'annonce parlee
# se declenche -- recalee sur le rythme d'une vraie gare (2026-08-30,
# demande explicite : "basculer sur les vraies annonces en gare, en flux
# temps reel entre annonce et arrivee du train"). Avant ce changement,
# l'annonce partait des qu'un train entrait dans la fenetre d'une heure
# (UPCOMING_WINDOW_SEC), soit jusqu'a 1h a l'avance -- desynchronise du
# bruitage d'ambiance (_schedule_ambient_sound), qui lui se declenche deja
# pile a l'heure reelle. Meme mecanisme (threading.Timer) reutilise pour
# les deux, l'annonce parlee precedant desormais le bruitage de
# ANNOUNCEMENT_LEAD_SEC secondes -- silence, puis annonce, puis le train.
ANNOUNCEMENT_LEAD_SEC = 180

POLL_INTERVAL_SEC = 180  # 3 min -- 2 appels/cycle (départs+arrivées), très large marge sous 5000/jour

DT_FORMAT = "%Y%m%dT%H%M%S"


def _parse_dt(value):
    return datetime.strptime(value, DT_FORMAT)


def _format_time_spoken(dt):
    """Format lu naturellement par espeak -- PAS de zéro en tête (ex:
    "01" prononcé chiffre par chiffre au lieu de "un"), pas de "h" lettre
    (mal prononcée) -- juste "7 heure 30" / "7 heure" pile."""
    if dt.minute == 0:
        return f"{dt.hour} heure"
    return f"{dt.hour} heure {dt.minute}"


def _vehicle_journey_id(entry):
    for link in entry.get("links", []):
        if link.get("type") == "vehicle_journey":
            return link.get("id")
    # repli : combinaison mode+heure théorique, unique en pratique
    info = entry.get("display_informations", {})
    return f"{info.get('trip_short_name')}_{entry['stop_date_time']['base_departure_date_time']}"


def _disruption_lookup(disruptions):
    """Construit un dict vehicle_journey_id -> {effect, motif, id} à
    partir du tableau "disruptions" déjà présent dans la réponse
    /departures (pas d'appel API supplémentaire). Le motif réel se trouve
    dans messages[0].text (ex: "Défaillance de matériel") -- le champ
    "cause" est souvent vide en pratique, on ne s'y fie qu'en repli."""
    lookup = {}
    for disr in disruptions:
        motif = None
        messages = disr.get("messages") or []
        if messages and messages[0].get("text"):
            motif = messages[0]["text"]
        if not motif:
            motif = disr.get("cause") or None
        effect = (disr.get("severity") or {}).get("effect")
        for obj in disr.get("impacted_objects", []):
            pt = obj.get("pt_object", {})
            if pt.get("embedded_type") != "trip" or "id" not in pt:
                continue
            vj_id = "vehicle_journey:" + pt["id"]
            lookup[vj_id] = {"effect": effect, "motif": motif, "id": disr.get("id")}
    return lookup


def fetch_departures():
    """Interroge l'API SNCF pour les prochains départs de la gare
    surveillée, dans la fenêtre configurée. Retourne (departures,
    disruptions) ou (None, None) en cas d'échec. Les perturbations sont
    déjà incluses dans la même réponse, pas d'appel API séparé."""
    params = {"duration": UPCOMING_WINDOW_SEC, "count": 50}
    try:
        resp = requests.get(DEPARTURES_URL, params=params, auth=(reglages.cle("sncf"), ""), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("departures") or [], data.get("disruptions") or []
    except Exception as exc:
        print(f"[sncf_watch] erreur API SNCF (départs) : {exc}")
        return None, None


def fetch_arrivals():
    """Miroir de fetch_departures() pour les trains qui ARRIVENT en gare.
    Retourne aussi origins_by_id (id de gare -> nom) car le champ
    "direction" d'une arrivée reste le terminus de la ligne, pas
    l'origine réelle du train -- il faut résoudre l'origine via le
    tableau "origins" de haut niveau de la réponse."""
    params = {"duration": UPCOMING_WINDOW_SEC, "count": 50}
    try:
        resp = requests.get(ARRIVALS_URL, params=params, auth=(reglages.cle("sncf"), ""), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        origins_by_id = {o["id"]: o.get("name", "origine inconnue") for o in data.get("origins") or []}
        return data.get("arrivals") or [], origins_by_id
    except Exception as exc:
        print(f"[sncf_watch] erreur API SNCF (arrivées) : {exc}")
        return None, None


def _origin_name(entry, origins_by_id):
    """Le lien vers l'origine reelle (category="origin") ne se trouve PAS
    dans entry["links"] (qui ne contient que line/company/vehicle_journey/
    route/commercial_mode/physical_mode/network) mais dans
    entry["display_informations"]["links"] -- confirme par inspection
    directe de la reponse API en session (2026-08-30). Bug trouve suite a
    un signalement utilisateur : "origine inconnue" systematique sur les
    arrivees alors que la donnee existe bel et bien cote API, juste
    cherchee au mauvais endroit."""
    info_links = entry.get("display_informations", {}).get("links", [])
    for link in info_links:
        if link.get("category") == "origin":
            return origins_by_id.get(link.get("id"), "origine inconnue")
    return "origine inconnue"


def _clean_place_name(name):
    """Le champ Navitia "direction" est souvent formate "Gare (Ville)"
    (ex: "Lyon Perrache (Lyon)", parfois "Le Puy-en-Velay (Le Puy-en-Velay)"
    -- la gare et la ville identiques, un pur doublon a l'oral). Ne garde
    que le nom de gare, deja suffisant pour un voyageur -- signale par
    l'utilisateur (2026-08-30) : "ça fait doublon"."""
    if name and " (" in name:
        return name.split(" (", 1)[0]
    return name


# --- Bruitage ambiant : joue un son realiste au moment REEL ou un train
# arrive/part, distinct du bulletin parle. Dossiers fournis par
# l'utilisateur sur le NAS, un par (categorie de train x action) :
# ter_arriving/ter_leaving/ter_passby, tgv_arriving/tgv_leaving/tgv_passby,
# et desormais ter_sag/tgv_sag ("stop and go", 2026-08-31 -- train qui
# s'arrete puis repart, un seul son combine au lieu d'un arriving+leaving
# separes). Son choisi au hasard dans le dossier correspondant --
# _play_ambient_sound() se contente de ne rien jouer (juste un log) si un
# dossier est absent ou vide, ce qui couvre nativement le cas "pas encore
# de son tgv_sag fourni" sans code special. "passby" (train qui traverse
# sans s'arreter) N'EST PAS declenche cette session : l'API passagers
# /departures et /arrivals ne renvoie QUE les trains qui s'arretent
# reellement a Chateaucreux, un vrai passage sans arret n'y apparait
# jamais -- pas de signal fiable pour le detecter avec cette source de
# donnees. Dossiers marchandise_* trouves sur le NAS mais idem, aucun
# signal (trains de fret hors du perimetre de l'API voyageurs, confirme
# aussi par recherche externe en session -- absent de tout open data
# ferroviaire francais actuel).
AMBIANT_SOUND_DIR = SOUND_DIR


def _train_category(mode):
    """mode ici est le libelle NETTOYE (sortie de _clean_train_mode).
    OUIGO ajoute explicitement (2026-08-31, bug latent trouve en
    revisant ce code) : sans lui, "Ouigo" ne contient pas la sous-chaine
    "TGV" et tombait a tort dans "ter" -- jamais declenche en pratique
    (aucun train OUIGO vu sur cette gare a ce jour) mais corrige quand
    meme puisque la marque appartient bien a la famille TGV."""
    upper = (mode or "").upper()
    return "tgv" if ("TGV" in upper or "OUIGO" in upper) else "ter"


def _clean_train_mode(mode):
    """Le champ commercial_mode de l'API SNCF n'est pas toujours lisible
    tel quel par la synthese vocale. Releve reel sur cette gare (2026-08-
    31, requete directe a l'API) : seulement deux valeurs observees sur
    une fenetre de 6h -- "REGIONAURA" (un seul mot colle, jamais "TER" ni
    meme "TER Auvergne-Rhone-Alpes" comme suppose au premier correctif --
    cause exacte du "regionora" mal prononce rapporte par l'utilisateur)
    et "TGV INOUI". Table exacte pour ces cas connus : les sigles courts
    (TGV, TER) DOIVENT rester en capitales pour etre epeles lettre par
    lettre par la synthese (demande explicite de l'utilisateur), alors
    que "INOUI" (nom de marque, pas un sigle) doit au contraire etre en
    casse mixte ("Inouï") pour etre lu comme un mot normal plutot
    qu'epele. "OUIGO" ajoute par anticipation (jamais vu sur cette gare a
    ce jour, mais memes marques SNCF que TGV INOUI). Repli : toute valeur
    contenant "TGV" mais absente de la table est quand meme epelee "TGV"
    (couvre une eventuelle variante non encore vue) ; toute autre valeur
    inconnue est prononcee telle quelle (pas de repli silencieux vers
    "TER", qui serait faux pour un service qui n'est pas regional) --
    mais loggee pour reperer facilement un futur cas a ajouter a la
    table."""
    key = (mode or "").strip().upper()
    if key in TRAIN_MODE_LABELS:
        return TRAIN_MODE_LABELS[key]
    if not key:
        return "Train"
    if "TGV" in key:
        return "TGV"
    print(f"[sncf_watch] commercial_mode inconnu, non mappe (prononce tel quel) : {mode!r}")
    return mode


TRAIN_MODE_LABELS = {
    "REGIONAURA": "TER",
    "TGV INOUI": "TGV Inouï",
    "OUIGO": "Ouigo",
}


def _train_label(mode, train_number):
    """Texte complet "mode [numero]" reutilise dans toutes les annonces --
    numero ajoute seulement si disponible (trip_short_name absent sur
    certains services), demande explicite de l'utilisateur (2026-08-31,
    "ca sonnera realiste"). Numero lu comme un nombre normal (PAS espace
    chiffre par chiffre) -- corrige le meme jour, l'utilisateur precise
    qu'en gare les numeros de train sont annonces comme des nombres, pas
    epeles."""
    if train_number:
        return dire("trains.numero", mode=mode, train_number=train_number)
    return mode


def _probe_duration_sec(path, default=30.0):
    """Sonde la durée réelle du fichier -- les dossiers de bruitage
    mélangent des ambiances longues (ex: arriving ~110-200s) et des sons
    courts (ex: passby ~7-18s), un timeout fixe unique ne convient a
    aucun des deux."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, timeout=10,
        )
        return float(result.stdout.decode().strip())
    except Exception:
        return default


HORN_SOUND = os.path.join(AMBIANT_SOUND_DIR, "horn1.mp3")
# Faible proba qu'un son de train declenche un evenement secondaire aleatoire
# (train qui passe, klaxon), MEME s'il n'est pas reel : colore et fait vivre
# la gare (2026-10-08, demande utilisateur).
AMBIENCE_EXTRA_PROB = 0.15


# Anti-doublon (2026-10-09, demande utilisateur) : deux trains ne doivent
# jamais jouer EXACTEMENT le meme fichier en meme temps (deux sons identiques
# alignes = ecart/echo bizarre). On tient un registre des fichiers en cours ;
# le choix evite ceux qui jouent deja. S'il ne reste aucun fichier libre (plus
# d'evenements simultanes que de fichiers dispo), le doublon est inevitable :
# on le DECALE (on laisse ~1/4 du son passer) et on le joue plus bas, pour que
# le recouvrement ne s'entende pas comme un echo.
DUP_VOLUME = 0.4            # volume d'un doublon force
DUP_OVERLAP = 0.25         # fraction du son laissee passer avant le doublon
_ambient_playing = {}      # chemin -> instant de fin (epoch)
_ambient_lock = threading.Lock()


def _is_playing(path):
    with _ambient_lock:
        return _ambient_playing.get(path, 0.0) > time.time()


def _mark_playing(path, start_in, duration):
    with _ambient_lock:
        _ambient_playing[path] = time.time() + start_in + duration


def _choose(files):
    """Un fichier au hasard en EVITANT ceux qui jouent deja. Retourne
    (chemin, doublon_force) ; doublon_force=True si tous jouent deja."""
    if not files:
        return None, False
    libres = [f for f in files if not _is_playing(f)]
    if libres:
        return random.choice(libres), False
    return random.choice(files), True


def _pick_ambient(category, action):
    folder = os.path.join(AMBIANT_SOUND_DIR, f"{category}_{action}")
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.lower().endswith((".mp3", ".wav"))]
    except FileNotFoundError:
        return None, False
    return _choose(files)


def _play_file(path, duplicate=False):
    """Joue un son d'ambiance SANS verrou exclusif : plusieurs sons se mixent
    sur la carte USB ("default" via dmix), donc une arrivee et un depart
    simultanes jouent ensemble (2026-10-08). duplicate=True : meme fichier deja
    en cours faute de mieux -> decale de ~1/4 du son et joue plus bas, pour
    eviter l'echo de deux sons identiques alignes (2026-10-09)."""
    dur = _probe_duration_sec(path)
    if duplicate:
        delay = max(0.3, dur * DUP_OVERLAP)
        _mark_playing(path, delay, dur)
        threading.Timer(delay, lambda: play_sound_effect(
            path, timeout_sec=dur + 5, label="sncf_watch", volume=DUP_VOLUME)).start()
    else:
        _mark_playing(path, 0.0, dur)
        play_sound_effect(path, timeout_sec=dur + 5, label="sncf_watch")


def _play_random_ambience():
    """Evenement secondaire d'ambiance : un train qui passe (TER ou TGV) ou un
    coup de klaxon, au hasard. Decoratif, pas forcement reel."""
    choix = random.choice(["ter_passby", "tgv_passby", "horn"])
    if choix == "horn":
        path, dup = _choose([HORN_SOUND])
    else:
        path, dup = _pick_ambient(choix.split("_")[0], "passby")
    if path and os.path.exists(path):
        _play_file(path, duplicate=dup)


def _play_ambient_sound(category, action):
    path, dup = _pick_ambient(category, action)
    if not path:
        print(f"[sncf_watch] dossier bruitage absent ou vide : {category}_{action}")
        return
    # Evenement secondaire aleatoire lance EN PARALLELE (il se mixe au son
    # principal) avec une faible probabilite, pour animer la gare.
    if random.random() < AMBIENCE_EXTRA_PROB:
        threading.Timer(random.uniform(1.0, 4.0), _play_random_ambience).start()
    _play_file(path, duplicate=dup)


def _schedule_ambient_sound(mode, action, event_dt):
    """Lance une tache en attente (thread daemon) qui se reveille pile a
    l'heure reelle de l'evenement (retard deja connu inclus, puisqu'on
    programme sur l'heure REELLE pas theorique) pour jouer le bruitage."""
    category = _train_category(mode)
    delay = max(0.0, (event_dt - datetime.now()).total_seconds())
    timer = threading.Timer(delay, _play_ambient_sound, args=(category, action))
    timer.daemon = True
    timer.start()


# Annonces assemblees par morceaux (lib/annonce_morceaux.py, 2026-10-18) :
# chaque morceau (parties fixes, gare, numero de train, heure) est synthetise
# une fois par voix et garde ; l'annonce d'un train est preparee des qu'il
# est connu (_schedule_announcement), puis seulement jouee a l'heure. Toujours
# avec l'effet "haut-parleur de quai" (reverb "pa").
_prepared = {}


def _parts_depart(mode, destination, scheduled_dt, train_number=None):
    return morceaux.fragments("trains.destination_depart_va_etre", mode_label=_train_label(mode, train_number),
                              destination=destination, v1=_format_time_spoken(scheduled_dt))


def _parts_terminus(mode, origin, scheduled_dt, train_number=None):
    return morceaux.fragments("trains.provenance_entrera_gare", mode_label=_train_label(mode, train_number),
                              origin=origin, v1=_format_time_spoken(scheduled_dt))


def _parts_stop_and_go(mode, origin, destination, scheduled_dt, train_number=None):
    return morceaux.fragments("trains.provenance_destination_entrera_gare", mode_label=_train_label(mode, train_number),
                              origin=origin, destination=destination, v1=_format_time_spoken(scheduled_dt))


def _say_parts(sound, parts, prepared=None):
    lock = audio_lock.acquire()
    try:
        morceaux.say(sound, parts, label="sncf_watch", reverb="pa", prepared=prepared)
    finally:
        audio_lock.release(lock)


def _prepared_key(fn, args):
    return (fn.__name__, repr(args))


def _speak_depart_announcement(mode, destination, scheduled_dt, train_number=None):
    """Train qui NAIT a cette gare (present dans /departures, absent de
    /arrivals) -- formulation "mise en place" fournie par l'utilisateur
    (2026-08-31, quatrieme correction sur cette annonce : la destination,
    omise dans la version precedente, doit y etre -- "coquille" signalee
    par l'utilisateur, pas une omission volontaire)."""
    args = (mode, destination, scheduled_dt, train_number)
    _say_parts(SOUND_MODERNE, _parts_depart(*args),
               _prepared.pop(_prepared_key(_speak_depart_announcement, args), None))


def _speak_terminus_announcement(mode, origin, scheduled_dt, train_number=None):
    """Train qui FINIT son service a cette gare (present dans /arrivals,
    absent de /departures) -- juste l'origine, pas de destination
    puisqu'il n'y en a pas apres Chateaucreux pour ce train."""
    args = (mode, origin, scheduled_dt, train_number)
    _say_parts(SOUND_MODERNE, _parts_terminus(*args),
               _prepared.pop(_prepared_key(_speak_terminus_announcement, args), None))


def _speak_stop_and_go_announcement(mode, origin, destination, scheduled_dt, train_number=None):
    """Train qui s'arrete puis repart (meme vehicle_journey_id present
    dans /departures ET /arrivals) -- UNE SEULE annonce combinee origine+
    destination, jamais deux annonces separees pour le meme train
    physique (demande explicite de l'utilisateur, 2026-08-31)."""
    args = (mode, origin, destination, scheduled_dt, train_number)
    _say_parts(SOUND_MODERNE, _parts_stop_and_go(*args),
               _prepared.pop(_prepared_key(_speak_stop_and_go_announcement, args), None))


_PARTS = {}  # rempli apres les definitions (voir plus bas)


def _schedule_announcement(speak_fn, args, actual_dt, state, vj_id):
    """Programme l'annonce parlee ANNOUNCEMENT_LEAD_SEC avant l'heure REELLE
    (retard deja connu inclus) -- meme mecanisme que _schedule_ambient_sound
    (threading.Timer), pour que l'annonce precede le bruitage plutot que de
    partir des la decouverte du train (jusqu'a 1h a l'avance auparavant).
    Garde une reference au timer dans state["scheduled_announcements"] pour
    pouvoir l'annuler si le train est supprime avant que l'annonce ne parte
    (voir _process_station)."""
    delay = max(0.0, (actual_dt - datetime.now()).total_seconds() - ANNOUNCEMENT_LEAD_SEC)
    # Annonce previsible des maintenant : on la calcule tout de suite (voix,
    # morceaux manquants), elle sera seulement jouee a l'heure.
    key = _prepared_key(speak_fn, args)
    if key not in _prepared and speak_fn in _PARTS:
        _prepared[key] = morceaux.prepare(_PARTS[speak_fn](*args))
    timer = threading.Timer(delay, _enqueue_announcement, args=(speak_fn, args))
    timer.daemon = True
    timer.start()
    state["scheduled_announcements"][vj_id] = timer


# --- Annonces GROUPEES (2026-10-08) : plusieurs trains annonces a la suite ou
# quasi en meme temps sont dits en un seul message ("le TER ... et le TER ...
# vont etre mis en place") au lieu d'une annonce par train. Chaque annonce qui
# "part" entre d'abord dans un tampon ; une courte fenetre plus tard, tout ce
# qui s'est accumule est regroupe par type et enonce ensemble.
_GROUP_WINDOW_SEC = 4.0
_pending_ann = []
_group_timer = None
_group_lock = threading.Lock()
# type d'annonce -> (fragment par train, phrase groupee avec le verbe)
_GROUP_TEMPLATES = {
    _speak_depart_announcement: ("trains.frag_depart", "trains.groupe_depart"),
    _speak_terminus_announcement: ("trains.frag_terminus", "trains.groupe_terminus"),
    _speak_stop_and_go_announcement: ("trains.frag_sag", "trains.groupe_sag"),
}


_PARTS.update({_speak_depart_announcement: _parts_depart, _speak_terminus_announcement: _parts_terminus,
               _speak_stop_and_go_announcement: _parts_stop_and_go})


def _enqueue_announcement(speak_fn, args):
    global _group_timer
    with _group_lock:
        _pending_ann.append((speak_fn, args))
        if _group_timer is not None:
            _group_timer.cancel()
        _group_timer = threading.Timer(_GROUP_WINDOW_SEC, _flush_announcements)
        _group_timer.daemon = True
        _group_timer.start()


def _flush_announcements():
    with _group_lock:
        items = _pending_ann[:]
        _pending_ann.clear()
    if not items:
        return
    if len(items) == 1:  # un seul train : annonce habituelle, mot pour mot
        fn, args = items[0]
        fn(*args)
        return
    groups = {}  # speak_fn -> [args, ...] : on regroupe par type d'annonce
    order = []
    for fn, args in items:
        if fn not in groups:
            groups[fn] = []
            order.append(fn)
        groups[fn].append(args)
    for fn in order:
        group = groups[fn]
        if len(group) == 1:
            fn(*group[0])
        else:
            _speak_grouped(fn, group)


def _speak_grouped(fn, group):
    """Un message pour plusieurs trains du MEME type (departs ensemble,
    arrivees ensemble, arrets ensemble)."""
    frag_key, verb_key = _GROUP_TEMPLATES[fn]
    frags = []
    for args in group:
        _prepared.pop(_prepared_key(fn, args), None)  # annonce seule preparee : remplacee par le groupe
        mode, train_number = args[0], args[-1]
        mode_label = _train_label(mode, train_number)
        heure = _format_time_spoken(args[-2])
        if fn is _speak_depart_announcement:          # (mode, destination, scheduled_dt, train_number)
            frags.append(morceaux.fragments(frag_key, mode_label=mode_label, destination=args[1], v1=heure))
        elif fn is _speak_terminus_announcement:      # (mode, origin, scheduled_dt, train_number)
            frags.append(morceaux.fragments(frag_key, mode_label=mode_label, origin=args[1], v1=heure))
        else:                                         # stop_and_go (mode, origin, destination, scheduled_dt, train_number)
            frags.append(morceaux.fragments(frag_key, mode_label=mode_label, origin=args[1], destination=args[2], v1=heure))
    liste = []
    for i, f in enumerate(frags):
        if i == len(frags) - 1 and i > 0:
            liste += [(dire("trains.et"), "")]
        elif i > 0:
            liste[-1] = (liste[-1][0], ",")
        liste += f
    _say_parts(SOUND_MODERNE, morceaux.fragments(verb_key, nombre=nombre(len(group), "train"), liste=liste))


def _cancel_scheduled_announcement(state, vj_id):
    # L'annonce preparee d'avance (si elle existe) n'est plus a jouer.
    timer = state["scheduled_announcements"].pop(vj_id, None)
    if timer is not None and timer.args:
        prepared = _prepared.pop(_prepared_key(*timer.args), None)
        path = prepared.wait(0) if prepared else None
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    if timer is not None:
        timer.cancel()


def _speak_disruption(mode, destination, scheduled_dt, delay_min, motif=None, train_number=None):
    reason = morceaux.fragments("trains.motif", motif=motif) if motif else []
    _say_parts(SOUND_RETRO, morceaux.fragments(
        "trains.perturbation_destination_prevu_partira", mode_label=_train_label(mode, train_number),
        destination=destination, v1=_format_time_spoken(scheduled_dt), delay_min=str(delay_min), reason=reason))


def _speak_cancellation(mode, destination, scheduled_dt, motif=None, train_number=None):
    reason = morceaux.fragments("trains.motif_2", motif=motif) if motif else morceaux.whole(dire("trains.motif_non_precise"))
    _say_parts(SOUND_RETRO, morceaux.fragments(
        "trains.suppression_destination_prevu_supprime", mode_label=_train_label(mode, train_number),
        destination=destination, v1=_format_time_spoken(scheduled_dt), reason=reason))


def _schedule_train_event(state, vj_id, dep, arr, origins_by_id):
    """Classe un train en 3 categories (demande explicite de l'utilisateur,
    2026-08-31) selon sa presence dans /departures, /arrivals, ou les
    deux, et programme l'annonce + le bruitage adaptes -- UNE SEULE fois
    par train physique, jamais deux annonces separees pour un stop-and-go
    (contrairement au comportement precedent, qui traitait chaque train
    comme un depart ET une arrivee independants)."""
    if dep and arr:
        category = "stop_and_go"
        entry = dep  # mode/numero identiques cote depart et arrivee
    elif dep:
        category = "depart"
        entry = dep
    else:
        category = "terminus"
        entry = arr

    info = entry.get("display_informations", {})
    mode = _clean_train_mode(info.get("commercial_mode"))
    train_number = info.get("trip_short_name")

    if category == "depart":
        destination = _clean_place_name(info.get("direction")) or "destination inconnue"
        std = dep.get("stop_date_time", {})
        try:
            scheduled_dt = _parse_dt(std["base_departure_date_time"])
            actual_dt = _parse_dt(std["departure_date_time"])
        except (KeyError, ValueError):
            return
        _schedule_announcement(
            _speak_depart_announcement, (mode, destination, scheduled_dt, train_number), actual_dt, state, vj_id,
        )
        _schedule_ambient_sound(mode, "leaving", actual_dt)

    elif category == "terminus":
        origin = _origin_name(arr, origins_by_id)
        std = arr.get("stop_date_time", {})
        try:
            scheduled_dt = _parse_dt(std["base_arrival_date_time"])
            actual_dt = _parse_dt(std["arrival_date_time"])
        except (KeyError, ValueError):
            return
        _schedule_announcement(
            _speak_terminus_announcement, (mode, origin, scheduled_dt, train_number), actual_dt, state, vj_id,
        )
        _schedule_ambient_sound(mode, "arriving", actual_dt)

    else:  # stop_and_go
        origin = _origin_name(arr, origins_by_id)
        destination = _clean_place_name(dep.get("display_informations", {}).get("direction")) or "destination inconnue"
        std = arr.get("stop_date_time", {})
        try:
            scheduled_dt = _parse_dt(std["base_arrival_date_time"])
            actual_dt = _parse_dt(std["arrival_date_time"])
        except (KeyError, ValueError):
            return
        _schedule_announcement(
            _speak_stop_and_go_announcement, (mode, origin, destination, scheduled_dt, train_number), actual_dt, state, vj_id,
        )
        # dossier ter_sag/tgv_sag (nouveau, 2026-08-31) -- silence propre
        # si le dossier n'existe pas encore (voir _play_ambient_sound)
        _schedule_ambient_sound(mode, "sag", actual_dt)


def _process_station(state):
    """Un seul cycle : recupere departs ET arrivees ENSEMBLE (necessaire
    pour detecter les stop-and-go par correlation de vehicle_journey_id
    entre les deux listes -- impossible en les traitant separement comme
    avant). Le suivi perturbations/suppressions/retards reste scope sur
    /departures uniquement (c'est la que l'API expose les disruptions),
    inchange sur le fond."""
    departures, disruptions = fetch_departures()
    arrivals, origins_by_id = fetch_arrivals()
    if departures is None or arrivals is None:
        return

    dep_by_vj = {_vehicle_journey_id(d): d for d in departures}
    arr_by_vj = {_vehicle_journey_id(a): a for a in arrivals}
    all_vj = set(dep_by_vj) | set(arr_by_vj)

    for vj_id in all_vj:
        if vj_id not in state["announced_events"]:
            state["announced_events"].add(vj_id)
            _schedule_train_event(state, vj_id, dep_by_vj.get(vj_id), arr_by_vj.get(vj_id), origins_by_id)

    # --- perturbations/suppressions/retards : scope /departures, inchange ---
    disruption_by_vj = _disruption_lookup(disruptions)
    seen_dep_ids = set()

    for dep in departures:
        vj_id = _vehicle_journey_id(dep)
        seen_dep_ids.add(vj_id)

        info = dep.get("display_informations", {})
        mode = _clean_train_mode(info.get("commercial_mode"))
        train_number = info.get("trip_short_name")
        destination = _clean_place_name(info.get("direction")) or "destination inconnue"

        std = dep.get("stop_date_time", {})
        try:
            scheduled_dt = _parse_dt(std["base_departure_date_time"])
            actual_dt = _parse_dt(std["departure_date_time"])
        except (KeyError, ValueError):
            continue

        disruption = disruption_by_vj.get(vj_id)

        # --- suppression : priorité absolue, pas de test de retard ---
        if disruption and disruption["effect"] == "NO_SERVICE":
            if state["known_disruptions"].get(vj_id) != disruption["id"]:
                _cancel_scheduled_announcement(state, vj_id)
                _speak_cancellation(mode, destination, scheduled_dt, disruption["motif"], train_number)
                state["known_disruptions"][vj_id] = disruption["id"]
            continue

        # --- perturbation (retard) avec motif si connu ---
        delay_min = round((actual_dt - scheduled_dt).total_seconds() / 60)
        previous_delay = state["known_delays"].get(vj_id)
        motif = disruption["motif"] if disruption else None
        if delay_min >= DELAY_THRESHOLD_MIN:
            disruption_id = disruption["id"] if disruption else None
            if previous_delay != delay_min or state["known_disruptions"].get(vj_id) != disruption_id:
                _speak_disruption(mode, destination, scheduled_dt, delay_min, motif, train_number)
                state["known_delays"][vj_id] = delay_min
                state["known_disruptions"][vj_id] = disruption_id
        elif previous_delay is not None:
            # retard résorbé
            _say_parts(SOUND_MODERNE, morceaux.fragments("trains.destination_plus_retard_notable",
                                                         mode_label=_train_label(mode, train_number), destination=destination))
            state["known_delays"].pop(vj_id, None)
            state["known_disruptions"].pop(vj_id, None)

    # nettoyage : les trains sortis de la fenêtre (déjà partis) sont oubliés
    # (les annonces de ces trains-la sont deja parties depuis longtemps --
    # ANNOUNCEMENT_LEAD_SEC est bien plus court que UPCOMING_WINDOW_SEC --
    # pas besoin d'annuler leurs timers ici, deja executes ou expires)
    state["announced_events"] &= all_vj
    state["known_delays"] = {k: v for k, v in state["known_delays"].items() if k in seen_dep_ids}
    state["known_disruptions"] = {k: v for k, v in state["known_disruptions"].items() if k in seen_dep_ids}


def main():
    print(f"[sncf_watch] démarrage de la veille gare ({STATION_NAME}, UIC {STATION_UIC})")
    # Habillage sonore : lit d'ambiance en fond, bas volume, pendant que la
    # gare est ouverte (+ ambiance meteo si pluie/orage/neige/vent reel).
    # Thread demon : s'arrete avec la veille ; dossiers vides = silencieux.
    import gare_ambiance
    threading.Thread(target=gare_ambiance.watch, name="gare_ambiance", daemon=True).start()
    state = {
        # un seul set unifie (2026-08-31) -- avant, deux sets separes
        # (announced_departures/announced_arrivals) laissaient un
        # stop-and-go declencher deux annonces distinctes pour le meme
        # train physique.
        "announced_events": set(),
        "known_delays": {},
        "known_disruptions": {},
        # timers en attente (threading.Timer) pour les annonces parlees pas
        # encore parties -- cle vehicle_journey_id NU dans tous les cas
        # desormais (un seul timer par train, quelle que soit sa
        # categorie). Permet d'annuler une annonce si le train est
        # supprime avant l'heure prevue (voir _cancel_scheduled_announcement,
        # appele dans _process_station).
        "scheduled_announcements": {},
    }

    while True:
        _process_station(state)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
