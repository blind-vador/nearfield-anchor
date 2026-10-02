import collections
import math
import re
import signal
import subprocess
import sys
import tempfile
import os
import time
import audioop
import threading
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
import stations
import profils_bruit
import reglages_audio
import relais
import meteo_locale
import adsb_local_watch
import aprs_watch
import balises_watch
import capteurs_watch
import lora_watch
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

# Effets sonores courts (habillage du scanner), fournis par l'utilisateur sur
# le NAS (deja monte localement sur le Pi, pas besoin de copie locale).
# Chemin de base centralise dans sound_paths.py (2026-08-31).
SOUND_EFFECTS_DIR = RADIO_SCANER_DIR
FREQ_OPEN_SOUND = os.path.join(SOUND_EFFECTS_DIR, "freq_open.mp3")
FREQ_OVER_SOUND = os.path.join(SOUND_EFFECTS_DIR, "freq_over.mp3")
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
DSD_FME_IDLE_SEC = 3.0
DSD_FME_MAX_SEC = 60

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


def _dsdfme_stdout_watcher(proc, stats=None):
    """Affiche en continu la sortie texte de dsd-fme (statut de synchro,
    identifiants, etc.) dans les logs -- utile pour comprendre en pratique ce
    qui accroche ou pas, sans avoir a rejouer manuellement. stats (dict) :
    compte les synchros propres ou en erreur et retient le protocole."""
    try:
        for raw_line in proc.stdout:
            text = raw_line.decode(errors="replace").strip()
            if not text:
                continue
            print(f"[radio_watch] dsd-fme: {text}")
            if stats is None:
                continue
            m = re.search(r"Sync: [+-]([\w-]+)", text)
            if m:
                stats["total"] += 1
                if "ERR" not in text:
                    stats["clean"] += 1
                    stats["protocol"] = DSD_FME_PROTOCOLS.get(m.group(1), m.group(1))
    except Exception:
        pass


def _dsdfme_validated(stats):
    return (stats["clean"] >= DSD_FME_MIN_CLEAN_SYNC
            and stats["clean"] >= DSD_FME_MIN_CLEAN_RATIO * stats["total"])


def _extra_digital_mode(freq_hz):
    for low, high, flag, name in DSD_FME_EXTRA_MODES:
        if low <= freq_hz <= high:
            return flag, name
    return None


def try_digital_voice_decode(freq_hz, mode_flag="-fa", band_label=""):
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
    le nom du protocole (vrai), sinon None."""
    if not DSD_FME_AVAILABLE:
        return False
    sock = None
    proc = None
    aplay = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((DSD_FME_UDP_HOST, DSD_FME_UDP_PORT))
        sock.settimeout(0.5)

        proc = subprocess.Popen(
            ["dsd-fme", mode_flag, "-i", f"rtl:0:{int(freq_hz)}:0:0:12:0:2",
             "-o", f"udp:{DSD_FME_UDP_HOST}:{DSD_FME_UDP_PORT}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        stats = {"clean": 0, "total": 0, "protocol": None}
        stdout_thread = threading.Thread(target=_dsdfme_stdout_watcher, args=(proc, stats), daemon=True)
        stdout_thread.start()

        # Sonde silencieuse : on garde le son recu en attente, sans rien jouer,
        # tant que les synchros n'ont pas confirme un vrai decodage.
        pending = []
        channels = 1 if mode_flag in DSD_FME_MONO_MODES else DSD_FME_VOICE_CHANNELS
        validated = False
        captured_chunks = []
        start = time.time()
        deadline = start + DSD_FME_PROBE_SEC
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                data = None
            if data and not validated:
                pending.append(data)
            if not validated and pending and _dsdfme_validated(stats):
                validated = True
                size = len(pending[0])
                if size == DSD_FME_STEREO_DATAGRAM:
                    channels = 2
                elif size == DSD_FME_MONO_DATAGRAM:
                    channels = 1
                protocol = stats["protocol"] or "numérique"
                print(f"[radio_watch] dsd-fme : decodage {protocol} confirme "
                      f"({stats['clean']} synchros propres sur {stats['total']}), {channels} voie(s)")
                # Carillon et annonce seulement maintenant ; le son continue
                # d'arriver dans le tampon de la socket pendant ce temps.
                speak_with_lead_sound(DECODING_NUMERIC_SOUND,
                                      f"Réception {protocol}" + (f", {band_label}" if band_label else "") + ".",
                                      label="radio_watch")
                # Meme encadrement que la voix analogique (2026-10-02) : son
                # d'ouverture avant la voix, son de fin apres (_handle_candidate).
                play_sound_effect(FREQ_OPEN_SOUND, label="radio_watch")
                aplay = subprocess.Popen(
                    ["aplay", "-D", AUDIO_DEVICE, "-r", str(DSD_FME_VOICE_SAMPLE_RATE),
                     "-f", "S16_LE", "-c", str(channels), "-q"],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                data = b"".join(pending)
                pending = []
            if not data or not validated:
                continue
            deadline = min(time.time() + DSD_FME_IDLE_SEC, start + DSD_FME_MAX_SEC)
            captured_chunks.append(data)
            try:
                aplay.stdin.write(audio_lock.scale(data))
            except Exception:
                break

        if not validated:
            if pending:
                print(f"[radio_watch] dsd-fme : son ecarte, synchro non confirmee "
                      f"({stats['clean']} propres sur {stats['total']})")
            return None

        log_session = sound_player.get_recording_session()
        if log_session is not None and captured_chunks:
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


def stream_until_quiet(freq_hz, mode, band_label, sample_rate=22050, target=None):
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

    rtl_fm = _open_rtl_fm(rtl_fm_cmd)

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
            print(f"[radio_watch] parasite connu ignore a {mhz:.4f} MHz (similarite {sim:.2f})")
            try:
                rtl_fm.terminate()
                rtl_fm.wait(timeout=2)
            except Exception:
                rtl_fm.kill()
            return False
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

    settings = _settings(target)
    debounce = settings["attente_fin_s"] or SILENCE_DEBOUNCE_SEC
    quiet_windows_needed = int(debounce * 1000 / ANALYSIS_WINDOW_MS)
    quiet_windows_seen = 0
    consecutive_signal_windows = 0
    total_bytes = 0
    max_bytes = int(sample_rate * 2 * MAX_HOLD_SEC)
    heard_signal = False  # confirmé : au moins MIN_CONSECUTIVE_SIGNAL_WINDOWS de suite
    decoded_messages = []

    rnnoise_state = _rnnoise_create()
    resample_state = None
    smoothed_prob = None
    captured_chunks = []

    # Filtre passe-bande vocal (300-3000Hz, 2026-08-31, demande explicite
    # de l'utilisateur suite a l'analyse spectrale de noise_study/) -- pas
    # applique en WFM (diffusion FM commerciale/pirate : musique/parole a
    # pleine bande, un filtrage vocal etroit degraderait le contenu sans
    # raison, ce n'est pas la bande visee par cette demande).
    voice_filter = (audio_filters.VoiceBandpassFilter(sample_rate, lowpass_hz=settings["passe_bas_hz"])
                    if mode != "wfm" else None)
    # Nettoyage doux (2026-10-02) : empreinte du bruit de cette frequence,
    # gresillements, compresseur leger ; en FM, retrait du pilote a 19 kHz.
    # RNNoise continue de juger le son NON nettoye (detection inchangee), et
    # le son brut est garde a cote pour la touche B du journal.
    if mode == "wfm":
        cleaner = audio_filters.FmPilotFilter(sample_rate)
    else:
        try:
            cleaner = audio_filters.cleaner_for(sample_rate, profils_bruit.get(target, freq_hz, sample_rate), settings)
        except Exception as exc:
            print(f"[radio_watch] nettoyage indisponible : {exc!r}")
            cleaner = None
    raw_chunks, filtered_chunks = [], []

    probs_seen, trigger_prob, early_cut = [], None, False
    start = time.time()
    try:
        while total_bytes < max_bytes and time.time() - start < MAX_HOLD_SEC + 5:
            # Timeout (pas un .get() bloquant sans limite) : une attente
            # bloquante pure sur Condition.wait() ne se reveille QUE quand la
            # file recoit quelque chose, et un signal (SIGTERM, "systemctl
            # restart") ne peut alors pas interrompre l'attente -- ca laissait
            # rtl_fm/aplay/multimon orphelins indefiniment (observe et
            # corrige en session, plusieurs redemarrages bloques). Une
            # reverification periodique laisse le signal s'appliquer.
            if prebuffer:
                chunk = prebuffer.pop(0)
            else:
                try:
                    chunk = audio_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
            if chunk is None:
                break
            if multimon is not None:
                # Signal BRUT, non filtre -- multimon-ng decode des tons
                # numeriques (POCSAG, AFSK1200...) qui peuvent deborder ou
                # etre sensibles a la phase/amplitude alteree par le
                # filtre vocal, mieux vaut ne pas y toucher pour le decode.
                try:
                    multimon.stdin.write(chunk)
                except Exception:
                    pass
            raw_chunks.append(chunk)
            if voice_filter is not None:
                chunk = voice_filter.process_s16le(chunk)
            filtered = chunk
            filtered_chunks.append(filtered)
            if cleaner is not None:
                try:
                    chunk = cleaner.process_s16le(chunk)
                except Exception as exc:
                    print(f"[radio_watch] nettoyage abandonne : {exc!r}")
                    cleaner = None
            try:
                aplay.stdin.write(audio_lock.scale(chunk))
            except Exception:
                # Le detail de l'erreur remonte via le fil aplay_stderr_thread
                # (surveillance continue, voir _aplay_stderr_watcher) -- ne pas
                # relire aplay.stderr ici, ce serait un deuxieme lecteur sur le
                # meme flux et ca entrerait en conflit avec ce fil.
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
            else:
                consecutive_signal_windows = 0
                quiet_windows_seen += 1
                if heard_signal and quiet_windows_seen >= quiet_windows_needed:
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
    _last_stream_metrics = {
        "prob_declenchement": round(trigger_prob, 3) if trigger_prob is not None else None,
        "prob_max": round(max(probs_seen), 3) if probs_seen else None,
        "prob_mediane": round(sorted(probs_seen)[len(probs_seen) // 2], 3) if probs_seen else None,
        "duree_s": round(total_bytes / (sample_rate * 2), 1),
        "coupure_precoce": early_cut,
    }

    if multimon_queue is not None:
        time.sleep(0.2)
        while not multimon_queue.empty():
            decoded_messages.append(multimon_queue.get_nowait())

    meaningful_messages = [m for m in decoded_messages if _is_meaningful_decoded_message(m)]
    ignored_count = len(decoded_messages) - len(meaningful_messages)
    if ignored_count:
        print(f"[radio_watch] {ignored_count} message(s) decode(s) ignore(s) (trop court/bruit)")
    for msg in meaningful_messages:
        print(f"[radio_watch] decode numerique: {msg}")
        speak_with_lead_sound(DECODING_MORSE_SOUND, f"Message numérique décodé : {msg}", label="radio_watch")

    return heard_signal or bool(meaningful_messages)


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
DATA_SCANNERS = {
    "acars": acars_watch.scan_window,
    "vdl2": vdl2_watch.scan_window,
    "aprs": aprs_watch.scan_window,
    "capteurs": capteurs_watch.scan_window,
    "pagers": pagers_watch.scan_window,
    "adsb": adsb_local_watch.scan_window,
    "balises": balises_watch.scan_window,
    "sondes": sondes_watch.scan_window,
    "lora": lora_watch.scan_window,
}


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
    cle pour sa duree (satellites_watch)."""
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


_meteo_pause = {"last_watch": 0.0, "announced": False}


def _thermal_pause_wait(duration):
    """Attente pendant la pause thermique, avec la veille meteo sous chaleur
    (voir METEO_PAUSE_MAX_C) : rien d'autre que les fenetres meteo."""
    end = time.time() + duration
    while time.time() < end:
        limit, level = _meteo_temp_limit()
        allowed = not _too_hot(limit)
        meteo_locale.set_scanner_state("pause_thermique_meteo" if allowed else "pause_thermique")
        wanted = meteo_locale.scan_requested() or level >= 2
        if allowed and wanted:
            if not _meteo_pause["announced"]:
                _meteo_pause["announced"] = True
                _speak_pause(f"Malgré la chaleur, je garde l'écoute météo seule, jusqu'à {limit:.0f} degrés.")
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


def _handle_candidate(freq, mode, band_label, target=None, level_db=None):
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
    try:
        protocol = try_digital_voice_decode(freq, band_label=band_label) if mode == "nfm" else None
        if protocol:
            play_sound_effect(FREQ_OVER_SOUND, label="radio_watch")
            speak(f"Réception {protocol} terminée.")
            path = log_session.finalize()
            noise_signatures.record_capture("rtl_fm", freq, mode, band_label, target, path, None,
                                            {"niveau_db": level_db, "numerique": True})
            saved = True
            return True
        if stream_until_quiet(freq, mode, band_label, target=target):
            play_sound_effect(FREQ_OVER_SOUND, label="radio_watch")
            path = log_session.finalize()
            _save_raw(path)
            metrics = dict(_last_stream_metrics, niveau_db=level_db)
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
        log_session.discard()
        extra = _extra_digital_mode(freq) if mode == "nfm" else None
        if extra:
            # Ecarte comme voix : peut-etre un mode numerique que -fa ne tente pas.
            log_session = radio_log.RadioLogSession(band_label)
            sound_player.set_recording_session(log_session)
            if try_digital_voice_decode(freq, extra[0], band_label):
                play_sound_effect(FREQ_OVER_SOUND, label="radio_watch")
                speak(f"Réception {extra[1]} terminée.")
                path = log_session.finalize()
                noise_signatures.record_capture("rtl_fm", freq, mode, band_label, target, path, None,
                                                {"niveau_db": level_db, "numerique": True, "mode_numerique": extra[1]})
                saved = True
                return True
            log_session.discard()
        print("[radio_watch] candidat ecarte (bruit, pas de signal reel)")
        # Un candidat ecarte EST un echantillon du bruit de fond de sa bande.
        noise_signatures.add_background(target, freq, _last_stream_fingerprint)
        try:  # et son empreinte de bruit, pour le nettoyage doux
            profils_bruit.learn(target, freq, *_last_stream_filtered)
        except Exception as exc:
            print(f"[radio_watch] empreinte de bruit non apprise : {exc!r}")
        return False
    finally:
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


def _station_capture(freq, mode):
    try:
        stations.record_capture(freq, mode)
    except Exception as exc:
        print(f"[radio_watch] station probable non mise a jour : {exc!r}")


def _settings(target):
    """Reglages audio de la bande (lib/reglages_audio.py, ajustes par le bilan des notes)."""
    try:
        return reglages_audio.get(target)
    except Exception as exc:
        print(f"[radio_watch] reglages audio illisibles : {exc!r}")
        return dict(reglages_audio.DEFAULTS)


def scan_fm_band(band):
    margin_db = band.get("margin_db", NOISE_MARGIN_DB) + _settings(band["name"])["marge_detection_db"]
    span_hz = band.get("span_hz", 25_000)
    candidates = check_channels_relative(band["channels"], span_hz=span_hz, margin_db=margin_db)
    for freq, db in candidates:
        print(f"[radio_watch] candidat sur {band['name']} a {freq/1e6:.5f} MHz (niveau brut {db:.1f} dB, depasse le plancher local de {margin_db} dB min.)")
        if band["name"].startswith("stations_"):
            freq = _centered(freq, band["mode"])
        if _handle_candidate(freq, band["mode"], f"canal {band['name_spoken']}", band["name"], level_db=db):
            _station_capture(freq, band["mode"])
            return True
    return False


def scan_sweep_band(band):
    margin_db = band.get("margin_db", NOISE_MARGIN_DB) + _settings(band["name"])["marge_detection_db"]
    freq, db = sweep_band_peak_relative(band["low_hz"], band["high_hz"], band["step_hz"], margin_db=margin_db)
    if freq is not None:
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
        freq = _centered(freq, band["mode"])
        if _handle_candidate(freq, band["mode"], f"bande {band['name_spoken']}", band["name"], level_db=db):
            _station_capture(freq, band["mode"])
            return True
    return False


def _scan_locked(capture):
    """Une iteration d'ecoute verrouillee sur la frequence de la capture
    demandee par la console ("accroche"). Meme chaine que le balayage
    normal (squelch reel, annonce, diffusion, journal), sur une seule
    frequence. Retourne True si une transmission a ete relayee."""
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
    return noise_signatures.lock_request() is not None or noise_signatures.manual_request() is not None


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
    for w in airband_bridge.AIRBAND_WINDOWS:
        plages.append({
            "source": "airband", "target": w["name"], "mode": w["mode"], "label": w["name_spoken"],
            "low_hz": None, "high_hz": None, "step_hz": None,
            "channels": [f for f, _ in w["channels"]],
        })
    noise_signatures.write_plages(plages)


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
    radio_log.start_purge_scheduler()
    _publish_plages()
    manual_stream = ManualStream()  # touche F = RTLSDR-Airband, voir manual_tuner.py
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
                    f"Pause du scanner radio, température élevée : {temp:.0f} degrés.",
                    sound=SCANER_ERROR_SOUND,
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
                            _speak_pause(f"Le Pi ne refroidit plus et reste à {temp:.0f} degrés. "
                                         f"Le scanner radio reste en pause. Vérifie la ventilation.")
                last_pause_end_time = time.time()
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
                    _speak_pause(f"Le Pi ne refroidit plus, le scanner radio reprend à {temp:.0f} degrés.")
                else:
                    _speak_pause("Le scanner radio reprend, température redescendue.")
                meteo_locale.set_scanner_state("actif")
                cycle_start = time.time()
                continue

        # Verrouillage (commandes console "accroche"/"decroche", 2026-09-29) :
        # apres la securite thermique (toujours prioritaire), avant la pause
        # programmee (suspendue tant que l'utilisateur ecoute une frequence).
        # Mode manuel (console, 2026-09-29) : ecoute en direct sans
        # silencieux de la frequence choisie au clavier. La console annonce
        # elle-meme les frequences : rien a dire ici.
        manual = noise_signatures.manual_request()
        if manual:
            meteo_locale.set_scanner_state("manuel")
            if current_lock_key != "manuel":
                print("[radio_watch] mode manuel")
                current_lock_key = "manuel"
            # Defilement rapide aux fleches : on attend que la frequence ne
            # bouge plus depuis MANUAL_SETTLE_SEC avant de relancer rtl_fm.
            wanted = (manual["freq_hz"], manual["mode"], manual.get("filter", False))
            if wanted != manual_pending[0]:
                manual_pending = (wanted, time.time())
            elif time.time() - manual_pending[1] >= MANUAL_SETTLE_SEC:
                manual_stream.tune(*wanted)
            time.sleep(0.1)
            continue
        manual_stream.stop()

        lock_capture = noise_signatures.lock_request()
        lock_key = (lock_capture["freq_hz"], lock_capture.get("target")) if lock_capture else None
        if lock_key != current_lock_key:
            if lock_capture and current_lock_key == "manuel":
                # La console vient de le dire en fermant le mode manuel.
                print(f"[radio_watch] fin du mode manuel, verrouillage sur {lock_capture['freq_hz']/1e6:.4f} MHz")
            elif lock_capture:
                print(f"[radio_watch] verrouillage sur {lock_capture['freq_hz']/1e6:.4f} MHz ({lock_capture['label']})")
                _speak_pause(f"Scanner verrouillé sur {lock_capture['label']}.")
            else:
                print("[radio_watch] fin du verrouillage, reprise du balayage")
                _speak_pause("Balayage radio repris.")
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
                f"Pause programmée du scanner radio, reprise dans {SCAN_OFF_DURATION_SEC // 60} minutes."
            )
            # Un balayage meteo demande par la console passe quand meme.
            meteo_locale.set_scanner_state("pause_programmee")
            pause_end = time.time() + SCAN_OFF_DURATION_SEC
            while time.time() < pause_end:
                if meteo_locale.scan_requested():
                    _meteo_scan_if_requested()
                time.sleep(1)
            meteo_locale.set_scanner_state("actif")
            _speak_pause("Le scanner radio reprend.")
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
            _meteo_scan_if_requested()  # la console attend son bulletin : on n'attend pas la fin du cycle
            _satellite_pass_if_due()
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
                speak_with_lead_sound(SCANER_ERROR_SOUND, "Attention, le dongle radio semble indisponible. Vérifie la connexion USB.", label="radio_watch", repetitive=True)
            finally:
                audio_lock.release(lock)
            device_down_announced = True
        elif _rtl_power_failure_count == 0 and device_down_announced:
            lock = audio_lock.acquire()
            try:
                speak("Le dongle radio est de nouveau disponible.")
            finally:
                audio_lock.release(lock)
            device_down_announced = False
        elif device_down_announced and _rtl_power_failure_count % DEVICE_DOWN_REMINDER_EVERY == 0:
            lock = audio_lock.acquire()
            try:
                speak_with_lead_sound(SCANER_ERROR_SOUND, "Le dongle radio est toujours indisponible.", label="radio_watch", repetitive=True)
            finally:
                audio_lock.release(lock)

        if not activity:
            time.sleep(0.3)


if __name__ == "__main__":
    main()
