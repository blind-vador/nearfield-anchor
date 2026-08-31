import asyncio
import os
import random
import subprocess
import tempfile
import threading

import edge_tts

from sound_player import play_sound_effect

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
        subprocess.run(["espeak", "-v", voice, "-b", "1", text], check=False, timeout=30)
    except Exception as exc:
        print(f"[espeak indisponible: {exc}]")


async def _do_synthesize(text, voice, path):
    communicate = edge_tts.Communicate(text, voice)
    await asyncio.wait_for(communicate.save(path), timeout=EDGE_TTS_TIMEOUT_SEC)


def _synthesize_edge_tts(text, result_holder):
    """Tourne dans un thread separe -- genere l'audio via le service cloud
    de Microsoft Edge (voix tiree au hasard parmi EDGE_TTS_VOICES a chaque
    appel) et stocke le chemin du fichier resultat dans result_holder[0]
    (reste None en cas d'echec/timeout)."""
    path = None
    try:
        voice = random.choice(EDGE_TTS_VOICES)
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="edge_tts_")
        os.close(fd)
        asyncio.run(_do_synthesize(text, voice, path))
        if os.path.getsize(path) > 0:
            result_holder[0] = path
        else:
            os.remove(path)
    except Exception as exc:
        print(f"[speak] synthese cloud (edge-tts) echouee : {exc}")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def speak_with_lead_sound(sound_path, text, sound_timeout_sec=15, label="speak"):
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

    A utiliser a la place de l'enchainement habituel
    play_sound_effect(son); speak(texte) partout ou ce couple existe deja."""
    result_holder = [None]
    thread = threading.Thread(target=_synthesize_edge_tts, args=(text, result_holder), daemon=True)
    thread.start()

    play_sound_effect(sound_path, timeout_sec=sound_timeout_sec, label=label)

    thread.join(EDGE_TTS_EXTRA_WAIT_SEC)
    audio_path = result_holder[0]
    print(text)
    if audio_path:
        try:
            play_sound_effect(audio_path, timeout_sec=15, label=label)
        finally:
            try:
                os.remove(audio_path)
            except OSError:
                pass
    else:
        try:
            subprocess.run(["espeak", "-v", "fr", "-b", "1", text], check=False, timeout=30)
        except Exception as exc:
            print(f"[espeak indisponible: {exc}]")
