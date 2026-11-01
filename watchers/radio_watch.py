import chemins
import collections
import math
import re
import signal
import subprocess
import sys
import tempfile
import json
import glob
import os
import time
import audioop
import threading

import juge_live
import queue
import socket

import numpy as np
import requests

from speak import journal_notice, speak, speak_with_lead_sound
import audio_lock
import airband_bridge
import radio_log
import sound_player
import audio_filters
import acars_watch
import satellites
import satellites_watch
import decodeurs
import dmr_filtres
import annonce_morceaux as morceaux
import restreint
import stations
import chaine_audio
import profils_bruit
import reglages_audio
import relais
import meteo_locale
import mesure_antenne
import ignores  # cibles mises de cote par la commande "ignorer" (2026-10-22)
import maintien  # tenir une ecoute selon l'interet ; "zapper" (2026-10-22)
import ctcss  # tonalite d'acces des relais, retrouvee dans le son capte (2026-10-23)
import prise_unique  # un seul rtl_fm, tous les decodeurs en meme temps (2026-10-25)
import tetra  # TETRA confie a la machine compagne (2026-10-26) ; le Pi ne decode pas
import adsb_local_watch
import aprs_watch
import balises_watch
import capteurs_watch
import lora_watch
import dab_watch
import pagers_watch
import sondes_watch
import vdl2_watch
import spectral_diagnostic
import noise_signatures
import attention
from manual_tuner import ManualStream, SETTLE_SEC as MANUAL_SETTLE_SEC
from rnnoise_lib import (
    create as _rnnoise_create,
    destroy as _rnnoise_destroy,
    process_mono_frame as _rnnoise_process_frame,
    FRAME_SIZE as RNNOISE_FRAME_SIZE,
    SAMPLE_RATE as RNNOISE_SAMPLE_RATE,
)

# SIGTERM (envoye par "systemctl stop/restart") tue le process par defaut
# SANS executer les blocs "finally" -- ce qui laissait regulierement des
# sous-processus orphelins (rtl_fm, aplay, multimon-ng, dsd-fme) tourner
# indefiniment apres l'arret du service, bloquant le redemarrage suivant
# ("deactivating"/"final-sigterm" observe plusieurs fois en session, chaque
# fois necessitant un pkill manuel). sys.exit() leve SystemExit, qui LUI
# deroule normalement la pile et execute les "finally" -- fix standard.
def _handle_sigterm(signum, frame):
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_sigterm)

# Compteur d'echecs consecutifs de rtl_power (dongle indisponible/deconnecte),
# incremente dans check_channels_relative/sweep_band_peak_relative, remis a zero
# des qu'un appel reussit. Utilise par le "chien de garde" dans main() pour
# avertir vocalement plutot que de rester silencieux indefiniment sans explication.
_rtl_power_failure_count = 0
DEVICE_DOWN_WARNING_THRESHOLD = 15  # environ 15 echecs consecutifs avant d'avertir
DEVICE_DOWN_REMINDER_EVERY = 200  # rappel periodique si ca reste indisponible longtemps

# Cycle marche/pause -- demande explicite (2026-08-30) : l'utilisateur a
# ressenti une legere decharge en touchant la jonction cable antenne/prise,
# absente auparavant, et s'inquiete de la chauffe du dongle/Pi en scan
# vraiment continu (24h/24 depuis l'activation au demarrage). Ceci ne
# diagnostique ni ne corrige un probleme electrique -- seul un controle
# physique par une personne voyante peut le faire -- mais reduit le temps
# total ou le dongle/l'antenne sont actifs, et donne un vrai repos
# thermique regulier plutot qu'un fonctionnement ininterrompu.
SCAN_ON_DURATION_SEC = 900   # 15 min de scan actif
SCAN_OFF_DURATION_SEC = 300  # 5 min de pause, dongle au repos

# Securite thermique independante du cycle regulier : verifie la
# temperature du Pi (vcgencmd, integre) et force une pause si elle depasse
# le seuil, meme en pleine phase "active". Mesure en session : 74.5C au
# repos relatif (charge CPU ~20%) sans throttling signale -- seuils choisis
# avec de la marge sous ce niveau deja constate.
TEMP_CHECK_INTERVAL_SEC = 60
TEMP_PAUSE_C_BASE = 72.0
# Plafond dur, JAMAIS depasse quelle que soit la marge adaptative ou l'etat
# de charge CPU -- Raspberry Pi OS declenche lui-meme un throttling au
# niveau du SoC BCM2711 aux alentours de 80-85C (confirme par recherche
# web en session, article framboise314 sur le decodage APRS). Aucune
# logique d'adaptation ci-dessous ne peut faire depasser ce seuil.
TEMP_CRITICAL_C = 80.0

# Veille meteo sous chaleur (demande utilisateur 2026-10-01) : pendant une
# pause thermique, si un bulletin est demande ou qu'une perturbation
# approche (niveau publie >= 2), on garde UNIQUEMENT les fenetres meteo
# (stations APRS, capteurs, ballon-sonde) -- tout le reste du scan reste
# suspendu -- tant que le Pi ne depasse pas METEO_PAUSE_MAX_C ; jusqu'a
# METEO_PAUSE_CRITICAL_MAX_C (exclu, c'est la coupure dure) si la menace
# est critique (niveau 3 : perturbation dangereuse toute proche).
METEO_PAUSE_MAX_C = 78.0
METEO_PAUSE_CRITICAL_MAX_C = TEMP_CRITICAL_C
METEO_PAUSE_TICK_SEC = 5
METEO_PAUSE_WATCH_EVERY_SEC = 300
METEO_PAUSE_WATCH = [("aprs", 30), ("capteurs", 30), ("sondes", 45)]
# Baisse de 65.0 a 60.0 (2026-08-30, demande explicite de l'utilisateur) :
# une pause de plus de 70 minutes suivie d'une re-pause au bout de
# seulement 2.5 minutes a ete observee en usage reel -- plus de marge de
# refroidissement demandee entre deux pauses.
TEMP_RESUME_C = 60.0  # hysteresis : attend d'etre redescendu sous ce seuil

# Reprise sur plateau (2026-09-29, demande explicite de l'utilisateur) :
# apres la reinstallation, le Pi restait a 65C AU REPOS, charge nulle,
# pendant 4 h -- jamais sous 60C, scanner bloque en pause indefiniment.
# Si la temperature ne baisse plus du tout pendant PLATEAU_WINDOW_SEC, le
# Pi a atteint son plancher de refroidissement : on reprend quand meme,
# mais seulement si ce plancher reste dans la plage demandee (60-68C).
# Au-dessus de PLATEAU_RESUME_MAX_C, on reste en pause et on previent une
# fois (ventilation a verifier). Ne remplace pas TEMP_RESUME_C : une
# reprise normale sous 60C reste prioritaire.
PLATEAU_WINDOW_SEC = 1200       # 20 min sans baisse = plancher atteint
PLATEAU_TOLERANCE_C = 1.5       # ecart max sur la fenetre (bruit de mesure du capteur)
PLATEAU_RESUME_MAX_C = 68.0     # borne haute de la reprise sur plateau
# Soupape (2026-10-07) : le seuil de reprise normal (TEMP_RESUME_C 60 C) est
# sous la temperature de repos du Pi (~65 C), donc souvent inatteignable ; la
# reprise ne tient plus qu'au plateau (20 min tres stables), fragile. Au-dela
# de MAX_PAUSE_SEC, si le Pi est simplement tiede (<= PLATEAU_RESUME_MAX_C,
# bien sous la base de coupure 72 C et le plafond 82 C), on reprend : un
# redemarrage a chaud ne doit pas figer le scanner une demi-heure.
MAX_PAUSE_SEC = 900

# Marge adaptative additionnelle (2026-08-30, demande explicite) : si les
# pauses thermiques se rapprochent dans le temps (intervalle actif entre
# deux pauses qui raccourcit -- signe d'une degradation progressive :
# chaleur ambiante qui monte, ventilation insuffisante...), etire
# automatiquement TEMP_RESUME_C encore plus bas pour forcer un repos plus
# long, plutot que de s'en tenir a un seuil fixe qui ne s'adapte jamais.
# Se relache tout seul si l'intervalle actif redevient normal.
TEMP_RESUME_ADAPTIVE_STEP_C = 2.0
TEMP_RESUME_ADAPTIVE_MAX_C = 10.0  # plancher absolu : TEMP_RESUME_C - ce max = 50.0C
PAUSE_HISTORY_LEN = 4  # "trace temporaire" demandee -- en memoire seulement, remise a zero a chaque redemarrage du service

# Seuil de coupure adaptatif (2026-08-31, demande explicite de
# l'utilisateur) : les pauses etaient jugees trop frequentes. Si le
# refroidissement d'une pause a ete rapide (bonne marge thermique/air
# ambiant frais -- comparee a l'historique des pauses precedentes), le
# seuil de coupure de la PROCHAINE periode active est releve un peu, sans
# jamais s'approcher du plafond critique a moins de
# TEMP_PAUSE_ADAPTIVE_MAX_C. Se relache si le refroidissement redevient
# lent. Meme mecanisme (deque glissant, comparaison a la moyenne) que
# l'adaptation du seuil de reprise ci-dessus.
TEMP_PAUSE_ADAPTIVE_STEP_C = 1.0
TEMP_PAUSE_ADAPTIVE_MAX_C = 6.0  # seuil effectif plafonne a TEMP_PAUSE_C_BASE + ce max = 78.0C, 2C sous TEMP_CRITICAL_C

# Zone de surveillance (2026-08-31, demande explicite) : si le seuil de
# coupure (adaptatif) est atteint mais que le CPU n'est pas charge par
# autre chose (load moyenne 1 min basse -- pas de gros calcul en cours,
# type dsd-fme/decode numerique), la chauffe est probablement due a
# l'environnement plutot qu'a une charge de calcul intense qui va
# s'aggraver -- au lieu d'une coupure franche, on reste actif mais on
# verifie beaucoup plus souvent, avec le plafond critique (TEMP_CRITICAL_C)
# comme seule limite absolue qui declenche quand meme une coupure.
CPU_LOAD_WATCH_THRESHOLD = 0.5
WATCH_ZONE_CHECK_INTERVAL_SEC = 15

# Contexte environnemental (2026-08-31, demande explicite) : le seuil de
# coupure fixe (72.0C) ignore que la marge de dissipation reelle du Pi
# depend fortement de l'heure et de la temperature exterieure -- de nuit la
# temperature ne peut que descendre (jamais de pic de chaleur avant le
# lever du jour), et une journee fraiche donne beaucoup plus de marge
# qu'une journee chaude. Reutilise l'API Open-Meteo (meme source deja
# validee dans meteo_watch.py, gratuite, sans cle) pour la temperature
# exterieure actuelle a la position du Pi (balcon, memes coordonnees que
# la veille meteo hyper-locale).
OUTDOOR_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
OUTDOOR_REF_LAT = 45.4397
OUTDOOR_REF_LON = 4.3872
# La temperature exterieure ne varie pas en quelques secondes -- inutile de
# la sonder a chaque verification thermique (60s) comme demande
# explicitement ("on ne devrait pas checker toute les 30 secondes").
OUTDOOR_TEMP_POLL_INTERVAL_SEC = 1800  # 30 min

# Nuit definie largement (22h-7h) : creneau ou la temperature exterieure ne
# peut que descendre, jamais remonter, avant le lever du jour.
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 7
NIGHT_BONUS_C = 2.0

# Bonus/malus sur le seuil de coupure selon la temperature exterieure --
# paliers recalibres (2026-08-31, retour utilisateur immediat) : 19.8C une
# nuit fraiche de fin aout tombait dans une zone "neutre" 18-25C qui ne
# donnait aucun bonus, alors que le seuil de base (72C) declenche des
# pauses meme par ~20C -- preuve que ce seuil de base a ete atteint dans
# des conditions plus chaudes que ca. Le palier "frais" est donc remonte a
# 20C pour couvrir ce cas (avec le bonus nuit +2C, ca sature au plafond
# ENV_BONUS_MAX_C=5.0 -- effective_pause_c = 77.0C, conforme au retour).
# Au-dela de 28C, on retire de la marge plutot que d'en ajouter (journee
# chaude = moins de dissipation reelle malgre un ciel degage).
OUTDOOR_TEMP_BONUS_TIERS = [
    # (seuil_max_exclu_C, bonus_C)
    (20.0, 3.0),
    (24.0, 1.5),
    (28.0, 0.0),
    (33.0, -2.0),
    (float("inf"), -4.0),
]
# Plafond du bonus environnemental combine (nuit + exterieur) -- meme
# esprit que TEMP_PAUSE_ADAPTIVE_MAX_C, additionne a celui-ci sous le meme
# garde-fou TEMP_CRITICAL_C - 2.0 (jamais contourne).
ENV_BONUS_MAX_C = 5.0

# Modes numeriques "donnees/texte" decodes en direct pendant l'ecoute FM.
# La voix numerique (DMR, P25, D-STAR...) est geree separement par dsd-fme
# (voir try_digital_voice_decode plus bas et ROADMAP.md).
# Liste complete disponible dans multimon-ng ("multimon-ng" sans argument) :
# POCSAG512/1200/2400 FLEX EAS UFSK1200 CLIPFSK FMSFSK AFSK1200/2400/2400_2/
# 2400_3 HAPN4800 FSK9600 DTMF ZVEI1/2/3 DZVEI PZVEI EEA EIA CCIR MORSE_CW X10.
# Retenus ici (meme outil deja valide, cout marginal nul) :
# - AFSK1200 : APRS (balises position/messages)
# - POCSAG512/1200/2400 : pageurs (protocole le plus courant)
# - FLEX : autre protocole de pageur, complementaire au POCSAG
# - FSK9600 : paquet radio 9600 bauds (AX.25/packet sur bande amateur 2m)
# - MORSE_CW : decodeur Morse direct
# - DTMF : tonalites multifrequences (controle repeteur, codes d'acces)
# - EEA/EIA/CCIR/ZVEI1/ZVEI2/ZVEI3/DZVEI/PZVEI : signalisation selective a
#   tons (normes francaise/europeennes, dispatch prive/securite -- pas les
#   services d'urgence officiels, deja exclus car chiffres/ANTARES).
# Ecartes : EAS (specifique USA), UFSK1200/CLIPFSK/FMSFSK/HAPN4800/AFSK2400_*
# (rares/obscurs dans ce contexte), X10 (domotique/alarme -- rabbit hole
# potentiellement invasif, pas ajoute sans demande explicite).
MULTIMON_ARGS = decodeurs.MULTIMON_ARGS  # regles communes (lib/decodeurs.py)

from sound_player import AUDIO_DEVICE, play_sound_effect
from sound_paths import RADIO_SCANER_DIR
from phrases import dire

# Effets sonores courts (habillage du scanner), fournis par l'utilisateur sur
# le NAS (deja monte localement sur le Pi, pas besoin de copie locale).
# Chemin de base centralise dans sound_paths.py (2026-08-31).
SOUND_EFFECTS_DIR = RADIO_SCANER_DIR
FREQ_OPEN_SOUND = os.path.join(SOUND_EFFECTS_DIR, "freq_open.mp3")
FREQ_OVER_SOUND = os.path.join(SOUND_EFFECTS_DIR, "freq_over.mp3")


def _son_fin(freq_hz):
    """Son de fin d'ecoute : celui du PMR446 sur ses canaux (2026-10-23, sons.py)."""
    import sons
    if 446.0e6 <= freq_hz <= 446.2e6:
        return sons.chemin("pmr446_fin", FREQ_OVER_SOUND)
    return FREQ_OVER_SOUND
SCANER_ERROR_SOUND = os.path.join(SOUND_EFFECTS_DIR, "scaner_error.mp3")
# Sons fournis par l'utilisateur en session (2026-08-30) : "incoming" joue
# avant l'annonce d'une reception capturee (tampon pendant la synthese
# cloud de l'annonce, voir speak_with_lead_sound) ; "decoding_numeric"/
# "decoding_morse" jouent pendant les deux chemins de decodage numerique
# (voix DMR/P25 via dsd-fme, et messages texte pageur/APRS via multimon-ng)
# pour combler le delai entre la detection et la vocalisation.
INCOMING_SOUND = os.path.join(SOUND_EFFECTS_DIR, "incoming.mp3")
DECODING_NUMERIC_SOUND = os.path.join(SOUND_EFFECTS_DIR, "decoding_numeric.mp3")
DECODING_MORSE_SOUND = os.path.join(SOUND_EFFECTS_DIR, "decoding_morse.mp3")
import sons
DMR_VOCAL_SOUND = sons.chemin("dmr_vocal", DECODING_NUMERIC_SOUND)  # son propre (2026-10-23, sons.py)
DMR_NOUVEAU_SOUND = sons.chemin("dmr_nouveau", DECODING_NUMERIC_SOUND)
# TETRA : sons propres (2026-10-30) ; repli sur le son DMR si absent.
TETRA_OUVERTURE_SOUND = sons.chemin("tetra_ouverture", DMR_VOCAL_SOUND)
TETRA_APPEL_PRIVE_SOUND = sons.chemin("tetra_appel_prive", TETRA_OUVERTURE_SOUND)
TETRA_MESSAGE_SOUND = sons.chemin("tetra_message", DMR_NOUVEAU_SOUND)
TETRA_PORTEUSE_SOUND = sons.chemin("tetra_nouvelle_porteuse", DMR_NOUVEAU_SOUND)

# --- Bandes surveillees ---
# type "fm_channels" : liste de frequences fixes en FM etroite (PMR446, marine...)
# type "sweep"        : balayage large, verrouillage sur le pic le plus fort trouve

# PMR446 et toutes les frequences aeronautiques locales (ATIS, tour, sol,
# approche Lyon, Saint-Etienne) sont gerees depuis le 2026-08-30 par
# RTLSDR-Airband (voir airband_bridge.py, AIRBAND_WINDOWS) -- squelch reel
# base sur le SNR par canal au lieu du couple rtl_power (pic brut) +
# discrimination RNNoise utilise ici. La bande "aviation_locale" (type
# fm_channels) est donc entierement retiree de BANDS -- plus aucune bande
# fm_channels active pour l'instant (l'infrastructure check_channels_relative/
# scan_fm_band reste en place, generique, pour une eventuelle bande a canaux
# fixes future). Migration hybride assumee : seules les bandes a canaux fixes
# connus a l'avance basculent, les bandes "sweep" (dont le but est justement
# de trouver une frequence inconnue) restent ici -- voir ROADMAP.md.

# Seuils margin_db calibres par bande (2026-08-31, demande explicite de
# l'utilisateur -- "nivelage" bande par bande plutot qu'un defaut unique
# 9.0 pour toutes). Methode : releve statistique reel sur le Pi (rtl_power,
# memes parametres bas/haut/step que chaque bande, 3 sondages, pics DC de
# rtl_power exclus comme le fait deja _read_csv_bins), plancher = 20e
# percentile (meme convention que le code), marge = ecart 99e-20e
# percentile (caracterise la variabilite normale du bruit de fond en
# excluant un eventuel pic isole reel, plutot que le max absolu qui aurait
# pu inclure une vraie transmission captee pendant la mesure) + 2 dB de
# securite, ecrete a [5.0, 12.0] pour rester dans une plage raisonnable
# autour du defaut (9.0). fm_diffusion NON touchee : deja calibree contre
# un signal reel connu (97.1 MHz), plus fiable qu'une mesure statistique
# de bruit -- une bande FM commerciale emet en continu 24h/24, ce qui aurait
# fausse la mesure de "plancher" pour cette bande specifique.
BANDS = [
    {"name": "cb_am", "name_spoken": "CB 27 megahertz", "type": "sweep",
     # La variante NFM ("cb") a ete retiree (2026-08-31, preuve directe en
     # session) : une capture reelle sur "cb" (NFM) analysee spectralement
     # -- signal fort et stable (-14 a -18dB, quasi sans variation, donc
     # PAS un probleme de reception faible), mais spectre tres tonal
     # (aplatissement 0.0009) concentre 500-1500Hz (formants voix), typique
     # d'un signal AM demodule en FM par erreur (le demodulateur FM
     # interprete les variations d'amplitude de l'AM comme du bruit de
     # frequence -- voix reconnaissable mais grésillante/deformee). La CB
     # en France est traditionnellement en AM (canal d'appel 27/27.275 MHz
     # notamment) -- avec la preuve que le trafic local reel est en AM,
     # la variante NFM ne faisait que capturer des copies degradees du
     # meme trafic au hasard de la rotation round-robin entre les deux.
     # Retiree plutot que rendue redondante : libere un passage de
     # rotation pour les autres bandes.
     "low_hz": 26_960_000, "high_hz": 27_410_000, "step_hz": 10_000, "mode": "am",
     "margin_db": 11.0},
    {"name": "lpd433", "name_spoken": "LPD 433", "type": "sweep",
     # Equivalent europeen non-licencie du PMR446, meme categorie d'usage
     # (talkies-walkies bon marche, jouets radio, etc.). Chevauche la plage
     # radioamateur 70cm (430-440 MHz) deja balayee plus bas -- entree
     # separee volontaire pour augmenter la frequence d'echantillonnage de
     # cette portion precise, plus susceptible d'activite que le reste de la
     # bande amateur (usage libre sans licence = beaucoup plus d'appareils).
     "low_hz": 433_075_000, "high_hz": 434_775_000, "step_hz": 25_000, "mode": "nfm",
     "margin_db": 5.7},
    {"name": "marine_vhf", "name_spoken": "VHF marine", "type": "sweep",
     "low_hz": 156_000_000, "high_hz": 162_025_000, "step_hz": 25_000, "mode": "nfm",
     "margin_db": 5.0},
    {"name": "aviation", "name_spoken": "aviation civile VHF", "type": "sweep",
     "low_hz": 118_000_000, "high_hz": 137_000_000, "step_hz": 25_000, "mode": "am",
     "margin_db": 10.8},
    {"name": "milair", "name_spoken": "aviation militaire UHF", "type": "sweep",
     # Bande aeronautique militaire UHF, standard international (OTAN),
     # meme convention que l'aviation civile (AM, pas de 25 kHz). Ecoute
     # passive uniquement -- comme partout ailleurs dans ce scanner, aucune
     # tentative de dechiffrement (le pipeline ne le permettrait de toute
     # facon pas) ; tout trafic chiffre est simplement ignore, meme
     # traitement que ANTARES/TETRA/GSM-R deja exclus plus haut. Precedent
     # confirme en session : pan etabli et normalise du hobby scanner
     # internationalement (RadioReference Wiki/Forums dedies), consensus
     # de la communaute = ecoute du non-chiffre legale, decodage du chiffre
     # non.
     "low_hz": 225_000_000, "high_hz": 400_000_000, "step_hz": 25_000, "mode": "am",
     "margin_db": 12.0},
    {"name": "radioamateur_10m", "name_spoken": "radioamateur 10 mètres", "type": "sweep",
     # Bande manquante par rapport aux autres (6m/2m/70cm/23cm deja
     # couvertes). Mode AM choisi car c'est historiquement et encore
     # aujourd'hui le mode dominant pour les echanges vocaux sur 10m chez
     # les radioamateurs (contrairement au 6m/2m/70cm largement FM/numerique
     # en pratique) -- SSB/CW aussi autorises mais non demodulables
     # correctement par rtl_fm.
     "low_hz": 28_000_000, "high_hz": 29_700_000, "step_hz": 25_000, "mode": "am",
     "margin_db": 8.3},
    {"name": "radioamateur_6m", "name_spoken": "radioamateur 6 mètres", "type": "sweep",
     "low_hz": 50_000_000, "high_hz": 51_000_000, "step_hz": 12_500, "mode": "nfm",
     "margin_db": 11.7},
    {"name": "radioamateur_2m", "name_spoken": "radioamateur 2 mètres", "type": "sweep",
     "low_hz": 144_000_000, "high_hz": 146_000_000, "step_hz": 12_500, "mode": "nfm",
     "margin_db": 5.6},
    {"name": "radioamateur_70cm", "name_spoken": "radioamateur 70 centimètres", "type": "sweep",
     "low_hz": 430_000_000, "high_hz": 440_000_000, "step_hz": 12_500, "mode": "nfm",
     "margin_db": 12.0},
    {"name": "radioamateur_23cm", "name_spoken": "radioamateur 23 centimètres", "type": "sweep",
     "low_hz": 1_240_000_000, "high_hz": 1_300_000_000, "step_hz": 25_000, "mode": "nfm",
     "margin_db": 8.4},
    {"name": "uhf_business", "name_spoken": "radio professionnelle UHF", "type": "sweep",
     "low_hz": 440_000_000, "high_hz": 470_000_000, "step_hz": 12_500, "mode": "nfm",
     "margin_db": 12.0},
    {"name": "vhf_business", "name_spoken": "radio professionnelle VHF", "type": "sweep",
     # Equivalent VHF de la bande "radio professionnelle UHF" ci-dessus --
     # plage classique en Europe pour le dispatch prive (taxis, securite
     # evenementielle, chantiers, maintenance). Chevauche volontairement la
     # VHF marine deja couverte (meme logique que LPD433/70cm plus haut).
     "low_hz": 148_000_000, "high_hz": 174_000_000, "step_hz": 12_500, "mode": "nfm",
     "margin_db": 7.6},
    {"name": "pmse", "name_spoken": "micros HF sans fil", "type": "sweep",
     # Bande PMSE officielle en France (Programme Making and Special
     # Events -- micros HF, in-ear, liaisons audio de spectacle/evenementiel/
     # theatre/eglise), autorisee au moins jusqu'en 2030 (confirme par
     # recherche web en session). Juste au-dessus de la bande UHF pro
     # ci-dessus, pas de chevauchement. Trafic reel local et imprevisible en
     # temps reel (spectacle en cours a proximite), pas de la diffusion.
     "low_hz": 470_000_000, "high_hz": 694_000_000, "step_hz": 25_000, "mode": "nfm",
     "margin_db": 12.0},
    {"name": "vhf_80", "name_spoken": "VHF 52 à 85 mégahertz", "type": "sweep",
     # Trou entre le radioamateur 6 m (51 MHz) et la radio FM (85 MHz), non
     # balaye jusqu'ici (2026-10-01) : ancienne "bande 80 MHz" des services
     # publics, restes possibles de reseaux locaux analogiques (taxis,
     # entretien, reseaux forestiers), ancienne bande TV I.
     "low_hz": 52_000_000, "high_hz": 85_000_000, "step_hz": 12_500, "mode": "nfm",
     "margin_db": 10.0},
    {"name": "uhf_410", "name_spoken": "UHF 406 à 430 mégahertz", "type": "sweep",
     # Trou entre les ballons-sondes / balises (406 MHz) et le radioamateur
     # 70 cm (430 MHz), non balaye jusqu'ici (2026-10-01) : reseaux
     # professionnels, anciens reseaux analogiques.
     "low_hz": 406_100_000, "high_hz": 430_000_000, "step_hz": 12_500, "mode": "nfm",
     "margin_db": 10.0},
    {"name": "vhf_low", "name_spoken": "VHF basse", "type": "sweep",
     # Trou entre la CB (jusqu'a 27.41 MHz) et le radioamateur 6m (50 MHz).
     # Historiquement dispatch taxi/forestier, en declin mais possibles
     # survivances locales.
     "low_hz": 30_000_000, "high_hz": 50_000_000, "step_hz": 12_500, "mode": "nfm",
     "margin_db": 11.4},
    {"name": "fm_diffusion", "name_spoken": "radio FM", "type": "sweep",
     # Plage legerement elargie (85-110 au lieu de 87.5-108 legal) pour
     # capter les debordements de radio pirate FM aux marges de la bande.
     "low_hz": 85_000_000, "high_hz": 110_000_000, "step_hz": 100_000, "mode": "wfm",
     # Marge reduite par rapport au defaut (9 dB) : la FM commerciale est un
     # signal large bande (~200 kHz) et fort quand present, contrairement aux
     # bandes etroites (446, aviation) ou la marge stricte sert a rejeter du
     # bruit/parasites. Calibre sur mesure reelle en session : station locale
     # Saint-Etienne (97.1 MHz) mesuree a 7.17 dB au-dessus du plancher local
     # -- 6 dB capte ce signal reel avec une petite marge de securite.
     "margin_db": 6.0},
]
# Note : le trafic radioamateur relaye par satellite (ISS, cubesats...) tombe dans les
# memes plages 2m/70cm/23cm ci-dessus, pas besoin d'entree dediee.
# HF (160m/80m/40m/20m, ondes courtes...) : HORS DE PORTEE avec ce dongle,
# confirme par test en session (pas juste un choix editorial). "-E direct"
# (rtl_fm) / "-D" (rtl_power) demarre sans erreur logicielle mais produit
# ZERO octet de donnees sur plusieurs frequences/modes testes (dont
# 4625 kHz, "UVB-76") -- root cause probable : ce dongle generique
# ("Realtek RTL2838UHIDIR, Generic RTL2832U OEM") n'a vraisemblablement pas
# le commutateur materiel d'echantillonnage direct present sur les dongles
# dedies HF (RTL-SDR Blog v3/v4). Le pilote ne remonte aucune erreur car la
# commande "reussit" de son point de vue, le probleme est en aval dans le
# circuit. Necessiterait un dongle avec vrai support HF materiel ou un
# upconverter externe -- ne pas retenter sur ce materiel sans lecture de
# cette note. Independamment de cette limitation materielle, ces bandes
# seraient de toute facon de la RADIODIFFUSION (contenu a sens unique), pas
# des echanges reels entre personnes -- voir plus haut dans ce document :
# priorite explicite aux echanges locaux imprevisibles. Exception notee par
# l'utilisateur : les "stations fantomes" type UVB-76 (imprevisibilite
# genuine, contenu mysterieux) l'interessent malgre le caractere
# unidirectionnel -- mais restent bloquees par le materiel pour l'instant.
#
# Deliberement NON inclus, confirme inecoutable par recherche documentaire (2026) :
# - Secours (police/pompiers/SAMU) : reseau ANTARES, numerique chiffre (TETRAPOL,
#   380-400 MHz), aucun outil ici ne peut le decoder.
# - SNCF grandes lignes : GSM-R chiffre (876-880/921-925 MHz).
# - Manoeuvres SNCF modernes : reseau TETRA chiffre.
# La bande "radio professionnelle UHF" ci-dessus (440-470 MHz) est un compromis :
# elle peut capter du trafic local non chiffre (securite, taxis, evenementiel,
# eventuels restes de manoeuvre ferroviaire regionale en analogique) mais rien n'est
# garanti -- ce sont des frequences attribuees au cas par cas, pas une liste fixe.

SQUELCH_CHECK_INTEGRATION_SEC = 1

# --- Squelch RELATIF au plancher de bruit (pas de seuil absolu global) ---
# Un pic n'est retenu que s'il depasse le plancher de bruit local (mediane des
# lectures de la bande / du passage) d'au moins cette marge, en dB.
NOISE_MARGIN_DB = 9.0

# --- Discrimination bruit / signal reel : RNNoise (reseau de neurones) ---
# Remplace l'ancienne heuristique maison (ratio passe-haut + taux de passage
# par zero, calibree "a l'aveugle" sur donnees synthetiques -- voir ROADMAP.md
# 2026-08-30) par un modele entraine specifiquement a distinguer voix et
# bruit large bande (Xiph/Mozilla RNNoise, via le wrapper local rnnoise_lib/).
# rnnoise_process_frame() retourne une probabilite de voix par frame de 10ms
# a 48kHz.
#
# IMPORTANT, trouve en session (2026-08-30) sur une VRAIE capture radio (pas
# du bruit blanc synthetique) : la probabilite par morceau de 200ms oscille
# violemment d'un morceau au suivant sur du bruit reel (mesure : 0.16 a 0.82
# sur une meme frequence vide en 4 secondes) -- un seuil brut applique
# directement au morceau courant declenchait donc de faux positifs frequents
# (le scanner "s'arretait sur tout", signale par l'utilisateur). Lissage par
# moyenne mobile exponentielle avant de comparer au seuil : nettement plus
# resistant a un pic isole de bruit. Seuil et facteur de lissage sont des
# POINTS DE DEPART, a affiner avec un retour reel sur le terrain, comme tous
# les autres seuils de ce fichier.
SPEECH_PROB_THRESHOLD = 0.65
SPEECH_PROB_SMOOTHING_ALPHA = 0.3  # poids du morceau courant dans la moyenne mobile

# --- Comportement scanner "nerveux" ---
# Porte de 1,5 a 3 s (2026-09-30, priorites fixees avec l'utilisateur pour une
# veille citoyenne : une fin coupee trop tot -- un interlocuteur qui marque une
# pause puis reprend -- est pire qu'un peu de souffle en fin de capture).
SILENCE_DEBOUNCE_SEC = 3.0
# Coupure precoce (etude du 2026-09-30) : la boucle ne s'arretait sur un
# silence qu'APRES avoir reconnu un signal ; un candidat juge "bruit" etait
# donc diffuse jusqu'a MAX_HOLD_SEC (duree mediane mesuree : 64 s, 290 cas sur
# 296, soit 6 h de souffle diffuse en septembre). Sans aucun signal reconnu
# apres EARLY_CUT_SEC, on arrete.
EARLY_CUT_SEC = 8
MAX_HOLD_SEC = 45
MIN_RMS_ACTIVE = 60
ANALYSIS_WINDOW_MS = 200  # granularite de l'analyse bruit/signal pendant le direct

# Il faut ce nombre de fenetres CONSECUTIVES classees "signal" avant de considerer
# qu'il y a vraiment une reception (evite de reagir a un simple declic/artefact
# d'ouverture de squelch d'une seule fenetre).
MIN_CONSECUTIVE_SIGNAL_WINDOWS = 3

# Voix numerique (DMR, P25, D-STAR, YSF, X2-TDMA, dPMR...) via dsd-fme, compile
# manuellement (voir ROADMAP.md). Tente d'abord un decodage numerique direct
# depuis le dongle sur les candidats FM etroite ; si rien n'accroche, on
# retombe sur l'ecoute analogique classique.
# Reactive en session : dsd-fme n'a pas de sortie ALSA directe (seulement
# pulse/udp/null/dev-dsp -- voir "dsd-fme -h"), et c'etait justement sa sortie
# "-o pulse" par defaut qui avait bloque tout le scanner au debut de la
# session (PulseAudio pas fiable en contexte service headless). Fix : sortie
# "-o udp" (dsd-fme "blaste" l'audio decode en PCM brut par UDP local) que
# l'on relit nous-memes via aplay/AUDIO_DEVICE, exactement comme pour l'audio
# analogique -- on ne demande plus jamais a dsd-fme de toucher l'audio
# lui-meme, PulseAudio n'est plus dans la chaine.
DSD_FME_AVAILABLE = True
DSD_FME_UDP_HOST = "127.0.0.1"
DSD_FME_UDP_PORT = 23456
# Frequence d'echantillonnage standard de la voix synthetisee par mbelib/dsd-fme.
DSD_FME_VOICE_SAMPLE_RATE = 8000
# En mode -fa, dsd-fme sort la voix en STEREO (une voie par slot DMR/TDMA,
# s16le entrelace, datagrammes de 640 octets). La relire en mono faisait
# jouer l'audio deux fois trop lentement, une octave plus grave, avec les
# deux slots melanges en gresillements.
DSD_FME_VOICE_CHANNELS = 2
# Modes tentes en plus du -fa quand une reception analogique est ecartee
# (un signal numerique passe pour du bruit aux yeux du detecteur de voix) :
# (plage basse, plage haute, option dsd-fme, nom dit). dPMR, NXDN et M17
# sortent en MONO (verifie dans dsd_main.c : pulse_digi_out_channels = 1).
DSD_FME_EXTRA_MODES = decodeurs.DSD_EXTRA_MODES
DSD_FME_MONO_MODES = decodeurs.DSD_MONO_MODES
# Duree max d'attente d'un premier paquet audio decode avant d'abandonner et
# de retomber sur l'ecoute analogique. Volontairement court (le scanner est
# "nerveux", pas question de s'attarder sur chaque candidat) -- a ajuster si
# le decodage reel met plus de temps a accrocher en pratique.
DSD_FME_PROBE_SEC = 6
# Validation d'un vrai decodage (2026-10-01) : dsd-fme peut sortir du son
# synthetise a partir de bruit (NXDN sans aucune synchro, M17 avec erreurs
# de controle sur 9 trames sur 10 : voix deformee, carillon sans message).
# Le son n'est joue -- et le carillon, l'annonce de la bande -- qu'apres
# au moins DSD_FME_MIN_CLEAN_SYNC synchros sans erreur, representant au
# moins DSD_FME_MIN_CLEAN_RATIO des synchros vues.
DSD_FME_MIN_CLEAN_SYNC = decodeurs.DSD_MIN_CLEAN_SYNC
DSD_FME_MIN_CLEAN_RATIO = decodeurs.DSD_MIN_CLEAN_RATIO
DSD_FME_PROTOCOLS = decodeurs.DSD_PROTOCOLS
# Taille des datagrammes UDP de dsd-fme (dsd_audio2.c) : 640 octets = stereo
# 16 bits (deux slots TDMA), 320 = mono. Plus fiable que de deviner d'apres
# l'option de mode, certains protocoles du -fa sortant en mono.
DSD_FME_STEREO_DATAGRAM = 640
DSD_FME_MONO_DATAGRAM = 320
# Une fois la voix accrochee, on continue tant que des paquets arrivent :
# fin apres DSD_FME_IDLE_SEC sans paquet, plafond DSD_FME_MAX_SEC.
DSD_FME_IDLE_SEC = 10.0  # 2026-10-03 : conversation DMR coupee entre deux interlocuteurs a 3 s
DSD_FME_MAX_SEC = 300  # une vraie conversation (qso) dure plusieurs minutes
DSD_FME_PRE_MAX = 500  # datagrammes gardes avant la confirmation (~10 s en stereo)
# Duree minimale d'ecoute une fois une voix reconnue (2026-10-19, toutes les
# bandes) : un clic de micro raccroche ne ferme pas l'ecoute en 3 s, et une
# reponse qui suit de pres n'est pas perdue.
MIN_ECOUTE_SEC = 8.0

# Rafales (2026-11-01, demande utilisateur) : le "clic" d'une fin d'emission
# (over, poste qui raccroche) ouvrait l'ecoute et journalisait une prise d'une
# seconde au plus. On n'ouvre plus -- ceremonie, son, journal -- qu'apres
# MIN_VOIX_OUVERTURE_SEC de voix reconnue. Une rafale plus courte reste en
# tampon et on surveille encore le canal (ATTENTE_RAFALE_SEC en analogique, la
# fenetre de fin du numerique sinon) : si une reponse vient, elle ouvre avec la
# rafale en tete ; sinon rien n'est joue ni journalise.
MIN_VOIX_OUVERTURE_SEC = 1.2
ATTENTE_RAFALE_SEC = 6.0

# Fenetre de reponse (2026-10-28, demande utilisateur) : une fois le locuteur
# silencieux, on NE FERME PAS aussitot -- on garde le canal ouvert ce temps-la
# pour qu'une reponse qui revient (l'autre interlocuteur, apres le "a vous") ne
# tombe pas sur un canal deja referme. Equivalent analogique de DSD_FME_IDLE_SEC
# (la voix numerique attend deja 10 s depuis le 2026-10-03, pour la meme raison :
# conversation coupee entre deux interlocuteurs). Sert de base a l'attente de fin
# FM quand l'utilisateur n'a pas regle "attente_fin_s" a la main ; maintien.delai
# la module ensuite selon l'interet du canal, MIN_ECOUTE_SEC reste le plancher et
# maintien.plafond le plafond.
ATTENTE_REPONSE_SEC = 7.0

# Pic DC : artefact materiel propre aux dongles RTL-SDR (architecture a conversion
# directe), qui apparait comme un pic artificiel au centre exact de chaque segment
# balaye par rtl_power. On exclut les bins trop proches du centre de chaque
# segment pour ne pas le prendre pour un vrai signal.
DC_SPIKE_GUARD_BINS = 3


def _read_csv_bins(path):
    """Lit un CSV rtl_power et retourne la liste de tous les niveaux dB valides,
    ainsi que la liste (freq, db) de chaque bin pour retrouver le pic. Exclut les
    bins trop proches du centre de chaque segment balaye (pic DC materiel) --
    mais SEULEMENT s'il y a plusieurs segments : sur une plage etroite tenant en
    un seul segment (ex: verification d'une frequence precise), le centre du
    segment peut coincider exactement avec la frequence recherchee, et exclure
    cette zone reviendrait a s'aveugler soi-meme sur la seule donnee utile
    (bug trouve en session sur un test cible a 97.1 MHz)."""
    rows = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            fields = line.strip().split(",")
            if len(fields) < 7:
                continue
            rows.append(fields)

    hop_count = len({(fields[2], fields[3]) for fields in rows})
    apply_dc_guard = hop_count > 1

    all_db = []
    bins = []
    for fields in rows:
        hz_low = float(fields[2])
        hz_high = float(fields[3])
        hz_step = float(fields[4])
        db_values = fields[6:]
        hop_center = (hz_low + hz_high) / 2
        for i, raw in enumerate(db_values):
            try:
                db = float(raw)
            except ValueError:
                continue
            if math.isnan(db):
                continue
            freq = hz_low + i * hz_step
            if apply_dc_guard and abs(freq - hop_center) <= hz_step * DC_SPIKE_GUARD_BINS:
                continue  # zone du pic DC, on ignore ce bin
            all_db.append(db)
            bins.append((freq, db))
    return all_db, bins


def _percentile(values, pct):
    """Estimateur de plancher de bruit plus robuste que la mediane sur les bandes
    densement occupees (ex: FM commerciale) : avec une mediane, si plus de la
    moitie des bins contiennent un vrai signal, le "plancher" est deja tire vers
    le haut par du contenu reel, ce qui peut faire echouer la detection meme d'un
    signal tres fort (bug trouve en session sur la bande FM). Un percentile bas
    (ex: 20e) reste ancre sur le vrai bruit tant qu'une minorite de bins suffit a
    l'estimer, meme si la majorite de la bande est occupee."""
    if not values:
        return None
    s = sorted(values)
    idx = int(len(s) * pct / 100)
    idx = min(idx, len(s) - 1)
    return s[idx]


def _median(values):
    return _percentile(values, 50)


def check_channels_relative(freqs, span_hz=25_000, margin_db=None):
    """Lit la puissance de plusieurs canaux fixes en un seul passage rtl_power,
    calcule le plancher de bruit local (mediane de tous les canaux lus), et
    retourne la liste des (freq, db) qui depassent plancher + marge, triee par
    force decroissante."""
    if margin_db is None:
        margin_db = NOISE_MARGIN_DB
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        low = int(min(freqs) - span_hz)
        high = int(max(freqs) + span_hz)
        step = span_hz
        band = f"{low}:{high}:{step}"
        result = subprocess.run(
            ["rtl_power", "-f", band, "-i", str(SQUELCH_CHECK_INTEGRATION_SEC), "-1", tmp_path],
            capture_output=True, text=True, errors="replace", timeout=20,
        )
        global _rtl_power_failure_count
        if result.returncode != 0:
            _rtl_power_failure_count += 1
            print(f"[radio_watch] ECHEC rtl_power (canaux fixes) rc={result.returncode}: {result.stderr.strip()[-200:]}")
            return []

        all_db, bins = _read_csv_bins(tmp_path)
        noise_floor = _percentile(all_db, 20)
        if noise_floor is None:
            _rtl_power_failure_count += 1
            return []
        _rtl_power_failure_count = 0

        threshold = noise_floor + margin_db
        candidates = []
        for target in freqs:
            nearest_db = min(bins, key=lambda b: abs(b[0] - target))[1] if bins else None
            if nearest_db is not None and nearest_db > threshold:
                candidates.append((target, nearest_db))
        candidates.sort(key=lambda c: c[1], reverse=True)
        return candidates
    except Exception:
        return []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def sweep_band_peak_relative(low_hz, high_hz, step_hz, margin_db=None):
    """Balaye une plage large, calcule le plancher de bruit local (mediane du
    balayage) et retourne (freq, db) du pic seulement s'il depasse
    plancher + marge. Sinon (None, None)."""
    if margin_db is None:
        margin_db = NOISE_MARGIN_DB
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        band = f"{low_hz}:{high_hz}:{step_hz}"
        result = subprocess.run(
            ["rtl_power", "-f", band, "-i", "1", "-1", tmp_path],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
        global _rtl_power_failure_count
        if result.returncode != 0:
            _rtl_power_failure_count += 1
            print(f"[radio_watch] ECHEC rtl_power (balayage) rc={result.returncode}: {result.stderr.strip()[-200:]}")
            return None, None

        all_db, bins = _read_csv_bins(tmp_path)
        noise_floor = _percentile(all_db, 20)
        if noise_floor is None or not bins:
            _rtl_power_failure_count += 1
            return None, None
        _rtl_power_failure_count = 0

        best_freq, best_db = max(bins, key=lambda b: b[1])
        if best_db > noise_floor + margin_db:
            return best_freq, best_db
        return None, None
    except Exception:
        return None, None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


SWEEP_MAX_PEAKS = 6   # pics retenus par passe de balayage large : on explore toute la bande, pas seulement son pic le plus fort (doctrine)


def sweep_band_peaks_relative(low_hz, high_hz, step_hz, margin_db=None, max_peaks=SWEEP_MAX_PEAKS):
    """Comme sweep_band_peak_relative, mais rend la LISTE des pics qui depassent
    plancher+marge (du plus fort au plus faible, dedupliques : un meme signal
    s'etale sur quelques bins voisins). Une bande large n'est donc plus reduite
    a son seul pic le plus fort -- on explore toute la bande, passe apres passe
    (doctrine : on balaie tout, on ne vise rien). Liste vide si rien ne depasse."""
    if margin_db is None:
        margin_db = NOISE_MARGIN_DB
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        band = f"{low_hz}:{high_hz}:{step_hz}"
        result = subprocess.run(
            ["rtl_power", "-f", band, "-i", "1", "-1", tmp_path],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
        global _rtl_power_failure_count
        if result.returncode != 0:
            _rtl_power_failure_count += 1
            print(f"[radio_watch] ECHEC rtl_power (balayage) rc={result.returncode}: {result.stderr.strip()[-200:]}")
            return []
        all_db, bins = _read_csv_bins(tmp_path)
        noise_floor = _percentile(all_db, 20)
        if noise_floor is None or not bins:
            _rtl_power_failure_count += 1
            return []
        _rtl_power_failure_count = 0
        seuil = noise_floor + margin_db
        ecart_min = max(2 * step_hz, 1)   # un meme signal occupe quelques bins voisins
        peaks = []
        for f, db in sorted(bins, key=lambda b: -b[1]):
            if db <= seuil or len(peaks) >= max_peaks:
                break
            if any(abs(f - pf) < ecart_min for pf, _ in peaks):
                continue
            peaks.append((f, db))
        return peaks
    except Exception:
        return []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def _rnnoise_speech_probability(pcm_bytes, rnnoise_state):
    """Fait tourner RNNoise sur le morceau (deja a 48kHz mono int16, voir
    _chunk_is_signal) frame par frame (10ms/480 echantillons chacune) et
    retourne la moyenne des probabilites de voix sur toutes les frames du
    morceau -- moyenner plusieurs frames stabilise la decision par rapport a
    une seule frame de 10ms (mesure en session : bruit blanc pur donne des
    probabilites par frame etalees sur toute la plage 0.04-0.83)."""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    n_frames = len(samples) // RNNOISE_FRAME_SIZE
    if n_frames == 0:
        return 0.0
    probs = []
    for i in range(n_frames):
        frame = samples[i * RNNOISE_FRAME_SIZE : (i + 1) * RNNOISE_FRAME_SIZE]
        _, prob = _rnnoise_process_frame(rnnoise_state, frame)
        probs.append(prob)
    return sum(probs) / len(probs)


def _chunk_is_signal(chunk_bytes, sample_rate, rnnoise_state, resample_state, smoothed_prob):
    """Decide si un morceau audio ressemble a un signal module plutot qu'a du
    bruit : filtre RMS bon marche d'abord (evite de solliciter RNNoise sur du
    silence pur), puis probabilite de voix RNNoise (lissee, voir plus haut)
    sur le morceau resample a 48kHz (frequence native du modele -- voir
    rnnoise_lib/rnnoise.py). Retourne (is_signal, rms, resample_state,
    smoothed_prob) -- resample_state ET smoothed_prob doivent tous les deux
    etre reportes d'un appel a l'autre (continuite du reechantillonnage et
    du lissage)."""
    n = len(chunk_bytes) // 2
    if n < 32:
        return False, 0, resample_state, smoothed_prob
    try:
        rms = audioop.rms(chunk_bytes, 2)
    except Exception:
        rms = 0
    if rms < MIN_RMS_ACTIVE:
        return False, rms, resample_state, smoothed_prob

    if sample_rate != RNNOISE_SAMPLE_RATE:
        resampled, resample_state = audioop.ratecv(
            chunk_bytes, 2, 1, sample_rate, RNNOISE_SAMPLE_RATE, resample_state
        )
    else:
        resampled = chunk_bytes

    chunk_prob = _rnnoise_speech_probability(resampled, rnnoise_state)
    if smoothed_prob is None:
        smoothed_prob = chunk_prob
    else:
        smoothed_prob = (
            SPEECH_PROB_SMOOTHING_ALPHA * chunk_prob
            + (1 - SPEECH_PROB_SMOOTHING_ALPHA) * smoothed_prob
        )
    is_signal = smoothed_prob >= SPEECH_PROB_THRESHOLD
    return is_signal, rms, resample_state, smoothed_prob


def _dsdfme_stdout_watcher(proc, stats=None, etiquette=""):
    """Affiche en continu la sortie texte de dsd-fme (statut de synchro,
    identifiants, etc.) dans les logs -- utile pour comprendre en pratique ce
    qui accroche ou pas, sans avoir a rejouer manuellement. stats (dict) :
    compte les synchros propres ou en erreur et retient le protocole.
    etiquette : le mode, quand plusieurs dsd-fme ecoutent ensemble (prise unique)."""
    try:
        for raw_line in proc.stdout:
            text = raw_line.decode(errors="replace").strip()
            if not text:
                continue
            print(f"[radio_watch] dsd-fme{etiquette}: {text}")
            if stats is not None:
                decodeurs.dsd_line(stats, text)  # regles communes avec le mode manuel
    except Exception:
        pass


def _dsdfme_validated(stats):
    return (stats["clean"] >= DSD_FME_MIN_CLEAN_SYNC
            and stats["clean"] >= DSD_FME_MIN_CLEAN_RATIO * stats["total"])


# Nombre minimal de trames de voix DMR (VC1..VC6 / VLC) pour OUVRIR le son :
# en dessous, la synchro est confirmee mais il n'y a PAS de voix a jouer --
# relais au repos ("IDLE"), terminateur de fin ("TLC"), signalisation
# (CSBK/DATA/PI) ou voix chiffree ("VC*"). On ne garde alors que les donnees,
# sans carillon ni annonce (2026-10-27 : de fausses ouvertures "carillon +
# bande + silence + reception terminee" se declenchaient sur des relais DMR au
# repos -- 32 000 trames IDLE pour une poignee de VC un jour type).
DSD_FME_MIN_VOICE_BURSTS = 2


def _dsdfme_voix(stats):
    """Vrai si le decodage DMR porte de la vraie voix EN CLAIR (trames VC/VLC),
    pas seulement des synchros d'un relais au repos ou de la signalisation.
    Les autres protocoles gardent le comportement anterieur (ouverture des que
    les synchros sont validees : ils n'ont pas le flot d'IDLE du DMR)."""
    if "DMR" in str(stats.get("protocol") or ""):
        return stats.get("voix", 0) >= DSD_FME_MIN_VOICE_BURSTS
    return True



def _reception_a_presenter(protocol, muted, had_audio, autre=False):
    """Decision UNIQUE et commune a TOUS les modes de demodulation : on ne
    declenche l'habillage (carillon de fermeture, annonce de fin de reception)
    et on ne garde l'enregistrement QUE si du contenu EN CLAIR a reellement ete
    capte (had_audio), ou si un autre cote de la conversation l'a ete (autre,
    p.ex. la FM d'un canal mixte). Synchro validee sans voix (relais au repos,
    signalisation, terminateur) ou flux chiffre : on a appris (registre, note
    'chiffre') mais on ne ceremonie pas et on ne garde pas un enregistrement
    vide -- source des 'ouvertures fantomes'. On ne vise aucun mode en
    particulier (2026-10-28)."""
    return (bool(protocol) and bool(had_audio) and not muted) or bool(autre)


def _extra_digital_mode(freq_hz):
    for low, high, flag, name in DSD_FME_EXTRA_MODES:
        if low <= freq_hz <= high:
            return flag, name
    return None


def _dmr_learn(stats):
    """Session DMR validee : registre (lib/dmr_registre.py) et annonce d'une
    frequence ou d'un groupe d'appel nouveaux (journal messages)."""
    if not stats.get("dmr") or "DMR" not in str(stats.get("protocol") or ""):
        return
    try:
        import dmr_registre
        import veille_donnees as vd
        news = dmr_registre.record(stats)
        mhz = vd.fr_number(stats["freq_hz"] / 1e6, 4)
        if news["nouvelle_frequence"]:
            vd.announce(dire("dmr.nouvelle_frequence", frequence=mhz), DMR_NOUVEAU_SOUND, "message_dmr", "radio_watch")
        elif news["nouveaux_groupes"]:
            vd.announce(dire("dmr.nouveaux_groupes", frequence=mhz, groupes=", ".join(news["nouveaux_groupes"])),
                        DMR_NOUVEAU_SOUND, "message_dmr", "radio_watch")
    except Exception as exc:  # le registre ne doit jamais casser une ecoute
        print(f"[radio_watch] registre DMR : {exc!r}")


# Veille DMR (2026-10-07) : fenetre du cycle qui retourne ecouter les
# frequences DMR deja captees (registre), les plus prometteuses d'abord
# (recence, heure habituelle d'activite, relais), en DMR seul (-fs : pas de
# fausses synchros M17 de l'auto-detection). Un relais n'emet que pendant
# les appels : on reste a l'ecoute toute la fenetre.
_dmr_turn = [0]
DMR_TOP = 4


def _dmr_window(dwell_sec=20, freq_hz=None):
    import dmr_registre
    # Toutes les frequences DMR connues sont revisitees a tour de role (pas
    # seulement les mieux notees) : on veut capter TOUT ce qui est ouvert
    # (2026-10-08). Les plus prometteuses passent plus souvent (DMR_TOP en
    # tete, puis le reste en rotation).
    if freq_hz is not None:
        return _dmr_ecoute(freq_hz, dwell_sec)
    known = [e for e in dmr_registre.targets() if dmr_filtres.freq_state(e["freq_hz"]) != "ignore"
             and not ignores.frequence_ignoree(e["freq_hz"])]
    if not known:
        return False
    # Ciblage (exploration dmr) : frequences favorites d'abord.
    known.sort(key=lambda e: dmr_filtres.freq_state(e["freq_hz"]) != "favori")
    head = known[:DMR_TOP]
    target = (head + known)[_dmr_turn[0] % (len(head) + len(known))]
    _dmr_turn[0] += 1
    freq = target["freq_hz"]
    print(f"[radio_watch] veille DMR : {freq / 1e6:.4f} MHz ({len(known)} frequence(s) suivie(s))")
    return _dmr_ecoute(freq, dwell_sec)


# Canaux mixtes (2026-10-24, signale par l'utilisateur : "l'interlocuteur
# semblait repondre a une parole qu'on n'entendait pas") : un meme relais porte
# du DMR et de la FM analogique (467,2375 MHz : postes FM a tonalite 199,5 Hz
# et postes DMR). La veille DMR n'entendait que le cote numerique, le balayage
# FM que l'autre, en deux entrees separees du journal. Sur un canal mixte,
# l'ecoute alterne : la parole numerique finie, on passe aussitot en FM sur le
# meme canal, puis de nouveau en DMR, tant que quelqu'un parle ; le tout dans la
# meme entree du journal. Un canal devient mixte des qu'une voix FM est captee
# a moins de MIXTE_ECART_HZ d'une frequence DMR du registre.
MIXTES_PATH = chemins.base("canaux_mixtes.json")
MIXTE_ECART_HZ = 7000
MIXTE_TOURS = 12
MIXTE_SONDE_SEC = 4
MIXTE_VIDES = 2          # tours de suite sans rien entendre : fin de la conversation


def _mixtes():
    try:
        with open(MIXTES_PATH, encoding="utf-8") as f:
            return json.load(f).get("canaux", [])
    except (OSError, ValueError):
        return []


def _canal_mixte(freq_hz):
    """{"fm": hz, "dmr": hz} du canal mixte de cette frequence, ou None."""
    for canal in _mixtes():
        if abs(canal["fm"] - freq_hz) <= MIXTE_ECART_HZ or abs(canal["dmr"] - freq_hz) <= MIXTE_ECART_HZ:
            return canal
    return None


def _noter_mixte(freq_fm):
    """Voix FM captee : canal mixte si une frequence DMR connue est tout pres."""
    if _canal_mixte(freq_fm):
        return
    try:
        import dmr_registre
        dmr = next((e["freq_hz"] for e in dmr_registre.targets()
                    if abs(e["freq_hz"] - freq_fm) <= MIXTE_ECART_HZ), None)
    except Exception as exc:
        print(f"[radio_watch] canal mixte : {exc!r}")
        return
    if dmr is None:
        return
    canaux = _mixtes() + [{"fm": int(freq_fm), "dmr": int(dmr)}]
    tmp = MIXTES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"canaux": canaux}, f)
    os.replace(tmp, MIXTES_PATH)
    print(f"[radio_watch] canal mixte : FM {freq_fm / 1e6:.4f} et DMR {dmr / 1e6:.4f} MHz")


def _suivi_mixte(canal, label, target=None, numerique_d_abord=False):
    """Alterne DMR et FM sur un canal mixte, dans la session du journal en
    cours, tant que quelqu'un parle. Rend True si quelque chose a ete entendu."""
    entendu, vides, numerique = False, 0, numerique_d_abord
    for _ in range(MIXTE_TOURS):
        if numerique:
            ok = try_digital_voice_decode(canal["dmr"], "-fs", label, probe_sec=MIXTE_SONDE_SEC, target="dmr")
            ok = bool(ok) and not try_digital_voice_decode.last_muted
        else:
            ok = stream_until_quiet(canal["fm"], "nfm", label, target=target)
        print(f"[radio_watch] canal mixte, {'DMR' if numerique else 'FM'} : {'entendu' if ok else 'rien'}")
        entendu = entendu or ok
        vides = 0 if ok else vides + 1
        if vides >= MIXTE_VIDES:
            break
        numerique = not numerique
    return entendu


def _dmr_ecoute(freq, dwell_sec=20):
    """Une ecoute DMR sur cette frequence : veille DMR, ou verrouillage dessus
    (2026-10-22 : le verrouillage reecoutait un appel DMR comme de la FM)."""
    label = dire("dmr.libelle_veille")
    # Meme habillage qu'une capture du balayage (_handle_candidate) : verrou
    # audio, session du journal radio, son de fin, empreinte de la capture.
    lock = audio_lock.acquire()
    log_session = radio_log.RadioLogSession(label)
    sound_player.set_recording_session(log_session)
    saved = False
    try:
        protocol = try_digital_voice_decode(freq, "-fs", label, probe_sec=dwell_sec, target="dmr")
        had_num = try_digital_voice_decode.last_had_audio   # clair capte cote numerique
        muet_num = try_digital_voice_decode.last_muted
        canal = _canal_mixte(freq)
        if not protocol and not canal:
            log_session.discard()
            return False
        if protocol and muet_num:  # appel chiffre : rien n'est garde (le poste a poste en clair, si)
            log_session.discard()
            return True
        fm = False
        if canal:  # canal mixte : l'autre cote de la conversation, en FM
            fm = _suivi_mixte(canal, label, numerique_d_abord=False)
            if not protocol and not fm:
                log_session.discard()
                return False
            protocol = f"{protocol} et FM" if protocol and fm else (protocol or "FM")
        if not _reception_a_presenter(protocol, muet_num, had_num, autre=fm):
            # synchro validee sans clair capte et pas de FM : appris, rien a
            # presenter ni a garder (fantome 2026-10-28)
            log_session.discard()
            return True
        play_sound_effect(FREQ_OVER_SOUND, label="radio_watch")
        morceaux.say(None, morceaux.whole(dire("radio.reception_terminee", protocol=protocol)), label="radio_watch")
        path = log_session.finalize()
        noise_signatures.record_capture("dmr", freq, "nfm", label, "dmr", path, None,
                                        {"numerique": True, "veille_dmr": True,
                                         "dmr": getattr(try_digital_voice_decode, "last_dmr", {})})
        saved = True
        return True
    finally:
        noise_signatures.capture_ended()
        sound_player.set_recording_session(None)
        audio_lock.release(lock)
        if saved and lock.silent:
            journal_notice()


# Recherche DMR (touche R de l'exploration dmr, 2026-10-17) : balayage des
# bandes professionnelles, puis courte sonde dsd-fme muette sur chaque pic
# inconnu ; ce qui accroche en DMR entre au registre (et dans l'exploration).
DMR_SEARCH_RANGES = [(440_000_000, 470_000_000), (148_000_000, 174_000_000)]
DMR_SEARCH_STEP_HZ = 6250
DMR_SEARCH_MARGIN_DB = 10.0
DMR_SEARCH_MAX_PEAKS = 10
DMR_SEARCH_PROBE_SEC = 4.0


def _dmr_probe(freq_hz, seconds=DMR_SEARCH_PROBE_SEC):
    """dsd-fme en DMR seul, sans son : rend True si le DMR s'est confirme."""
    stats = decodeurs.new_stats(freq_hz)
    proc = subprocess.Popen(["dsd-fme", "-fs", "-i", f"rtl:0:{int(freq_hz)}:0:0:12:0:2", "-o", "null"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    watcher = threading.Thread(target=_dsdfme_stdout_watcher, args=(proc, stats), daemon=True)
    watcher.start()
    end = time.time() + seconds
    try:
        while time.time() < end and proc.poll() is None:
            if _dsdfme_validated(stats) and (stats.get("dmr") or {}).get("appels"):
                time.sleep(0.5)  # le temps de lire groupe et emetteur
                break
            time.sleep(0.2)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
    if _dsdfme_validated(stats) and "DMR" in str(stats.get("protocol") or ""):
        _dmr_learn(stats)
        return True
    return False


def _dmr_search():
    import dmr_registre
    import manual_tuner
    known = [e["freq_hz"] for e in dmr_registre.load().values()]
    found = []
    manual_tuner.event(dire("dmr_exploration.recherche_debut"))
    for low, high in DMR_SEARCH_RANGES:
        if not dmr_filtres.search_requested():
            break  # annulee par la console
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            run = subprocess.run(["rtl_power", "-f", f"{low}:{high}:{DMR_SEARCH_STEP_HZ}", "-i", "1", "-1", path],
                                 capture_output=True, timeout=90)
            all_db, bins = _read_csv_bins(path) if run.returncode == 0 else ([], [])
        except Exception as exc:
            print(f"[radio_watch] recherche DMR : balayage impossible ({exc!r})")
            all_db, bins = [], []
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        floor = _percentile(all_db, 20) if all_db else None
        if floor is None:
            continue
        peaks = []
        for f, db in sorted(bins, key=lambda b: -b[1]):
            if db < floor + DMR_SEARCH_MARGIN_DB or len(peaks) >= DMR_SEARCH_MAX_PEAKS:
                break
            if any(abs(f - p) < 25_000 for p in peaks) or any(abs(f - k) < 12_500 for k in known):
                continue
            peaks.append(f)
        print(f"[radio_watch] recherche DMR {low / 1e6:.0f}-{high / 1e6:.0f} MHz : {len(peaks)} pic(s) a sonder")
        for f in peaks:
            if not dmr_filtres.search_requested():
                break
            f = int(round(f / DMR_SEARCH_STEP_HZ) * DMR_SEARCH_STEP_HZ)
            if _dmr_probe(f):
                found.append(f)
                manual_tuner.event(dire("dmr_exploration.recherche_trouve",
                                        frequence=f"{f / 1e6:.4f}".rstrip("0").replace(".", ",")))
    dmr_filtres.search_done()
    if found:
        manual_tuner.event(dire("dmr_exploration.recherche_fin", nombre=len(found)))
    else:
        manual_tuner.event(dire("dmr_exploration.recherche_rien"))


# Fantomes P25/DMR (2026-10-31) : une synchro validee qui n'emet qu'un filet
# d'IDLE / terminateur passait pour "clair capte". On exige maintenant une
# vraie voix : au moins une super-trame (DMR 360 ms, plancher physique de la
# litterature) ET au-dessus du silence numerique pur.
DSD_FME_VOIX_MIN_SEC = 0.36
DSD_FME_VOIX_RMS_MIN = 60


def _mesure_voix(chunks, channels):
    """(duree_s, rms S16) de la voix captee ; (0, 0) si rien."""
    if not chunks:
        return 0.0, 0
    data = b"".join(chunks)
    sec = len(data) / (DSD_FME_VOICE_SAMPLE_RATE * 2 * max(1, channels))
    try:
        rms = audioop.rms(data, 2)
    except Exception:
        rms = 0
    return sec, rms


# Double ceremonie (2026-10-31) : chaque nouvelle prise de parole d'une meme
# conversation re-declenchait try_digital_voice_decode, donc _annonce_dmr. On
# ne rejoue pas la ceremonie si une a deja eu lieu sur cette frequence il y a
# moins de REANNONCE_FENETRE_SEC.
REANNONCE_FENETRE_SEC = 30.0
REANNONCE_ECART_HZ = 6250
_derniere_annonce = {"freq": None, "t": 0.0}


def _reprise_recente(freq_hz):
    d = _derniere_annonce
    return (d["freq"] is not None
            and abs(freq_hz - d["freq"]) <= REANNONCE_ECART_HZ
            and time.time() - d["t"] < REANNONCE_FENETRE_SEC)


def _annonce_dmr(protocol, band_label, ticket, reprise=False):
    """Carillon, "Reception DMR, <bande>", son d'ouverture : joue pendant que
    try_digital_voice_decode garde le son en tampon. ticket : celui du fil
    appelant (priorites, journal ouvert, calibrage). reprise : nouvelle prise
    de parole de la MEME conversation -- pas de ceremonie (le fil tourne quand
    meme, vide, pour ne rien changer au tampon ni a l'ouverture d'aplay)."""
    audio_lock._local.ticket = ticket
    try:
        # Morceaux en cache (lib/annonce_morceaux.py) : "Reception DMR", nom de bande.
        parts = morceaux.whole(dire("radio.reception", protocol=protocol) + ("," if band_label else "."))
        if band_label:
            parts += morceaux.whole(band_label + ".")
        if reprise:
            # Pas rejouee au haut-parleur, mais chaque prise du journal garde sa
            # ceremonie entiere (2026-11-01 : des prises sans annonce ni carillon
            # d'ouverture, seulement la voix puis la fin).
            session = sound_player.get_recording_session()
            if session is not None:
                annonce = morceaux.prepare(parts).wait()
                for chemin in (DMR_VOCAL_SOUND, annonce, FREQ_OPEN_SOUND):
                    if chemin:
                        session.add_audio_file(chemin)
                if annonce:
                    try:
                        os.remove(annonce)
                    except OSError:
                        pass
            return
        morceaux.say(DMR_VOCAL_SOUND, parts, label="radio_watch")
        # Meme encadrement que la voix analogique (2026-10-02) : son
        # d'ouverture avant la voix, son de fin apres (_handle_candidate).
        play_sound_effect(FREQ_OPEN_SOUND, label="radio_watch")
    except Exception as exc:
        print(f"[radio_watch] annonce DMR : {exc!r}")


def _dmr_courant(stats):
    """{"groupe", "appelant"} de l'appel DMR entendu en dernier, ou {}."""
    d = (stats or {}).get("dmr") or {}
    slot = d.get("dernier_intervalle")
    if not slot:
        return {}
    out = {"groupe": (d.get("en_cours") or {}).get(slot), "appelant": (d.get("parle") or {}).get(slot)}
    return {k: v for k, v in out.items() if v}


def try_digital_voice_decode(freq_hz, mode_flag="-fa", band_label="", probe_sec=None, target=None, live=False,
                             prise=None, port=None, arbitre=None):
    """Tente un decodage vocal numerique (DMR/P25/NXDN/D-STAR/YSF/dPMR/X2-TDMA)
    directement depuis le dongle RTL-SDR via dsd-fme. dsd-fme n'a pas de
    sortie ALSA directe -- on le fait sortir en UDP local (PCM brut, voix
    synthetisee) et on relit ce flux nous-memes via aplay/AUDIO_DEVICE,
    plutot que de lui laisser gerer l'audio (sa sortie "pulse" par defaut est
    ce qui avait bloque tout le scanner plus tot dans la session). Retourne
    True des qu'un premier paquet audio decode est recu (heuristique directe
    : si dsd-fme envoie de l'audio synthetise, c'est qu'il a reellement
    accroche quelque chose -- pas de decode = pas d'envoi). Depuis le
    2026-10-01 : rien n'est joue ni annonce tant que des synchros propres
    n'ont pas confirme le decodage (voir DSD_FME_MIN_CLEAN_SYNC) ; rend alors
    le nom du protocole (vrai), sinon None.

    Prise unique (2026-10-25, prise/port/arbitre) : dsd-fme lit le flux commun
    ("-i -") au lieu d'ouvrir le dongle, plusieurs modes ecoutent en meme temps
    (chacun son port UDP), et seul celui que l'arbitre laisse parler joue ; les
    autres se taisent et s'arretent. L'ecoute dure tant que la prise est ouverte
    (une voix FM peut etre suivie de numerique sur le meme canal)."""
    if not DSD_FME_AVAILABLE:
        return False
    sock = None
    proc = None
    aplay = None
    brut, stats = None, None
    validated = muted = filtered_out = False
    port = port or DSD_FME_UDP_PORT
    live_ouvert = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:  # marge si la boucle prend du retard (le tampon principal reste "attente")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        sock.bind((DSD_FME_UDP_HOST, port))
        sock.settimeout(0.5)

        if prise is not None:  # son brut garde une seule fois, par la prise (_ecoute_prise)
            proc = subprocess.Popen(
                ["dsd-fme", mode_flag, "-i", "-", "-s", str(prise_unique.TAUX), "-o", f"udp:{DSD_FME_UDP_HOST}:{port}"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            prise.vers_processus(proc.stdin)
        else:
            os.makedirs(BRUT_DIR, exist_ok=True)
            brut = os.path.join(BRUT_DIR, f"{int(freq_hz)}_{mode_flag.lstrip('-')}_{time.strftime('%Y%m%d_%H%M%S')}.wav")
            proc = subprocess.Popen(
                ["dsd-fme", mode_flag, "-i", f"rtl:0:{int(freq_hz)}:0:0:12:0:2",
                 "-o", f"udp:{DSD_FME_UDP_HOST}:{port}", "-6", brut],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        stats = decodeurs.new_stats(freq_hz)  # textes transportes (alias, SMS, GPS) : lib/decodeurs.py
        if arbitre is not None:
            arbitre.stats[mode_flag] = stats
        # Reglages de la voix numerique (2026-10-14) : attente de fin et volume
        # propres, ajustes par le bilan (fins coupees) ou a la main ("reglages son").
        digital = _settings(target) if target else {}
        idle_sec = maintien.delai(digital.get("attente_fin_numerique_s") or DSD_FME_IDLE_SEC, target or "dmr", freq_hz)
        gain = 10 ** ((digital.get("gain_numerique_db") or 0.0) / 20)
        stdout_thread = threading.Thread(target=_dsdfme_stdout_watcher,
                                         args=(proc, stats, f" {mode_flag}" if arbitre is not None else ""),
                                         daemon=True)
        stdout_thread.start()
        if live and arbitre is None:  # prise unique : annonce seulement par le decodeur qui prend la parole
            try:
                juge_live.debut(freq_hz, band_label, mode_flag)
                live_ouvert = True
            except Exception:
                pass

        # Sonde silencieuse : on garde le son recu en attente, sans rien jouer,
        # tant que les synchros n'ont pas confirme un vrai decodage.
        pending = []
        channels = 1 if mode_flag in DSD_FME_MONO_MODES else DSD_FME_VOICE_CHANNELS
        validated = False
        annonce, attente, validated_at = None, [], 0.0
        voix_attendue = False
        filtered_out = False
        # Plus de coupure d'un appel en cours (2026-10-22 : des conversations coupees
        # pile a 2 minutes) : la duree max ne vaut que pour ce qui interesse peu.
        max_sec = maintien.plafond(min(DSD_FME_MAX_SEC, dmr_filtres.max_duration()), target or "dmr", freq_hz)
        captured_chunks = []
        start = time.time()
        if probe_sec:
            deadline = start + probe_sec          # sonde explicite (veille DMR, mixte) : telle quelle
        else:
            try:  # balayage : la sonde par defaut suit le tempo (survol en dechaine, patient en lent)
                import tempo
                deadline = start + tempo.sonde(DSD_FME_PROBE_SEC, freq_hz)
            except Exception:
                deadline = start + DSD_FME_PROBE_SEC
        muted = False  # appel chiffre : ni joue ni enregistre ; poste a poste en clair garde (regle utilisateur 2026-10-25)
        juge_suivant = False
        dmr_vu = {}
        if arbitre is None:
            try_digital_voice_decode.last_dmr = {}
        # Prise unique : tant que la prise est ouverte, on reste a l'ecoute, meme
        # en silence quand un autre decodeur a la parole ("juge suivant" peut la
        # lui passer, et une voix FM peut etre suivie de numerique).
        while (time.time() < deadline
               or (arbitre is not None and not validated and not filtered_out and prise.ouverte
                   and not arbitre.termine.is_set())):
            if maintien.zap_depuis(start):
                print("[radio_watch] zapper : ecoute numerique arretee a la demande")
                break
            if arbitre is not None and mode_flag in arbitre.cedes:
                break  # ecarte par "juge suivant" sur cette ecoute
            if live and (arbitre is None or validated):
                acte = juge_live.action_depuis(start)
                if acte == "suivant":   # ce decodeur n'est pas le bon : au suivant, meme signal
                    try:
                        juge_live.noter(band_label, mode_flag, ok=False)
                    except Exception:
                        pass
                    print(f"[radio_watch] juge suivant : on quitte {mode_flag} a la demande")
                    juge_suivant = True
                    if arbitre is not None:  # la parole passe a un autre decodeur deja a l'ecoute
                        arbitre.ceder(mode_flag)
                    break
                elif acte == "ok":      # c'est le bon decodeur
                    try:
                        juge_live.noter(band_label, mode_flag, ok=True)
                    except Exception:
                        pass
                    print(f"[radio_watch] juge ok : {mode_flag} confirme sur {band_label}")
            courant = _dmr_courant(stats)
            if courant and courant != dmr_vu and (arbitre is None or validated):  # qui parle : "ignorer", "verrouiller"
                dmr_vu = courant
                try_digital_voice_decode.last_dmr = courant
                noise_signatures.update_live(dmr=courant)
                if live_ouvert:
                    try:
                        juge_live.rafraichir(stats.get("protocol"))
                    except Exception:
                        pass
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                data = None
            if data and not validated:
                pending.append(data)
                pending = pending[-DSD_FME_PRE_MAX:]  # avant la confirmation : les dernieres secondes
            # Ciblage DMR (lib/dmr_filtres.py) : un groupe ignore ne declenche
            # ni carillon ni annonce, et ne retient pas le scanner.
            # Ouverture : synchros validees ET (pour le DMR) de vraies trames de
            # voix en clair (VC/VLC). Sur simple synchro sans voix -- relais au
            # repos (IDLE), terminateur (TLC), signalisation ou voix chiffree --
            # on prend en silence (la voix FM ne s'ouvre pas sur la porteuse
            # numerique) sans carillon ni annonce ; les donnees (registre) sont
            # notees par le fil stdout (2026-10-27).
            if not validated and pending and _dsdfme_validated(stats):
                cible = dmr_filtres.slot_gains(stats)[2]
                voix = _dsdfme_voix(stats)
                if not (cible and voix):
                    if not filtered_out:
                        raison = ("groupe hors cible" if (voix and not cible)
                                  else "relais au repos / signalisation, pas de voix")
                        print(f"[radio_watch] dsd-fme : {stats.get('protocol')} sans ouverture "
                              f"({raison}), donnees seulement")
                        if arbitre is not None:
                            arbitre.prendre(mode_flag, stats["protocol"])  # en silence : la voix FM ne s'ouvre pas dessus
                    filtered_out = True
                else:
                    if arbitre is not None:
                        if not arbitre.prendre(mode_flag, stats["protocol"]):
                            continue  # un autre decodeur numerique a la parole : on attend en silence
                        try_digital_voice_decode.last_dmr = dmr_vu
                        if live:
                            try:
                                juge_live.debut(freq_hz, band_label, mode_flag, stats["protocol"])
                                live_ouvert = True
                            except Exception:
                                pass
                    validated = True
                    size = len(pending[0])
                    if size == DSD_FME_STEREO_DATAGRAM:
                        channels = 2
                    elif size == DSD_FME_MONO_DATAGRAM:
                        channels = 1
                    protocol = stats["protocol"] or dire("radio.numerique")
                    print(f"[radio_watch] dsd-fme : decodage {protocol} confirme "
                          f"({stats['clean']} synchros propres sur {stats['total']}), {channels} voie(s)")
                    voix_attendue = True   # la ceremonie attend assez de voix (rafales, plus bas)
                    validated_at = time.time()
                    try:  # guet appris : combien ce spot a mis a confirmer (lib/guet.py)
                        import guet
                        guet.noter(freq_hz, validated_at - start)
                    except Exception:
                        pass
                    if target == "dmr":  # veille DMR : "verrouiller" et "ignorer" visent cet appel
                        noise_signatures.capture_started("dmr", freq_hz, "dmr", band_label, "dmr")
                        if dmr_vu:
                            noise_signatures.update_live(dmr=dmr_vu)
                    attente, pending, data = list(pending), [], None
            if validated and voix_attendue:
                # Rafales (2026-11-01) : un clic de fin d'emission ne declenche
                # rien. La voix reste en tampon ; chaque paquet prolonge la
                # surveillance (une reponse peut suivre). Assez de voix : la
                # ceremonie part et rejouera tout le tampon, clic compris.
                if data:
                    attente.append(data)
                    deadline = max(deadline, time.time() + idle_sec)
                if _mesure_voix(attente, channels)[0] < MIN_VOIX_OUVERTURE_SEC:
                    continue
                voix_attendue, data = False, None
                # Carillon et annonce dans un fil a part (2026-10-19) : pendant
                # qu'ils jouent, cette boucle continue de lire la socket et met
                # TOUT en tampon (avant, la socket debordait au bout de ~6 s
                # d'annonce : le debut de la transmission etait perdu).
                reprise = _reprise_recente(freq_hz)
                _derniere_annonce.update(freq=freq_hz, t=time.time())
                annonce = threading.Thread(target=_annonce_dmr, daemon=True,
                                           args=(protocol, band_label, audio_lock.ticket_courant(), reprise))
                annonce.start()
            if validated and annonce is not None:
                if data:
                    attente.append(data)
                deadline = max(deadline, time.time() + idle_sec)  # on ne finit pas pendant l'annonce
                if annonce.is_alive():
                    continue
                annonce = None
                aplay = subprocess.Popen(
                    ["aplay", "-D", AUDIO_DEVICE, "-r", str(DSD_FME_VOICE_SAMPLE_RATE),
                     "-f", "S16_LE", "-c", str(channels), "-q"],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                data, attente = b"".join(attente), []   # tout ce qui a ete entendu, depuis le debut
            if not data or not validated:
                continue
            data, heard = dmr_filtres.apply(data, stats, channels)
            if not heard:
                continue  # groupe ignore ou hors cible : ne prolonge pas l'ecoute
            # Ecoute au moins MIN_ECOUTE_SEC apres la confirmation : un clic de
            # micro ne ferme pas l'ecoute, une reponse qui suit n'est pas perdue.
            deadline = min(max(time.time() + idle_sec, validated_at + MIN_ECOUTE_SEC), start + max_sec)
            # Seul le chiffrement coupe le son (regle utilisateur, 2026-10-25 : un
            # appel prive en clair, simple adressage point a point, nous interesse
            # aussi). Un chiffrement a clef connue reste du son chiffre tant qu'on
            # ne dechiffre pas : on ne le garde pas.
            if stats.get("chiffre") and not muted:
                muted = True
                captured_chunks = []
                if aplay is not None:
                    try:
                        aplay.terminate()
                    except Exception:
                        pass
                print("[radio_watch] dsd-fme : appel chiffre, son coupe et non enregistre (donnees seulement)")
            if muted:
                continue
            if gain != 1.0:
                data = audioop.mul(data, 2, gain)
            captured_chunks.append(data)
            try:
                aplay.stdin.write(audio_lock.scale(data, canal="ecoute"))
            except Exception:
                break

        if juge_suivant:
            return "SUIVANT"
        if not validated and filtered_out:
            # Decode mais groupe ignore / hors cible : on apprend quand meme
            # (registre), rien n'est joue ; "muet" pour les appelants.
            print(f"[radio_watch] dsd-fme : groupe {(stats.get('dmr') or {}).get('en_cours')} filtre, on passe")
            _dmr_learn(stats)
            if arbitre is None or arbitre.gagnant == mode_flag:
                try_digital_voice_decode.last_muted = True
                try_digital_voice_decode.last_had_audio = False
            return stats["protocol"] or "numérique"
        if not validated:
            if pending:
                print(f"[radio_watch] dsd-fme : son ecarte, synchro non confirmee "
                      f"({stats['clean']} propres sur {stats['total']})")
            return None

        _dmr_learn(stats)
        if voix_attendue:
            print(f"[radio_watch] dsd-fme : rafale de {_mesure_voix(attente, channels)[0]:.2f} s sans suite "
                  f"(moins de {MIN_VOIX_OUVERTURE_SEC} s de voix), ni ouverture ni journal")
        voix_sec, voix_rms = _mesure_voix(captured_chunks, channels)
        assez = voix_sec >= DSD_FME_VOIX_MIN_SEC and voix_rms >= DSD_FME_VOIX_RMS_MIN
        if captured_chunks and not muted:
            print(f"[radio_watch] dsd-fme voix : {voix_sec:.2f} s, rms {voix_rms} -> "
                  f"{'gardee' if assez else 'ecartee (fantome)'}")
        if arbitre is None or arbitre.gagnant == mode_flag:
            try_digital_voice_decode.last_muted = muted
            try_digital_voice_decode.last_had_audio = bool(captured_chunks) and not muted and assez
        log_session = sound_player.get_recording_session()
        if log_session is not None and captured_chunks and not muted and assez:
            log_session.add_raw_pcm(b"".join(captured_chunks), DSD_FME_VOICE_SAMPLE_RATE, "s16le",
                                    channels=channels)
        print(f"[radio_watch] dsd-fme a decode de l'audio numerique sur {freq_hz/1e6:.4f} MHz")
        return stats["protocol"] or "numérique"
    except Exception as exc:
        print(f"[radio_watch] dsd-fme indisponible: {exc}")
        return False
    finally:
        for p in (proc, aplay):
            if p is not None:
                try:
                    p.terminate()
                    p.wait(timeout=2)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        if live_ouvert:
            try:
                juge_live.fin()
            except Exception:
                pass
        _garder_brut(brut, freq_hz, mode_flag, band_label, stats, validated and not muted, muted or filtered_out)


# Son brut du dongle tel que dsd-fme le recoit (2026-10-24, option -6, 48 kHz),
# avec sa fiche .json : rejoue ensuite devant tous les decodeurs, sans le dongle,
# par juges_watch.py (qui le retire une fois tous les juges passes).
BRUT_DIR = chemins.base("brut_dsd")
BRUT_MIN_OCTETS = 48000           # moins d'une demi-seconde : rien a juger
_bruts_candidat = None            # sons bruts du candidat en cours (_handle_candidate), sinon None


def _garder_brut(chemin, freq_hz, mode_flag, band_label, stats, valide, muet):
    if not chemin or not os.path.exists(chemin):
        return
    try:
        if muet or os.path.getsize(chemin) < BRUT_MIN_OCTETS:
            os.remove(chemin)  # chiffre ou groupe ignore : rien n'est garde (regle utilisateur)
            return
        stats = stats or {}
        fiche = {"freq_hz": int(freq_hz), "mode": mode_flag, "band": band_label, "t": time.time(),
                 "etiquette": "reference" if valide else ("en_cours" if _bruts_candidat is not None else "inconnu"),
                 "direct": {"valide": bool(valide), "protocole": stats.get("protocol"),
                            "propres": stats.get("clean", 0), "total": stats.get("total", 0),
                            "prive": bool(stats.get("prive")), "chiffre": bool(stats.get("chiffre"))}}
        with open(chemin[:-4] + ".json", "w", encoding="utf-8") as f:
            json.dump(fiche, f, ensure_ascii=False)
        if _bruts_candidat is not None:
            _bruts_candidat.append(chemin)
    except OSError as exc:
        print(f"[radio_watch] son brut : {exc}")


def _etiqueter_bruts(etiquette=None):
    """Fin du candidat : rejet (ecarte comme bruit) ou analogique (voix FM) servent
    de parasites aux juges ; sans verdict, un son reste "inconnu"."""
    for chemin in _bruts_candidat or []:
        fiche_path = chemin[:-4] + ".json"
        try:
            with open(fiche_path, encoding="utf-8") as f:
                fiche = json.load(f)
            if fiche.get("etiquette") == "reference":
                continue
            if etiquette or fiche.get("etiquette") == "en_cours":
                fiche["etiquette"] = etiquette or "inconnu"
                with open(fiche_path, "w", encoding="utf-8") as f:
                    json.dump(fiche, f, ensure_ascii=False)
        except (OSError, ValueError):
            pass


try_digital_voice_decode.last_muted = False  # derniere session : son coupe (appel chiffre) ?
try_digital_voice_decode.last_had_audio = False  # derniere session : du contenu en clair a-t-il ete capte ?


# Nombre minimal de caracteres alphanumeriques utiles pour qu'un message
# decode soit considere reel plutot que bruit. Trouve en session : sur une
# fenetre de 25 minutes d'ecoute reelle, TOUS les messages decodes etaient
# soit un caractere isole ("O", "G"...) soit du bruit MORSE_CW non resolu
# (notation entre chevrons type "<___.>", "<____>") -- zero vrai contenu.
# MORSE_CW en particulier est tres sujet aux faux positifs sur du bruit/clics
# rythmiques (risque deja identifie a son ajout, confirme ici par les logs).
MIN_DECODED_MESSAGE_CHARS = 4


def _is_meaningful_decoded_message(msg, min_chars=MIN_DECODED_MESSAGE_CHARS):
    """Filtre les faux positifs des decodeurs multimon-ng : retire d'abord
    les fragments morse non resolus (notation entre chevrons, ex:
    "<___.>") puis exige un minimum de caracteres alphanumeriques reels
    dans ce qui reste."""
    cleaned = re.sub(r"<[^>]*>", "", msg)
    alnum_count = sum(1 for c in cleaned if c.isalnum())
    return alnum_count >= min_chars


def _multimon_reader(proc, out_queue):
    try:
        for raw_line in proc.stdout:
            text = raw_line.decode(errors="replace").strip()
            if not text:
                continue
            if text.startswith("Enabled demodulators") or text.startswith("multimon-ng"):
                continue
            out_queue.put(text)
    except Exception:
        pass


RTL_FM_LOCK_ATTEMPTS = 3
RTL_FM_LOCK_PROBE_SEC = 0.5


def _aplay_stderr_watcher(proc):
    """Lit en continu le stderr d'aplay pendant toute la diffusion. Le mode
    silencieux (-q) supprime l'affichage normal, mais pas forcement les
    avertissements de sous-alimentation de tampon (underrun) -- invisibles
    jusqu'ici puisque rien ne lisait ce flux en continu (seulement sur echec
    d'ecriture). Un underrun cote ALSA se traduit typiquement par un
    grattement/craquement audible, hypothese testee ici pour la distorsion
    signalee en session (voir ROADMAP.md)."""
    try:
        for raw_line in proc.stderr:
            text = raw_line.decode(errors="replace").strip()
            if not text:
                continue
            print(f"[radio_watch] aplay signalement: {text}")
    except Exception:
        pass


def _rtl_fm_stderr_watcher(proc, pll_failed_event):
    """Lit en continu le stderr de rtl_fm (jete auparavant via DEVNULL -- ce
    qui masquait ce bug pendant tout le debut de la session). Signale via
    l'evenement partage un verrouillage PLL rate, sans quoi le message reste
    invisible bien que le flux audio demodule soit inutilisable (bruit/
    sifflement au lieu du contenu reel, meme symptome que celui deja trouve
    avec rtl_power en session -- voir ROADMAP.md)."""
    try:
        for raw_line in proc.stderr:
            text = raw_line.decode(errors="replace").strip()
            if not text:
                continue
            if "PLL not locked" in text:
                pll_failed_event.set()
            elif "error" in text.lower() or "failed" in text.lower():
                print(f"[radio_watch] rtl_fm signalement: {text}")
    except Exception:
        pass


def _open_rtl_fm(rtl_fm_cmd):
    """Ouvre rtl_fm et verifie que le tuner a bien verrouille sa frequence
    avant de rendre la main. Le R820T rate parfois son verrouillage PLL sur
    un changement de frequence a froid (confirme a plusieurs reprises en
    session, aussi bien avec rtl_power qu'ici) : le process ne plante pas,
    mais le signal demodule est inutilisable. Reessaie quelques fois avant
    d'abandonner et d'utiliser quand meme le dernier essai (mieux que rien)."""
    proc = None
    for attempt in range(1, RTL_FM_LOCK_ATTEMPTS + 1):
        proc = subprocess.Popen(rtl_fm_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pll_failed_event = threading.Event()
        stderr_thread = threading.Thread(
            target=_rtl_fm_stderr_watcher, args=(proc, pll_failed_event), daemon=True,
        )
        stderr_thread.start()
        time.sleep(RTL_FM_LOCK_PROBE_SEC)
        if not pll_failed_event.is_set():
            return proc
        if attempt < RTL_FM_LOCK_ATTEMPTS:
            print(f"[radio_watch] rtl_fm: PLL non verrouille (essai {attempt}/{RTL_FM_LOCK_ATTEMPTS}), nouvelle tentative...")
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    print(f"[radio_watch] rtl_fm: echec de verrouillage PLL apres {RTL_FM_LOCK_ATTEMPTS} tentatives, on continue quand meme")
    return proc


def _rtl_fm_reader(stream, out_queue, chunk_size):
    """Lit en continu le flux demodule de rtl_fm et empile les morceaux dans
    une file, independamment du rythme de consommation cote lecture/analyse.
    Demarre des l'ouverture du canal (avant meme l'annonce TTS) pour que rien
    ne soit perdu du debut de la transmission pendant que l'annonce et le son
    d'ouverture jouent -- la file sert de cache, videe en rafale des que la
    lecture commence reellement."""
    try:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                out_queue.put(None)
                break
            out_queue.put(chunk)
    except Exception:
        out_queue.put(None)


def stream_until_quiet(freq_hz, mode, band_label, sample_rate=22050, target=None, prise=None, arbitre=None):
    """Diffuse en direct sur les haut-parleurs tant qu'il y a un vrai signal
    (pas juste du bruit), coupe des que le canal redevient silencieux/bruite
    (avec un court debounce), plafonne a MAX_HOLD_SEC. Pour les canaux FM, tente
    en parallele un decodage numerique (APRS, pageurs) via multimon-ng et lit au
    TTS tout texte decode. Retourne True si un signal reel a ete entendu ou si du
    texte a ete decode."""
    global _last_stream_fingerprint, _last_stream_metrics, _last_stream_raw, _last_stream_filtered
    _last_stream_fingerprint, _last_stream_metrics = None, {}
    mhz = freq_hz / 1_000_000
    rtl_fm_mode = "wbfm" if mode == "wfm" else mode
    if mode == "wfm":
        # bug trouve en session : rtl_fm plante (SIGFPE) en mode wbfm avec un
        # taux de sortie de 22050 Hz (ratio de suréchantillonnage problematique).
        # Un taux d'entree/sortie plus classique pour la diffusion FM evite le crash.
        sample_rate = 48000
        # "-E dc" : filtre de blocage DC. Le tuner R820T a un artefact materiel
        # connu (architecture a conversion directe) qui produit un decalage/pic
        # DC pres du centre de la bande accordee -- deja identifie en session
        # avec rtl_power (DC_SPIKE_GUARD_BINS) et confirme responsable d'un
        # sifflement aigu constant, present meme sur du bruit neutre, signale
        # par l'utilisateur sur plusieurs bandes.
        rtl_fm_cmd = ["rtl_fm", "-f", str(int(freq_hz)), "-M", rtl_fm_mode,
                      "-s", "200000", "-r", str(sample_rate), "-l", "0",
                      "-E", "dc", "-E", "deemp"]
    else:
        # Pas de "-E deemp" ici : cette correction compense la pre-accentuation
        # des emetteurs de diffusion FM commerciale (wfm) -- appliquee a tort
        # au NFM/AM (446, aviation, marine, radioamateur...) qui n'utilisent
        # pas cette convention, ca attenue anormalement les aigus et rend la
        # voix etouffee/brouillee (trouve en session : la voix de test PMR446
        # etait audible mais peu claire, symptome caracteristique). "-E dc"
        # garde par contre : voir commentaire ci-dessus (branche wfm).
        rtl_fm_cmd = ["rtl_fm", "-f", str(int(freq_hz)), "-M", rtl_fm_mode,
                      "-s", str(sample_rate), "-l", "0", "-E", "dc"]

    # Prise unique (2026-10-25) : le meme flux que les decodeurs numeriques,
    # ramene a sample_rate ; sinon rtl_fm a soi, comme avant.
    rtl_fm = prise.flux(sample_rate) if prise is not None else _open_rtl_fm(rtl_fm_cmd)

    chunk_size = int(sample_rate * 2 * ANALYSIS_WINDOW_MS / 1000)
    audio_queue = queue.Queue()
    reader_thread = threading.Thread(
        target=_rtl_fm_reader, args=(rtl_fm.stdout, audio_queue, chunk_size), daemon=True,
    )
    reader_thread.start()

    # Parasites connus (commande console "parasite", 2026-09-29) : si cette
    # frequence a deja donne un faux positif signale par l'utilisateur, on
    # ecoute PRECHECK_SEC SANS RIEN ANNONCER et on compare l'empreinte
    # sonore ; si c'est le meme parasite, abandon silencieux. Aucun delai
    # sur les autres frequences. L'audio pre-ecoute n'est pas perdu : il
    # est rejoue en tete de diffusion (prebuffer).
    prebuffer = []
    if noise_signatures.has_signatures_near(freq_hz):
        precheck_end = time.time() + noise_signatures.PRECHECK_SEC
        while time.time() < precheck_end:
            try:
                item = audio_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            prebuffer.append(item)
            if item is None:
                break
        pre_pcm = b"".join(c for c in prebuffer if c)
        if mode != "wfm":
            # Meme filtre que l'audio d'ou vient l'empreinte apprise (coherence).
            pre_pcm = audio_filters.VoiceBandpassFilter(sample_rate).process_s16le(pre_pcm)
        is_noise, sim = noise_signatures.matches_known_noise(freq_hz, pre_pcm, sample_rate, "s16le")
        if is_noise:
            # On n'ABANDONNE PLUS la frequence sur un parasite connu (demande
            # utilisateur 2026-10-27). Un parasite ne doit pas faire IGNORER un
            # canal, seulement empecher d'OUVRIR sur SA rafale : c'est _prelisten
            # qui ecreme (il refuse de confirmer une ouverture dont l'empreinte
            # colle a un parasite connu du canal, et continue d'ecouter la vraie
            # voix). Ici, on ne fait que signaler la proximite. La suppression de
            # la FM sur du numerique muet (IDLE/donnees/chiffre) reste assuree
            # plus bas par l'arbitre (synchro numerique validee).
            print(f"[radio_watch] {mhz:.4f} MHz : parasite connu proche (similarite {sim:.2f}), on ecreme en pre-ecoute sans abandonner le canal")
        else:
            print(f"[radio_watch] pre-ecoute : pas un parasite connu (meilleure similarite {sim})")

    # Annonce et son d'ouverture AVANT la diffusion : la capture tourne deja
    # en arriere-plan (fil ci-dessus) pendant ce temps, donc rien n'est perdu
    # du debut de la transmission -- voir _rtl_fm_reader. "incoming" avant
    # l'annonce (tampon pendant la synthese cloud, voir speak_with_lead_sound) ;
    # annonce simplifiee (2026-08-30, demande de l'utilisateur) : la bande
    # seule, sans la frequence exacte en MHz -- peu intelligible a l'oral et
    # sans interet pour l'usage courant, band_label porte deja le nom
    # d'usage clair ("canal 446 civil", "bande aviation militaire UHF"...).
    # band_label : petit ensemble fixe de noms de bande (voir BANDS) --
    # candidat naturel au cache repetitif (2026-08-31).
    multimon = None
    multimon_queue = None
    multimon_thread = None
    if mode == "nfm":
        try:
            multimon = subprocess.Popen(
                ["multimon-ng"] + MULTIMON_ARGS,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            multimon_queue = queue.Queue()
            multimon_thread = threading.Thread(
                target=_multimon_reader, args=(multimon, multimon_queue), daemon=True,
            )
            multimon_thread.start()
        except Exception:
            multimon = None

    # Pre-ecoute silencieuse (2026-10-03, demande utilisateur) : on n'ouvre le
    # son que si ca vaut la peine -- voix reconnue ou message decode --, puis
    # on joue tout ce qui a ete mis en attente (tant pis pour le temps reel).
    confirmed, decoded_early, pre_filtered = _prelisten(audio_queue, prebuffer, sample_rate, target, freq_hz, mode,
                                                        multimon, multimon_queue)
    if not confirmed:
        print(f"[radio_watch] pre-ecoute {mhz:.4f} MHz : rien qui vaille la peine, son jamais ouvert")
        _last_stream_fingerprint = (noise_signatures.fingerprint(pre_filtered, sample_rate, "s16le")
                                    if pre_filtered else None)
        _last_stream_filtered = (pre_filtered, sample_rate)
        for proc in (rtl_fm, multimon):
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
        return False

    # Prise unique : une voix FM ne s'ouvre pas sur du numerique. Des synchros
    # deja vues : on laisse aux decodeurs ATTENTE_NUMERIQUE_SEC pour se confirmer.
    if arbitre is not None:
        if arbitre.numerique_en_vue():
            arbitre.attendre_numerique(ATTENTE_NUMERIQUE_SEC)
        # Signal CONFIRME numerique (synchros validees sur un mode) : ce n'est
        # pas de la voix FM, meme si aucune voix n'a ete decodee -- relais DMR
        # au repos ("IDLE"), donnees, appel prive/chiffre. Sans ca, la
        # pre-ecoute se declenchait parfois sur le souffle d'un relais DMR
        # demodule en FM et ouvrait une "micro-ouverture" muette (carillon +
        # annonce de bande + silence d'une seconde + fermeture, rien d'audible ;
        # signale le 2026-10-26). DMR et FM d'un canal mixte ne sont jamais
        # emis au meme instant : une vraie voix FM (aucune synchro numerique a
        # cet instant) continue donc de s'ouvrir normalement.
        numerique = (arbitre.numerique_gagne()
                     or any(_dsdfme_validated(s) for s in arbitre.stats.values()))
        if numerique or not arbitre.prendre("analogique"):
            if arbitre.numerique_gagne():
                print(f"[radio_watch] {mhz:.4f} MHz : voix numerique ({arbitre.protocole}), la FM ne s'ouvre pas")
            else:
                print(f"[radio_watch] {mhz:.4f} MHz : signal numerique sans voix (IDLE/donnees/chiffre), la FM ne s'ouvre pas")
            for proc in (rtl_fm, multimon):
                if proc is not None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
            return False

    speak_with_lead_sound(INCOMING_SOUND, band_label, label="radio_watch", repetitive=True)
    play_sound_effect(FREQ_OPEN_SOUND, label="radio_watch")

    # Peripherique explicite (AUDIO_DEVICE) : le "default" ALSA de cette
    # machine route vers PulseAudio, qui se heurte a un bug connu du pilote
    # audio embarque du Pi (snd_bcm2835 -- "ALSA woke us up... nothing to
    # write", confirme dans le journal systemd) et echoue silencieusement a
    # produire du son (aucune exception cote Python : les ecritures dans le
    # pipe reussissent meme si rien n'est reellement joue). plughw parle
    # direct au materiel et fait la conversion de format/frequence/canaux
    # necessaire (dmix seul refuse le mono demande ici -- teste et ecarte en
    # session). L'acces exclusif qu'il impose n'est pas un probleme : espeak
    # et les effets sonores ne sont appeles qu'avant ou apres la diffusion,
    # jamais pendant, donc aucune vraie concurrence sur le peripherique.
    aplay = subprocess.Popen(
        # -B : tampon ALSA elargi (500ms) pour absorber l'irregularite de
        # rythme d'ecriture introduite par le passage par la file
        # audio_queue/fil de lecture (hypothese de sous-alimentation de
        # tampon -- underrun -- comme cause de la distorsion signalee en
        # session, a confirmer via le flux d'erreur surveille ci-dessous).
        ["aplay", "-D", AUDIO_DEVICE,
         "-r", str(sample_rate), "-f", "S16_LE", "-c", "1", "-q", "-B", "500000"],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    aplay_stderr_thread = threading.Thread(
        target=_aplay_stderr_watcher, args=(aplay,), daemon=True,
    )
    aplay_stderr_thread.start()

    settings = _settings(target)
    # Base = fenetre de reponse (on attend un retour avant de fermer), pas le
    # simple debounce de 3 s : une fin coupee entre deux interlocuteurs est pire
    # qu'un peu de souffle en fin de capture (meme choix que la voix numerique,
    # DSD_FME_IDLE_SEC). "attente_fin_s" regle a la main par l'utilisateur prime ;
    # sinon ATTENTE_REPONSE_SEC. maintien.delai module selon l'interet du canal.
    debounce = maintien.delai(settings["attente_fin_s"] or ATTENTE_REPONSE_SEC, target, freq_hz)
    quiet_windows_needed = int(debounce * 1000 / ANALYSIS_WINDOW_MS)
    quiet_windows_seen = 0
    consecutive_signal_windows = 0
    total_bytes = 0
    hold_sec = maintien.plafond(MAX_HOLD_SEC, target, freq_hz)  # plus de coupure d'un echange interessant
    max_bytes = int(sample_rate * 2 * hold_sec)
    heard_signal = True  # deja confirme par la pre-ecoute (_prelisten)
    decoded_messages = list(decoded_early)

    rnnoise_state = _rnnoise_create()
    resample_state = None
    smoothed_prob = None
    captured_chunks = []

    # Filtre passe-bande vocal (300-3000Hz, 2026-08-31, demande explicite
    # de l'utilisateur suite a l'analyse spectrale de noise_study/) -- pas
    # applique en WFM (diffusion FM commerciale/pirate : musique/parole a
    # pleine bande, un filtrage vocal etroit degraderait le contenu sans
    # raison, ce n'est pas la bande visee par cette demande).
    # Chaine de filtres commune (lib/chaine_audio.py : la meme que la touche F
    # du mode manuel et les canaux RTLSDR-Airband). RNNoise juge le son
    # filtre NON nettoye (detection inchangee) ; le son brut est garde a cote
    # pour la touche B du journal.
    chain = chaine_audio.nouvelle(target, freq_hz, sample_rate, mode)
    raw_chunks, filtered_chunks = [], []

    probs_seen, trigger_prob, early_cut = [], None, False
    raison_fin = "plafond"   # pourquoi on a ferme (diagnostic de calibrage, 2026-10-28)
    start = signal_at = time.time()
    try:
        while total_bytes < max_bytes and time.time() - start < hold_sec + 5:
            if maintien.zap_depuis(start):
                print("[radio_watch] zapper : ecoute arretee a la demande")
                raison_fin = "zap"
                break
            if arbitre is not None and arbitre.numerique_gagne():
                print(f"[radio_watch] voix numerique ({arbitre.protocole}) sur le canal : la FM s'efface")
                raison_fin = "voix numerique"
                break
            # Timeout (pas un .get() bloquant sans limite) : une attente
            # bloquante pure sur Condition.wait() ne se reveille QUE quand la
            # file recoit quelque chose, et un signal (SIGTERM, "systemctl
            # restart") ne peut alors pas interrompre l'attente -- ca laissait
            # rtl_fm/aplay/multimon orphelins indefiniment (observe et
            # corrige en session, plusieurs redemarrages bloques). Une
            # reverification periodique laisse le signal s'appliquer.
            replaying = bool(prebuffer)  # deja donne a multimon pendant la pre-ecoute
            if prebuffer:
                chunk = prebuffer.pop(0)
            else:
                try:
                    chunk = audio_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
            if chunk is None:
                raison_fin = "flux termine"
                break
            if multimon is not None and not replaying:
                # Signal BRUT, non filtre -- multimon-ng decode des tons
                # numeriques (POCSAG, AFSK1200...) qui peuvent deborder ou
                # etre sensibles a la phase/amplitude alteree par le
                # filtre vocal, mieux vaut ne pas y toucher pour le decode.
                try:
                    multimon.stdin.write(chunk)
                except Exception:
                    pass
            raw_chunks.append(chunk)
            filtered, chunk = chain.process_s16le(chunk)
            filtered_chunks.append(filtered)
            try:
                aplay.stdin.write(audio_lock.scale(chunk, canal="ecoute"))
            except Exception:
                # Le detail de l'erreur remonte via le fil aplay_stderr_thread
                # (surveillance continue, voir _aplay_stderr_watcher) -- ne pas
                # relire aplay.stderr ici, ce serait un deuxieme lecteur sur le
                # meme flux et ca entrerait en conflit avec ce fil.
                raison_fin = "erreur audio"
                break
            captured_chunks.append(chunk)
            total_bytes += len(chunk)

            is_signal, rms, resample_state, smoothed_prob = _chunk_is_signal(
                filtered, sample_rate, rnnoise_state, resample_state, smoothed_prob
            )
            if smoothed_prob is not None:
                probs_seen.append(smoothed_prob)
            if is_signal:
                consecutive_signal_windows += 1
                quiet_windows_seen = 0
                if consecutive_signal_windows >= MIN_CONSECUTIVE_SIGNAL_WINDOWS and not heard_signal:
                    # Diagnostic (2026-08-30) : trace la valeur reelle qui a
                    # declenche la detection -- indispensable pour calibrer
                    # SPEECH_PROB_THRESHOLD/SPEECH_PROB_SMOOTHING_ALPHA sur de
                    # vraies conditions RF (impossible a rejouer apres coup,
                    # le spectre change en permanence -- voir ROADMAP.md).
                    print(f"[radio_watch] declenchement signal : smoothed_prob={smoothed_prob:.3f}, rms={rms}")
                    heard_signal = True
                    trigger_prob = smoothed_prob
                    signal_at = time.time()
            else:
                consecutive_signal_windows = 0
                quiet_windows_seen += 1
                if heard_signal and quiet_windows_seen >= quiet_windows_needed \
                        and time.time() - signal_at >= MIN_ECOUTE_SEC:
                    raison_fin = "fenetre de reponse ecoulee"
                    break
            if not heard_signal and time.time() - start >= EARLY_CUT_SEC:
                early_cut = True
                print(f"[radio_watch] coupure precoce : aucun signal reconnu en {EARLY_CUT_SEC} s")
                break

            if multimon_queue is not None:
                while not multimon_queue.empty():
                    decoded_messages.append(multimon_queue.get_nowait())
    finally:
        procs = [rtl_fm, aplay]
        if multimon is not None:
            procs.append(multimon)
        for proc in procs:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _rnnoise_destroy(rnnoise_state)

    log_session = sound_player.get_recording_session()
    if log_session is not None and captured_chunks:
        log_session.add_raw_pcm(b"".join(captured_chunks), sample_rate, "s16le")
    # Empreinte de l'audio radio seul (sans annonces) : memorisee avec la
    # derniere capture, pour la commande console "parasite".
    # (variables globales declarees en tete de fonction)
    _last_stream_fingerprint = (noise_signatures.fingerprint(b"".join(filtered_chunks), sample_rate, "s16le")
                                if filtered_chunks else None)
    _last_stream_raw = (b"".join(raw_chunks), sample_rate)
    _last_stream_filtered = (b"".join(filtered_chunks), sample_rate)
    # Mesures du systeme, confrontees plus tard aux notes de l'utilisateur.
    silence_final_s = round(quiet_windows_seen * ANALYSIS_WINDOW_MS / 1000, 1)
    _last_stream_metrics = {
        "prob_declenchement": round(trigger_prob, 3) if trigger_prob is not None else None,
        "prob_max": round(max(probs_seen), 3) if probs_seen else None,
        "prob_mediane": round(sorted(probs_seen)[len(probs_seen) // 2], 3) if probs_seen else None,
        "duree_s": round(total_bytes / (sample_rate * 2), 1),
        "coupure_precoce": early_cut,
        "raison_fin": raison_fin,
        "silence_final_s": silence_final_s,
    }
    print(f"[radio_watch] fermeture {mhz:.4f} MHz : {raison_fin} "
          f"(duree {_last_stream_metrics['duree_s']} s, silence final {silence_final_s} s, "
          f"attente de reponse {round(debounce, 1)} s)")
    if mode == "nfm" and raw_chunks:  # son brut : la tonalite y est encore (le filtre vocal la retire)
        try:
            _last_stream_metrics["ctcss_hz"] = ctcss.detecter(b"".join(raw_chunks), sample_rate)
        except Exception as exc:
            print(f"[radio_watch] tonalite non mesuree : {exc!r}")

    if multimon_queue is not None:
        time.sleep(0.2)
        while not multimon_queue.empty():
            decoded_messages.append(multimon_queue.get_nowait())

    if hold_sec >= maintien.SECURITE_SEC and total_bytes >= max_bytes:
        # Coupee par le garde-fou, pas perdue (2026-10-23) : on le dit.
        speak(dire("radio.coupe_garde_fou", minutes=round(hold_sec / 60)))
    meaningful_messages = [m for m in decoded_messages if _is_meaningful_decoded_message(m)]
    ignored_count = len(decoded_messages) - len(meaningful_messages)
    if ignored_count:
        print(f"[radio_watch] {ignored_count} message(s) decode(s) ignore(s) (trop court/bruit)")
    for msg in meaningful_messages:
        print(f"[radio_watch] decode numerique: {msg}")
        speak_with_lead_sound(DECODING_MORSE_SOUND, dire("radio.message_numerique_decode", msg=msg), label="radio_watch")

    return heard_signal or bool(meaningful_messages)


PRELISTEN_MAX_SEC = 6.0   # pre-ecoute silencieuse au plus ; voix reconnue = ouverture aussitot
ATTENTE_NUMERIQUE_SEC = 2.0  # prise unique : synchros vues, la voix FM attend que le numerique se confirme


def _prelisten(audio_queue, prebuffer, sample_rate, target, freq_hz, mode, multimon, multimon_queue):
    """Ecoute sans rien jouer : (ca vaut la peine ?, messages decodes, audio
    filtre). Vaut la peine : MIN_CONSECUTIVE_SIGNAL_WINDOWS fenetres de voix
    d'affilee (RNNoise, meme detecteur que pendant la diffusion) ou un message
    de donnees lisible. Tout l'audio recu reste dans prebuffer, rejoue ensuite."""
    chain = chaine_audio.nouvelle(target, freq_hz, sample_rate, mode)
    rnnoise_state = _rnnoise_create()
    resample_state = smoothed_prob = None
    consecutive, filtered_all, decoded = 0, [], []
    analysed = 0
    # Rafales (2026-11-01) : voix reconnue mais trop courte -> on garde tout en
    # tampon et on surveille ATTENTE_RAFALE_SEC de plus, le temps d'une reponse.
    voix_fenetres, voix_vue_a = 0, None
    voix_min = int(MIN_VOIX_OUVERTURE_SEC * 1000 / ANALYSIS_WINDOW_MS)
    start = time.time()
    try:
        while time.time() - start < PRELISTEN_MAX_SEC or (
                voix_vue_a is not None and time.time() - voix_vue_a < ATTENTE_RAFALE_SEC):
            if analysed < len(prebuffer):
                chunk = prebuffer[analysed]
            else:
                try:
                    chunk = audio_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                prebuffer.append(chunk)
            analysed += 1
            if chunk is None:
                break
            if multimon is not None:
                try:
                    multimon.stdin.write(chunk)
                except Exception:
                    pass
            filtered, _ = chain.process_s16le(chunk)
            filtered_all.append(filtered)
            is_signal, _, resample_state, smoothed_prob = _chunk_is_signal(
                filtered, sample_rate, rnnoise_state, resample_state, smoothed_prob)
            consecutive = consecutive + 1 if is_signal else 0
            if multimon_queue is not None:
                while not multimon_queue.empty():
                    decoded.append(multimon_queue.get_nowait())
            if voix_vue_a is not None and is_signal:
                voix_fenetres += 1
                voix_vue_a = time.time()   # chaque morceau de voix prolonge la surveillance
                if voix_fenetres >= voix_min:
                    print(f"[radio_watch] pre-ecoute : assez de voix en {time.time() - start:.1f} s, on ouvre")
                    return True, decoded, b"".join(filtered_all)
            elif consecutive >= MIN_CONSECUTIVE_SIGNAL_WINDOWS:
                # Ecremage (2026-10-27) : avant d'ouvrir, verifier que la rafale
                # qui declenche n'est pas un parasite connu du canal. Si c'en est
                # un, on NE confirme PAS -- on ecarte ce qui a ete analyse (pas
                # d'ouverture NI de rejeu de la rafale) et on continue d'ecouter la
                # vraie voix, sans jamais abandonner la frequence. Seuil de
                # similarite conservateur (MATCH_SIMILARITY) : un faux ecremage
                # jetterait de la vraie voix (litterature : seuil a regler par ROC,
                # cote faux-rejet couteux ici).
                if noise_signatures.has_signatures_near(freq_hz):
                    recent = b"".join(filtered_all)[-int(sample_rate * 2 * noise_signatures.MAX_AUDIO_SEC):]
                    is_parasite, psim = noise_signatures.matches_known_noise(
                        freq_hz, recent, sample_rate, "s16le")
                    if is_parasite:
                        print(f"[radio_watch] pre-ecoute {freq_hz / 1e6:.4f} MHz : rafale parasite connue "
                              f"ecremee (similarite {psim:.2f}), on continue d'ecouter")
                        del prebuffer[:analysed]
                        analysed = 0
                        filtered_all = []
                        consecutive = 0
                        continue
                print(f"[radio_watch] pre-ecoute : voix reconnue en {time.time() - start:.1f} s "
                      f"(prob {smoothed_prob:.2f})")
                voix_fenetres, voix_vue_a = consecutive, time.time()
                if voix_fenetres >= voix_min:
                    return True, decoded, b"".join(filtered_all)
            if any(_is_meaningful_decoded_message(m) for m in decoded):
                return True, decoded, b"".join(filtered_all)
    finally:
        _rnnoise_destroy(rnnoise_state)
    if voix_vue_a is not None:
        print(f"[radio_watch] pre-ecoute {freq_hz / 1e6:.4f} MHz : rafale de "
              f"{voix_fenetres * ANALYSIS_WINDOW_MS / 1000:.1f} s sans suite, ni ouverture ni journal")
    return False, decoded, b"".join(filtered_all)


HF_ANALYSIS_MAX_HZ = 30_000_000  # analyse "vraie station ou artefact" sous 30 MHz
_last_stream_fingerprint = None
_last_stream_metrics = {}
_last_stream_raw = (b"", 22050)       # son brut de la derniere ecoute (touche B du journal)
_last_stream_filtered = (b"", 22050)  # filtre vocal seul : empreinte du bruit si ecarte


def _save_raw(path):
    """Son brut a cote de l'enregistrement : <nom>.brut.mp3 (meme purge a 48 h)."""
    pcm, rate = _last_stream_raw
    if not path or not pcm:
        return

    def work():
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(rate),
                            "-ac", "1", "-i", "-", path[:-4] + ".brut.mp3"], input=pcm, timeout=60)
        except Exception as exc:
            print(f"[radio_watch] son brut non garde : {exc!r}")

    threading.Thread(target=work, daemon=True).start()


# Fenetres de donnees du cycle (lib/attention.py, DATA_WINDOWS) : ACARS,
# VDL2, APRS, capteurs sans fil 433/868 MHz (et pneus), pagers 466 MHz,
# ADS-B direct 1090 MHz, balises de detresse 406 MHz. Chacune prend le dongle pour
# sa duree d'ecoute puis le rend.
# Chien de garde des fenetres de donnees (2026-10-03) : une cle RTL-SDR qui
# cesse d'envoyer des echantillons laisse le programme radio en attente, et
# la fenetre avec lui (scanner bloque 8 h 30 sur LoRa, plus aucune ecoute).
# Au-dela de la duree prevue + WINDOW_GRACE_SEC, les programmes lances par
# la veille sont arretes : la lecture bloquee rend la main, le cycle reprend.
WINDOW_GRACE_SEC = 120
# Seuls les programmes qui tiennent la cle sont arretes (jamais la voix) : le
# reste de la chaine (sox, decodeurs, multimon-ng) se termine faute d'entree.
DONGLE_PROGRAMS = ("rtl_sdr", "rtl_fm", "rtl_433", "rtl_airband", "dump1090", "dumpvdl2", "acarsdec", "dec406",
                   "welle-cli", "rtl_tcp")
_window = {"nom": None, "fin": 0.0}


def _descendants(pid):
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def _comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def _window_watchdog():
    while True:
        time.sleep(10)
        name, end = _window["nom"], _window["fin"]
        if name is None or time.time() < end + WINDOW_GRACE_SEC:
            continue
        stuck = [pid for pid in _descendants(os.getpid()) if _comm(pid).startswith(DONGLE_PROGRAMS)]
        print(f"[radio_watch] fenetre {name} bloquee depuis {round(time.time() - end)} s : "
              f"arret de {len(stuck)} programme(s) radio")
        for pid in stuck:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _window["fin"] = time.time()  # nouvel essai dans WINDOW_GRACE_SEC si rien ne bouge


def _guarded(name, scan):
    def run(dwell_sec, **kwargs):
        _window.update(nom=name, fin=time.time() + dwell_sec)
        try:
            return scan(dwell_sec=dwell_sec, **kwargs)
        except Exception:
            # Une fenetre en erreur ne fait plus tomber tout le scanner
            # (2026-10-03 : redemarrages en boucle sur une erreur de la fenetre
            # DAB+, le cycle reprenait toujours par les capteurs).
            import traceback
            print(f"[radio_watch] fenetre {name} en erreur, le cycle continue :")
            traceback.print_exc()
            return False
        finally:
            _window.update(nom=None, fin=0.0)
    return run


# --- Fenetre TETRA (2026-10-26) --------------------------------------------------
# Le TETRA (pi/4-DQPSK) demande de l'I/Q brut, que le Pi ne sait pas decoder
# sans chauffer. Dans sa fenetre, le Pi SERT son dongle par rtl_tcp et la
# machine compagne la plus libre decode en direct (lib/tetra.py ->
# compagnon_serveur -> bridge/tetra_neo). Comme les fenetres acars/airband
# prennent le dongle quelques secondes, celle-ci le prend le temps de sonder
# une ou deux porteuses. Sans machine compagne connectee capable : fenetre
# sautee (on ne chauffe pas le Pi pour rien). Ecoute passive, metadonnees en
# clair + voix non chiffree uniquement -- jamais de dechiffrement.
TETRA_RTL_TCP_PORT = 1234
TETRA_SECONDES = 14          # par porteuse : large de quoi verrouiller + guetter la voix
# Suivi d'appel (2026-10-27) : la machine compagne suit l'appel sur sa porteuse
# de trafic (dsd-neo -T) et prolonge l'ecoute tant qu'il dure, jusqu'a ce plafond.
TETRA_SUIVI_MAX_SEC = 90
TETRA_EXPLORATION_SEC = 8    # une piste inconnue : assez pour une synchro de controle


def _tetra_compagnon_present():
    try:
        import compagnon
        return any("tetra" in (c.get("capacites") or []) and c.get("presente")
                   for c in compagnon.companions())
    except Exception as exc:
        print(f"[radio_watch] TETRA, machines compagnes illisibles : {exc!r}")
        return False


_tetra_dernier_reseau = None
_tetra_dernier_clairs = {}  # identite reseau -> nb d'appels clairs deja annonces (anti-repetition)


def _tetra_nouvelles(freq_hz, res):
    """Registre + journal interne d'une ecoute TETRA ; rend ses nouvelles."""
    try:
        news = tetra.enregistrer(freq_hz, res)
        tetra.archiver(freq_hz, res, news)
        return news
    except Exception as exc:
        print(f"[radio_watch] registre TETRA : {exc!r}")
        return {}


def _annonce_tetra(freq_hz, res, news=None):
    """Journalise l'identite reseau TETRA (metadonnees en clair) et joue la voix
    en clair si la machine compagne en a decode.

    Pas d'ouverture fantome (signale 2026-10-27) : sur une porteuse de controle,
    le BSCH est present en PERMANENCE, donc _annonce_tetra etait appele a chaque
    fenetre (toutes les 3 min) et jouait carillon + annonce + son d'ouverture +
    ~1 s de silence + fermeture, sans aucun trafic -- une fausse reception. On ne
    joue l'habillage sonore (ouverture / voix / fermeture) QUE s'il y a de la voix
    en clair. Sinon on journalise les metadonnees sans bruit, et on n'annonce
    vocalement l'identite reseau que si elle a CHANGE depuis la derniere fois.

    2026-10-27 : le resume compte desormais les postes actifs, qui bougent a
    chaque fenetre ; le changement se juge donc sur l'IDENTITE (reseau + zone),
    pas sur la phrase. Nouvelles du registre (porteuse de trafic, postes jamais
    vus) dites sobrement, comme les nouvelles du DMR."""
    global _tetra_dernier_reseau, _tetra_dernier_clairs
    import veille_donnees as vd
    from phrases import nombre
    news = news or {}
    resume = res.get("resume") or dire("tetra.recu")
    voix = res.get("voix_wav")
    a_voix = bool(voix and os.path.exists(voix))
    r = res.get("reseau") or {}
    identite = (r.get("mcc"), r.get("mnc"), r.get("la"))
    reseau_change = identite != _tetra_dernier_reseau
    lock = audio_lock.acquire()
    log_session = radio_log.RadioLogSession(dire("tetra.libelle"))
    sound_player.set_recording_session(log_session)
    saved = False
    try:
        if a_voix:
            prive_tetra = any(a.get("prive") for a in (res.get("activite") or {}).get("appels", {}).values())
            play_sound_effect(TETRA_APPEL_PRIVE_SOUND if prive_tetra else TETRA_OUVERTURE_SOUND, label="radio_watch")
            speak(resume)
            play_sound_effect(FREQ_OPEN_SOUND, label="radio_watch")
            # Par l'arbitre (play_sound_effect) : la voix s'attenue sous une
            # source plus prioritaire et est enregistree dans la prise du
            # ticket courant -- plus d'aplay brut a plein volume.
            play_sound_effect(voix, timeout_sec=60, label="radio_watch")
            play_sound_effect(FREQ_OVER_SOUND, label="radio_watch")
            path = log_session.finalize()
        else:
            # Signalisation seule : aucune ceremonie sonore, et SURTOUT on ne
            # fait pas croire a une reception. dsd-neo voit des appels en clair
            # dans la signalisation mais n'en sort pas la voix de trafic ici
            # (diag 2026-10-31) : on le dit honnetement, et seulement quand le
            # nombre d'appels clairs AUGMENTE (sinon une bascule de zone
            # relancait l'annonce a vide). Sans appel clair nouveau, on se
            # contente de l'identite reseau quand elle change.
            appels = (res.get('activite') or {}).get('appels') or {}
            clairs = sum(1 for a in appels.values() if not a.get('chiffre'))
            vus = _tetra_dernier_clairs.get(identite, 0)
            if clairs > vus:
                speak(dire('tetra.clair_sans_voix', reseau=f"{r.get('mcc')}-{r.get('mnc')}",
                           nombre=nombre(clairs, 'appel en clair', 'appels en clair')))
                _tetra_dernier_clairs[identite] = clairs
            elif reseau_change:
                speak(resume)
            log_session.discard()
            path = None
        for f in news.get("nouvelles_porteuses") or []:
            vd.announce(dire("tetra.nouvelle_porteuse", frequence=format(f / 1e6, ".4f").replace(".", ",")),
                        TETRA_PORTEUSE_SOUND, "message_tetra", "radio_watch")
        for m in news.get("textes") or []:  # message texte en clair : dit comme un message APRS
            vd.announce(dire("tetra.message_texte", src=m["src"], dst=m.get("dst") or "?", texte=m["texte"]),
                        TETRA_MESSAGE_SOUND, f"message_tetra_{m['src']}", "radio_watch")
        if news.get("nouveaux_postes"):
            vd.announce(dire("tetra.nouveaux_postes", nombre=nombre(len(news["nouveaux_postes"]), "nouveau poste",
                                                                   "nouveaux postes")),
                        TETRA_PORTEUSE_SOUND, "message_tetra", "radio_watch")
        _tetra_dernier_reseau = identite
        # Cible "tetra" : la notation du journal (console) pique ou apaise la
        # fenetre TETRA, comme les autres cibles du scanner.
        noise_signatures.record_capture("tetra", freq_hz, "tetra", "TETRA", "tetra", path, None,
                                        {"numerique": True,
                                         "tetra": {"reseau": res.get("reseau"), "enc": res.get("enc"),
                                                   "clair": res.get("clair"), "bsch": res.get("bsch"),
                                                   "voix": a_voix, "machine": res.get("machine"),
                                                   "nouvelles": news}})
        saved = a_voix
        print(f"[radio_watch] TETRA {freq_hz / 1e6:.4f} MHz : {resume}"
              + (" + voix en clair" if a_voix else " (signalisation, pas d'ouverture)"))
    except Exception as exc:
        print(f"[radio_watch] annonce TETRA : {exc!r}")
    finally:
        sound_player.set_recording_session(None)
        audio_lock.release(lock)
        if saved and lock.silent:
            journal_notice()


_tetra_manuel_etat = {"freq": None, "quand": 0.0}


def _tetra_gain_de(freq_hz):
    for p in tetra.porteuses():
        if abs(p["freq_hz"] - freq_hz) <= tetra.TOLERANCE_HZ:
            return p.get("gain", tetra.GAIN_DEFAUT)
    return tetra.GAIN_DEFAUT


def _tetra_manuel(manual):
    """Mode manuel sur une porteuse TETRA : un decodage en direct par la machine
    compagne (annonce de l'identite reseau + voix en clair), une fois par accord
    (re-sonde toutes les 30 s si on reste dessus). Rend True si la frequence est
    TETRA (prise en charge ici au lieu de rtl_fm). L'appelant a deja arrete le
    recepteur manuel : pas de rtl_fm en meme temps que rtl_tcp."""
    freq = manual["freq_hz"]
    if not (tetra.active() and tetra.est_tetra(freq)):
        return False
    import manual_tuner
    if _tetra_manuel_etat["freq"] == freq and time.time() - _tetra_manuel_etat["quand"] < 30:
        return True
    _tetra_manuel_etat.update(freq=freq, quand=time.time())
    if not _tetra_compagnon_present():
        manual_tuner.event(dire("tetra.pas_de_machine"))
        return True
    manual_tuner.event(dire("tetra.ecoute", mhz=format(freq / 1e6, ".4f").replace(".", ",")))
    try:
        import compagnon
        host = compagnon._local_ip()
    except Exception:
        host = None
    try:
        proc = subprocess.Popen(["rtl_tcp", "-a", "0.0.0.0", "-p", str(TETRA_RTL_TCP_PORT)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"[radio_watch] rtl_tcp (TETRA manuel) impossible : {exc!r}")
        return True
    try:
        time.sleep(2.0)
        if proc.poll() is not None:
            manual_tuner.event(dire("tetra.echec"))
            return True
        ecoute = tetra.centre(freq)   # alias recentre : on ecoute le vrai centre
        res = tetra.decoder(ecoute, _tetra_gain_de(freq), TETRA_SECONDES, host=host, port=TETRA_RTL_TCP_PORT,
                            max_secondes=TETRA_SUIVI_MAX_SEC)
        tmp = res.pop("_tmp", None)
        try:
            tetra.noter(ecoute, res)
            if res.get("ok") and res.get("bsch"):
                _annonce_tetra(ecoute, res, _tetra_nouvelles(ecoute, res))
            elif res.get("ok"):
                manual_tuner.event(dire("tetra.rien"))
            else:
                manual_tuner.event(dire("tetra.echec"))
        finally:
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return True


# Autodecouverte TETRA (2026-10-27) : meme principe que la recherche DMR, mais
# le Pi ne decode pas le TETRA. Le balayage rtl_power ne fait que REPERER
# l'energie sur le raster 25 kHz des bandes civiles TETRA ; chaque pic inconnu
# devient une piste (tetra.decouvrir) que la fenetre TETRA va sonder via la
# machine compagne, seule capable de confirmer une porteuse de controle. Ainsi
# le scanner trouve ses porteuses lui-meme au lieu d'etre cable sur une seule.
TETRA_SEARCH_MARGIN_DB = 10.0
TETRA_SEARCH_MAX_PEAKS = 12


def _tetra_search():
    """Balaye les bandes TETRA (tetra.BANDES) et ajoute les pics inconnus comme
    pistes a sonder. Rend la liste des frequences ajoutees. Utilise le dongle
    en direct (rtl_power) : l'appelant doit l'avoir libre (pas de rtl_tcp)."""
    known = [p["freq_hz"] for p in tetra.porteuses()]
    ajoutees = []
    for low, high in tetra.BANDES:
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            run = subprocess.run(["rtl_power", "-f", f"{low}:{high}:{tetra.RASTER_HZ}", "-i", "1", "-1", path],
                                 capture_output=True, timeout=180)
            all_db, bins = _read_csv_bins(path) if run.returncode == 0 else ([], [])
        except Exception as exc:
            print(f"[radio_watch] balayage TETRA {low / 1e6:.0f}-{high / 1e6:.0f} MHz impossible ({exc!r})")
            all_db, bins = [], []
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        floor = _percentile(all_db, 20) if all_db else None
        if floor is None:
            continue
        peaks = []
        for f, db in sorted(bins, key=lambda b: -b[1]):
            if db < floor + TETRA_SEARCH_MARGIN_DB or len(peaks) >= TETRA_SEARCH_MAX_PEAKS:
                break
            f = int(round(f / tetra.RASTER_HZ) * tetra.RASTER_HZ)   # porteuses TETRA sur le raster 25 kHz
            if any(abs(f - k) < tetra.TOLERANCE_HZ for k in known) or any(abs(f - p) < tetra.RASTER_HZ for p in peaks):
                continue
            peaks.append(f)
        for f in peaks:
            if tetra.decouvrir(f):
                ajoutees.append(f)
                known.append(f)
        print(f"[radio_watch] balayage TETRA {low / 1e6:.0f}-{high / 1e6:.0f} MHz : "
              f"{len(peaks)} pic(s), {sum(1 for f in peaks if f in ajoutees)} nouvelle(s) piste(s)")
    return ajoutees


def _tetra_autodecouverte_si_due(limit_c):
    """Avant d'ouvrir rtl_tcp pour la machine compagne : si un balayage
    d'autodecouverte est du et que le Pi n'est pas trop chaud, on balaie les
    bandes TETRA avec le dongle libre, puis on elague les pistes muettes."""
    if not tetra.decouverte_due():
        return
    if _too_hot(limit_c):
        return   # on reessaiera a la prochaine fenetre, une fois refroidi
    meteo_locale.set_scanner_state("balayage_tetra")
    try:
        ajoutees = _tetra_search()
        if ajoutees:
            print(f"[radio_watch] autodecouverte TETRA : {len(ajoutees)} porteuse(s) a sonder")
        oubliees = tetra.elaguer()
        if oubliees:
            print(f"[radio_watch] autodecouverte TETRA : {oubliees} piste(s) muette(s) oubliee(s)")
    except Exception as exc:
        print(f"[radio_watch] autodecouverte TETRA : {exc!r}")
    finally:
        tetra.decouverte_faite()   # meme en cas d'echec : pas de boucle serree
        meteo_locale.set_scanner_state("actif")


def _tetra_window(dwell_sec=TETRA_SECONDES * 2):
    """Sonde une ou deux porteuses TETRA via la machine compagne (le Pi sert
    rtl_tcp). Autodecouverte : porteuses confirmees d'abord, puis exploration.

    2026-10-27 : la porteuse de controle confirmee garde l'essentiel de la
    fenetre et la machine compagne y SUIT les appels (prolongation jusqu'a
    TETRA_SUIVI_MAX_SEC tant qu'un appel dure) ; une piste d'exploration n'a que
    TETRA_EXPLORATION_SEC. Chaque ecoute nourrit le registre et le journal
    interne. Rend True (piqure) seulement si l'ecoute a apporte du neuf ou du
    trafic -- appel, voix en clair, porteuse de trafic, postes jamais vus --,
    pas sur la simple synchro permanente du controle."""
    if not tetra.active():
        return False
    if not _tetra_compagnon_present():
        print("[radio_watch] fenetre TETRA sautee : aucune machine compagne connectee pour le TETRA")
        return False
    # Autodecouverte d'abord (dongle libre, avant rtl_tcp) : le scanner cherche
    # ses propres porteuses TETRA au lieu d'etre cable sur une seule.
    _tetra_autodecouverte_si_due(_meteo_temp_limit()[0])
    cands = tetra.candidats(2)
    if not cands:
        return False
    try:
        import compagnon
        host = compagnon._local_ip()
    except Exception:
        host = None
    try:
        proc = subprocess.Popen(["rtl_tcp", "-a", "0.0.0.0", "-p", str(TETRA_RTL_TCP_PORT)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"[radio_watch] rtl_tcp (TETRA) impossible : {exc!r}")
        return False
    hit = False
    try:
        time.sleep(2.0)  # rtl_tcp pret a servir
        if proc.poll() is not None:
            print("[radio_watch] rtl_tcp (TETRA) s'est arrete tout de suite (dongle occupe ?)")
            return False
        reste = float(dwell_sec or TETRA_SECONDES * 2)
        principale = tetra.porteuses() and any(p.get("confirmee") for p in tetra.porteuses())
        for i, (freq, gain) in enumerate(cands):
            if i == 0 and principale:   # le controle : l'essentiel de la fenetre, appels suivis
                sec = int(max(6, reste - (len(cands) - 1) * (TETRA_EXPLORATION_SEC + 2)))
                plafond = max(sec, TETRA_SUIVI_MAX_SEC)
            else:                       # exploration : sonde courte si calme, mais on SUIT
                # un appel qui s'y presente -- meme marge de suivi que le controle,
                # sinon un appel clair detecte en exploration restait "repere, pas
                # suivi" (2026-10-31, doctrine : capter la voix a tout prix).
                sec = int(min(TETRA_EXPLORATION_SEC, reste))
                plafond = max(sec, TETRA_SUIVI_MAX_SEC)
            if sec < 6:
                break
            res = tetra.decoder(freq, gain, sec, host=host, port=TETRA_RTL_TCP_PORT, max_secondes=plafond)
            reste -= (res.get("duree") or sec) + 2
            tmp = res.pop("_tmp", None)
            try:
                if not res.get("ok"):
                    if res.get("erreur"):
                        print(f"[radio_watch] TETRA {freq / 1e6:.4f} MHz : {res['erreur']}")
                    tetra.noter(freq, res)
                    if "compagne" in (res.get("erreur") or ""):
                        break  # plus de machine : inutile d'insister cette fenetre
                    continue
                tetra.noter(freq, res)
                if res.get("bsch"):
                    news = _tetra_nouvelles(freq, res)
                    hit = hit or tetra.piquant(news)
                    _annonce_tetra(freq, res, news)
            finally:
                if tmp:
                    import shutil
                    shutil.rmtree(tmp, ignore_errors=True)
            if reste < 6:
                break
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return hit


DATA_SCANNERS = {name: _guarded(name, scan) for name, scan in {
    "acars": acars_watch.scan_window,
    "vdl2": vdl2_watch.scan_window,
    "aprs": aprs_watch.scan_window,
    "dmr": _dmr_window,
    "capteurs": capteurs_watch.scan_window,
    "pagers": pagers_watch.scan_window,
    "adsb": adsb_local_watch.scan_window,
    "balises": balises_watch.scan_window,
    "sondes": sondes_watch.scan_window,
    "lora": lora_watch.scan_window,
    "dab": dab_watch.scan_window,
    "tetra": _tetra_window,
}.items()}


def _speech_detector():
    """Detecteur de voix (RNNoise, comme les captations) pour les passages
    de satellites radioamateurs : fonction (morceau PCM, frequence) -> voix ?"""
    state = {"rn": _rnnoise_create(), "rs": None, "p": None}

    def check(chunk, rate):
        voice, _, state["rs"], state["p"] = _chunk_is_signal(chunk, rate, state["rn"], state["rs"], state["p"])
        return voice
    return check


def _satellite_pass_if_due():
    """Passage de satellite qui commence (lib/satellites.py) : il prend la
    cle pour sa duree (satellites_watch). Saute si l'interet "satellites" est
    "désactivé" (commande intérêts) -- ex. pas d'antenne pour les capter
    (2026-10-09)."""
    try:
        import interets
        if interets.disabled("satellites"):
            return
    except Exception:
        pass
    try:
        p = satellites.due()
    except Exception as exc:  # orbites indisponibles : le scan continue
        print(f"[radio_watch] satellites : {exc!r}")
        return
    if p:
        print(f"[radio_watch] passage {p['nom']} ({p['famille']}, {p['elevation_max']} degres)")
        satellites_watch.run_pass(p, _speech_detector)


def _meteo_temp_limit():
    """(temperature maxi permise pour la veille meteo, niveau de menace)."""
    level = meteo_locale.published_level()
    return (METEO_PAUSE_CRITICAL_MAX_C if level >= 3 else METEO_PAUSE_MAX_C), level


def _too_hot(limit):
    temp = _get_cpu_temp_c()
    if temp is None:
        return False
    return temp >= limit if limit >= TEMP_CRITICAL_C else temp > limit


def _meteo_scan_if_requested(max_temp=None):
    """Balayage du panel radio meteo demande par la console (commande
    "meteo") : stations APRS, capteurs 433 et 868 MHz, ballon-sonde a portee.
    La console attend la fin (meteo_locale.wait_scan) pour donner un
    bulletin complet, ultra-local compris. max_temp : pendant une pause
    thermique, on s'arrete si le Pi depasse ce seuil."""
    try:
        if not meteo_locale.take_scan_request():
            return
        started = time.time()
        for name, dwell in meteo_locale.FORCED_SCAN + [("sondes", 45)]:  # sondes : rend la main sans ballon
            if max_temp is not None and _too_hot(max_temp):
                print(f"[radio_watch] balayage meteo ecourte : plus de {max_temp:.0f}C")
                break
            DATA_SCANNERS[name](dwell_sec=dwell)
        stations, sensors = meteo_locale.scan_counts(started)
        meteo_locale.scan_done(started, time.time(), stations, sensors)
    except Exception as exc:
        print(f"[radio_watch] balayage meteo : {exc!r}")


def _antenne_if_requested():
    """Mesure de l'antenne demandee par la console (commande "antenne",
    lib/mesure_antenne.py) : une passe par bande, la console attend le resultat."""
    if not mesure_antenne.requested():
        return
    mesure_antenne.take_request()
    print("[radio_watch] mesure de l'antenne demandee par la console")
    meteo_locale.set_scanner_state("mesure_antenne")
    try:
        res = mesure_antenne.measure(BANDS, lambda name: _settings(name)["marge_detection_db"])
    except Exception as exc:
        print(f"[radio_watch] mesure de l'antenne : {exc!r}")
        res = {"t": time.time(), "erreur": "rtl_power", "bandes": []}
    mesure_antenne.done(res)
    meteo_locale.set_scanner_state("actif")


_meteo_pause = {"last_watch": 0.0, "announced": False}


# Ecoute manuelle pendant une pause thermique (2026-10-04 : mode manuel muet,
# sans explication, pendant toute une pause) : une seule reception, peu de
# calcul, demandee par l'utilisateur -- permise tant que le Pi reste sous
# MANUAL_HEAT_MAX_C ; au-dessus, coupee et la console dit pourquoi.
MANUAL_HEAT_MAX_C = 70.0
_manual = {"stream": None, "voulu": None, "depuis": 0.0, "averti": None}


def _manual_during_pause():
    """Rend True si l'utilisateur ecoute en manuel (attente courte)."""
    stream = _manual["stream"]
    request = noise_signatures.manual_request()
    if stream is None or not request:
        if stream is not None:
            stream.stop()
        _manual["averti"] = None
        return False
    temp = _get_cpu_temp_c()
    if temp is not None and temp > MANUAL_HEAT_MAX_C:
        stream.stop()
        if _manual["averti"] != "chaud":
            _manual["averti"] = "chaud"
            import manual_tuner
            manual_tuner.event(dire("manuel.trop_chaud", temp=format(temp, ".0f"), max=format(MANUAL_HEAT_MAX_C, ".0f")))
        return True
    if _manual["averti"] == "chaud":
        import manual_tuner
        manual_tuner.event(dire("manuel.reprise_apres_chaleur"))
    _manual["averti"] = "ok"
    wanted = (request["freq_hz"], request["mode"], request.get("filter", False), request.get("target"),
              request.get("numerique", False))
    if wanted != _manual["voulu"]:
        _manual["voulu"], _manual["depuis"] = wanted, time.time()
    elif time.time() - _manual["depuis"] >= MANUAL_SETTLE_SEC:
        stream.tune(*wanted)
    return True


def _thermal_pause_wait(duration):
    """Attente pendant la pause thermique, avec la veille meteo sous chaleur
    (voir METEO_PAUSE_MAX_C) : rien d'autre que les fenetres meteo, et
    l'ecoute manuelle tant que le Pi le supporte (_manual_during_pause)."""
    end = time.time() + duration
    while time.time() < end:
        if _manual_during_pause():
            time.sleep(0.1)
            continue
        limit, level = _meteo_temp_limit()
        allowed = not _too_hot(limit)
        meteo_locale.set_scanner_state("pause_thermique_meteo" if allowed else "pause_thermique")
        if allowed and mesure_antenne.requested():  # mesure courte, permise comme la veille meteo
            _antenne_if_requested()
        wanted = meteo_locale.scan_requested() or level >= 2
        if allowed and wanted:
            if not _meteo_pause["announced"]:
                _meteo_pause["announced"] = True
                _speak_pause(dire("radio.malgre_chaleur_garde_ecoute", limit=format(limit, ".0f")))
            if meteo_locale.scan_requested():
                _meteo_scan_if_requested(max_temp=limit)
            elif level >= 2 and time.time() - _meteo_pause["last_watch"] >= METEO_PAUSE_WATCH_EVERY_SEC:
                _meteo_pause["last_watch"] = time.time()
                print(f"[radio_watch] veille meteo sous chaleur (menace niveau {level})")
                for name, dwell in METEO_PAUSE_WATCH:
                    if _too_hot(limit):
                        break
                    DATA_SCANNERS[name](dwell_sec=dwell)
        time.sleep(METEO_PAUSE_TICK_SEC)


def _numerique_live(freq, band_label, target):
    """Ecoute numerique en direct : commence par le decodeur le plus sur pour la
    bande (ordre de confiance), et "juge suivant" fait passer au decodeur suivant
    sur le MEME signal. Sans action de l'utilisateur, se comporte comme avant
    (un seul passage, auto-detection d'abord)."""
    try:
        import juges_watch
        modes = juges_watch.ordre(juges_watch._scores(), band_label, list(juges_watch.JUGES))
    except Exception:
        modes = ["-fa"]
    for mode_flag in modes:
        res = try_digital_voice_decode(freq, mode_flag, band_label, target=target, live=True)
        if res == "SUIVANT":          # l'utilisateur veut un autre decodeur
            continue
        return res
    return None


def _ouvrir_prise(freq):
    """Prise unique sur freq (lib/prise_unique.py), ou None : l'ancien
    enchainement prend alors le relais."""
    try:
        return prise_unique.Prise(_open_rtl_fm(prise_unique.commande(freq)))
    except Exception as exc:
        print(f"[radio_watch] prise unique impossible ({exc!r}), ancien enchainement")
        return None


def _ecoute_prise(prise, freq, band_label, target):
    """Tous les decodeurs sur le meme flux, en meme temps (2026-10-25) : un
    dsd-fme par mode utile sur la bande (auto-detection + NXDN/dPMR/M17 selon
    decodeurs.extra_modes) et la voix FM, l'arbitre donnant la parole. Le son
    brut de toute l'ecoute est garde une fois pour les juges. Rend (protocole
    numerique entendu ou None, voix FM entendue)."""
    arbitre = prise_unique.Arbitre()
    modes = list(dict.fromkeys(["-fa"] + [f for f, _ in decodeurs.extra_modes(freq)]))
    resultats = {}
    os.makedirs(BRUT_DIR, exist_ok=True)
    brut = os.path.join(BRUT_DIR, f"{int(freq)}_prise_{time.strftime('%Y%m%d_%H%M%S')}.wav")
    arreter_brut = prise.enregistrer(brut)
    # Ticket du fil appelant, propage aux fils decodeurs (2026-11-01) : sans lui,
    # leur ceremonie (_annonce_dmr) et leur voix n'atteignaient pas la session du
    # journal (rattachee au ticket) -- seul le son de fin, joue ici, y entrait :
    # prises "fin de reception" seules, sans annonce ni flux.
    ticket = audio_lock.ticket_courant()

    def ecouter(i, mode_flag):
        audio_lock._local.ticket = ticket
        resultats[mode_flag] = try_digital_voice_decode(freq, mode_flag, band_label, target=target, live=True,
                                                        prise=prise, port=DSD_FME_UDP_PORT + 1 + i, arbitre=arbitre)
    fils = {m: threading.Thread(target=ecouter, args=(i, m), daemon=True) for i, m in enumerate(modes)}
    for f in fils.values():
        f.start()
    print(f"[radio_watch] prise unique {freq / 1e6:.4f} MHz : FM + {' '.join(modes)} en meme temps")
    analogique = False
    try:
        analogique = stream_until_quiet(freq, "nfm", band_label, target=target, prise=prise, arbitre=arbitre)
        if not arbitre.numerique_gagne() and arbitre.numerique_en_vue():
            arbitre.attendre_numerique(ATTENTE_NUMERIQUE_SEC)  # un decodeur etait sur le point de se confirmer
        while True:  # le numerique a la parole : il finit de lui-meme au silence
            gagnant = arbitre.gagnant if arbitre.numerique_gagne() else None
            if gagnant and fils[gagnant].is_alive():
                fils[gagnant].join(0.5)
                continue
            if not gagnant and arbitre.cedes and arbitre.attendre_numerique(4.0):
                continue  # "juge suivant" : un autre decodeur reprend la parole
            break
    finally:
        arbitre.termine.set()
        prise.fermer()
        for f in fils.values():
            f.join(timeout=5)
        arreter_brut()
    gagnant = arbitre.gagnant if arbitre.numerique_gagne() else None
    protocol = resultats.get(gagnant) if gagnant else None
    if protocol == "SUIVANT":
        protocol = None
    muet = bool(protocol) and try_digital_voice_decode.last_muted
    _garder_brut(brut, freq, gagnant or "prise", band_label, arbitre.stats.get(gagnant) or {},
                 bool(protocol) and not muet, muet)
    return protocol, bool(analogique) and not gagnant


# --- Demodulation DELEGUEE a une machine compagne (offload 2c-final, 2026-10-31) ---
# Doctrine (utilisateur) : quand le Pi chauffe, il NE demodule plus lui-meme
# certaines fenetres -- il sert son dongle par rtl_tcp et une compagne fait
# TOUTE la demod de la fenetre (numerique ET FM analogique sur UN seul process
# dsd-neo, bridge/numerique_neo en plein demod), ce qui preserve la
# multi-detection d'UNE fenetre au lieu de la fragmenter. lib/demod tranche
# local/distant via lib/offload (gradient thermique) ; le distant n'entre en
# action QUE si le Pi en a besoin. Gracieux : pas de compagne libre / echec ->
# on rend la main a la demod locale.
NUMERIQUE_SONDE_SEC = 14          # sonde de base d'une fenetre deleguee (= son poids pour offload)
NUMERIQUE_SUIVI_MAX_SEC = 90      # plafond tant que de la voix numerique arrive (suivi d'appel)
NUMERIQUE_GAIN = 32               # gain du dongle servi (verrou local Saint-Etienne, cf tetra.GAIN_DEFAUT)


def _juger_raffiner_fm(wav_path, target, freq_hz, mode):
    """Juge ET raffine l'audio FM brut rendu par la compagne, avec la MEME chaine
    que la demod locale : filtre vocal (chaine_audio) + detecteur de voix RNNoise
    (_chunk_is_signal, fenetres de ANALYSIS_WINDOW_MS). Rend (vaut_le_coup,
    pcm_nettoye, taux). Pas de voix reconnue -> vaut_le_coup False : on n'ouvre
    pas sur du bruit / des donnees (fausses captations signalees 2026-10-31,
    doctrine : le Mac rend le brut, le Pi raffine)."""
    import wave as _wave
    try:
        with _wave.open(wav_path) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return False, b"", 48000
            taux = w.getframerate()
            brut = w.readframes(w.getnframes())
    except Exception as exc:
        print(f"[radio_watch] audio FM distant illisible : {exc!r}")
        return False, b"", 48000
    chain = chaine_audio.nouvelle(target, freq_hz, taux, mode)
    rn = _rnnoise_create()
    resample_state = smoothed_prob = None
    pas = max(2, int(taux * 2 * ANALYSIS_WINDOW_MS / 1000))   # octets par fenetre d'analyse
    nettoye = []
    consecutifs = 0
    vu = False
    try:
        for i in range(0, len(brut), pas):
            chunk = brut[i:i + pas]
            if len(chunk) < 2:
                continue
            filtre, propre = chain.process_s16le(chunk)
            nettoye.append(propre)
            is_signal, _, resample_state, smoothed_prob = _chunk_is_signal(
                filtre, taux, rn, resample_state, smoothed_prob)
            consecutifs = consecutifs + 1 if is_signal else 0
            if consecutifs >= MIN_CONSECUTIVE_SIGNAL_WINDOWS:
                vu = True
    finally:
        _rnnoise_destroy(rn)
    return vu, b"".join(nettoye), taux


def _presenter_distant(freq, mode, band_label, target, level_db, log_session, res):
    """Presente une fenetre demodulee a distance : voix NUMERIQUE en clair, ou
    audio FM, ou rien (appris). Meme habillage que la demod locale / _annonce_tetra.
    Rend True si une vraie reception a ete presentee."""
    proto = res.get("protocole")
    machine = res.get("machine")
    meta = res.get("meta") or {}
    voix = res.get("voix_wav") if os.path.exists(res.get("voix_wav") or "") else None
    audio = res.get("audio_wav") if os.path.exists(res.get("audio_wav") or "") else None
    if res.get("numerique_confirme"):
        # Doctrine _reception_a_presenter : pas d'ouverture fantome ni
        # d'enregistrement vide. Clair capte -> ceremonie ; chiffre / repos /
        # signalisation seule -> appris (registre), mais pas de ceremonie.
        if voix and _reception_a_presenter(proto, res.get("chiffre"), True):
            parts = morceaux.whole(dire("radio.reception", protocol=proto) + ("," if band_label else "."))
            if band_label:
                parts += morceaux.whole(band_label + ".")
            morceaux.say(DMR_VOCAL_SOUND, parts, label="radio_watch")
            play_sound_effect(FREQ_OPEN_SOUND, label="radio_watch")
            play_sound_effect(voix, timeout_sec=NUMERIQUE_SUIVI_MAX_SEC, label="radio_watch")
            play_sound_effect(FREQ_OVER_SOUND, label="radio_watch")
            path = log_session.finalize()
            noise_signatures.record_capture("rtl_fm", freq, mode, band_label, target, path, None,
                                            {"niveau_db": level_db, "numerique": True, "offload": machine,
                                             "protocole": proto, "dmr": meta})
            print(f"[radio_watch] demod deleguee {freq / 1e6:.4f} MHz : {res.get('resume')} + voix (via {machine})")
            return True
        log_session.discard()
        noise_signatures.record_capture("rtl_fm", freq, mode, band_label, target, None, None,
                                        {"niveau_db": level_db, "numerique": True, "offload": machine,
                                         "protocole": proto, "chiffre": bool(res.get("chiffre")), "dmr": meta})
        print(f"[radio_watch] demod deleguee {freq / 1e6:.4f} MHz : {res.get('resume')} (appris, pas de clair)")
        return False
    if res.get("digital_actif"):
        # Porteuse NUMERIQUE active (detectee au SIGNAL : synchro soutenue) mais
        # non decodee proprement cette fenetre (auto-detection qui flotte, FEC,
        # pas de Color Code...) : on APPREND, on ne joue JAMAIS la FM -- ce serait
        # le bruit du numerique demodule (bruit blanc signale 2026-10-31).
        log_session.discard()
        print(f"[radio_watch] demod deleguee {freq / 1e6:.4f} MHz : porteuse numerique active (sync {res.get('sync_hits')}), pas de decode clair, pas de FM")
        return False
    if audio:
        # FM analogique : le Mac rend le brut, le Pi RAFFINE + JUGE la voix (meme
        # chaine que le local). Pas de voix reconnue -> ecarte : pas de ceremonie
        # ni de capture (fausses captations de bruit signalees 2026-10-31).
        vaut, propre, taux = _juger_raffiner_fm(audio, target, freq, mode)
        if not vaut:
            log_session.discard()
            print(f"[radio_watch] demod deleguee {freq / 1e6:.4f} MHz : audio FM sans voix reconnue (ecarte)")
            return False
        import wave as _wave
        propre_wav = os.path.join(os.path.dirname(audio), "fm_propre.wav")
        try:
            with _wave.open(propre_wav, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(taux)
                w.writeframes(propre)
        except Exception as exc:
            print(f"[radio_watch] ecriture audio FM raffine : {exc!r}")
            propre_wav = audio   # repli : le brut
        # Ceremonie complete, comme une captation FM locale (carillon INCOMING +
        # annonce de l'origine, puis shim d'ouverture, audio, son de fin).
        speak_with_lead_sound(INCOMING_SOUND, band_label, label="radio_watch", repetitive=True)
        play_sound_effect(FREQ_OPEN_SOUND, label="radio_watch")
        play_sound_effect(propre_wav, timeout_sec=NUMERIQUE_SUIVI_MAX_SEC, label="radio_watch")
        play_sound_effect(_son_fin(freq), label="radio_watch")
        path = log_session.finalize()
        noise_signatures.record_capture("rtl_fm", freq, mode, band_label, target, path, None,
                                        {"niveau_db": level_db, "offload": machine, "analogique": True})
        print(f"[radio_watch] demod deleguee {freq / 1e6:.4f} MHz : voix FM en clair (via {machine})")
        return True
    log_session.discard()
    print(f"[radio_watch] demod deleguee {freq / 1e6:.4f} MHz : rien d'exploitable")
    return False


def _demod_deleguee(freq, mode, band_label, target, level_db, log_session):
    """Intermediaire local/distant. Si le Pi doit deleguer (offload thermique)
    ET qu'une compagne "numerique" est libre : sert rtl_tcp, fait demoduler la
    fenetre a distance (demod.distant, plein demod), la presente, rend True/False
    (reception presentee ?). Rend None s'il NE faut PAS deleguer ou en cas
    d'echec -> l'appelant garde sa demod locale. Ne leve pas."""
    try:
        import demod
    except Exception as exc:
        print(f"[radio_watch] offload indisponible ({exc!r}), demod locale")
        return None
    try:
        plan = demod.plan(float(NUMERIQUE_SONDE_SEC))
    except Exception as exc:
        print(f"[radio_watch] offload : plan impossible ({exc!r}), demod locale")
        return None
    if not plan.get("deleguer"):
        return None
    try:
        import compagnon
        host = compagnon._local_ip()
    except Exception:
        host = None
    # AGNOSTIQUE (doctrine) : -fa DECOUVRE la modulation au signal, on ne la
    # devine PAS depuis le registre. Le registre ne sert qu'a TROUVER le centre
    # exact du signal (observation : "la porteuse est a X Hz", pas "c'est du
    # DMR"), ce qui aide l'auto-detection a verrouiller. Le garde anti-bruit est
    # lui aussi au signal (res["digital_actif"], densite de synchro), pas au
    # registre.
    mode_dsd = "-fa"
    freq_decode = int(freq)
    try:
        import dmr_registre
        best = None
        for e in dmr_registre.load().values():
            fh = e.get("freq_hz")
            if fh and abs(fh - freq) <= dmr_registre.STEP_HZ:
                if best is None or abs(fh - freq) < abs(best - freq):
                    best = int(fh)
        if best is not None:
            freq_decode = best   # centre observe (pas de forcage de mode)
    except Exception:
        pass
    cie = plan.get("compagne") or {}
    nom = cie.get("nom") or cie.get("id") or "compagne"
    print(f"[radio_watch] {freq / 1e6:.4f} MHz : Pi a {plan.get('temp_c')} C -> demod deleguee a {nom} ({mode_dsd})")
    try:
        proc = subprocess.Popen(["rtl_tcp", "-a", "0.0.0.0", "-p", str(TETRA_RTL_TCP_PORT)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"[radio_watch] rtl_tcp (offload) impossible : {exc!r}, demod locale")
        return None
    res = None
    try:
        time.sleep(2.0)   # rtl_tcp pret a servir
        if proc.poll() is not None:
            print("[radio_watch] rtl_tcp (offload) arrete tout de suite (dongle occupe ?), demod locale")
            return None
        res = demod.distant(freq_decode, NUMERIQUE_GAIN, NUMERIQUE_SONDE_SEC, host=host,
                            port=TETRA_RTL_TCP_PORT, max_secondes=NUMERIQUE_SUIVI_MAX_SEC, mode=mode_dsd)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    tmp = res.pop("_tmp", None) if res else None
    try:
        if not res or not res.get("ok"):
            if res and res.get("erreur"):
                print(f"[radio_watch] demod deleguee {freq / 1e6:.4f} MHz : {res['erreur']}, demod locale")
            return None   # echec distant -> repli local (le dongle est de nouveau libre)
        try:
            return _presenter_distant(freq, mode, band_label, target, level_db, log_session, res)
        except Exception as exc:
            # Jamais laisser une erreur de presentation faire tomber le scanner :
            # la fenetre est consommee (on a deja ecoute), on n'y retombe pas en local.
            print(f"[radio_watch] presentation deleguee : {exc!r}")
            try:
                log_session.discard()
            except Exception:
                pass
            return False
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def _handle_candidate(freq, mode, band_label, target=None, level_db=None):
    if ignores.frequence_ignoree(freq):
        print(f"[radio_watch] {freq / 1e6:.4f} MHz ignoree (commande ignorer), on passe")
        return False
    # Verrou audio partage avec adsb_watch.py (processus separe) : garantit
    # qu'aucune annonce d'avion ne chevauche l'annonce/diffusion/son de fin
    # d'un candidat radio. Tenu pendant tout le cycle actif, relache des que
    # termine (y compris en cas d'echec/candidat ecarte).
    lock = audio_lock.acquire()
    # Journal audio (radio_log.py, 2026-08-31, demande explicite) : une
    # session par candidat, activee pour toute la duree du cycle -- tout
    # son joue via play_sound_effect() (habillage + annonce TTS cloud) y
    # est automatiquement copie (voir sound_player.py), le contenu radio
    # capte y est ajoute explicitement dans try_digital_voice_decode()/
    # stream_until_quiet(). Sauvegardee seulement si un vrai signal a ete
    # entendu -- un candidat ecarte comme bruit n'est pas archive.
    log_session = radio_log.RadioLogSession(band_label)
    sound_player.set_recording_session(log_session)
    saved = False
    # Ce qu'on entend maintenant : cible de "verrouille" pendant l'ecoute.
    noise_signatures.capture_started("rtl_fm", freq, mode, band_label, target)
    global _bruts_candidat
    _bruts_candidat = []
    try:
        # Intermediaire offload (2c-final) : si le Pi chauffe et qu'une compagne
        # est libre, TOUTE la demod de la fenetre part a distance (numerique + FM
        # sur un seul flux) ; sinon None -> demod locale habituelle ci-dessous.
        if mode == "nfm":
            presente = _demod_deleguee(freq, mode, band_label, target, level_db, log_session)
            if presente is not None:
                saved = bool(presente)
                return True
        # Prise unique (2026-10-25) : FM et decodeurs numeriques sur le meme flux,
        # en meme temps ; sinon (interrupteur, echec) l'ancien enchainement.
        prise = _ouvrir_prise(freq) if mode == "nfm" and prise_unique.active() else None
        if prise is not None:
            try_digital_voice_decode.last_muted = False
            protocol, entendu_fm = _ecoute_prise(prise, freq, band_label, target)
        else:
            protocol = _numerique_live(freq, band_label, target) if mode == "nfm" else None
        if protocol and not _reception_a_presenter(protocol, try_digital_voice_decode.last_muted,
                                                    try_digital_voice_decode.last_had_audio):
            # chiffre, ou synchro validee sans clair capte : appris (registre),
            # pas d'habillage ni d'enregistrement vide (fantome 2026-10-28)
            log_session.discard()
            return True
        if protocol:
            play_sound_effect(FREQ_OVER_SOUND, label="radio_watch")
            morceaux.say(None, morceaux.whole(dire("radio.reception_terminee", protocol=protocol)), label="radio_watch")
            path = log_session.finalize()
            noise_signatures.record_capture("rtl_fm", freq, mode, band_label, target, path, None,
                                            {"niveau_db": level_db, "numerique": True,
                                             "dmr": getattr(try_digital_voice_decode, "last_dmr", {})})
            saved = True
            return True
        heard = entendu_fm if prise is not None else stream_until_quiet(freq, mode, band_label, target=target)
        if heard:
            _etiqueter_bruts("analogique")
            if mode == "nfm":  # canal mixte : la suite de la conversation peut venir en DMR (2026-10-24)
                _noter_mixte(freq)
                canal = _canal_mixte(freq)
                if canal:
                    _suivi_mixte(canal, band_label, target, numerique_d_abord=True)
            play_sound_effect(_son_fin(freq), label="radio_watch")
            path = log_session.finalize()
            _save_raw(path)
            metrics = dict(_last_stream_metrics, niveau_db=level_db)
            try:  # relais connu : entendu, et confirme si sa tonalite y est (2026-10-23)
                verdict = relais.note_capture(freq, metrics.get("ctcss_hz"))
                if verdict:
                    metrics["relais"] = verdict
                    print(f"[radio_watch] relais {freq / 1e6:.4f} MHz : {verdict}"
                          + (f" (tonalite {metrics['ctcss_hz']} Hz)" if metrics.get("ctcss_hz") else ""))
                elif metrics.get("ctcss_hz"):
                    print(f"[radio_watch] tonalite {metrics['ctcss_hz']} Hz sur {freq / 1e6:.4f} MHz")
            except Exception as exc:
                print(f"[radio_watch] audibilite du relais : {exc!r}")
            if freq < HF_ANALYSIS_MAX_HZ:
                # CB, 10 m : vraie emission ou artefact de la cle saturee ?
                # (largeur du signal, baisse avec le gain ; lib/stations.py)
                try:
                    result = stations.analyse(freq)
                    if result:
                        metrics["analyse"] = result
                        stations.note_analysis(freq, mode, result)
                        print(f"[radio_watch] analyse {freq / 1e6:.4f} MHz : {result}")
                except Exception as exc:
                    print(f"[radio_watch] analyse impossible : {exc!r}")
            noise_signatures.record_capture("rtl_fm", freq, mode, band_label, target, path,
                                            _last_stream_fingerprint, metrics)
            saved = True
            return True
        # Au lieu de jeter l'audio : on le garde comme extrait de rejet
        # (journal des rejets, lib/rejets.py) pour un tri humain a l'oreille
        # et le recalibrage. Si c'etait en fait du numerique (extra ci-dessous),
        # on efface cet extrait : ce n'etait pas un rejet.
        reject_path = log_session.finalize(subdir="rejets")
        for extra in (decodeurs.extra_modes(freq) if mode == "nfm" and prise is None else []):
            # Ecarte comme voix : peut-etre un mode numerique que -fa ne tente pas
            # (avec la prise unique, ils ont deja ecoute en meme temps que la FM).
            log_session = radio_log.RadioLogSession(band_label)
            sound_player.set_recording_session(log_session)
            if try_digital_voice_decode(freq, extra[0], band_label, target=target) \
                    and _reception_a_presenter(extra[1], try_digital_voice_decode.last_muted,
                                               try_digital_voice_decode.last_had_audio):
                play_sound_effect(FREQ_OVER_SOUND, label="radio_watch")
                speak(dire("radio.reception_terminee_2", v1=extra[1]))
                path = log_session.finalize()
                noise_signatures.record_capture("rtl_fm", freq, mode, band_label, target, path, None,
                                                {"niveau_db": level_db, "numerique": True, "mode_numerique": extra[1]})
                if reject_path:
                    try:
                        os.remove(reject_path)
                    except OSError:
                        pass
                saved = True
                return True
            log_session.discard()
        print("[radio_watch] candidat ecarte (bruit, pas de signal reel)")
        # Autodecouverte TETRA (2026-10-26) : un pic dans une bande TETRA que
        # AUCUN decodeur audio/numerique n'a su lire est peut-etre une porteuse
        # TETRA (pi/4-DQPSK, inaudible en FM). On la note comme piste a sonder
        # par la fenetre TETRA (la machine compagne tranchera), sans chauffer le
        # Pi ici. Meme logique de decouverte que le reste du scanner.
        if mode == "nfm" and tetra.active() and tetra.dans_bande(freq):
            try:
                if tetra.decouvrir(freq):
                    print(f"[radio_watch] {freq / 1e6:.4f} MHz : piste TETRA notee (fenetre TETRA)")
            except Exception as exc:
                print(f"[radio_watch] piste TETRA non notee : {exc!r}")
        _etiqueter_bruts("rejet")
        if reject_path:  # extrait garde pour le journal des rejets
            try:
                import rejets
                rejets.record(reject_path, freq, band_label, "bruit", _last_stream_fingerprint)
            except Exception as exc:
                print(f"[radio_watch] rejet non journalise : {exc!r}")
        # Un candidat ecarte EST un echantillon du bruit de fond de sa bande.
        noise_signatures.add_background(target, freq, _last_stream_fingerprint)
        try:  # et son empreinte de bruit, pour le nettoyage doux
            profils_bruit.learn(target, freq, *_last_stream_filtered)
        except Exception as exc:
            print(f"[radio_watch] empreinte de bruit non apprise : {exc!r}")
        return False
    finally:
        _etiqueter_bruts()
        _bruts_candidat = None
        noise_signatures.capture_ended()
        sound_player.set_recording_session(None)
        audio_lock.release(lock)
        if saved and lock.silent:
            # Journal ouvert : capte en silence, une phrase courte previent.
            journal_notice()


def _centered(freq, mode):
    """Recentrage sur une station probable proche (lib/stations.py) ; jamais bloquant."""
    try:
        return stations.center(freq, mode)
    except Exception as exc:
        print(f"[radio_watch] recentrage impossible : {exc!r}")
        return freq


def _recaler(freq, mode):
    """Recentrage du pic sur une porteuse connue. Toujours : stations
    probables (lib/stations). En mode pics (Close-Call) seulement, si les
    stations n'ont rien recale, on croise aussi le pic avec les porteuses
    DMR puis TETRA apprises -- le savoir n'oriente pas la cible, il affine
    la prise une fois le pic deja detecte par le balayage (doctrine)."""
    base = _centered(freq, mode)
    if base != freq:
        return base   # une station probable a deja recale : plus specifique
    try:
        import tempo
        if tempo.courant() != "pics":
            return base
    except Exception:
        return base
    try:
        import dmr_registre
        e = dmr_registre.load().get(dmr_registre.key(freq))
        if e and e.get("freq_hz") and abs(e["freq_hz"] - freq) <= dmr_registre.STEP_HZ:
            print(f"[radio_watch] pic recale sur relais DMR connu {e['freq_hz'] / 1e6:.4f} MHz")
            return int(e["freq_hz"])
    except Exception as exc:
        print(f"[radio_watch] recentrage DMR impossible : {exc!r}")
    try:
        import tetra
        if tetra.est_tetra(freq):
            c = tetra.centre(freq)
            if c and int(c) != int(freq):
                print(f"[radio_watch] pic recale sur porteuse TETRA connue {c / 1e6:.4f} MHz")
                return int(c)
    except Exception as exc:
        print(f"[radio_watch] recentrage TETRA impossible : {exc!r}")
    return base




def _station_name(freq, mode):
    """Nom donne a la station probable proche (F3 du mode manuel), ou None."""
    try:
        return stations.name_near(freq, mode)
    except Exception as exc:
        print(f"[radio_watch] nom de station illisible : {exc!r}")
        return None


def _station_capture(freq, mode):
    try:
        stations.record_capture(freq, mode)
    except Exception as exc:
        print(f"[radio_watch] station probable non mise a jour : {exc!r}")


def _settings(target):
    """Reglages audio de la bande (lib/reglages_audio.py, ajustes par le bilan
    des notes), lus par la chaine commune (lib/chaine_audio.py)."""
    return chaine_audio.settings(target)


def scan_fm_band(band):
    margin_db = band.get("margin_db", NOISE_MARGIN_DB) + _settings(band["name"])["marge_detection_db"]
    span_hz = band.get("span_hz", 25_000)
    candidates = check_channels_relative(band["channels"], span_hz=span_hz, margin_db=margin_db)
    if band["name"].startswith("relais_"):
        relais.note_ecoutes(band["channels"])  # audibilite des relais appris (commande "relais")
    for freq, db in candidates:
        print(f"[radio_watch] candidat sur {band['name']} a {freq/1e6:.5f} MHz (niveau brut {db:.1f} dB, depasse le plancher local de {margin_db} dB min.)")
        if band["name"].startswith("stations_"):
            freq = _recaler(freq, band["mode"])
        label = _station_name(freq, band["mode"]) or f"canal {band['name_spoken']}"
        if _handle_candidate(freq, band["mode"], label, band["name"], level_db=db):
            _station_capture(freq, band["mode"])
            return True
    return False


def scan_sweep_band(band):
    margin_db = band.get("margin_db", NOISE_MARGIN_DB) + _settings(band["name"])["marge_detection_db"]
    # Tous les pics de la passe, pas seulement le plus fort : une bande large
    # (milair 225-400, FM...) etait sinon reduite a son unique pic dominant, le
    # reste jamais explore (doctrine : balayer toute la bande). On ouvre sur le
    # premier pic retenu par _handle_candidate ; un pic de bruit ecarte ne fait
    # plus abandonner toute la bande, on passe au suivant.
    peaks = sweep_band_peaks_relative(band["low_hz"], band["high_hz"], band["step_hz"], margin_db=margin_db)
    for freq, db in peaks:
        if _lock_pending():   # la console demande la main : on ne finit pas la liste
            break
        print(f"[radio_watch] candidat sur {band['name']} a {freq/1e6:.4f} MHz (niveau brut {db:.1f} dB, depasse le plancher local de {margin_db} dB min.)")
        # Mode diagnostic spectral (2026-08-31, demande explicite) : ne fait
        # rien (cout quasi nul) sauf si arme sur CETTE bande via
        # "spectral_diagnostic.py arm <bande>". Si arme, affine
        # immediatement sur ce meme candidat avant la vraie capture --
        # retarde son demarrage de ~1-2s tant que le mode reste arme,
        # accepte car opt-in et sert justement a etudier la bande armee.
        spectral_diagnostic.maybe_diagnose(band, freq, db)
        # Pres d'une station probable : on se cale sur son vrai centre
        # (plus d'ecoute en bord de canal, demande utilisateur 2026-10-02).
        freq = _recaler(freq, band["mode"])
        label = _station_name(freq, band["mode"]) or f"bande {band['name_spoken']}"
        if _handle_candidate(freq, band["mode"], label, band["name"], level_db=db):
            _station_capture(freq, band["mode"])
            return True
    return False


def _scan_locked(capture):
    """Une iteration d'ecoute verrouillee sur la frequence de la capture
    demandee par la console ("accroche"). Meme chaine que le balayage
    normal (squelch reel, annonce, diffusion, journal), sur une seule
    frequence. Retourne True si une transmission a ete relayee."""
    if capture.get("source") == "dmr" or capture.get("target") == "dmr":
        return _dmr_ecoute(capture["freq_hz"], dwell_sec=20)
    if capture["source"] == "airband":
        window = next((w for w in airband_bridge.AIRBAND_WINDOWS if w["name"] == capture["target"]), None)
        port = next((pt for f, pt in window["channels"] if f == capture["freq_hz"]), None) if window else None
        if port is None:
            return False
        return airband_bridge.scan_window(dict(window, channels=[(capture["freq_hz"], port)]), dwell_sec=20)
    band = next((b for b in BANDS if b["name"] == capture.get("target")), {})
    candidates = check_channels_relative([capture["freq_hz"]], span_hz=band.get("span_hz", 25_000),
                                         margin_db=band.get("margin_db", NOISE_MARGIN_DB))
    if candidates:
        return _handle_candidate(capture["freq_hz"], capture["mode"], capture["label"], capture.get("target"))
    time.sleep(1)
    return False


def _lock_pending():
    import capteurs_miens  # appairage d'un capteur demande par la console : la fenetre en cours s'arrete
    return noise_signatures.lock_request() is not None or noise_signatures.manual_request() is not None \
        or capteurs_miens.requested() is not None


def _publish_plages():
    """Liste des plages pour le mode manuel de la console : les bandes de
    ce fichier et les fenetres RTLSDR-Airband, sans liste en double."""
    plages = []
    for b in BANDS:
        sweep = b["type"] == "sweep"
        plages.append({
            "source": "rtl_fm", "target": b["name"], "mode": b["mode"],
            "label": f"{'bande' if sweep else 'canal'} {b['name_spoken']}",
            "low_hz": b.get("low_hz"), "high_hz": b.get("high_hz"), "step_hz": b.get("step_hz"),
            "channels": [] if sweep else list(b["channels"]),
        })
    # Fenetres RTLSDR-Airband : celles a plusieurs canaux (PMR446...) sont de
    # vraies plages ; celles a UN seul canal sont en fait des stations connues
    # (tours, ATIS, sol, approche d'aeroport) -- on les regroupe en une seule
    # plage "stations aeroport" plutot que d'encombrer la liste (2026-10-08,
    # demande utilisateur : "ce ne sont pas des plages mais des stations").
    aero = []
    for w in airband_bridge.AIRBAND_WINDOWS:
        if len(w["channels"]) > 1:
            plages.append({
                "source": "airband", "target": w["name"], "mode": w["mode"], "label": w["name_spoken"],
                "low_hz": None, "high_hz": None, "step_hz": None,
                "channels": [f for f, _ in w["channels"]],
            })
        else:
            f = w["channels"][0][0]
            aero.append({"freq_hz": f, "mode": w["mode"], "label": w["name_spoken"],
                         "bande": w["name"], "source": "airband"})
    if aero:
        plages.append({
            "source": "airband", "target": "stations_aero", "mode": aero[0]["mode"],
            "label": dire("manuel.plage_aeroport"), "low_hz": None, "high_hz": None, "step_hz": None,
            "channels": [s["freq_hz"] for s in aero], "stations": aero,
        })
    noise_signatures.write_plages(plages)
    # Cibles du mode restreint (lib/restreint.py) : tout ce que le cycle visite.
    try:
        cibles = [("stations", dire("restreint.stations"), "stations"), ("relais", dire("restreint.relais"), "relais")]
        cibles += [(b["name"], f"{'bande' if b['type'] == 'sweep' else 'canal'} {b['name_spoken']}", "bande")
                   for b in BANDS]
        cibles += [(w["name"], w["name_spoken"], "fenetre") for w in airband_bridge.AIRBAND_WINDOWS]
        cibles += [(n, attention.DATA_LABELS.get(n, n), "donnees") for n in attention.DATA_WINDOWS]
        restreint.publish(cibles)
    except Exception as exc:
        print(f"[radio_watch] cibles du mode restreint non publiees : {exc!r}")


def _get_cpu_temp_c():
    """Temperature CPU du Pi via vcgencmd (integre a Raspberry Pi OS).
    Retourne None si indisponible plutot que de lever une exception --
    la securite thermique se contente alors du cycle marche/pause
    regulier."""
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_temp"], capture_output=True, timeout=5, text=True,
        )
        # format attendu : "temp=74.5'C"
        value = result.stdout.strip().split("=")[1].rstrip("'C")
        return float(value)
    except Exception:
        return None


def _get_cpu_load_1min():
    """Charge moyenne 1 minute (os.getloadavg(), integre a Python/Linux,
    pas de dependance externe). Sert a distinguer une chauffe due a un
    calcul intense en cours (dsd-fme, decode numerique...) d'une chauffe
    due a l'environnement (chaleur ambiante) -- voir CPU_LOAD_WATCH_THRESHOLD.
    Retourne None si indisponible."""
    try:
        return os.getloadavg()[0]
    except Exception:
        return None


def _fetch_outdoor_temp_c():
    """Temperature exterieure actuelle (Open-Meteo, meme source que
    meteo_watch.py, gratuite/sans cle). Retourne None en cas d'echec --
    l'appelant retombe alors sur un bonus environnemental neutre."""
    try:
        resp = requests.get(
            OUTDOOR_WEATHER_URL,
            params={
                "latitude": OUTDOOR_REF_LAT,
                "longitude": OUTDOOR_REF_LON,
                "current": "temperature_2m",
                "timezone": "Europe/Paris",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("current", {}).get("temperature_2m")
    except Exception as exc:
        print(f"[radio_watch] erreur API temperature exterieure : {exc}")
        return None


def _is_night_now():
    hour = time.localtime().tm_hour
    if NIGHT_START_HOUR > NIGHT_END_HOUR:
        return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR
    return NIGHT_START_HOUR <= hour < NIGHT_END_HOUR


def _outdoor_temp_bonus_c(outdoor_temp):
    if outdoor_temp is None:
        return 0.0
    for threshold, bonus in OUTDOOR_TEMP_BONUS_TIERS:
        if outdoor_temp < threshold:
            return bonus
    return 0.0


def _compute_env_bonus_c(outdoor_temp):
    """Bonus (ou malus) a ajouter au seuil de coupure de base, combinant
    heure (nuit = plus de marge, la temperature ne peut que descendre) et
    temperature exterieure actuelle. Toujours ecrete a ENV_BONUS_MAX_C --
    le garde-fou TEMP_CRITICAL_C - 2.0 protege quoi qu'il arrive."""
    bonus = _outdoor_temp_bonus_c(outdoor_temp)
    if _is_night_now():
        bonus += NIGHT_BONUS_C
    return max(-ENV_BONUS_MAX_C, min(bonus, ENV_BONUS_MAX_C))


def _speak_pause(message, sound=None):
    lock = audio_lock.acquire()
    try:
        if sound:
            speak_with_lead_sound(sound, message, label="radio_watch")
        else:
            speak(message)
    finally:
        audio_lock.release(lock)


def main():
    print("[radio_watch] demarrage de la veille radio")
    threading.Thread(target=_window_watchdog, daemon=True).start()
    airband_bridge.SPEECH_DETECTOR_FACTORY = _speech_detector  # pre-ecoute du 446 et de l'aviation
    radio_log.start_purge_scheduler()
    _publish_plages()
    manual_stream = ManualStream()  # touche F = RTLSDR-Airband, voir manual_tuner.py
    _manual["stream"] = manual_stream  # aussi pendant une pause thermique (_manual_during_pause)
    manual_pending = (None, 0.0)  # (frequence, mode) demandee, et depuis quand
    fm_bands = [b for b in BANDS if b["type"] == "fm_channels"]
    sweep_bands = [b for b in BANDS if b["type"] == "sweep"]
    sweep_idx = 0
    device_down_announced = False

    cycle_start = time.time()
    last_temp_check = 0.0

    # Historique glissant des pauses thermiques -- voir commentaire sur
    # PAUSE_HISTORY_LEN plus haut. pause_active_durations garde la duree de
    # chaque periode active (entre la fin d'une pause et le debut de la
    # suivante) ; sert a detecter un resserrement (les pauses se rapprochent)
    # et a etirer resume_adaptive_offset_c en reponse. pause_durations garde
    # la duree de chaque pause elle-meme (temps de refroidissement reel) ;
    # sert a relever pause_adaptive_offset_c quand le refroidissement est
    # rapide (bonne marge thermique -- voir TEMP_PAUSE_ADAPTIVE_MAX_C).
    pause_active_durations = collections.deque(maxlen=PAUSE_HISTORY_LEN)
    pause_durations = collections.deque(maxlen=PAUSE_HISTORY_LEN)
    last_pause_end_time = None
    resume_adaptive_offset_c = 0.0
    pause_adaptive_offset_c = 0.0

    # Zone de surveillance (voir CPU_LOAD_WATCH_THRESHOLD) : quand le seuil
    # de coupure adaptatif est atteint mais que le CPU n'est pas charge par
    # autre chose, reste actif mais verifie beaucoup plus souvent au lieu de
    # couper franchement -- seul TEMP_CRITICAL_C force alors une coupure.
    in_watch_zone = False

    # Contexte environnemental (voir _compute_env_bonus_c) -- sonde exterieur
    # peu frequente (30 min), bonus recalcule a chaque verification thermique
    # (l'heure, elle, change en continu sans appel reseau).
    last_outdoor_poll = 0.0
    outdoor_temp_c = None

    current_lock_key = None  # (frequence, bande/fenetre) du verrouillage en cours

    while True:
        now = time.time()
        check_interval = WATCH_ZONE_CHECK_INTERVAL_SEC if in_watch_zone else TEMP_CHECK_INTERVAL_SEC

        try:  # resume des connexions DMR du jour, range au journal a 23 h 50
            import dmr_registre
            dmr_registre.daily_tick()
        except Exception as exc:
            print(f"[radio_watch] resume DMR : {exc!r}")

        if now - last_outdoor_poll >= OUTDOOR_TEMP_POLL_INTERVAL_SEC:
            last_outdoor_poll = now
            outdoor_temp_c = _fetch_outdoor_temp_c()

        # Securite thermique -- verifiee AVANT la pause programmee, peut se
        # declencher meme en pleine phase active si ca chauffe trop.
        if now - last_temp_check >= check_interval:
            last_temp_check = now
            temp = _get_cpu_temp_c()
            env_bonus_c = _compute_env_bonus_c(outdoor_temp_c)
            effective_pause_c = min(
                TEMP_PAUSE_C_BASE + pause_adaptive_offset_c + env_bonus_c,
                TEMP_CRITICAL_C - 2.0,
            )

            should_pause = False
            if temp is not None and temp >= TEMP_CRITICAL_C:
                # Plafond dur : coupure immediate, aucune exception possible
                # (ni marge adaptative, ni zone de surveillance).
                should_pause = True
            elif temp is not None and temp >= effective_pause_c:
                load = _get_cpu_load_1min()
                if load is not None and load < CPU_LOAD_WATCH_THRESHOLD:
                    if not in_watch_zone:
                        in_watch_zone = True
                        print(f"[radio_watch] zone de surveillance thermique : {temp:.1f}C >= "
                              f"{effective_pause_c:.1f}C mais charge CPU basse ({load:.2f}) -- "
                              f"pas de coupure, verification toutes les {WATCH_ZONE_CHECK_INTERVAL_SEC}s")
                    # reste actif, pas de coupure tant que la charge reste basse
                else:
                    should_pause = True
            elif in_watch_zone:
                in_watch_zone = False
                print(f"[radio_watch] sortie de la zone de surveillance thermique "
                      f"(temperature redescendue sous {effective_pause_c:.1f}C)")

            if should_pause:
                in_watch_zone = False
                pause_start_time = now
                if last_pause_end_time is not None:
                    active_duration = pause_start_time - last_pause_end_time
                    if pause_active_durations:
                        avg_previous = sum(pause_active_durations) / len(pause_active_durations)
                        if active_duration < avg_previous * 0.8:
                            resume_adaptive_offset_c = min(
                                resume_adaptive_offset_c + TEMP_RESUME_ADAPTIVE_STEP_C,
                                TEMP_RESUME_ADAPTIVE_MAX_C,
                            )
                            print(f"[radio_watch] resserrement des pauses thermiques detecte "
                                  f"(actif {active_duration:.0f}s contre {avg_previous:.0f}s en moyenne) -- "
                                  f"marge de refroidissement etiree a {TEMP_RESUME_C - resume_adaptive_offset_c:.1f}C")
                        elif active_duration > avg_previous * 1.2 and resume_adaptive_offset_c > 0:
                            resume_adaptive_offset_c = max(resume_adaptive_offset_c - TEMP_RESUME_ADAPTIVE_STEP_C, 0.0)
                            print(f"[radio_watch] intervalle actif redevenu normal -- "
                                  f"marge relachee a {TEMP_RESUME_C - resume_adaptive_offset_c:.1f}C")
                    pause_active_durations.append(active_duration)

                effective_resume_c = TEMP_RESUME_C - resume_adaptive_offset_c
                print(f"[radio_watch] pause thermique : {temp:.1f}C >= {effective_pause_c:.1f}C "
                      f"(base {TEMP_PAUSE_C_BASE:.1f}C + adaptatif {pause_adaptive_offset_c:+.1f}C "
                      f"+ environnement {env_bonus_c:+.1f}C [exterieur "
                      f"{outdoor_temp_c if outdoor_temp_c is not None else '?'}C, "
                      f"{'nuit' if _is_night_now() else 'jour'}]) "
                      f"(reprise prevue sous {effective_resume_c:.1f}C)")
                manual_stream.stop()  # la securite thermique coupe aussi l'ecoute manuelle
                _speak_pause(
                    dire("radio.pause_scanner_radio_temperature", temp=format(temp, ".0f")),
                    sound=sons.chemin("pause_thermique", SCANER_ERROR_SOUND),  # son propre (2026-10-23)
                )
                meteo_locale.set_scanner_state("pause_thermique")
                _meteo_pause["announced"] = False
                pause_actual_start = time.time()
                recent_temps = collections.deque()  # (horodatage, temperature) sur PLATEAU_WINDOW_SEC
                plateau_resume = False
                plateau_warned = False
                while True:
                    _thermal_pause_wait(TEMP_CHECK_INTERVAL_SEC)
                    temp = _get_cpu_temp_c()
                    if temp is None or temp <= effective_resume_c:
                        break
                    now = time.time()
                    recent_temps.append((now, temp))
                    while recent_temps and now - recent_temps[0][0] > PLATEAU_WINDOW_SEC:
                        recent_temps.popleft()
                    if now - pause_actual_start >= MAX_PAUSE_SEC and temp <= PLATEAU_RESUME_MAX_C:
                        plateau_resume = True
                        print(f"[radio_watch] pause prolongee ({(now - pause_actual_start) / 60:.0f} min) et "
                              f"temperature tiede ({temp:.1f}C <= {PLATEAU_RESUME_MAX_C:.0f}C) -- reprise")
                        break
                    window_full = now - pause_actual_start >= PLATEAU_WINDOW_SEC
                    values = [t for _, t in recent_temps]
                    if window_full and max(values) - min(values) <= PLATEAU_TOLERANCE_C:
                        if temp <= PLATEAU_RESUME_MAX_C:
                            plateau_resume = True
                            print(f"[radio_watch] plateau thermique a {temp:.1f}C depuis "
                                  f"{PLATEAU_WINDOW_SEC // 60} min (<= {PLATEAU_RESUME_MAX_C:.0f}C) -- reprise")
                            break
                        if not plateau_warned:
                            plateau_warned = True
                            print(f"[radio_watch] plateau thermique a {temp:.1f}C, au-dessus de "
                                  f"{PLATEAU_RESUME_MAX_C:.0f}C -- pause maintenue")
                            _speak_pause(dire("radio.pi_refroidit_plus_reste", temp=format(temp, ".0f")))
                last_pause_end_time = time.time()
                play_sound_effect(sons.chemin("scanner_reprise", FREQ_OVER_SOUND), label="radio_watch")  # reprise
                pause_duration = last_pause_end_time - pause_actual_start

                if pause_durations:
                    avg_pause_duration = sum(pause_durations) / len(pause_durations)
                    if pause_duration < avg_pause_duration * 0.8:
                        pause_adaptive_offset_c = min(
                            pause_adaptive_offset_c + TEMP_PAUSE_ADAPTIVE_STEP_C,
                            TEMP_PAUSE_ADAPTIVE_MAX_C,
                        )
                        print(f"[radio_watch] refroidissement rapide constate "
                              f"({pause_duration:.0f}s contre {avg_pause_duration:.0f}s en moyenne) -- "
                              f"seuil de coupure releve a {TEMP_PAUSE_C_BASE + pause_adaptive_offset_c:.1f}C")
                    elif pause_duration > avg_pause_duration * 1.2 and pause_adaptive_offset_c > 0:
                        pause_adaptive_offset_c = max(pause_adaptive_offset_c - TEMP_PAUSE_ADAPTIVE_STEP_C, 0.0)
                        print(f"[radio_watch] refroidissement redevenu normal -- "
                              f"seuil de coupure relache a {TEMP_PAUSE_C_BASE + pause_adaptive_offset_c:.1f}C")
                pause_durations.append(pause_duration)

                print(f"[radio_watch] reprise apres refroidissement (temperature : {temp})")
                if plateau_resume:
                    _speak_pause(dire("radio.pi_refroidit_plus_scanner", temp=format(temp, ".0f")))
                else:
                    _speak_pause(dire("radio.scanner_radio_reprend_temperature"))
                meteo_locale.set_scanner_state("actif")
                cycle_start = time.time()
                continue

        # Verrouillage (commandes console "accroche"/"decroche", 2026-09-29) :
        # apres la securite thermique (toujours prioritaire), avant la pause
        # programmee (suspendue tant que l'utilisateur ecoute une frequence).
        # Mode manuel (console, 2026-09-29) : ecoute en direct sans
        # silencieux de la frequence choisie au clavier. La console annonce
        # elle-meme les frequences : rien a dire ici.
        if dmr_filtres.search_requested():  # exploration dmr, touche R
            manual_stream.stop()
            meteo_locale.set_scanner_state("recherche_dmr")
            current_lock_key = None
            try:
                _dmr_search()
            except Exception as exc:
                print(f"[radio_watch] recherche DMR : {exc!r}")
                dmr_filtres.search_done()
            manual_pending = (None, 0.0)
            continue
        if mesure_antenne.requested():  # commande "antenne" : la cle est a elle le temps de la mesure
            manual_stream.stop()
            current_lock_key = None
            _antenne_if_requested()
            manual_pending = (None, 0.0)
            continue
        manual = noise_signatures.manual_request()
        if manual:
            meteo_locale.set_scanner_state("manuel")
            if current_lock_key != "manuel":
                print("[radio_watch] mode manuel")
                current_lock_key = "manuel"
            # Porteuse TETRA accordee a la main (2026-10-26) : on arrete le
            # recepteur FM (pas de rtl_fm en meme temps que rtl_tcp) et la
            # machine compagne decode en direct -- meme fonctionnement que le
            # balayage, mais a la demande. Les fleches vers une autre frequence
            # reprennent la chaine normale.
            if tetra.active() and tetra.est_tetra(manual["freq_hz"]):
                manual_stream.stop()
                _tetra_manuel(manual)
                manual_pending = (None, 0.0)
                time.sleep(0.1)
                continue
            # Defilement rapide aux fleches : on attend que la frequence ne
            # bouge plus depuis MANUAL_SETTLE_SEC avant de relancer rtl_fm.
            wanted = (manual["freq_hz"], manual["mode"], manual.get("filter", False), manual.get("target"),
                      manual.get("numerique", False))
            if wanted != manual_pending[0]:
                manual_pending = (wanted, time.time())
            elif time.time() - manual_pending[1] >= MANUAL_SETTLE_SEC:
                manual_stream.tune(*wanted)
            time.sleep(0.1)
            continue
        manual_stream.stop()

        # Appairage d'un capteur (console "nouveau capteur", 2026-10-07) : la
        # radio des capteurs ecoute 433 et 868 MHz jusqu'a la fin de la demande.
        import capteurs_miens
        if capteurs_miens.requested():
            meteo_locale.set_scanner_state("appairage")
            current_lock_key = "appairage"
            _window.update(nom="appairage", fin=capteurs_miens.requested() + 30)
            try:
                capteurs_watch.pairing_session()
            except Exception:
                import traceback
                traceback.print_exc()
                capteurs_miens.stop()
            finally:
                _window.update(nom=None, fin=0.0)
            continue

        lock_capture = noise_signatures.lock_request()
        lock_key = (lock_capture["freq_hz"], lock_capture.get("target")) if lock_capture else None
        if lock_key != current_lock_key:
            if lock_capture and current_lock_key == "manuel":
                # La console vient de le dire en fermant le mode manuel.
                print(f"[radio_watch] fin du mode manuel, verrouillage sur {lock_capture['freq_hz']/1e6:.4f} MHz")
            elif lock_capture:
                print(f"[radio_watch] verrouillage sur {lock_capture['freq_hz']/1e6:.4f} MHz ({lock_capture['label']})")
                _speak_pause(dire("radio.scanner_verrouille", label=lock_capture['label']))
            else:
                print("[radio_watch] fin du verrouillage, reprise du balayage")
                _speak_pause(dire("radio.balayage_radio_repris"))
                cycle_start = time.time()
            current_lock_key = lock_key
        if lock_capture:
            meteo_locale.set_scanner_state("accroche")
            _scan_locked(lock_capture)
            continue
        meteo_locale.set_scanner_state("actif")

        # Pause reguliere programmee -- repos du dongle/antenne meme sans
        # probleme de temperature.
        if now - cycle_start >= SCAN_ON_DURATION_SEC:
            print(f"[radio_watch] pause programmee ({SCAN_OFF_DURATION_SEC}s)")
            _speak_pause(
                dire("radio.pause_programmee_scanner_radio", v1=SCAN_OFF_DURATION_SEC // 60)
            )
            # Un balayage meteo demande par la console passe quand meme.
            meteo_locale.set_scanner_state("pause_programmee")
            pause_end = time.time() + SCAN_OFF_DURATION_SEC
            while time.time() < pause_end:
                if meteo_locale.scan_requested():
                    _meteo_scan_if_requested()
                _antenne_if_requested()
                time.sleep(1)
            meteo_locale.set_scanner_state("actif")
            _speak_pause(dire("radio.scanner_radio_reprend"))
            cycle_start = time.time()
            continue

        activity = False

        # Relais appris par l'APRS (lib/relais.py) ; stations probables a
        # revisiter selon leur interet et leurs habitudes (lib/stations.py).
        try:
            probable = stations.bands()
        except Exception as exc:
            print(f"[radio_watch] stations probables : {exc!r}")
            probable = []
        for band in fm_bands + relais.bands() + probable:
            if _lock_pending():
                break
            import interets
            if interets.disabled(band["name"]) or restreint.exclu(band["name"]):
                continue  # interet "désactivé" ou hors du mode restreint : bande sautee
            if scan_fm_band(band):
                activity = True

        # Programme du cycle donne par le systeme d'attention (lib/attention.py,
        # 2026-10-01) au lieu d'un tourniquet fixe : fenetres RTLSDR-Airband
        # (PMR446, aeroports), ACARS et bandes balayees recoivent plus de temps
        # et de visites la ou "ca pique" (activite captee recente, notes de
        # l'utilisateur, contexte aerien d'adsb_watch), avec un plancher
        # d'exploration pour les bandes balayees. Meme dongle, toujours en
        # sequence -- jamais deux ecoutes en parallele.
        _meteo_scan_if_requested()
        _satellite_pass_if_due()
        try:
            plan = attention.plan_cycle(airband_bridge.AIRBAND_WINDOWS, sweep_bands)
        except Exception as exc:  # l'attention ne doit jamais arreter la veille
            print(f"[radio_watch] attention indisponible ({exc!r}), tourniquet simple")
            plan = ([("airband", w, w["name"], w.get("dwell_sec", 15), 0) for w in airband_bridge.AIRBAND_WINDOWS]
                    + [("data", name, name, dwell, 0) for name, dwell in attention.DATA_WINDOWS.items()]
                    + ([("sweep", sweep_bands[sweep_idx % len(sweep_bands)], "", None, 0)] if sweep_bands else []))
            sweep_idx += 1
        for kind, obj, target, dwell, air_boost in plan:
            if _lock_pending():
                break
            # Programme fait en debut de cycle : un mode restreint ou un interet
            # "désactivé" choisi entre-temps vaut tout de suite (2026-10-17).
            if attention._disabled(target):
                print(f"[radio_watch] {target} saute (mode restreint ou interet désactivé)")
                continue
            _meteo_scan_if_requested()  # la console attend son bulletin : on n'attend pas la fin du cycle
            _antenne_if_requested()
            _satellite_pass_if_due()
            # Une ligne par fenetre (2026-10-07) : retrouver laquelle precede une
            # remise a zero de la cle par le noyau (journalctl -k).
            print(f"[radio_watch] fenetre {kind} {target or obj.get('name', '') if isinstance(obj, dict) else target or obj}")
            if kind == "airband":
                hit = airband_bridge.scan_window(obj, dwell_sec=dwell)
            elif kind == "data":
                hit = DATA_SCANNERS[obj](dwell_sec=dwell)
            else:
                hit = scan_sweep_band(obj)
            if hit:
                activity = True
            if target:
                try:
                    attention.visited(target, hit, air_boost)
                except Exception as exc:
                    print(f"[radio_watch] attention : memorisation impossible ({exc!r})")

        # Chien de garde : avertir vocalement si le dongle semble indisponible
        # depuis un moment, plutot que de rester silencieux sans explication.
        # Verrou audio partage (voir _handle_candidate) : ce bloc tournait
        # jusqu'ici SANS le verrou, seul point du fichier a etre passe entre
        # les mailles -- corrige en session (2026-08-30, audit demande par
        # l'utilisateur sur le chevauchement des annonces inter-services).
        global _rtl_power_failure_count
        if _rtl_power_failure_count == DEVICE_DOWN_WARNING_THRESHOLD and not device_down_announced:
            lock = audio_lock.acquire()
            try:
                speak_with_lead_sound(SCANER_ERROR_SOUND, dire("radio.attention_dongle_radio_semble"), label="radio_watch", repetitive=True)
            finally:
                audio_lock.release(lock)
            device_down_announced = True
        elif _rtl_power_failure_count == 0 and device_down_announced:
            lock = audio_lock.acquire()
            try:
                speak(dire("radio.dongle_radio_nouveau_disponible"))
            finally:
                audio_lock.release(lock)
            device_down_announced = False
        elif device_down_announced and _rtl_power_failure_count % DEVICE_DOWN_REMINDER_EVERY == 0:
            lock = audio_lock.acquire()
            try:
                speak_with_lead_sound(SCANER_ERROR_SOUND, dire("radio.dongle_radio_toujours_indisponible"), label="radio_watch", repetitive=True)
            finally:
                audio_lock.release(lock)

        if not activity:
            time.sleep(0.3)


if __name__ == "__main__":
    main()
