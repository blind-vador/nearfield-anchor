"""Alarmes (2026-10-24, demande utilisateur) : un evenement grave (urgence
d'avion, balise de detresse, alarme electrique, antenne muette...) ne se dit
pas une fois : sa sonnerie tourne en boucle, et le message vocal est dit
par-dessus, a chaque moitie de la longueur de la sonnerie, jusqu'a ce que
l'utilisateur tape "compris" a la console (acquitter()).

  - le message est synthetise UNE fois (voix de la source : Microsoft, Piper,
    Mac, Google...), garde dans CACHE_DIR le temps de l'alarme, puis rejoue
    tel quel : aucun nouveau calcul tant qu'elle dure ;
  - la voix part sur un temps de la sonnerie (multiples d'une demi-sonnerie),
    jamais sur elle-meme : apres la fin du message, au temps suivant ; la
    sonnerie baisse (SOUS_LA_VOIX) le temps qu'elle parle ;
  - garde-fou : au bout de MAX_SEC sans "compris", l'alarme s'arrete ; elle
    reste au journal des alarmes, notee sans reponse ;
  - journal des alarmes (lib/journaux.py, prefixe "alarme_") : chaque alarme
    jouee y est rangee, une seule sonnerie suivie du message, avec sa fiche
    (evenement, texte, et comment elle a fini) ;
  - commande "sons" : "son seul" = la sonnerie en boucle sans la voix ;
    "rien" = rien d'audible, mais l'alarme est quand meme rangee au journal.

Les alarmes d'un meme service passent l'une apres l'autre ; entre services,
le verrou audio (source "alertes", la plus prioritaire) les met en file.
"""
import datetime
import hashlib
import json
import os
import subprocess
import threading
import time

import numpy as np

import audio_lock
import sons
from phrases import dire

ACTIVES_DIR = "/tmp/alarmes_actives"      # une fiche par alarme qui sonne (pid : purge des morts)
ACK_PATH = "/home/vador/autopilot/alarmes_acquit.json"
CACHE_DIR = "/tmp/alarmes_voix"           # messages synthetises, le temps de l'alarme
MAX_SEC = 30 * 60
PREMIERE_VOIX_MAX_SEC = 4.0               # sonnerie longue (7500 : 1 min) : la voix n'attend pas 30 s
SOUS_LA_VOIX = 0.45
SOURCE_AUDIO = "alertes"
RATE = 44100
CANAUX = 2
_une_a_la_fois = threading.Lock()
_en_cours = set()                         # evenements deja en file ou qui sonnent, dans ce service


def _log(texte):
    print(f"[alarmes] {texte}", flush=True)


# --- acquittement (commande "compris") -------------------------------------------

def _vivant(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def actives():
    """Alarmes qui sonnent en ce moment, tous services confondus : [fiche]."""
    out = []
    try:
        noms = sorted(os.listdir(ACTIVES_DIR))
    except OSError:
        return out
    for nom in noms:
        chemin = os.path.join(ACTIVES_DIR, nom)
        try:
            with open(chemin, encoding="utf-8") as f:
                fiche = json.load(f)
        except (OSError, ValueError):
            continue
        if _vivant(fiche.get("pid", 0)):
            out.append(fiche)
        else:
            try:
                os.remove(chemin)
            except OSError:
                pass
    return out


def acquitter():
    """ "compris" : arrete toutes les alarmes en cours ; rend leurs fiches."""
    en_cours = actives()
    tmp = f"{ACK_PATH}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"t": time.time()}, f)
    os.replace(tmp, ACK_PATH)
    return en_cours


def _acquittee(debut):
    try:
        return os.path.getmtime(ACK_PATH) > debut
    except OSError:
        return False


# --- son ---------------------------------------------------------------------------

def _pcm(chemin):
    """PCM entier 16 bits entrelace (RATE, CANAUX), en flottants ; None si illisible."""
    if not chemin or not os.path.exists(chemin):
        return None
    try:
        brut = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", chemin,
                               "-f", "s16le", "-ac", str(CANAUX), "-ar", str(RATE), "-"],
                              capture_output=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    brut = brut[: len(brut) - len(brut) % (2 * CANAUX)]
    return np.frombuffer(brut, dtype=np.int16).astype(np.float32) if brut else None


def _voix(texte, source):
    """Message synthetise une fois, garde dans CACHE_DIR ; chemin ou None."""
    import prise_vocale
    os.makedirs(CACHE_DIR, exist_ok=True)
    cle = hashlib.sha1(f"{source}|{texte}".encode()).hexdigest()[:16]
    chemin = os.path.join(CACHE_DIR, f"{cle}_{os.getpid()}.mp3")
    if os.path.exists(chemin) and os.path.getsize(chemin) > 0:
        return chemin
    try:
        if prise_vocale.synthesize(texte, chemin, source):
            return chemin
    except Exception as exc:
        _log(f"synthese impossible : {exc!r}")
    return None


def _sonner(sonnerie, voix, debut):
    """Sonnerie en boucle, voix par-dessus a chaque demi-sonnerie, jusqu'au
    "compris" ; rend "compris" ou "sans_reponse"."""
    alarme = _pcm(sonnerie)
    if alarme is None:
        _log(f"sonnerie illisible : {sonnerie}")
        return "sans_son"
    parole = _pcm(voix) if voix else None
    n = len(alarme)
    demi = max(CANAUX, (n // 2) // CANAUX * CANAUX)
    morceau = RATE * CANAUX // 10
    prochaine = min(demi, int(PREMIERE_VOIX_MAX_SEC * RATE) * CANAUX)
    voix_debut = None
    pos = 0
    fin = debut + MAX_SEC
    lecteur = subprocess.Popen(["aplay", "-q", "-D", "default", "-r", str(RATE), "-f", "S16_LE", "-c", str(CANAUX)],
                               stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    resultat = "sans_reponse"
    try:
        while time.time() < fin:
            if _acquittee(debut):
                resultat = "compris"
                break
            a = alarme[np.arange(pos, pos + morceau) % n]
            if parole is not None:
                if voix_debut is None and pos + morceau > prochaine:
                    voix_debut = prochaine
                if voix_debut is not None:
                    decal = pos - voix_debut
                    v = np.zeros(morceau, dtype=np.float32)
                    de, dans = max(0, -decal), max(0, decal)
                    k = max(0, min(morceau - de, len(parole) - dans))
                    v[de:de + k] = parole[dans:dans + k]
                    gain = np.ones(morceau, dtype=np.float32)
                    gain[de:de + k] = SOUS_LA_VOIX
                    a = a * gain + v
                    if decal + morceau >= len(parole):
                        fin_voix = voix_debut + len(parole)
                        prochaine = (fin_voix // demi + 1) * demi
                        voix_debut = None
            data = np.clip(a, -32768, 32767).astype(np.int16).tobytes()
            lecteur.stdin.write(audio_lock.scale(data))
            pos += morceau
    except (BrokenPipeError, OSError) as exc:
        _log(f"lecture interrompue : {exc!r}")
    finally:
        try:
            lecteur.stdin.close()
        except OSError:
            pass
        if resultat == "compris" or lecteur.poll() is None:
            lecteur.terminate()  # "compris" : on coupe net, sans attendre la fin du tampon
        try:
            lecteur.wait(timeout=3)
        except subprocess.TimeoutExpired:
            lecteur.kill()
    return resultat


# --- journal des alarmes -------------------------------------------------------------

def _au_journal(evenement, texte, sonnerie, voix, source):
    """Une sonnerie puis le message (sans nouvelle synthese) ; chemin de la prise."""
    import prise_vocale
    try:
        return prise_vocale.record(texte, sonnerie, f"alarme_{evenement}", {"evenement": evenement,
                                   "alarme": True}, source, voix=voix)
    except Exception as exc:
        _log(f"journal des alarmes : {exc!r}")
        return None


def _noter_fin(prise, resultat, debut):
    if not prise:
        return
    fiche = prise[:-4] + ".json"
    try:
        with open(fiche, encoding="utf-8") as f:
            data = json.load(f)
        data.update(fin=resultat, duree_sec=round(time.time() - debut),
                    fin_a=datetime.datetime.now().strftime("%H:%M"))
        with open(fiche, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
    except (OSError, ValueError):
        pass


# --- declenchement -----------------------------------------------------------------

def _alarme(evenement, texte, source, quand_fini):
    with _une_a_la_fois:
        sonnerie = sons.chemin(evenement)
        mode = sons.mode(evenement)
        message = f"{texte} {dire('alarmes.dis_compris')}" if texte else None
        voix = _voix(message, source) if message and mode == "son_phrase" else None
        prise = [None]
        rangement = threading.Thread(target=lambda: prise.__setitem__(
            0, _au_journal(evenement, texte or dire(f"sons.evenement_{evenement}"), sonnerie,
                           voix if mode == "son_phrase" else None, source)), daemon=True)
        rangement.start()
        _log(f"{evenement} ({mode}) : {texte}")
        resultat = "muette"
        fiche_active = None
        debut = time.time()
        if mode != "rien":
            lock = audio_lock.acquire(SOURCE_AUDIO)
            try:
                debut = time.time()
                os.makedirs(ACTIVES_DIR, exist_ok=True)
                fiche_active = os.path.join(ACTIVES_DIR, f"{debut:.3f}_{os.getpid()}.json")
                with open(fiche_active, "w", encoding="utf-8") as f:
                    json.dump({"evenement": evenement, "texte": texte or "", "pid": os.getpid(), "debut": debut},
                              f, ensure_ascii=False)
                resultat = _sonner(sonnerie, voix, debut)
            finally:
                audio_lock.release(lock)
                if fiche_active:
                    try:
                        os.remove(fiche_active)
                    except OSError:
                        pass
        rangement.join(timeout=120)
        _noter_fin(prise[0], resultat, debut)
        if voix:
            try:
                os.remove(voix)
            except OSError:
                pass
        _log(f"{evenement} : fin ({resultat})")
    if quand_fini:
        try:
            quand_fini(resultat)
        except Exception as exc:
            _log(f"apres l'alarme : {exc!r}")
    return resultat


def _alarme_sure(evenement, *args):
    try:
        _alarme(evenement, *args)
    except Exception as exc:  # une alarme ratee n'arrete pas la veille
        _log(f"{evenement} : echec {exc!r}")
    finally:
        _en_cours.discard(evenement)


def declencher(evenement, texte, source=None, quand_fini=None, attendre=False):
    """Lance l'alarme (fil a part : la veille continue). quand_fini(resultat) :
    "compris", "sans_reponse", "muette" (reglee sur rien) ou "sans_son"."""
    if evenement in _en_cours:
        _log(f"{evenement} sonne deja : pas de seconde alarme")
        return None
    _en_cours.add(evenement)
    if source is None:
        import voix_sources
        source = voix_sources.source_pour()
    fil = threading.Thread(target=_alarme_sure, args=(evenement, texte, source, quand_fini), daemon=True,
                           name=f"alarme_{evenement}")
    fil.start()
    if attendre:
        fil.join()
    return fil
