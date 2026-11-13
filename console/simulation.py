"""Simulations des annonces des watchers (successeur de simulate_all.py,
perdu avec l'ancienne carte SD -- voir ROADMAP "suite 9").

Chaque scenario appelle DIRECTEMENT les fonctions d'annonce des watchers
deployes, avec des donnees fictives : on entend exactement le vrai rendu
(memes sons, memes phrases, meme verrou audio partage), sans attendre
qu'un vrai train soit supprime ou qu'une vraie alerte orange tombe.

Selection par mots-cles sur la ligne tapee apres "simulation" :
- "simulation train"          -> tous les scenarios qui portent "train" ;
- "simulation train retard"   -> seulement le plus precis (le plus de mots-cles) ;
- "simulation tout"           -> le catalogue complet ;
- "simulation" seul           -> liste les categories.
Les scenarios tournent dans un thread : la console reste utilisable.
"""
import os
import sys
import threading
from datetime import datetime, timedelta

import matcher

# nas_watch vient du depot (nas/), plus des anciens dossiers ~/autopilot qui
# passaient devant les modules a jour (2026-11-13)
_NAS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nas")
if _NAS not in sys.path:
    sys.path.append(_NAS)

from phrases import _code, dire, etiquette, nombre  # noqa: E402

TRIGGER_WORDS = {"simulation", "simulations", "simule", "simuler", "simulez"}
START_DELAY_SEC = 4


def _in_minutes(minutes):
    return (datetime.now() + timedelta(minutes=minutes)).replace(second=0, microsecond=0)


def _locked(fn):
    import audio_lock
    lock = audio_lock.acquire()
    try:
        fn()
    finally:
        audio_lock.release(lock)


# --- Trains (sncf_watch) ---

def train_depart():
    import sncf_watch
    sncf_watch._speak_depart_announcement("TER", "Lyon Part-Dieu", _in_minutes(4), "886512")


def train_arrivee():
    import sncf_watch
    sncf_watch._speak_terminus_announcement("TGV Inouï", "Paris Gare de Lyon", _in_minutes(3), "6671")


def train_passage():
    import sncf_watch
    sncf_watch._speak_stop_and_go_announcement("TER", "Le Puy-en-Velay", "Lyon Part-Dieu",
                                               _in_minutes(5), "886311")


def train_retard():
    import sncf_watch
    sncf_watch._speak_disruption("TER", "Firminy", _in_minutes(10), 12,
                                 motif="Difficultés lors de la préparation du train",
                                 train_number="886730")


def train_suppression():
    import sncf_watch
    sncf_watch._speak_cancellation("TGV Inouï", "Paris Gare de Lyon", _in_minutes(20),
                                   motif="Mouvement social", train_number="6672")


def train_bruitage():
    import sncf_watch
    sncf_watch._play_ambient_sound("tgv", "passby")


# --- Avions (adsb_watch) ---

def avion_contact():
    import adsb_watch
    from speak import speak_with_lead_sound
    text = "Avion détecté : AFR1234. Avion de ligne. Altitude 10500 mètres. Distance 12 kilomètres."
    _locked(lambda: speak_with_lead_sound(adsb_watch.SIMPLE_CONTACT_SOUND, text, label="simulation"))


def avion_survol():
    import adsb_watch
    adsb_watch._announce_flyby("EZY42KL")


# --- Meteo (meteo_watch) ---

def meteo_vigilance_monte():
    import meteo_watch
    meteo_watch._speak_lightly(
        f"Le niveau d'alerte orages pour le département de la {meteo_watch.DEPARTMENT_NAME} "
        f"vient de monter, passant de vert à jaune.")


def meteo_pluie():
    import meteo_watch
    data = {"wind_direction_10m": [290, 295, 300, 305], "wind_speed_10m": [18, 22, 25, 27]}
    meteo_watch._nowcast_announce(2, "precipitation", 30, data)


def meteo_alerte_orage():
    import meteo_watch
    meteo_watch._speak_initial_alert(3, "orages", meteo_watch.SAFETY_ADVICE["orages"])


def meteo_alerte_vent():
    import meteo_watch
    meteo_watch._speak_initial_alert(4, "vent", meteo_watch.SAFETY_ADVICE["vent"])


def meteo_approche():
    import meteo_watch
    meteo_watch._speak_proximity_alert(3, "orages", 15)


def meteo_calme():
    import meteo_watch
    meteo_watch._speak_lightly(
        f"Le niveau d'alerte orages pour le département de la {meteo_watch.DEPARTMENT_NAME} "
        f"vient de baisser, passant de orange à vert.")


# --- Crues (crue_watch) ---

def crue_changement():
    import crue_watch
    crue_watch._speak_lightly(
        f"Le niveau d'alerte crue pour le tronçon {crue_watch.TRONCON_NAME} "
        f"vient de monter, passant de vert à jaune.")


def crue_alerte():
    import crue_watch
    crue_watch._speak_initial_alert(3, dire("crue.safety_advice"))


# --- Bus et tram (stas_watch) ---

def stas_une():
    import stas_watch
    stas_watch._speak_new_disruptions([
        {"title": "Ligne T1 : travaux, l'arrêt Bellevue n'est pas desservi jusqu'à 18 heures."}])


def stas_plusieurs():
    import stas_watch
    stas_watch._speak_new_disruptions([
        {"title": "Ligne T3 : accident, trafic interrompu entre Châteaucreux et Carnot."},
        {"title": "Ligne M1 : déviation, rue de la République fermée."},
        {"title": "Ligne 14 : retards importants en raison d'une manifestation."}])


# --- Radio (radio_watch, messages du chien de garde du dongle) ---

def radio_perdue():
    from sound_paths import RADIO_SCANER_DIR
    from speak import speak_with_lead_sound
    _locked(lambda: speak_with_lead_sound(
        f"{RADIO_SCANER_DIR}/scaner_error.mp3",
        "Attention, le dongle radio semble indisponible. Vérifie la connexion USB.",
        label="simulation", repetitive=True))


def radio_revenue():
    from speak import speak
    _locked(lambda: speak("Le dongle radio est de nouveau disponible."))


# --- Disques (nas_watch) ---

def _disk_label(index):
    import nas_watch
    return nas_watch.DISKS[index]["label"]


def disque_perdu():
    import nas_watch
    nas_watch._speak(f"Attention, {_disk_label(1)} introuvable. Vérifie la connexion.")


def disque_revenu():
    import nas_watch
    nas_watch._speak(f"{_disk_label(1)} reconnecté, remontage automatique réussi.")


def disque_echec():
    import nas_watch
    nas_watch._speak(f"Attention, échec du remontage automatique du {_disk_label(0)}.")


# (nom parle, mots-cles, fonction). Le premier mot-cle est la categorie :
# commun a tous les scenarios du groupe, il les selectionne tous d'un coup.
# --- Imprimante (imprimante_watch, lib/sons_imprimante.py), 2026-10-18 ---
# Une seance d'impression enchainee, pour apprendre a reconnaitre chaque
# bruitage : chaque etape est d'abord nommee, puis on entend le vrai son (ou
# la vraie boucle, quelques secondes) et le vrai message. Rien n'est envoye a
# l'imprimante : aucun mouvement, aucune chauffe.
FICHIER_SIMULE = "support_casque.gcode"


def _etape(nom):
    from speak import speak
    _locked(lambda: speak(dire("simulation.etape", nom=nom)))


def _boucle(nom, secondes, phrase=None):
    import time
    import sons_imprimante as sons
    from speak import speak
    jeton = sons.demarrer_boucle(nom)
    try:
        if phrase:
            _locked(lambda: speak(phrase))
        time.sleep(secondes)
    finally:
        sons.arreter_boucle(jeton)


def impression_complete():
    import time
    import imprimante_watch as iw
    import sons_imprimante as sons
    from speak import speak
    pause = 1.5
    steps = [
        (dire("simulation.imp_chauffe"),
         lambda: (_boucle("printer_waiting_temperature_loop", 6),
                  iw._speak(dire("imprimante.evt_temperature_atteinte", nom=dire("imprimante.buse"), cible=210),
                            iw.PROGRES))),
        (dire("simulation.imp_tete"),
         lambda: _boucle("printer_moving_head_loop", 6, dire("imprimante.net.centrage"))),
        (dire("simulation.imp_plateau"),
         lambda: _boucle("printer_moving_bed_loop", 5, dire("imprimante.net.plateau_vient"))),
        (dire("simulation.imp_filament"),
         lambda: (_boucle("printer_pushing_filament_loop", 6),
                  sons.jouer("printer_filament_in", attendre=True),
                  _locked(lambda: speak(dire("imprimante.charge.fini"))))),
        (dire("simulation.imp_lancement"),
         lambda: iw._speak(dire("imprimante.evt_lancee", fichier=FICHIER_SIMULE) + " " +
                           dire("imprimante.evt_estimee", duree=iw._duree(2 * 3600 + 15 * 60)), iw.PROGRES)),
        (dire("simulation.imp_progression"),
         lambda: [iw._speak(dire("imprimante.evt_progression", pourcent=p) + " " +
                            dire("imprimante.reste", duree=iw._duree(r)), iw.PROGRES)
                  for p, r in ((25, 6000), (50, 4000), (75, 2000))]),
        (dire("simulation.imp_pause_filament"),
         lambda: (iw._speak(dire("imprimante.evt_pause", fichier=FICHIER_SIMULE), iw.WARN),
                  iw._speak(dire("imprimante.evt_filament_pause_active"), iw.WARN),
                  iw._speak(dire("imprimante.evt_filament_ok"), "printer_filament_in"),
                  iw._speak(dire("imprimante.evt_reprise")))),
        (dire("simulation.imp_fin"),
         lambda: iw._speak(dire("imprimante.evt_terminee", fichier=FICHIER_SIMULE,
                                duree=iw._duree(2 * 3600 + 22 * 60)), iw.FINI)),
        (dire("simulation.imp_retrait"),
         lambda: (sons.jouer("printer_filament_ejected", attendre=True),
                  _locked(lambda: speak(dire("imprimante.filament_retire", mm=50))))),
        (dire("simulation.imp_erreur"),
         lambda: iw._speak(dire("imprimante.evt_erreur", erreur=dire("simulation.imp_erreur_exemple")), iw.WARN)),
        (dire("simulation.imp_camera"),
         lambda: (sons.jouer("camera_take_picture", attendre=True),
                  _boucle("camera_loading_data_loop", 5, dire("vision.analyse_cours")))),
    ]
    for nom, action in steps:
        _etape(nom)
        action()
        time.sleep(pause)


SCENARIOS = [
    ("départ de train", ["train", "depart", "mise en place"], train_depart),
    ("arrivée de train au terminus", ["train", "arrivee", "terminus"], train_arrivee),
    ("train qui s'arrête et repart", ["train", "passage", "arret"], train_passage),
    ("train en retard", ["train", "retard", "perturbation train"], train_retard),
    ("train supprimé", ["train", "suppression", "supprime", "annulation"], train_suppression),
    ("bruitage de TGV qui passe", ["train", "bruitage", "ambiance", "tgv"], train_bruitage),
    ("nouvel avion détecté", ["avion", "contact", "detecte"], avion_contact),
    ("avion qui passe au-dessus", ["avion", "survol", "survole", "au dessus"], avion_survol),
    ("vigilance météo qui monte", ["meteo", "vigilance"], meteo_vigilance_monte),
    ("pluie qui approche", ["meteo", "pluie"], meteo_pluie),
    ("alerte orange orage", ["meteo", "orage", "alerte", "alerte orange"], meteo_alerte_orage),
    ("alerte rouge vent", ["meteo", "vent", "alerte", "alerte rouge"], meteo_alerte_vent),
    ("orage dans quinze minutes", ["meteo", "approche", "quinze minutes", "15 minutes"], meteo_approche),
    ("retour au calme météo", ["meteo", "calme", "fin d alerte"], meteo_calme),
    ("vigilance crue qui monte", ["crue", "vigilance crue", "changement"], crue_changement),
    ("alerte orange crue", ["crue", "alerte", "alerte crue", "inondation"], crue_alerte),
    ("une perturbation bus ou tram", ["stas", "bus", "tram", "une perturbation"], stas_une),
    ("plusieurs perturbations bus et tram", ["stas", "bus", "tram", "plusieurs"], stas_plusieurs),
    ("antenne radio perdue", ["radio", "dongle", "antenne", "perdue", "panne radio"], radio_perdue),
    ("antenne radio revenue", ["radio", "dongle", "antenne", "revenue", "retablie"], radio_revenue),
    ("disque perdu", ["disque", "nas", "perdu", "introuvable"], disque_perdu),
    ("disque reconnecté", ["disque", "nas", "reconnecte", "revenu"], disque_revenu),
    ("échec de remontage de disque", ["disque", "nas", "echec"], disque_echec),
    ("séance d'impression complète", ["imprimante", "impression", "printer", "imprimer"], impression_complete),
]


_running = None


def _nom(name):
    """Nom parle d'un scenario dans la langue (le nom francais reste la cle)."""
    return etiquette(f"simulation.scenario.{_code(name)}", name)


def _select(request):
    text = matcher.normalize(request)
    text = " ".join(w for w in text.split() if w not in TRIGGER_WORDS)
    if not text:
        return None
    if text in ("tout", "toutes", "complete", "tout le catalogue"):
        return list(SCENARIOS)
    scored = []
    for scenario in SCENARIOS:
        hits = sum(1 for kw in scenario[1] if f" {matcher.normalize(kw)} " in f" {text} ")
        if hits:
            scored.append((hits, scenario))
    if not scored:
        return []
    best = max(h for h, _ in scored)
    return [s for h, s in scored if h == best]


def _run(scenarios):
    # speak() du projet (espeak bloquant, sans verrou propre) : appele sous
    # _locked, il ne doit pas reprendre lui-meme le verrou.
    from speak import speak
    announce_each = len(scenarios) > 1
    for name, _, fn in scenarios:
        if announce_each:
            _locked(lambda n=name: speak(dire("simulation.annonce", nom=_nom(n))))
        try:
            fn()
        except Exception as exc:
            print(f"[console] simulation {name} en echec : {exc!r}", file=sys.stderr)
            _locked(lambda n=name: speak(dire("simulation.echec", nom=_nom(n))))


def handle(request):
    """Point d'entree depuis la console. Retourne la phrase de reponse ;
    les annonces simulees tournent ensuite en arriere-plan."""
    global _running
    selection = _select(request)
    if selection is None:
        return dire("simulation.disponibles", categories=dire("simulation.categories"))
    if not selection:
        return dire("simulation.non_reconnu", categories=dire("simulation.categories"))
    if _running is not None and _running.is_alive():
        return dire("simulation.deja_en_cours")
    # Petit delai : laisser la console dire sa reponse avant le premier scenario.
    _running = threading.Timer(START_DELAY_SEC, _run, args=(selection,))
    _running.daemon = True
    _running.start()
    if len(selection) == 1:
        return dire("simulation.annonce", nom=_nom(selection[0][0]))
    note = (" " + dire("simulation.alertes_durent")) if any("alerte" in s[0] for s in selection) else ""
    return dire("simulation.lances", scenarios=nombre(len(selection), "scénario de simulation lancé",
                                                      "scénarios de simulation lancés")) + note
