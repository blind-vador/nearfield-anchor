import asyncio
import hashlib
import os
import random
import shutil
import subprocess

import audio_lock
import espeak_out
import tempfile
import threading

import edge_tts

from sound_player import play_sound_effect
from sound_paths import TTS_CACHE_DIR

# --- Voix cloud (edge-tts) + repli local (espeak) ---
# Ajoute en session (2026-08-30) suite a un constat direct de l'utilisateur
# (qui ecoute de la synthese vocale toute la journee via NVDA) : les voix
# neuronales locales type Piper ne justifient pas leur cout CPU sur ce Pi,
# deja charge (scanner radio, RTL-SDR). edge-tts (service cloud gratuit de
# Microsoft, sans cle API, voix neuronales de qualite) deporte tout le
# calcul -- zero charge CPU locale, juste un appel reseau. Ce n'est PAS une
# API officielle (acces reverse-engineered au moteur d'Edge) : peut casser
# sans preavis, d'ou le repli espeak systematique, jamais optionnel --
# aucune annonce ne doit pouvoir devenir silencieuse si le cloud est
# indisponible (certaines annonces de ce projet sont des alertes de
# securite -- meteo severe, crue).
# Tirage au hasard d'UNE voix parmi ce lot a CHAQUE annonce -- demande
# explicite de l'utilisateur (2026-08-30) apres avoir ecoute les 13 voix
# francophones disponibles (via voice_showcase.py) et les avoir toutes
# validees. Raison donnee : la mission premiere de ce systeme est la lutte
# contre le trouble du sommeil non-24h (N24, voir memoire projet) --
# l'habituation acoustique (une voix fixe et previsible) irait a l'encontre
# de cet objectif, ou l'imprevisibilite est justement recherchee. Toutes
# les variantes francophones retenues (France, Belgique, Canada, Suisse).
EDGE_TTS_VOICES = [
    "fr-FR-VivienneMultilingualNeural",
    "fr-FR-RemyMultilingualNeural",
    "fr-FR-DeniseNeural",
    "fr-FR-EloiseNeural",
    "fr-FR-HenriNeural",
    "fr-BE-CharlineNeural",
    "fr-BE-GerardNeural",
    "fr-CA-ThierryNeural",
    "fr-CA-AntoineNeural",
    "fr-CA-JeanNeural",
    "fr-CA-SylvieNeural",
    "fr-CH-ArianeNeural",
    "fr-CH-FabriceNeural",
]
EDGE_TTS_TIMEOUT_SEC = 8  # borne dure sur l'appel reseau lui-meme (thread de fond)
# Delai supplementaire tolere APRES la fin du son d'accompagnement pour
# laisser la synthese cloud se terminer si elle n'etait pas tout a fait
# prete -- au-dela, repli espeak plutot que de faire attendre l'utilisateur.
EDGE_TTS_EXTRA_WAIT_SEC = 2.0


def speak(text, voice="fr"):
    """Repli local garanti (espeak) -- zero dependance reseau, toujours
    disponible. Utilisable directement pour un texte sans son
    d'accompagnement, et employe en secours automatique par
    speak_with_lead_sound() si la synthese cloud echoue ou n'est pas prete
    a temps."""
    print(text)
    try:
        # -b 1 force l'interpretation UTF-8 en entree -- sans ca, espeak
        # (le vieux binaire, pas espeak-ng) devine l'encodage et se trompe
        # sur les caracteres accentues (ex: "inondee" -> "inondii",
        # "troncon" -> lit le c sans le son du cedille).
        if audio_lock.dropped():
            return  # source qui a abandonne (priorites) : on se tait
        espeak_out.say(text, voice, timeout=30, scale=audio_lock.scale)
    except Exception as exc:
        print(f"[espeak indisponible: {exc}]")


async def _do_synthesize(text, voice, path):
    communicate = edge_tts.Communicate(text, voice)
    await asyncio.wait_for(communicate.save(path), timeout=EDGE_TTS_TIMEOUT_SEC)


def _synthesize_edge_tts(text, result_holder, candidate_voices=None):
    """Tourne dans un thread separe -- genere l'audio via le service cloud
    de Microsoft Edge (voix tiree au hasard parmi candidate_voices, ou
    EDGE_TTS_VOICES en entier par defaut) et stocke (chemin, voix) dans
    result_holder[0] (reste None en cas d'echec/timeout)."""
    path = None
    try:
        voice = random.choice(candidate_voices or EDGE_TTS_VOICES)
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="edge_tts_")
        os.close(fd)
        asyncio.run(_do_synthesize(text, voice, path))
        if os.path.getsize(path) > 0:
            result_holder[0] = (path, voice)
        else:
            os.remove(path)
    except Exception as exc:
        print(f"[speak] synthese cloud (edge-tts) echouee : {exc}")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# --- Cache des annonces repetitives (2026-08-31, demande explicite) ---
# But : les textes fixes annonces tres souvent (nom de bande radio,
# messages systeme type "dongle indisponible") n'ont pas besoin d'un
# aller-retour reseau a CHAQUE occurrence -- une fois qu'on possede un
# echantillon pre-synthetise pour chacune des voix edge-tts, on peut
# piocher dedans au hasard (variete vocale preservee) sans jamais
# rappeler l'API. Les textes qui changent a chaque fois (annonces SNCF,
# survols ADS-B, messages ACARS traduits, alertes meteo/crue avec leurs
# valeurs) restent en repetitive=False (defaut) -- l'API est rappelee a
# chaque fois pour eux, la qualite prime sur l'economie reseau.

def _phrase_folder_name(text):
    """Nom de dossier lisible (utile en cas d'inspection manuelle via
    NVDA/Samba) mais borne en longueur, complete d'un court hash pour
    garantir l'unicite meme entre phrases tronquees identiques."""
    slug = "".join(c if c.isalnum() else "_" for c in text).strip("_")[:60].strip("_")
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return f"{slug}_{digest}" if slug else digest


def _repetitive_cache_dir(text):
    return os.path.join(TTS_CACHE_DIR, _phrase_folder_name(text))


def _cached_repetitive_sample(text):
    """Si le cache de cette phrase est COMPLET (un echantillon par voix
    disponible), retourne le chemin d'un echantillon tire au hasard parmi
    eux. Sinon (dossier absent ou incomplet) retourne None -- il faut
    passer par l'API."""
    cache_dir = _repetitive_cache_dir(text)
    try:
        samples = [f for f in os.listdir(cache_dir) if f.endswith(".mp3")]
    except OSError:
        # Cache sur le NAS : dossier absent, mais aussi disque decroche
        # (EIO, 2026-09-29 -- nas-watch plantait en annoncant justement la
        # perte du disque). Pas de cache = synthese normale, jamais de plantage.
        return None
    if len(samples) < len(EDGE_TTS_VOICES):
        return None
    return os.path.join(cache_dir, random.choice(samples))


def _missing_voices_for(text):
    """Voix pas encore echantillonnees pour cette phrase -- utilise pour
    orienter le tirage au hasard vers les voix manquantes plutot que de
    compter sur la pure chance pour completer le cache (probleme du
    collectionneur de coupons : ~40 tirages purement aleatoires en
    moyenne pour les 13 voix, contre 13 au pire ainsi). Retourne None si
    aucun cache n'existe encore (tirage normal parmi toutes les voix)."""
    cache_dir = _repetitive_cache_dir(text)
    try:
        existing = {os.path.splitext(f)[0] for f in os.listdir(cache_dir) if f.endswith(".mp3")}
    except OSError:
        # Cache sur le NAS : dossier absent, mais aussi disque decroche
        # (EIO, 2026-09-29 -- nas-watch plantait en annoncant justement la
        # perte du disque). Pas de cache = synthese normale, jamais de plantage.
        return None
    missing = [v for v in EDGE_TTS_VOICES if v not in existing]
    return missing or None


def _save_repetitive_sample(text, voice, src_path):
    """Ajoute l'echantillon au cache de cette phrase, nomme par la voix --
    n'ecrase jamais un echantillon deja present pour cette voix."""
    cache_dir = _repetitive_cache_dir(text)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        dest = os.path.join(cache_dir, f"{voice}.mp3")
        if not os.path.exists(dest):
            shutil.copyfile(src_path, dest)
    except OSError as exc:
        print(f"[speak] echec sauvegarde cache repetitif : {exc}")


def speak_with_lead_sound(sound_path, text, sound_timeout_sec=15, label="speak", repetitive=False):
    """Diffuse sound_path puis annonce text -- lance la synthese cloud
    (edge-tts) EN PARALLELE du son d'accompagnement plutot qu'apres coup :
    au moment ou l'appelant declenche le son, il sait deja ce qu'il va dire
    (idee de l'utilisateur en session 2026-08-30), donc le temps reseau se
    cache derriere la duree du son -- zero latence percue dans le cas
    courant (mesure en session : ~0.5s de synthese, largement sous la duree
    de n'importe quel son d'accompagnement de ce projet). Si la synthese
    n'est pas prete a temps ou echoue (reseau lent/indisponible, service
    cloud bloque...), bascule automatiquement sur speak() (espeak local) --
    jamais de silence total meme si le cloud est hors service.

    repetitive=True (2026-08-31, demande explicite) : a reserver aux
    textes FIXES annonces souvent (nom de bande radio, "dongle
    indisponible"...) -- PAS aux textes qui changent a chaque fois
    (annonces SNCF, survols, meteo/crue avec leurs valeurs, ACARS
    traduits). Si un echantillon existe deja pour chacune des voix
    edge-tts pour ce texte exact, ne fait AUCUN appel reseau : pioche au
    hasard dans le cache (variete vocale preservee). Sinon, appelle
    l'API comme d'habitude (en priorisant les voix pas encore
    echantillonnees pour ce texte) et persiste le resultat dans le cache
    au lieu de le jeter.

    A utiliser a la place de l'enchainement habituel
    play_sound_effect(son); speak(texte) partout ou ce couple existe deja."""
    if repetitive:
        cached_path = _cached_repetitive_sample(text)
        if cached_path:
            print(text)
            play_sound_effect(sound_path, timeout_sec=sound_timeout_sec, label=label)
            play_sound_effect(cached_path, timeout_sec=15, label=label)
            return
        candidate_voices = _missing_voices_for(text)
    else:
        candidate_voices = None

    result_holder = [None]
    thread = threading.Thread(target=_synthesize_edge_tts, args=(text, result_holder, candidate_voices), daemon=True)
    thread.start()

    play_sound_effect(sound_path, timeout_sec=sound_timeout_sec, label=label)

    thread.join(EDGE_TTS_EXTRA_WAIT_SEC)
    result = result_holder[0]
    print(text)
    if result:
        audio_path, voice = result
        if repetitive:
            _save_repetitive_sample(text, voice, audio_path)
        try:
            play_sound_effect(audio_path, timeout_sec=15, label=label)
        finally:
            try:
                os.remove(audio_path)
            except OSError:
                pass
    else:
        try:
            espeak_out.say(text, "fr", timeout=30, scale=audio_lock.scale)
        except Exception as exc:
            print(f"[espeak indisponible: {exc}]")


def speak_cloud(text, repetitive=False):
    """Comme speak_with_lead_sound(), sans son d'accompagnement -- pour les
    watchers sans chime dedie (nas_watch.py, 2026-08-31). Sans son a jouer
    en parallele pour masquer la latence reseau, un cache-miss expose donc
    le delai complet de la synthese cloud (jusqu'a EDGE_TTS_TIMEOUT_SEC)
    avant que quoi que ce soit ne se fasse entendre. Acceptable pour ce
    genre d'annonce de fond (etat des disques...) car transitoire : une
    fois le cache repetitif rempli pour une phrase donnee, plus aucun
    appel reseau -- NE PAS utiliser pour une alerte ou chaque milliseconde
    compte des le premier appel."""
    if repetitive:
        cached_path = _cached_repetitive_sample(text)
        if cached_path:
            print(text)
            play_sound_effect(cached_path, timeout_sec=15, label="speak")
            return
        candidate_voices = _missing_voices_for(text)
    else:
        candidate_voices = None

    result_holder = [None]
    _synthesize_edge_tts(text, result_holder, candidate_voices)
    result = result_holder[0]
    print(text)
    if result:
        audio_path, voice = result
        if repetitive:
            _save_repetitive_sample(text, voice, audio_path)
        try:
            play_sound_effect(audio_path, timeout_sec=15, label="speak")
        finally:
            try:
                os.remove(audio_path)
            except OSError:
                pass
    else:
        try:
            espeak_out.say(text, "fr", timeout=30, scale=audio_lock.scale)
        except Exception as exc:
            print(f"[espeak indisponible: {exc}]")
