import asyncio
import hashlib
import os
import random
import shutil
import subprocess
import unicodedata

import audio_lock
import espeak_out
import piper_out
import voix_sources
import tempfile
import threading
import time

import edge_tts

import edge_langue

edge_langue.force_language()  # requete en francais (edge-tts l'ecrit en anglais)

import sound_player
from sound_player import play_sound_effect
from sound_paths import TTS_CACHE_DIR
from phrases import dire

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
# Sans les voix "Multilingual" (Vivienne, Remy) depuis le 2026-10-03 : elles
# devinent la langue phrase par phrase et basculent en anglais au premier mot
# anglais ; le service gratuit de Edge refuse la balise <lang> qui fixerait
# leur langue (essai reel : aucun son renvoye). Voir lib/edge_langue.py.
EDGE_TTS_VOICES = [
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


# Les voix Microsoft sortent en MP3 a 48 kbit/s : la duree se deduit de la
# taille. Avant (2026-10-01) : 15 s fixes, et les annonces longues (APRS avec
# indicatif epele et commentaire) etaient coupees en plein milieu.
SPEECH_BITRATE_BPS = 48000
SPEECH_TIMEOUT_MIN_SEC = 15
SPEECH_TIMEOUT_MARGIN_SEC = 5


def _speech_timeout(path):
    try:
        seconds = os.path.getsize(path) * 8 / SPEECH_BITRATE_BPS
    except OSError:
        return SPEECH_TIMEOUT_MIN_SEC
    return max(SPEECH_TIMEOUT_MIN_SEC, seconds + SPEECH_TIMEOUT_MARGIN_SEC)




JOURNAL_NOTICE_EVERY_SEC = 60
_journal_notice = {}


def journal_notice(journal=None):
    """Previent qu'une captation a ete enregistree en silence pendant que le
    journal est ouvert (audio_lock.SILENT_WHEN_JOURNAL). Source console :
    passe entre deux ecoutes du journal. Rien si le journal est deja ferme, et
    le scanner n'attend pas (il restait bloque jusqu'a 2 minutes, puis
    annoncait une captation au journal ferme, 2026-11-06).
    journal (2026-11-10) : la categorie (lib/journaux.py). Sans son nom, une donnee
    FleetSync annoncee pendant qu'on parcourait le journal radio semblait y arriver."""
    maintenant = time.time()
    cle = journal or "radio"
    if not audio_lock.journal_open() or maintenant - _journal_notice.get(cle, 0) < JOURNAL_NOTICE_EVERY_SEC:
        return
    _journal_notice[cle] = maintenant
    threading.Thread(target=_dire_journal_notice, args=(cle,), daemon=True).start()


def _dire_journal_notice(journal="radio"):
    import journaux
    lock = audio_lock.acquire("console", timeout=120)
    try:
        if not lock.dropped and audio_lock.journal_open():
            speak_cloud(dire("annonce.journal_notice", journal=journaux.NAMES.get(journal, journal)),
                        repetitive=True)
    finally:
        audio_lock.release(lock)


def speak(text, voice="fr", source=None):
    """Annonce sans son d'accompagnement, dans la voix de la source (commande
    "voix", lib/voix_sources.py) : Microsoft si choisi et present, sinon la
    voix de secours (Piper), espeak en dernier."""
    _annoncer(None, text, label="speak", source=source)


async def _do_synthesize(text, voice, path, rate="+0%", pitch="+0Hz"):
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await asyncio.wait_for(communicate.save(path), timeout=EDGE_TTS_TIMEOUT_SEC)


def _synthesize_edge_tts(text, result_holder, candidate_voices=None, rate="+0%", pitch="+0Hz"):
    """Tourne dans un thread separe -- genere l'audio via le service cloud
    de Microsoft Edge (voix tiree au hasard parmi candidate_voices, ou
    EDGE_TTS_VOICES en entier par defaut) et stocke (chemin, voix) dans
    result_holder[0] (reste None en cas d'echec/timeout)."""
    path = None
    try:
        voice = random.choice(candidate_voices or EDGE_TTS_VOICES)
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="edge_tts_")
        os.close(fd)
        asyncio.run(_do_synthesize(text, voice, path, rate, pitch))
        if os.path.getsize(path) > 0:
            result_holder[0] = (path, voice)
            voix_sources.noter_microsoft(True)
        else:
            os.remove(path)
    except Exception as exc:
        print(f"[speak] synthese cloud (edge-tts) echouee : {str(exc)[:120]}")
        voix_sources.noter_microsoft(False)
        voix_sources.erreur_microsoft(exc)
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
    garantir l'unicite meme entre phrases tronquees identiques.
    NFC : le meme mot accentue ne rate pas le cache selon la machine qui l'ecrit."""
    text = unicodedata.normalize("NFC", text)
    slug = "".join(c if c.isalnum() else "_" for c in text).strip("_")[:60].strip("_")
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return f"{slug}_{digest}" if slug else digest


def _repetitive_cache_dir(text):
    return os.path.join(TTS_CACHE_DIR, _phrase_folder_name(text))


def _cached_repetitive_sample(text):
    """Retourne un echantillon tire au hasard dans le cache de cette phrase,
    s'il en existe au moins un. Un cache partiel (quelques voix) vaut mieux
    qu'espeak -- surtout quand le cloud edge-tts est en panne (403 Microsoft,
    oct. 2026). Le remplissage des voix manquantes reste guide par
    _missing_voices_for() au prochain succes cloud."""
    cache_dir = _repetitive_cache_dir(text)
    try:
        samples = [f for f in os.listdir(cache_dir) if f.endswith(".mp3")]
    except OSError:
        # Cache sur le NAS : dossier absent, mais aussi disque decroche
        # (EIO, 2026-09-29 -- nas-watch plantait en annoncant justement la
        # perte du disque). Pas de cache = synthese normale, jamais de plantage.
        return None
    samples = [f for f in samples if not f.startswith(PIPER_PREFIX)]  # Piper : seulement en secours
    if not samples:
        return None
    return os.path.join(cache_dir, random.choice(samples))


# Cache Piper (2026-10-18) : meme dossier que les echantillons Microsoft,
# fichiers "piper-<voix>.wav". Joues seulement si Microsoft ne repond pas et
# n'a rien en cache ; fabriques une fois (a la volee en secours, ou a
# l'avance par watchers/voix_preparation.py), puis toujours rejoues.
PIPER_PREFIX = "piper-"
TEXT_FILE = "texte.txt"  # le texte exact de la phrase, pour la preparation a l'avance


def remember_repetitive(text):
    """Note le texte exact dans son dossier de cache (preparation a l'avance)."""
    path = os.path.join(_repetitive_cache_dir(text), TEXT_FILE)
    if os.path.exists(path):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _echantillon(text, choix):
    """Echantillon en cache pour ce choix de voix : la voix fixee, ou (voix
    aleatoire) n'importe quel echantillon du meme moteur et du meme debit."""
    folder = _repetitive_cache_dir(text)
    if not choix.get("aleatoire"):
        path = voix_sources.fichier_cache(folder, choix)
        return path if os.path.exists(path) else None
    suf = voix_sources.suffixe(choix)
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    piper = choix["moteur"] == "piper"
    ext = ".wav" if piper else ".mp3"
    pool = [n for n in names if n.startswith(PIPER_PREFIX) == piper and n.endswith(suf + ext)
            and (suf or "@" not in n) and voix_sources.echantillon_permis(n, choix, suf)]
    return os.path.join(folder, random.choice(pool)) if pool else None


def piper_sample(text, choix=None, make=True):
    """Echantillon Piper de la phrase pour ce choix (voix de secours de la
    source par defaut) ; s'il n'existe pas et make=True, il est fait maintenant."""
    choix = choix or voix_sources.secours(voix_sources.source_pour())
    if choix["moteur"] != "piper":
        return None
    found = _echantillon(text, choix)
    if found or not make:
        return found
    path = voix_sources.fichier_cache(_repetitive_cache_dir(text), choix)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return None
    ok = piper_out.to_file(text, path, voice_id=choix["voix"], scale_len=piper_out.length_scale(choix["debit"]))
    return path if ok else None


def _missing_voices_for(text):
    """Voix pas encore echantillonnees pour cette phrase -- utilise pour
    orienter le tirage au hasard vers les voix manquantes plutot que de
    compter sur la pure chance pour completer le cache (probleme du
    collectionneur de coupons : ~40 tirages purement aleatoires en
    moyenne pour les 13 voix, contre 13 au pire ainsi). Retourne None si
    aucun cache n'existe encore (tirage normal parmi toutes les voix)."""
    cache_dir = _repetitive_cache_dir(text)
    try:
        existing = {os.path.splitext(f)[0] for f in os.listdir(cache_dir)
                    if f.endswith(".mp3") and not f.startswith(PIPER_PREFIX)}
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


def speak_with_lead_sound(sound_path, text, sound_timeout_sec=15, label="speak", repetitive=False, reverb=False,
                          source=None):
    """Diffuse sound_path puis annonce text, dans la voix de la source (commande
    "voix" : voix principale, debit, hauteur, voix de secours ; lib/voix_sources.py).

    Voix Microsoft : la synthese est lancee EN PARALLELE du son
    d'accompagnement (idee de l'utilisateur, 2026-08-30) : le temps reseau
    se cache derriere le son. Microsoft absent (etat partage, pas d'attente
    inutile) ou en echec : voix de secours Piper, espeak en dernier.

    repetitive=True : textes FIXES annonces souvent (nom de bande...) : garde
    en cache par voix (et par debit/hauteur), rejoue sans aucun calcul."""
    _annoncer(sound_path, text, sound_timeout_sec, label, repetitive, reverb, source)


def parler(text, voix=None, debit=None, hauteur=None, source="logiciels"):
    """Parole demandée par un logiciel installé (point d'accès des extensions) : une voix
    du catalogue par son nom (lib/voix_sources.py), à son tour dans la file des priorités.
    Rend la voix qui a parlé, ou None si une source plus importante l'a fait taire."""
    import audio_lock
    choix = voix_sources.choix_impose(voix, source, debit, hauteur)
    lock = audio_lock.acquire(source)
    try:
        if lock.dropped:
            return None
        _annoncer(None, text, label=source, source=source, choix=choix)
        return choix.get("voix")
    finally:
        audio_lock.release(lock)


def _annoncer(sound_path, text, sound_timeout_sec=15, label="speak", repetitive=False, reverb=False, source=None,
              choix=None):
    if audio_lock.dropped():
        print(f"(muet, priorites) {text}")
        return  # source qui a cede sa place : ni son ni voix, pas de synthese inutile
    import sons
    if sound_path and sons.mode_chemin(sound_path) != "son_phrase":  # commande "sons" : le son seul, ou rien
        print(f"(son seul) {text}")
        play_sound_effect(sound_path, timeout_sec=sound_timeout_sec, label=label, reverb=reverb)
        return
    source = source or voix_sources.source_pour()
    choix = choix or voix_sources.choisir(source)
    effet = voix_sources.effet(source)   # commande "voix" : effet sur la voix, tire a chaque annonce
    # Qui parle, a chaque annonce (2026-11-13 : "jamais entendu Google" alors que le
    # tirage le donne une fois sur deux ; les repli disent deja "muette, secours").
    print(f"[voix] {source} : {choix.get('voix')}" + (f", effet {effet}" if effet else ""))
    lead_done = [False]

    def lead():
        if sound_path and not lead_done[0]:
            lead_done[0] = True
            play_sound_effect(sound_path, timeout_sec=sound_timeout_sec, label=label, reverb=reverb)

    def play(path):
        print(text)
        voix = lambda: play_sound_effect(path, timeout_sec=_speech_timeout(path), label=label, reverb=reverb,  # noqa: E731
                                         effet=effet)
        if sound_path and not lead_done[0]:
            # Voix prete d'avance (cache) : elle part juste apres la derniere note
            # du jingle, sur sa traine, au lieu d'attendre la fin du fichier (2026-10-19).
            lead_done[0] = True
            sound_player.play_lead_then(sound_path, voix, label=label, reverb=reverb, timeout_sec=sound_timeout_sec)
        else:
            voix()

    if voix_sources.en_direct(choix):
        import atomes_parole
        text = atomes_parole.indicatifs_en_clair(text)
        print(text)
        lead()
        _dire_sans_cache(text, choix, label, reverb, effet)
        return
    if choix["moteur"] in ("edge", "piper") and not voix_sources.phrase_entiere(choix):
        # Nombres et indicatifs : colles depuis les mots en cache.
        # Google et le Mac passent en une phrase, plus bas.
        try:
            import annonce_morceaux as morceaux
            if morceaux.a_assembler(text):
                parts = morceaux.expand_texte(text)
                if reverb or effet:   # un effet se pose sur un fichier, pas sur le flux
                    chemin = morceaux.assemble(parts, choix)
                    if chemin:
                        try:
                            play(chemin)
                        finally:
                            try:
                                os.remove(chemin)
                            except OSError:
                                pass
                        return
                else:
                    ticket = audio_lock.ticket_courant()
                    repli = voix_sources.secours(source)
                    if repli.get("moteur") not in ("edge", "piper"):
                        repli = None
                    lecture = morceaux.Lecture(
                        parts, choix, lead=None if lead_done[0] else sound_path, label=label,
                        regler=lambda data, ticket=ticket: audio_lock.scale(data, ticket=ticket),
                        journal=True, repli=repli)
                    print(text)
                    lecture.wait()
                    if sound_path:
                        lead_done[0] = True
                    if lecture.a_parle:
                        return
                dit = morceaux.spoken(parts)
                if dit:
                    text = dit
            else:
                # Flash et longs textes : un seul passage, mais l'indicatif
                # est deja en alphabet radio (Foxtrot, pas « F »).
                import atomes_parole
                text = atomes_parole.indicatifs_en_clair(text)
        except Exception as exc:
            print(f"[speak] assemblage : {exc!r}")
    if repetitive:
        remember_repetitive(text)
        # Microsoft absent : ce que la voix demandee a deja dit passe avant le secours.
        cached = (_echantillon(text, choix["demande"]) if choix.get("demande") else None) or _echantillon(text, choix)
        if cached:
            play(cached)
            return
    if choix["moteur"] == "edge":
        holder = [None]
        thread = threading.Thread(target=_synthesize_edge_tts, daemon=True,
                                  args=(text, holder, [choix["voix"]], choix["debit"], choix["hauteur"]))
        thread.start()
        lead()
        thread.join(EDGE_TTS_EXTRA_WAIT_SEC if sound_path else EDGE_TTS_TIMEOUT_SEC + 2)
        if holder[0]:
            audio_path, _ = holder[0]
            try:
                if repetitive:
                    dest = voix_sources.fichier_cache(_repetitive_cache_dir(text), choix)
                    try:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        if not os.path.exists(dest):
                            shutil.copyfile(audio_path, dest)
                    except OSError as exc:
                        print(f"[speak] echec sauvegarde cache repetitif : {exc}")
                play(audio_path)
            finally:
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
            return
        choix = voix_sources.secours(source)  # Microsoft n'a pas repondu a temps
        if repetitive:
            cached = _echantillon(text, choix)
            if cached:
                play(cached)
                return
    lead()
    if repetitive and choix["moteur"] == "piper":
        path = piper_sample(text, choix)
        if path:
            play(path)
            return
    print(text)
    _espeak_recorded(text, label, reverb=reverb, choix=choix, effet=effet)


def _fichier_puis_effet(text, choix, label, reverb, effet):
    """Voix locale (Piper, MBROLA) dans un fichier, jouee avec son effet. True si elle a parle."""
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="effet_")
    os.close(fd)
    try:
        if audio_lock.dropped() or not piper_out.to_file(text, path, voice_id=choix.get("voix"),
                                                          scale_len=piper_out.length_scale(choix.get("debit"))):
            return False
        play_sound_effect(path, timeout_sec=_speech_timeout(path), label=label, reverb=reverb, effet=effet)
        return True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _dire_sans_cache(text, choix, label, reverb, effet=None):
    """MBROLA ou espeak, en direct. Le fichier sert au journal, puis il est
    effacé : il n'entre pas dans le cache d'une voix."""
    voix = (choix or {}).get("voix") or ""
    if effet and voix.startswith("piper:mbrola#") and _fichier_puis_effet(text, choix, label, reverb, effet):
        return
    if voix.startswith("piper:mbrola#") and not audio_lock.dropped():
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="mbrola_")
        os.close(fd)
        try:
            if piper_out.say(text, scale=audio_lock.scale, record_path=path, voice_id=voix,
                             scale_len=piper_out.length_scale((choix or {}).get("debit"))):
                session = sound_player.get_recording_session()
                if session is not None:
                    session.add_audio_file(path)
                return
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="espeak_")
    os.close(fd)
    try:
        subprocess.run(["espeak", "-v", "fr", "-b", "1", "-w", path, text], capture_output=True,
                       timeout=espeak_out.duree_max(text))
        if os.path.getsize(path) > 0:
            play_sound_effect(path, timeout_sec=_speech_timeout(path), label=label, reverb=reverb, effet=effet)
            return
    except Exception as exc:
        print(f"[espeak fichier indisponible: {exc}]")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        espeak_out.say(text, "fr", scale=audio_lock.scale)
    except Exception as exc:
        print(f"[espeak indisponible: {exc}]")


def _espeak_recorded(text, label, reverb=False, choix=None, effet=None):
    """Voix locale : Piper (voix et debit de la source, en flux, copiee au
    journal en cours, 2026-10-18), puis espeak en passant par un fichier :
    la voix est jouee ET copiee dans l'enregistrement du journal en cours
    (sinon la prise n'a que le carillon, 2026-10-03)."""
    choix = choix or voix_sources.secours(voix_sources.source_pour())
    if effet and choix["moteur"] == "piper" and _fichier_puis_effet(text, choix, label, reverb, effet):
        return
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="espeak_")
    os.close(fd)
    session = sound_player.get_recording_session()
    if choix["moteur"] == "piper" and not audio_lock.dropped() and piper_out.say(
            text, scale=audio_lock.scale, record_path=path, voice_id=choix["voix"],
            scale_len=piper_out.length_scale(choix["debit"])):
        try:
            if session is not None:
                session.add_audio_file(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return
    if voix_sources.phrase_entiere(choix):
        # Google ou le Mac n a pas parle : le secours choisi, phrase entiere.
        try:
            os.remove(path)
        except OSError:
            pass
        repli = voix_sources.secours(voix_sources.source_pour())
        print("[voix] %s muette, secours %s" % (choix.get("voix"), repli.get("voix")))
        if voix_sources.en_direct(repli):
            _dire_sans_cache(text, repli, label, reverb, effet)
            return
        if effet and repli.get("moteur") == "piper" and _fichier_puis_effet(text, repli, label, reverb, effet):
            return
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="secours_")
        os.close(fd)
        if repli.get("moteur") == "piper" and not audio_lock.dropped() and piper_out.say(
                text, scale=audio_lock.scale, record_path=path, voice_id=repli["voix"],
                scale_len=piper_out.length_scale(repli.get("debit"))):
            try:
                if session is not None:
                    session.add_audio_file(path)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return
        try:
            os.remove(path)
        except OSError:
            pass
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="espeak_")
        os.close(fd)
    try:
        subprocess.run(["espeak", "-v", "fr", "-b", "1", "-w", path, text], capture_output=True,
                       timeout=espeak_out.duree_max(text))
        if os.path.getsize(path) > 0:
            play_sound_effect(path, timeout_sec=_speech_timeout(path), label=label, reverb=reverb, effet=effet)
            return
    except Exception as exc:
        print(f"[espeak fichier indisponible: {exc}]")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        espeak_out.say(text, "fr", scale=audio_lock.scale)
    except Exception as exc:
        print(f"[espeak indisponible: {exc}]")


def speak_cloud(text, repetitive=False, source=None):
    """Comme speak_with_lead_sound(), sans son d'accompagnement (veilles sans
    carillon dedie). Microsoft present : latence reseau entiere au premier
    passage d'une phrase, puis cache si repetitive."""
    _annoncer(None, text, label="speak", repetitive=repetitive, source=source)
