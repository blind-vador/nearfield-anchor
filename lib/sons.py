"""Catalogue des sons d'evenements (2026-10-23) : un son propre par evenement,
prepare et nomme par l'utilisateur, au lieu de sons partages par des choses
sans rapport (une detresse sonnait comme une panne du scanner).

chemin(evenement) rend le fichier a jouer : la copie de la carte SD
(~/autopilot/sons/...) d'abord, pour sonner des le demarrage et sans le NAS,
sinon celle du NAS (NAS_BASE/...), recopiee au passage sur la carte. Un
evenement inconnu ou un son introuvable : le repli donne, sinon None.

Ajouter un son : le poser sur le NAS sous server/<dossier>/, puis une ligne
ici ; la copie locale se fait toute seule au premier usage."""
import os
import shutil

from sound_paths import NAS_BASE

LOCAL_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sons")

SONS = {
    # avions
    "urgence_7700": "flight_scaner/urgence_code1.mp3",
    "urgence_7600": "flight_scaner/urgence_code2.mp3",
    "urgence_7500": "flight_scaner/urgence_code3.mp3",
    "message_avion": "flight_scaner/message_avion.mp3",       # ACARS, VDL2
    "helico": "flight_scaner/helico.mp3",
    "avion_leger": "flight_scaner/avion_leger.mp3",           # planeurs OGN, petits appareils
    "drone": "flight_scaner/drone.mp3",
    # radio
    "balise_detresse": "radio_scaner/balise_detresse.mp3",
    "aprs": "radio_scaner/aprs.mp3",
    "bipeur": "radio_scaner/bipeur.mp3",
    "dmr_vocal": "radio_scaner/dmr_vocal.mp3",
    "dmr_nouveau": "radio_scaner/dmr_nouveau_groupe.mp3",
    "satellite": "radio_scaner/satellite.mp3",
    "voiture": "radio_scaner/voiture.mp3",
    "capteur_meteo": "radio_scaner/capteur_meteo.mp3",
    "nouveau_capteur": "radio_scaner/nouveau_capteur.mp3",
    "nouvel_emetteur": "radio_scaner/nouvel_emetteur.mp3",
    "fin_ignore": "radio_scaner/fin_ignore.mp3",
    "scanner_reprise": "radio_scaner/scanner_reprise.mp3",
    "antenne_muette": "radio_scaner/antenne_muette_loop.mp3",
    "pmr446_fin": "radio_scaner/pmr446_fin.mp3",
    "trame_non_dite": "radio_scaner/trame_non_dite.mp3",
    # meteo et flash info
    "vigilance_baisse": "meteo_scaner/vigilence_baisse.mp3",
    "jingle_flashinfo": "jingles/flashinfo.mp3",
    "jingle_meteo": "jingles/meteo.mp3",
    "jingle_transport": "jingles/transport.mp3",
    "jingle_sport": "jingles/sport.mp3",
    # terrain, plantes, camera
    "terrain_urgence": "terrain/urgence.mp3",
    "terrain_alerte": "terrain/alerte.mp3",
    "plante_photo": "plantes/nouvelle_photo.mp3",
    "plante_besoin_eau": "plantes/besoin_d_eau.mp3",
    "plante_assez_eau": "plantes/assez_d_eau.mp3",
    "plante_trop_eau": "plantes/trop_d_eau.mp3",
    "plante_arrosage_valide": "plantes/arrosage_valide.mp3",
    "plante_observation_a_lire": "plantes/observation_a_lire.mp3",
    "plante_observation_a_confirmer": "plantes/observation_a_confirmer.mp3",
    "plante_observation_validee": "plantes/observation_validee.mp3",
    "camera_coupee": "camera/camera_coupee.mp3",
    # systeme
    "demarrage": "systeme/demarrage.mp3",
    "arret": "systeme/arret.mp3",
    "mise_a_jour": "systeme/mise_a_jour.mp3",
    "voix_en_calcul": "systeme/voix_en_calcul.mp3",
    "horloge_perdue": "systeme/horloge_perdue.mp3",
    "peripherique_decroche": "systeme/peripherique_decroche.mp3",
    "peripherique_branche": "systeme/peripherique_branche.mp3",
    "sous_tension": "systeme/sous_tension.mp3",
    "alarme_electrique": "systeme/alarme_electrique.mp3",     # surintensite USB, tension : en boucle avec la voix
    "pi_bride_chaleur": "systeme/pi_bride_chaleur.mp3",       # UNITE_TOO_HOT
    "pause_thermique": "radio_scaner/pause_thermique.mp3",    # POWER_LIMITED_DUE_TO_TEMPERATURE
    "nas_injoignable": "systeme/nas_injoignable.mp3",
    "journal_non_sauve": "systeme/journal_non_sauve.mp3",
    "veille_plantee": "systeme/veille_plantee.mp3",           # MICROSOFT-VOICES-UNREACHABLE : veille en boucle
    "mac_connecte": "systeme/mac_connecte.mp3",
    "mac_deconnecte": "systeme/mac_deconnecte.mp3",
    "pont_intrus": "systeme/pont_intrus.mp3",
    "securite_bilan": "systeme/securite_bilan.mp3",
    "securite_faille": "systeme/securite_faille.mp3",
    "non_lus": "systeme/non_lus.mp3",
}


def chemin(evenement, repli=None):
    rel = SONS.get(evenement)
    if not rel:
        return repli
    local = os.path.join(LOCAL_BASE, rel)
    if os.path.exists(local):
        return local
    nas = os.path.join(NAS_BASE, rel)
    if not os.path.exists(nas):
        return repli
    try:
        os.makedirs(os.path.dirname(local), exist_ok=True)
        tmp = f"{local}.{os.getpid()}"
        shutil.copyfile(nas, tmp)
        os.replace(tmp, local)
        return local
    except OSError:
        return nas


def manquants():
    """Evenements sans son ni sur la carte ni sur le NAS."""
    return [e for e in SONS if chemin(e) is None]
