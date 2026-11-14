"""Catalogue des sons d'evenements (2026-10-23) : un son propre par evenement,
prepare et nomme par l'utilisateur, au lieu de sons partages par des choses
sans rapport (une detresse sonnait comme une panne du scanner).

chemin(evenement) rend le fichier a jouer : la copie de la carte SD
(~/autopilot/sons/...) d'abord, pour sonner des le demarrage et sans le NAS,
sinon celle du NAS (NAS_BASE/...), recopiee au passage sur la carte. Un
evenement inconnu ou un son introuvable : le repli donne, sinon None.

Ajouter un son : le poser sur le NAS sous server/<dossier>/, puis une ligne
ici ; la copie locale se fait toute seule au premier usage."""
import chemins
import os
import shutil

from sound_paths import NAS_BASE

# ~/autopilot/sons en dur (axe 4) : avant, deduit de __file__ en supposant
# que le module vivait dans autopilot/pi_console ; depuis le depot, le calcul
# relatif pointait vers nearfield_anchor/sons (vide). Les sons deployes sont
# dans ~/autopilot/sons.
LOCAL_BASE = chemins.base("sons")

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
    # TETRA : sons propres (2026-10-30) ; avant, le TETRA empruntait les shims DMR
    "tetra_ouverture": "radio_scaner/tetra_ouverture.mp3",
    "tetra_appel_prive": "radio_scaner/tetra_appel_prive.mp3",
    "tetra_message": "radio_scaner/tetra_message.mp3",
    "tetra_nouvelle_porteuse": "radio_scaner/tetra_nouvelle_porteuse.mp3",
    "lora_message": "radio_scaner/lora_message.mp3",
    "satellite": "radio_scaner/satellite.mp3",
    "voiture": "radio_scaner/voiture.mp3",
    "capteur_meteo": "radio_scaner/capteur_meteo.mp3",
    "nouveau_capteur": "radio_scaner/nouveau_capteur.mp3",
    "nouvel_emetteur": "radio_scaner/nouvel_emetteur.mp3",
    "nommage": "radio_scaner/nommage.mp3",
    "fin_ignore": "radio_scaner/fin_ignore.mp3",
    "scanner_reprise": "radio_scaner/scanner_reprise.mp3",
    "antenne_muette": "radio_scaner/antenne_muette_loop.mp3",
    "pmr446_fin": "radio_scaner/pmr446_fin.mp3",
    "trame_non_dite": "radio_scaner/trame_non_dite.mp3",
    # meteo et flash info
    "vigilance_1": "meteo_scaner/vigilence_1.mp3",
    "vigilance_2": "meteo_scaner/vigilence_2.mp3",
    "vigilance_3": "meteo_scaner/vigilence_3.mp3",
    "vigilance_4": "meteo_scaner/vigilence_4.mp3",
    "vigilance_baisse": "meteo_scaner/vigilence_baisse.mp3",
    "meteo_averse": "meteo_scaner/averse.mp3",
    "meteo_neige": "meteo_scaner/neige.mp3",
    "meteo_gel": "meteo_scaner/gel.mp3",
    "meteo_foudre": "meteo_scaner/foudre.mp3",
    "jingle_flashinfo": "jingles/flashinfo.mp3",
    "jingle_meteo": "jingles/meteo.mp3",
    "jingle_transport": "jingles/transport.mp3",
    "jingle_sport": "jingles/sport.mp3",
    "jingle_resume_donnees": "jingles/resume_donnees.mp3",   # 2026-11-14 : plus celui du flash info
    # terrain, plantes, camera
    "terrain_urgence": "terrain/urgence.mp3",
    "terrain_alerte": "terrain/alerte.mp3",
    "plante_photo": "plantes/nouvelle_photo.mp3",
    "plante_a_recolter": "plantes/plante_a_recolter.mp3",
    "plante_besoin_eau": "plantes/besoin_d_eau.mp3",
    "plante_assez_eau": "plantes/assez_d_eau.mp3",
    "plante_trop_eau": "plantes/trop_d_eau.mp3",
    "plante_arrosage_valide": "plantes/arrosage_valide.mp3",
    "plante_observation_a_lire": "plantes/observation_a_lire.mp3",
    "plante_observation_a_confirmer": "plantes/observation_a_confirmer.mp3",
    "plante_observation_validee": "plantes/observation_validee.mp3",
    "sonde_stabilisation": "plantes/sonde_stabilisation_loop.mp3",   # boucle : la sonde attend que l'eau se repartisse
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
    # sante des disques (2026-11-12, lib/sante_disques.py) : sons d'attente, a remplacer par les tiens
    "disque_defaillant": "systeme/disque_defaillant.mp3",     # SMART en echec, auto-test rate : en boucle
    "disque_secteurs": "systeme/disque_secteurs.mp3",         # secteurs abimes : rappel toutes les 2 h
    "disque_erreurs": "systeme/disque_erreurs.mp3",           # erreurs d'E/S, lecture seule : rappel toutes les heures
    "disque_chaud": "systeme/disque_chaud.mp3",               # disque a 55 degres ou plus : rappel toutes les 30 min
}


def _variantes(rel):
    """Le fichier catalogué, puis le même nom sous une autre extension audio."""
    dossier, base = os.path.split(rel)
    tige, ext = os.path.splitext(base)
    exts = [ext] + [e for e in (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus") if e != ext]
    return [os.path.join(dossier, tige + e) if dossier else tige + e for e in exts]


def chemin(evenement, repli=None):
    rel = SONS.get(evenement)
    if not rel:
        return repli
    for variante in _variantes(rel):
        local = os.path.join(LOCAL_BASE, variante)
        if os.path.exists(local):
            return local
        nas = os.path.join(NAS_BASE, variante)
        if not os.path.exists(nas):
            continue
        try:
            os.makedirs(os.path.dirname(local), exist_ok=True)
            tmp = f"{local}.{os.getpid()}"
            shutil.copyfile(nas, tmp)
            os.replace(tmp, local)
            return local
        except OSError:
            return nas
    return repli


# --- reglages de la commande "sons" (console/sons_cmd.py) ---------------------------
# Par evenement : "son_phrase" (par defaut), "son_seul" (la phrase n'est pas
# dite ; une prise reste gardee au journal) ou "rien". Appliques la ou le son
# est joue (sound_player, speak, annonce_morceaux, prise_vocale), reconnu a son
# chemin : un changement vaut tout de suite, sans redemarrer les veilles.
MODES = ["son_phrase", "son_seul", "rien"]
REGLAGES = os.path.join(os.path.dirname(LOCAL_BASE), "sons_reglages.json")
_reglages = {"mtime": None, "data": {}}


def _lire_reglages():
    import json
    try:
        mtime = os.path.getmtime(REGLAGES)
    except OSError:
        return {}
    if mtime != _reglages["mtime"]:
        try:
            with open(REGLAGES, encoding="utf-8") as f:
                _reglages.update(mtime=mtime, data=json.load(f))
        except (OSError, ValueError):
            return {}
    return _reglages["data"]


def mode(evenement):
    m = _lire_reglages().get(evenement)
    return m if m in MODES else MODES[0]


def regler(evenement, nouveau):
    import json
    data = dict(_lire_reglages())
    if nouveau == MODES[0]:
        data.pop(evenement, None)
    else:
        data[evenement] = nouveau
    tmp = f"{REGLAGES}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, REGLAGES)
    _reglages.update(mtime=os.path.getmtime(REGLAGES), data=data)  # deux reglages dans le meme instant


def mode_chemin(chemin):
    """Mode de l'evenement dont c'est le son ; "son_phrase" pour tout autre son."""
    if not chemin or not _lire_reglages():
        return MODES[0]
    tige = os.path.splitext(os.path.basename(chemin))[0]
    for evenement, rel in SONS.items():
        if os.path.splitext(os.path.basename(rel))[0] == tige:
            return mode(evenement)
    return MODES[0]


# Evenements joues en alarme : sonnerie en boucle, message par-dessus, jusqu'au
# "compris" (lib/alarmes.py, 2026-10-24). Ceux qui appellent alarmes.declencher().
ALARMES = ["urgence_7700", "urgence_7600", "urgence_7500", "balise_detresse", "alarme_electrique", "antenne_muette",
           "sous_tension", "pi_bride_chaleur", "veille_plantee", "nas_injoignable",
           "disque_defaillant", "disque_secteurs", "disque_erreurs", "disque_chaud"]
# pi_bride_chaleur : un seul tour ; veille_plantee, nas_injoignable, disque_secteurs/erreurs/chaud : rappels espaces.


def manquants():
    """Evenements sans son ni sur la carte ni sur le NAS."""
    return [e for e in SONS if chemin(e) is None]
