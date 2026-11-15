#!/usr/bin/env python3
"""Module compagnon de nearfield anchor : une machine plus puissante (Mac,
PC Linux) fait les calculs lourds a la place du Raspberry Pi.

Un "pont" relie cette machine a un Pi. Elle garde une paire de cles Ed25519
(~/.nearfield-bridge/cle.pem) ; a l'appairage elle echange sa cle publique
avec celle du Pi par le disque du NAS, et les deux disent un code a 4
chiffres tire des deux cles. Ensuite le service se connecte au Pi, se fait
reconnaitre par signature, attend qu'il annonce un travail, le calcule sur
le disque du NAS et previent le Pi quand c'est fini. Voir
bridge/pont_protocole.py et, cote Pi, lib/compagnon.py.

Commandes (utilisees par l'application Nearfield Bridge, ou a la main) :
  service                 le service de fond (lance par l'installateur)
  etat                    ponts, connexions, statistiques
  appairage               appairage complet dans le terminal
  appairage-attendre      etape 1 : attend la demande du Pi, rend le code
  appairage-code          etape 2 : dialogue du code ; valider d'un seul cote
                          suffit (Oui ici, ou Entree sur le Pi), l'autre suit
  ponts                   liste des ponts (id, tabulation, description)
  oublier <pi>            supprime le pont avec ce Pi (le Pi est prevenu)
  service-pause / service-reprise / service-etat

Compatible Python 3.9 (celui d'Apple).
"""
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pont_protocole as proto  # noqa: E402

try:  # protocole v2 (module Rust pont/python) : inactif s'il manque
    import nearfield_pont as pont2  # noqa: E402
    import pont_v2_traduction as trad  # noqa: E402
except ImportError:
    pont2 = trad = None

HOME = os.path.expanduser("~/.nearfield-bridge")
CONFIG = os.path.join(HOME, "config.json")
KEY_PATH = os.path.join(HOME, "cle.pem")
BRIDGES = os.path.join(HOME, "ponts")
STATS = os.path.join(HOME, "stats")
PENDING = os.path.join(HOME, "appairage.json")
BIN_DIR = os.path.join(HOME, "bin")
GR_PYTHON = os.path.join(HOME, "gnuradio", "bin", "python")
LABEL = "local.nearfield.bridge"
PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)
SHARE_GUESSES = ["/Volumes/server/server/nearfield-anchor-bridge",  # Mac
                 "/mnt/server/server/nearfield-anchor-bridge",      # Linux
                 "/media/nas/nd1/server/nearfield-anchor-bridge"]   # le Pi lui-meme (essais)
BEAT_SEC = 30
RELANCE_V2_SEC = (60, 900)  # session v2 tombee ou refusee : nouvelle migration (certificat neuf), puis on retente
SCAN_SEC = 30  # etait 300 : decrire_images restait bloque
# Sans liaison directe : fonctionnement en DIFFERE par le NAS (decision
# utilisateur 2026-10-02 : pas d'ecritures ni de lectures frequentes, les
# disques s'useraient) -> travaux releves et signe de vie depose toutes les 12 h.
NAS_FALLBACK_SEC = 12 * 3600
LOOP_SEC = 5
PAIRING_WAIT_SEC = 180
RECONNECT_SEC = (10, 120)
V2_DIR = os.path.join(HOME, "v2")            # identite.pem (cle v2), ponts/<pi>.json (cle v2 du Pi epinglee)
CERT_MI_VIE_SEC = 6 * 3600  # certificat v2 de 12 h : renouvele a mi-vie, dans la session


# --- outils -------------------------------------------------------------------------

def log(text):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text), flush=True)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def config():
    conf = _read(CONFIG) or {}
    if "id" not in conf:
        conf = {"id": uuid.uuid4().hex[:12], "nom": socket.gethostname().split(".")[0]}
        _write(CONFIG, conf)
    return conf


def key():
    return proto.load_or_create_key(KEY_PATH)


def bridges():
    """{pi: fiche} des ponts de confiance."""
    found = {}
    try:
        names = os.listdir(BRIDGES)
    except OSError:
        return {}
    for name in names:
        card = _read(os.path.join(BRIDGES, name)) if name.endswith(".json") else None
        if card and (card.get("cle") or card.get("v2")):  # cle v1 du Pi, ou pont appaire en v2
            found[name[:-5]] = card
    return found


def stats(pi):
    return _read(os.path.join(STATS, pi + ".json")) or {}


_stats_lock = threading.Lock()


def update_stats(pi, **fields):
    with _stats_lock:
        data = stats(pi)
        data.update(fields)
        _write(os.path.join(STATS, pi + ".json"), data)
        return data


_lora_ok = {}


def _lora_ready():
    """GNU Radio + gr-lora_sdr installes (verifie une fois par session)."""
    if "ok" not in _lora_ok:
        try:
            _lora_ok["ok"] = subprocess.run([GR_PYTHON, "-c", "import gnuradio.lora_sdr"], capture_output=True,
                                            timeout=60).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _lora_ok["ok"] = False
    return _lora_ok["ok"]


def capabilities():
    caps = ["lora"] if _lora_ready() else []
    try:
        import numpy  # noqa: F401
        if all(os.access(os.path.join(BIN_DIR, b), os.X_OK) for b in ("meteor_demod", "meteor_decode")):
            caps.append("meteor")
    except ImportError:
        pass
    try:
        import decrire_images
        if decrire_images.available():
            caps.append("decrire_images")
    except Exception:
        pass
    try:  # sons Piper prepares d'avance pour le Pi (2026-10-19)
        import voix_piper
        if voix_piper.available():
            caps.append("voix")
    except Exception:
        pass
    if mac_voices():  # voix macOS dites en direct pour le Pi (2026-10-22)
        caps.append("voix_mac")
    if os.access(RADIO_DSD, os.X_OK):  # juges du scanner radio (2026-10-25, construire_dsd.sh)
        caps.append("radio_juges")
    if os.access(RADIO_DSD, os.X_OK):  # tickets partages : "ticket_sans_x" = ce que cette machine ne sait pas
        try:
            import tickets_partages as tp
            caps.append("tickets")
            toutes = list(tp.DONNEES_SON) + tp.epreuves_blu(["usb", "lsb"])
            caps += ["ticket_sans_" + e for e in tp.impossibles(toutes, _outils_tickets())]
        except Exception:
            traceback.print_exc()
    try:  # transcription du soir des prises validees (whisper.cpp)
        import whisper_radio
        if whisper_radio.disponible():
            caps.append("whisper")
    except Exception:
        pass
    try:  # examen des prises radio, touche E et passe de la nuit (2026-11-11, mac/analyse.py)
        import examen_mac
        if examen_mac.disponible():
            caps += ["examen", "examen_lot", "examen_serie"]
    except Exception:
        pass
    try:  # TETRA : decode en direct le flux rtl_tcp du Pi (2026-10-26, dsd-neo)
        import tetra_neo
        if tetra_neo.disponible():
            caps.append("tetra")
    except Exception:
        pass
    try:  # DMR/P25... en direct quand le Pi chauffe et delegue (2026-10-31, offload)
        import numerique_neo
        if numerique_neo.disponible():
            caps.append("numerique")
            # Banc de demodulation (2026-11-01) : AM/FM/BLU sur le meme IQ + preuve
            # de modulation ; le Pi lui confie alors ses candidats.
            import banc_demod  # noqa: F401
            caps.append("banc")
    except Exception:
        pass
    return caps


# --- juges du scanner radio (2026-10-25) ---------------------------------------------
# Le Pi confie un son brut du dongle et la liste des juges (modes de dsd-fme) a
# passer ; ils tournent ici en parallele, autant a la fois que la part libre de
# la machine le permet (puissance.py), et les verdicts repartent signes.
RADIO_DSD = os.path.join(HOME, "radio", "bin", "dsd-fme")


def _radio_juges(job, folder):
    from concurrent.futures import ThreadPoolExecutor
    import juges_radio
    p = job["params"]
    wav = os.path.join(folder, p["fichier"])
    juges = list(p.get("juges") or [])
    m = power(session_state()) or {}
    part = (m.get("disponible") or 0) / m["puissance"] if m.get("puissance") else 0.5
    workers = max(1, min(len(juges) or 1, int(round((m.get("coeurs") or 2) * part))))

    def un(juge):
        voix = os.path.join(folder, "voix_%s.wav" % juge.lstrip("-"))
        return juge, juges_radio.juger(juge, wav, dsd=RADIO_DSD, voix=voix, priorite=("nice", "-n", "10"),
                                       garder=bool(p.get("garder_voix")))

    verdicts, sorties = {}, ["verdicts.json"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for juge, (verdict, voix) in pool.map(un, juges):
            verdicts[juge] = verdict
            if voix:
                sorties.append(os.path.basename(voix))
    _write(os.path.join(folder, "verdicts.json"), verdicts)
    props = sum(1 for v in verdicts.values() if v.get("propose"))
    return "fait", sorties, "%d juges (%d a la fois), %d proposition%s" % (
        len(verdicts), workers, props, "s" if props > 1 else "")


# --- tickets partages (2026-11-04, tickets_partages.py) ----------------------------------
# Le Pi pose les sons sur le NAS ; ici on prend ce qu'on sait faire et on rend
# nos verdicts sur le ticket. Reveille par le Pi (message "tickets") ; le NAS
# n'est relu qu'au rythme du filet de securite (SCAN_SEC). Le Pi donne le nom du
# ticket : le cache SMB de macOS cache un dossier neuf une trentaine de secondes.
_tickets_reveil = threading.Event()
_tickets_attendus = {}             # {ticket annonce: on cesse de le guetter a}
TICKET_ATTENTE_SEC = 20


def _outil_radio(nom):
    chemin = os.path.join(HOME, "radio", "bin", nom)
    return chemin if os.access(chemin, os.X_OK) else None


def _outils_tickets():
    deco = os.path.join(HOME, "decodeurs")
    return {"dsd": _outil_radio("dsd-fme"), "multimon": _outil_radio("multimon-ng"),
            "direwolf": _outil_radio("direwolf"), "direwolf_conf": os.path.join(deco, "direwolf_donnees.conf"),
            "python_sstv": os.path.join(HOME, "py311", "bin", "python"),
            "sstv_script": os.path.join(deco, "sstv_veille.py"),
            "freedv": _outil_radio("freedv_rx"), "jt9": _outil_radio("jt9")}


def _part_de_tickets():
    m = power(session_state()) or {}
    part = (m.get("disponible") or 0) / m["puissance"] if m.get("puissance") else 0.5
    return max(1, int(round((m.get("coeurs") or 2) * part)))


def _tickets_boucle():
    import tickets_partages as tp
    sys.path.insert(0, os.path.join(HOME, "decodeurs"))
    outils = _outils_tickets()
    while True:
        _tickets_reveil.wait(1 if _tickets_attendus else SCAN_SEC)
        _tickets_reveil.clear()
        for tid, jusque in list(_tickets_attendus.items()):
            if time.time() > jusque:
                _tickets_attendus.pop(tid, None)
        conf = config()
        for pi, card in bridges().items():
            link = _links.get(pi)
            if not link or not link.direct or not card.get("cle"):
                continue  # tickets du lot 1 : signes par les cles v1 (le lot 2 les passera en v2)
            cles = {pi: card["cle"], conf["id"]: proto.public_b64(key())}
            try:
                share = find_share(card)
                if not share:
                    continue
                encore = True
                while encore:
                    encore = False
                    for tid in sorted(set(tp.ouverts(share)) | set(_tickets_attendus)):
                        if tp.existe(share, tid):
                            _tickets_attendus.pop(tid, None)
                        fait = tp.travailler(share, tid, conf["id"], conf["nom"], key(), cles, outils,
                                             _part_de_tickets(), priorite=("nice", "-n", "10"))
                        if fait:
                            encore = True
                            log("%s : ticket %s, %d epreuves" % (pi, tid, fait))
            except Exception:
                traceback.print_exc()


# --- voix macOS en direct (2026-10-22) ---------------------------------------------
# Le Pi demande "dire" {id, texte, voix, debit} par la liaison ; la machine fait
# parler "say" dans un fichier et renvoie le son en morceaux signes ("son", PCM
# 16 bits mono a SAY_RATE Hz, en base64), puis "son_fin". Aucun port a ouvrir :
# tout passe par la connexion que le Mac a deja etablie vers le Pi.
SAY = "/usr/bin/say"
SAY_RATE = 22050
SAY_CHUNK = 36000          # octets PCM par message (ligne < MAX_LINE une fois en base64)
SAY_WPM = 185              # debit "+0%" en mots par minute
SAY_LOCALES = ("fr_",)
_mac_voices = {"t": 0, "liste": []}


def mac_voices():
    """Noms des voix macOS en francais installees (liste relue toutes les heures)."""
    if not os.access(SAY, os.X_OK):
        return []
    if time.time() - _mac_voices["t"] > 3600:
        voices = []
        try:
            out = subprocess.run([SAY, "-v", "?"], capture_output=True, text=True, timeout=20).stdout
            for line in out.splitlines():
                m = re.match(r"^(.+?)\s+([a-z]{2}_[A-Z]{2})\s+#", line)
                if m and m.group(2).startswith(SAY_LOCALES):
                    voices.append(m.group(1).strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
        _mac_voices.update(t=time.time(), liste=voices)
    return _mac_voices["liste"]


def _say_rate(debit):
    try:
        pct = float(str(debit or "0").strip().rstrip("%") or 0)
    except ValueError:
        pct = 0.0
    return str(int(SAY_WPM * max(0.3, 1 + pct / 100)))


def say_to_pi(link, msg):
    import base64
    import tempfile
    import wave
    req = msg.get("id")
    voice = msg.get("voix")
    text = (msg.get("texte") or "").strip()
    if not req or voice not in mac_voices() or not text:
        link.send({"type": "son_fin", "reponse_a": req, "ok": False, "erreur": "voix ou texte invalide"})
        return
    tmp = tempfile.mkdtemp(prefix="voix_mac_")
    try:
        src, wav = os.path.join(tmp, "texte.txt"), os.path.join(tmp, "son.wav")
        with open(src, "w", encoding="utf-8") as f:
            f.write(text)
        subprocess.run([SAY, "-v", voice, "-r", _say_rate(msg.get("debit")), "-f", src, "-o", wav,
                        "--data-format=LEI16@%d" % SAY_RATE], capture_output=True, timeout=60)
        with wave.open(wav) as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError("format inattendu")
            n = 0
            while True:
                data = w.readframes(SAY_CHUNK // 2)
                if not data:
                    break
                if not link.send({"type": "son", "reponse_a": req, "n": n,
                                  "pcm": base64.b64encode(data).decode()}):
                    return
                n += 1
        link.send({"type": "son_fin", "reponse_a": req, "ok": True, "taux": SAY_RATE})
    except Exception as exc:
        log("voix mac : %r" % exc)
        link.send({"type": "son_fin", "reponse_a": req, "ok": False, "erreur": repr(exc)[:200]})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- TETRA en direct (2026-10-26) ---------------------------------------------------
# Le Pi sert son dongle par rtl_tcp pendant une fenetre du scanner et demande
# "tetra" {id, host, port, freq_hz, gain, secondes} par la liaison ; cette
# machine decode (dsd-neo -fT, bridge/tetra_neo.py), renvoie l'identite reseau
# (metadonnees en clair) par "tetra_resultat", et, si une voix EN CLAIR a ete
# synthetisee, la renvoie d'abord en morceaux "tetra_voix" (PCM 16 bits mono
# 48 kHz, base64) -- jamais rien de dechiffre. Le Pi ne decode jamais lui-meme.
TETRA_CHUNK = 36000        # octets PCM par message (ligne < MAX_LINE une fois en base64)


def tetra_to_pi(link, msg):
    import base64
    import wave
    import tetra_neo
    rid = msg.get("id")
    if not rid:
        return
    try:
        res = tetra_neo.decoder(msg["host"], msg["port"], msg["freq_hz"], msg.get("gain", 32),
                                msg.get("secondes", 12), ppm=msg.get("ppm", 0),
                                max_seconds=msg.get("max_secondes"))
    except Exception as exc:
        link.send({"type": "tetra_resultat", "reponse_a": rid, "ok": False, "erreur": repr(exc)[:200]})
        return
    tmp = res.pop("_tmp", None)
    voix_envoyee = False
    try:
        voix = res.get("voix_wav")
        if voix and os.path.exists(voix):
            try:
                with wave.open(voix) as w:
                    taux = w.getframerate()
                    ch = w.getnchannels()
                    # dsd-neo ecrit -w en STEREO (2 voies, 8 kHz) : on ramene en
                    # mono, sinon la voix TETRA en clair n'arrivait JAMAIS au Pi
                    # (doctrine : capter la voix TETRA a tout prix ; bug 2026-10-31).
                    if w.getsampwidth() == 2 and ch in (1, 2):
                        n = 0
                        while True:
                            data = w.readframes(TETRA_CHUNK // 2)
                            if not data:
                                break
                            if ch == 2:
                                import audioop
                                data = audioop.tomono(data, 2, 0.5, 0.5)
                            if not link.send({"type": "tetra_voix", "reponse_a": rid, "n": n,
                                              "taux": taux, "pcm": base64.b64encode(data).decode()}):
                                break
                            n += 1
                        voix_envoyee = n > 0
            except Exception as exc:
                log("tetra voix illisible : %r" % exc)
        link.send({"type": "tetra_resultat", "reponse_a": rid, "ok": bool(res.get("ok")),
                   "erreur": res.get("erreur"), "reseau": res.get("reseau") or {},
                   "bsch": res.get("bsch", 0), "sysinfo": res.get("sysinfo", 0),
                   "enc": res.get("enc") or [], "clair": bool(res.get("clair")),
                   "mac_resource": res.get("mac_resource", 0), "clip": bool(res.get("clip")),
                   "voix": voix_envoyee, "resume": tetra_neo.resume_fr(res),
                   "activite": res.get("activite") or {}, "duree": res.get("duree")})
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def numerique_to_pi(link, msg):
    """Decodage numerique en direct (DMR/P25... -fa, numerique_neo) demande
    par le Pi quand il delegue pour se refroidir. Renvoie les metadonnees en
    clair par "numerique_resultat" et, si une voix EN CLAIR a ete synthetisee,
    la renvoie d'abord en morceaux "numerique_voix" (PCM 16 bits mono, base64).
    Jamais rien de dechiffre. Calque de tetra_to_pi."""
    import base64
    import wave
    import numerique_neo
    rid = msg.get("id")
    if not rid:
        return
    try:
        res = numerique_neo.decoder(msg["host"], msg["port"], msg["freq_hz"], msg.get("gain", 32),
                                    msg.get("secondes", 8), ppm=msg.get("ppm", 0),
                                    max_seconds=msg.get("max_secondes"), mode=msg.get("mode", "-fa"),
                                    banc=bool(msg.get("banc")), modes_seul=msg.get("modes"))
    except Exception as exc:
        link.send({"type": "numerique_resultat", "reponse_a": rid, "ok": False, "erreur": repr(exc)[:200]})
        return
    tmp = res.pop("_tmp", None)

    def _streamer(chemin, kind):
        """Envoie un wav 16 bits en morceaux base64 (type `kind`), ramene en
        MONO. dsd-neo ecrit la voix de synthese (-w) en STEREO (2 voies, 8 kHz) :
        sans ce repli mono, la voix numerique en clair n'arrivait jamais au Pi
        (bug 2026-10-31). L'audio FM brut (-6) est deja mono. Rend le taux, ou
        None si rien envoye."""
        if not (chemin and os.path.exists(chemin)):
            return None
        try:
            with wave.open(chemin) as w:
                taux = w.getframerate()
                ch = w.getnchannels()
                if w.getsampwidth() != 2 or ch not in (1, 2):
                    return None
                n = 0
                while True:
                    data = w.readframes(TETRA_CHUNK // 2)
                    if not data:
                        break
                    if ch == 2:
                        import audioop
                        data = audioop.tomono(data, 2, 0.5, 0.5)
                    if not link.send({"type": kind, "reponse_a": rid, "n": n,
                                      "taux": taux, "pcm": base64.b64encode(data).decode()}):
                        break
                    n += 1
                return taux if n > 0 else None
        except Exception as exc:
            log("%s illisible : %r" % (kind, exc))
            return None

    voix_envoyee = False
    audio_envoye = False
    try:
        # Voix NUMERIQUE en clair (-w) et/ou audio ANALOGIQUE (-6) : les deux
        # flux possibles d'une demod complete. Jamais rien de dechiffre.
        voix_envoyee = _streamer(res.get("voix_wav"), "numerique_voix") is not None
        audio_envoye = _streamer(res.get("audio_wav"), "numerique_audio") is not None
        flux_envoye = _streamer(res.get("flux_wav"), "numerique_flux") is not None
        # Banc (2026-11-01) : un son par modulation, chacun sous son propre type.
        banc_envoye = [m for m, chemin in sorted((res.get("banc") or {}).items())
                       if _streamer(chemin, "numerique_banc_" + m) is not None]
        # Chaque juge numerique qui a une voix en clair, pas seulement le gagnant.
        rendus_meta = []
        for r in res.get("rendus") or []:
            mode = str(r.get("mode") or "")
            envoye = False
            if r.get("voix") and not r.get("chiffre"):
                envoye = _streamer(r.get("wav"), "numerique_rendu_" + mode.lstrip("-")) is not None
            rendus_meta.append({"mode": mode, "protocole": r.get("protocole"),
                                "confirme": bool(r.get("confirme")), "hits": r.get("hits") or 0,
                                "rms": r.get("rms") or 0, "chiffre": bool(r.get("chiffre")),
                                "voix": envoye})
        link.send({"type": "numerique_resultat", "reponse_a": rid, "ok": bool(res.get("ok")),
                   "erreur": res.get("erreur"), "protocole": res.get("protocole"),
                   "meta": res.get("meta") or {}, "chiffre": bool(res.get("chiffre")),
                   "clair": bool(res.get("clair")), "voix": voix_envoyee, "audio": audio_envoye, "flux": flux_envoye,
                   "numerique_confirme": bool(res.get("numerique_confirme")),
                   "digital_actif": bool(res.get("digital_actif")), "sync_hits": res.get("sync_hits"),
                   "voix_rms": res.get("voix_rms"), "audio_rms": res.get("audio_rms"),
                   "resume": numerique_neo.resume_fr(res), "duree": res.get("duree"),
                   "banc": banc_envoye, "banc_preuve": res.get("banc_preuve"), "preuve": res.get("preuve"),
                   "rendus": rendus_meta})
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


_last_locked = {}


def session_state():
    """Etat de la session macOS, envoye au Pi a chaque battement (2026-10-14) :
    ecran verrouille ou non, et depuis combien de secondes personne n'a touche
    clavier ni souris. Lu par ioreg : le Python des outils de developpement n'a
    pas PyObjC. Un changement de verrouillage est note au journal du pont."""
    import plistlib
    state = {}
    try:
        out = subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, timeout=5).stdout
        users = plistlib.loads(out).get("IOConsoleUsers", [])
        me = next((u for u in users if u.get("kCGSSessionOnConsoleKey")), None)
        state["session_ouverte"] = me is not None
        state["verrouille"] = bool(me and me.get("CGSSessionScreenIsLocked"))
    except Exception:
        pass
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"HIDIdleTime" = (\d+)', out)
        if m:
            state["inactif_s"] = int(m.group(1)) // 1_000_000_000
    except Exception:
        pass
    if "verrouille" in state and _last_locked.get("v") != state["verrouille"]:
        if "v" in _last_locked:
            log("ecran %s" % ("verrouille" if state["verrouille"] else "deverrouille"))
        _last_locked["v"] = state["verrouille"]
    return state


def power(etat=None):
    """Puissance reelle et disponibilite du moment (puissance.py), envoyees au Pi
    a chaque battement (2026-10-25) : il confie chaque travail a la machine qui
    finira le plus tot. Jamais bloquant : une mesure ratee n'envoie rien."""
    try:
        import puissance
        return puissance.mesure(os.path.join(HOME, "puissance_etalon.json"), etat)
    except Exception:
        traceback.print_exc()
        return None


def _attributed_elsewhere(folder):
    """Le Pi a confie ce travail a une AUTRE machine (attribution.json, 2026-10-25),
    et elle a encore le temps de le prendre : on le lui laisse."""
    a = _read(os.path.join(folder, "attribution.json"))
    return bool(a and a.get("pour") not in (None, config()["id"]) and a.get("jusqu", 0) > time.time())


def network_identity():
    """(adresse materielle, [ips]) de l'interface de la route par defaut."""
    iface, mac, ips = None, None, []
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True).stdout
            m = re.search(r"interface:\s*(\S+)", out)
            iface = m.group(1) if m else None
            out = subprocess.run(["ifconfig", iface], capture_output=True, text=True).stdout if iface else ""
            m = re.search(r"ether\s+([0-9a-f:]{17})", out)
            mac = m.group(1) if m else None
            ips = re.findall(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        else:
            out = subprocess.run(["ip", "-o", "route", "get", "1.1.1.1"], capture_output=True, text=True).stdout
            m = re.search(r"dev\s+(\S+).*src\s+(\S+)", out)
            if m:
                iface, ips = m.group(1), [m.group(2)]
                with open("/sys/class/net/%s/address" % iface) as f:
                    mac = f.read().strip()
    except (OSError, IndexError):
        pass
    return mac, ips


def find_share(card=None):
    """Dossier du pont sur le NAS ; essaie de remonter le partage s'il manque."""
    candidates = ([card["partage"]] if card and card.get("partage") else []) + SHARE_GUESSES
    for path in candidates:
        if os.path.isdir(os.path.dirname(path)):
            os.makedirs(path, exist_ok=True)
            return path
    if card and card.get("montage") and sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'mount volume "%s"' % card["montage"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if os.path.isdir(os.path.dirname(card["partage"])):
            return card["partage"]
    return None


def _mount_url(path):
    """URL smb:// du partage qui contient path (pour le remonter apres la veille)."""
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True).stdout
    except OSError:
        return None
    best = None
    for line in out.splitlines():
        m = re.match(r"//(\S+?) on (\S+) \((smbfs|cifs)", line)
        if m and path.startswith(m.group(2) + "/") and (not best or len(m.group(2)) > len(best[1])):
            best = ("smb://" + m.group(1).split(";")[-1], m.group(2))
    return best[0] if best else None


# --- appairage ------------------------------------------------------------------------

def pairing_wait():
    """Etape 1 : rend ("OK", pi, code) ou ("ERREUR", message)."""
    conf = config()
    share = find_share()
    if not share:
        return "ERREUR", "Je ne trouve pas le disque partagé du NAS. Monte le partage server, puis recommence."
    if pont2 is not None:
        return _pairing_wait_v2(conf, share)
    return _pairing_wait_v1(conf, share)


def _pairing_wait_v1(conf, share):
    request_path = os.path.join(share, "appairage", "demande.json")
    deadline = time.time() + PAIRING_WAIT_SEC
    while time.time() < deadline:
        request = _read(request_path)
        if request and request.get("cle") and time.time() - request.get("t", 0) < PAIRING_WAIT_SEC:
            break
        time.sleep(2)
    else:
        return "ERREUR", "Le Pi ne s'est pas manifesté en trois minutes. Appairage abandonné."
    my_pub = proto.public_b64(key())
    mac, ips = network_identity()
    _write(os.path.join(share, "appairage", "reponse-%s.json" % conf["id"]),
           {"id": conf["id"], "nom": conf["nom"], "cle": my_pub, "capacites": capabilities(),
            "adresse_materielle": mac, "ips": ips, "systeme": platform.platform(terse=True), "t": time.time()})
    code = proto.pairing_code(request["cle"], my_pub)
    _write(PENDING, {"demande": request, "partage": share, "code": code, "t": time.time()})
    return "OK", request.get("pi", "inconnu"), " ".join(code)


def _accord(pending):
    """Accord du Pi : "ok" (valide et enregistre), "faux", ou None (pas encore)."""
    conf = config()
    request, share = pending["demande"], pending["partage"]
    accord_path = os.path.join(share, "appairage", "accord-%s.json" % conf["id"])
    accord = _read(accord_path)
    if not accord:
        return None
    if not (accord.get("compagnon") == conf["id"] and accord.get("cle_compagnon") == proto.public_b64(key())
            and proto.verify(request["cle"], accord, max_age=None)):
        return "faux"
    pi = request["pi"]
    _write(os.path.join(BRIDGES, pi + ".json"),
           {"pi": pi, "cle": request["cle"], "hote": request.get("hote"), "ip": request.get("ip"),
            "port": request.get("port", proto.PORT), "partage": share, "montage": _mount_url(share),
            "appaire": time.time()})
    _remove(accord_path)
    return "ok"


def _ask(question, wait_while):
    """Question Oui / Non dans un dialogue systeme (langue du systeme, lu par
    VoiceOver). Le dialogue est ferme tout seul si wait_while() rend False
    (l'autre machine a deja tranche) : rend "Oui", "Non" ou None."""
    script = ('activate\ndisplay dialog %s with title "Appairage" buttons {"Non", "Oui"} default button "Oui"'
              % json.dumps(question, ensure_ascii=False))
    proc = subprocess.Popen(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True)
    while proc.poll() is None:
        if not wait_while():
            proc.terminate()
            return None
        time.sleep(0.5)
    out = proc.stdout.read()
    return "Oui" if "Oui" in out else "Non"


def pairing_decide():
    """Etape 2 : dialogue du code. Valider d'un seul cote suffit : Oui ici
    (le Pi suit tout seul), ou Entree sur le Pi (ce dialogue se ferme)."""
    conf = config()
    pending = _read(PENDING)
    if not pending:
        return "ERREUR", "Aucun appairage en cours."
    if pending.get("v2"):
        return _pairing_decide_v2(conf, pending)
    request, share = pending["demande"], pending["partage"]
    pi = request.get("pi", "inconnu")
    done = "Pont créé avec %s : nos clés sont échangées. Le service s'y connecte dans quelques secondes." % pi
    state = {}

    def still_waiting():
        state["accord"] = _accord(pending)
        return state["accord"] is None

    try:
        answer = _ask("Pi trouvé : %s.\nCode : %s.\n\nSi le Pi affiche le même code, valide ici ou sur la console "
                      "du Pi : l'autre suivra tout seul." % (pi, " ".join(pending["code"])), still_waiting)
        if answer is None:  # le Pi a tranche pendant que le dialogue etait ouvert
            if state.get("accord") == "ok":
                return "OK", "Le Pi a validé le code. " + done
            return "ERREUR", "L'accord reçu n'est pas signé par ce Pi. Rien n'est enregistré."
        if answer == "Non":
            _write(os.path.join(share, "appairage", "refus-%s.json" % conf["id"]), {"t": time.time()})
            return "OK", "Appairage refusé, rien n'est enregistré. Le Pi ferme son dialogue tout seul."
        _write(os.path.join(share, "appairage", "confirme-%s.json" % conf["id"]),
               proto.sign(key(), {"type": "confirme", "pi": pi, "compagnon": conf["id"], "cle_pi": request["cle"]}))
        deadline = time.time() + 60
        while time.time() < deadline:
            result = _accord(pending)
            if result == "ok":
                return "OK", done
            if result == "faux":
                return "ERREUR", "L'accord reçu n'est pas signé par ce Pi. Rien n'est enregistré."
            time.sleep(0.5)
        return "ERREUR", "Le Pi n'a pas répondu. Est-il toujours en mode appairage ?"
    finally:
        _remove(PENDING)


# --- appairage v2 (Prismaforge/specs/protocole-v2.md §3 ; lib/pont_v2.py cote Pi) ---------
# Code de 6 chiffres, dit en 3 paires ; la personne valide sur LES DEUX machines. On
# s'engage sur notre cle avant de connaitre l'alea du Pi.

def _pairing_wait_v2(conf, share):
    dossier = os.path.join(share, "appairage", "v2")
    deadline = time.time() + PAIRING_WAIT_SEC
    ouvert = None
    while time.time() < deadline:
        ouvert = _read(os.path.join(dossier, "ouvert.json"))
        if ouvert and ouvert.get("parc") and time.time() - ouvert.get("t", 0) < PAIRING_WAIT_SEC:
            break
        if _read(os.path.join(share, "appairage", "demande.json")):
            break  # Pi encore en v1 : ancien appairage
        time.sleep(2)
    else:
        return "ERREUR", "Le Pi ne s'est pas manifesté en trois minutes. Appairage abandonné."
    if not ouvert or not ouvert.get("parc"):
        return _pairing_wait_v1(conf, share)
    os.makedirs(V2_DIR, mode=0o700, exist_ok=True)
    moi = pont2.Coffre.charger_ou_creer(os.path.join(V2_DIR, "identite.pem"), "p256")
    transport = pont2.CleTransport()
    rejoignant = pont2.Rejoignant(moi.cle_publique(), transport.public)
    n = uuid.uuid4().hex[:12]
    _write(os.path.join(dossier, "engagement-%s.json" % n),
           {"engagement": rejoignant.engagement, "compagnon": conf["id"], "nom": conf["nom"],
            "capacites": capabilities(), "niveau": moi.niveau, "t": time.time()})
    presentation = None
    while time.time() < deadline:
        presentation = _read(os.path.join(dossier, "presentation-%s.json" % n))
        if presentation:
            break
        time.sleep(1)
    else:
        return "ERREUR", "Le Pi n'a pas répondu à notre engagement. Appairage abandonné."
    try:
        ma_presentation, code = rejoignant.recevoir(presentation.get("presentation"))
    except pont2.ErreurPont as exc:
        return "ERREUR", "Réponse du Pi invalide : %s" % exc
    _write(os.path.join(dossier, "revelation-%s.json" % n), {"presentation": ma_presentation})
    # La cle de transport du premier certificat doit survivre jusqu'a l'etape 2.
    attente = os.path.join(V2_DIR, "appairage")
    os.makedirs(attente, mode=0o700, exist_ok=True)
    transport.enregistrer(os.path.join(attente, "transport.cle"))
    _write(PENDING, {"v2": True, "n": n, "partage": share, "code": code, "ouvert": ouvert,
                     "ordonnateur": presentation["presentation"]["identite"], "t": time.time()})
    return "OK", ouvert.get("pi", "inconnu"), pont2.code_parle(code)


def _pairing_decide_v2(conf, pending):
    share, n, ouvert = pending["partage"], pending["n"], pending["ouvert"]
    dossier = os.path.join(share, "appairage", "v2")
    try:
        answer = _ask("Pi trouvé : %s.\nCode : %s.\n\nSi le Pi dit le même code, valide ici, puis appuie sur "
                      "Entrée sur la console du Pi." % (ouvert.get("pi", "inconnu"), pont2.code_parle(pending["code"])),
                      lambda: True)
        if answer != "Oui":
            _write(os.path.join(dossier, "refus-%s.json" % n), {"t": time.time()})
            return "OK", "Appairage refusé, rien n'est enregistré. Le Pi ferme son dialogue tout seul."
        deadline = time.time() + 120
        while time.time() < deadline:
            reponse = _read(os.path.join(dossier, "certificat-%s.json" % n))
            if reponse:
                break
            time.sleep(1)
        else:
            return "ERREUR", "Le Pi n'a pas validé dans les deux minutes. Rien n'est enregistré."
        import base64
        parc = pending["ordonnateur"]
        if pont2.identifiant(parc) != reponse.get("parc"):
            return "ERREUR", "Le certificat ne vient pas du Pi dont on a comparé le code. Rien n'est enregistré."
        transport = pont2.CleTransport.charger(os.path.join(V2_DIR, "appairage", "transport.cle"))
        cert = reponse.get("objet")
        try:
            c = pont2.verifier_certificat(cert, parc, reponse["parc"], pont2.Revocations(), True)
            moi = pont2.Coffre.ouvrir(os.path.join(V2_DIR, "identite.pem"))
            if c["identite"] != moi.cle_publique() or base64.b64decode(c["transport"]) != transport.public:
                raise pont2.CertificatRefuse("ce certificat n'est pas le mien")
        except pont2.ErreurPont as exc:
            return "ERREUR", "Certificat refusé : %s. Rien n'est enregistré." % exc
        pi = reponse["parc"]
        _write(os.path.join(V2_DIR, "ponts", "%s.json" % pi), {"ordonnateur": parc, "parc": pi, "epingle": time.time()})
        _garder_v2(pi, transport, cert, int(reponse.get("port") or 47812))
        _write(os.path.join(BRIDGES, pi + ".json"),
               {"pi": pi, "nom": ouvert.get("pi"), "v2": True, "hote": ouvert.get("hote"), "ip": ouvert.get("ip"),
                "partage": share, "montage": _mount_url(share), "appaire": time.time()})
        return "OK", ("Pont créé avec %s (protocole v2) : nos clés sont échangées. Le service s'y connecte dans "
                      "quelques secondes." % ouvert.get("pi", pi))
    finally:
        _remove(PENDING)
        _remove(os.path.join(V2_DIR, "appairage", "transport.cle"))


def pairing_terminal():
    result = pairing_wait()
    if result[0] != "OK":
        print(result[1])
        return 1
    result = pairing_decide()
    print(result[1])
    return 0 if result[0] == "OK" else 1


def forget(pi):
    """Supprime le pont ; le Pi est prevenu par un message signe sur le NAS."""
    conf = config()
    card = bridges().get(pi)
    if not card:
        return "ERREUR", "Pas de pont avec %s." % pi
    share = find_share(card)
    epingle = _read(os.path.join(V2_DIR, "ponts", "%s.json" % pi))
    if share and pont2 is not None and epingle and os.path.exists(os.path.join(V2_DIR, "identite.pem")):
        moi = pont2.Coffre.ouvrir(os.path.join(V2_DIR, "identite.pem"))  # retrait signe en v2
        _write(os.path.join(share, "revocations", conf["id"] + ".json"),
               moi.signer_objet({"type": "oubli", "parc": epingle["parc"], "id": "oubli-" + conf["id"],
                                 "compagnon": conf["id"], "pi": card.get("nom") or pi}))
    elif share:
        _write(os.path.join(share, "revocations", conf["id"] + ".json"),
               proto.sign(key(), {"type": "oubli", "compagnon": conf["id"], "pi": pi}))
    _remove(os.path.join(BRIDGES, pi + ".json"))
    _remove(os.path.join(STATS, pi + ".json"))
    _remove(os.path.join(_dossier_session(pi), "certificat.json"))
    _remove(os.path.join(_dossier_session(pi), "transport.cle"))
    return "OK", ("Pont avec %s supprimé%s." % (pi, "" if share else
                  ", mais le disque du NAS est absent : le Pi ne le saura qu'au prochain essai"))


# --- service ----------------------------------------------------------------------------

# Deux files : les rafales LoRa (petites, a annoncer tout de suite) ne doivent
# pas attendre derriere un passage Meteor de plusieurs minutes.
FAST = ("lora", "decrire_images", "radio_juges", "examen")
_queues = {"rapide": queue.Queue(), "lourd": queue.Queue()}
_links = {}  # pi -> Link


def _wake_workers(pi):
    # Au plus 2 avis en file : un decrire_images ne doit pas attendre le
    # balayage NAS (5 min) si le premier claim a rate (retard SMB).
    for q in _queues.values():
        if q.qsize() < 2:
            q.put(pi)


def _nas_beat(pi, card):
    """Signe de vie signe sur le NAS : le Pi sait que la machine est la meme
    sans liaison directe (macOS peut refuser le reseau local a l'application,
    jamais le disque partage)."""
    share = find_share(card)
    if not share:
        return
    conf = config()
    mac, ips = network_identity()
    _write(os.path.join(share, "compagnons", "%s.battement.json" % conf["id"]),
           proto.sign(key(), {"type": "battement", "compagnon": conf["id"], "pi": pi, "nom": conf["nom"],
                              "capacites": capabilities(), "adresse_materielle": mac, "ips": ips,
                              "puissance": power(session_state())}))


class Link(threading.Thread):
    """Connexion au Pi : presentation signee, battements, avis de travail."""

    def __init__(self, pi):
        threading.Thread.__init__(self, daemon=True)
        self.pi = pi
        self.channel = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.transport_v2 = None
        self.pilote_v2 = None
        self.relance_v2 = RELANCE_V2_SEC[0]

    def send(self, msg, v1=False):
        """Vers le Pi : par la session v2 si elle est ouverte (liste fermee, chiffre),
        sinon -- ou si la v2 refuse ce message -- par la liaison v1. v1=True : la v1
        seulement (battements de presence de la liaison v1)."""
        pilote = self.pilote_v2
        if not v1 and pilote is not None and pilote.envoyer(msg):
            return True
        with self.lock:
            if self.channel:
                try:
                    self.channel.send(msg)
                    return True
                except OSError:
                    self.channel.close()
        return False

    @property
    def direct(self):
        """Liaison directe ouverte (v1, ou session v2)."""
        pilote = self.pilote_v2
        return self.channel is not None or (pilote is not None and pilote.etablie and not pilote.fin.is_set())

    def shutdown(self):
        """Pont supprime : coupe la liaison tout de suite."""
        self.stop.set()
        with self.lock:
            if self.channel:
                self.channel.close()
        if self.pilote_v2:
            self.pilote_v2.arreter()

    def _connect(self, card):
        self.last_error = None
        for host in (card.get("hote"), card.get("ip")):
            if not host:
                continue
            try:
                sock = socket.create_connection((host, card.get("port", proto.PORT)), timeout=10)
            except OSError as exc:
                self.last_error = "%s : %s" % (host, exc)
                continue
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            return sock
        return None

    def _session(self, card):
        conf = config()
        sock = self._connect(card)
        if not sock:
            error = "Pi injoignable (%s)" % self.last_error
            if stats(self.pi).get("erreur") != error:
                log("%s : %s" % (self.pi, error))
            update_stats(self.pi, connecte=False, erreur=error)
            return False
        channel = proto.Channel(sock, key())
        mac, ips = network_identity()
        etat = session_state()
        hello = {"type": "bonjour", "compagnon": conf["id"], "pi": self.pi, "nom": conf["nom"],
                 "capacites": capabilities(), "adresse_materielle": mac, "ips": ips, "nonce": uuid.uuid4().hex,
                 "etat": etat, "voix_mac": mac_voices(), "puissance": power(etat)}
        if pont2 is not None:
            hello["v2"] = True  # le Pi lancera la migration s'il sait la faire
        channel.send(hello)
        welcome = channel.receive(card["cle"])
        if not welcome or welcome.get("type") != "bienvenue" or welcome.get("reponse_a") != hello["nonce"]:
            channel.close()
            update_stats(self.pi, connecte=False, erreur="le Pi n'a pas reconnu cette machine")
            return False
        sock.settimeout(None)
        with self.lock:
            self.channel = channel
        update_stats(self.pi, connecte=True, depuis=time.time(), erreur=None)
        log("connecte a %s" % self.pi)
        _wake_workers(self.pi)  # ce qui attendait
        beat = threading.Thread(target=self._beat, daemon=True)
        beat.start()
        try:
            while not self.stop.is_set():
                msg = channel.receive(card["cle"])
                if msg is None:
                    break
                if msg.get("type") in ("migration_v2", "certificat_v2", "migration_v2_refusee") and pont2:
                    self._v2(card, msg)
                else:
                    self.recu(msg)
        except (OSError, ValueError) as exc:
            log("%s : liaison coupee (%r)" % (self.pi, exc))
        finally:
            with self.lock:
                self.channel = None
            channel.close()
            if self.pilote_v2:
                self.pilote_v2.arreter()
                self.pilote_v2 = None
            update_stats(self.pi, connecte=False, vu=time.time())
        return True

    def recu(self, msg):
        """Un message du Pi, arrive par la v1 ou par la v2 (deja traduit)."""
        t = msg.get("type")
        if t == "travail":
            log("%s : travail %s annonce" % (self.pi, msg.get("travail")))
            _wake_workers(self.pi)
        elif t == "tickets":
            for tid in msg.get("ids") or []:
                _tickets_attendus[tid] = time.time() + TICKET_ATTENTE_SEC
            _tickets_reveil.set()
        elif t == "dire":
            threading.Thread(target=say_to_pi, args=(self, msg), daemon=True).start()
        elif t == "tetra":
            threading.Thread(target=tetra_to_pi, args=(self, msg), daemon=True).start()
        elif t == "numerique":
            threading.Thread(target=numerique_to_pi, args=(self, msg), daemon=True).start()

    # --- protocole v2 (Prismaforge/specs/protocole-v2.md §8-9) ------------------------

    def _v2(self, card, msg):
        """Migration par le canal v1 : le Pi annonce sa cle v2 (contresignee par sa
        cle v1, celle de la fiche), on l'epingle et on annonce la notre ; il rend
        un certificat v2, on ouvre la session Noise a cote de la v1."""
        try:
            if msg["type"] == "migration_v2":
                objet = msg.get("objet") or {}
                parc = (objet.get("charge") or {}).get("parc")
                m = pont2.verifier_migration(objet, parc, {"alg": "ed25519", "pub": card["cle"]})
                if pont2.identifiant(m["identite"]) != parc:
                    raise pont2.MigrationRefusee("le parc n'est pas l'identifiant de l'ordonnateur")
                chemin = os.path.join(V2_DIR, "ponts", "%s.json" % self.pi)
                epingle = _read(chemin)
                if epingle and epingle.get("ordonnateur") != m["identite"]:
                    raise pont2.MigrationRefusee("la cle v2 du Pi a change : refuse (a verifier a la main)")
                if not epingle:
                    _write(chemin, {"ordonnateur": m["identite"], "parc": parc, "epingle": time.time()})
                    log("%s : cle v2 du Pi epinglee (parc %s)" % (self.pi, parc))
                self._annoncer_migration(parc)
            elif msg["type"] == "certificat_v2":
                epingle = _read(os.path.join(V2_DIR, "ponts", "%s.json" % self.pi))
                if not epingle or self.transport_v2 is None:
                    return
                cert = msg.get("objet")
                c = pont2.verifier_certificat(cert, epingle["ordonnateur"], epingle["parc"], pont2.Revocations(), True)
                moi = pont2.Coffre.ouvrir(os.path.join(V2_DIR, "identite.pem"))
                import base64
                if c["identite"] != moi.cle_publique() or base64.b64decode(c["transport"]) != self.transport_v2.public:
                    raise pont2.CertificatRefuse("ce certificat n'est pas le mien")
                _garder_v2(self.pi, self.transport_v2, cert, int(msg.get("port") or 47812))
                # v2 seule desormais (2026-11-16) : la liaison v1 s'arrete, LiaisonV2 prend
                # le relais avec ce certificat ; il se renouvelle ensuite dans la session.
                log("%s : migration v2 terminee, la v1 n'est plus utilisee" % self.pi)
                self.stop.set()
                _basculer_v2(self.pi)
                with self.lock:
                    if self.channel:
                        self.channel.close()
            else:
                log("%s : migration v2 refusee par le Pi : %s" % (self.pi, msg.get("raison")))
        except pont2.ErreurPont as exc:
            log("%s : v2 refusee : %s" % (self.pi, exc))
            update_stats(self.pi, v2=False, erreur_v2=str(exc))

    def _annoncer_migration(self, parc):
        """Notre objet "migration" (cle v2 contresignee par la cle v1), par la v1 :
        le Pi rend un certificat v2 de 12 h pour une cle de transport neuve."""
        os.makedirs(V2_DIR, mode=0o700, exist_ok=True)
        moi = pont2.Coffre.charger_ou_creer(os.path.join(V2_DIR, "identite.pem"), "p256")
        self.transport_v2 = pont2.CleTransport()
        objet = pont2.preparer_migration(pont2.Coffre.ouvrir(KEY_PATH), moi, parc, config()["nom"],
                                         self.transport_v2.public)
        self.send({"type": "migration_v2", "objet": objet}, v1=True)

    def v2_terminee(self, etablie):
        """La session v2 est tombee (ou n'a pas pu s'ouvrir : pare-feu, Pi redemarre,
        certificat expire). Tant que la v1 vit, on redemande un certificat neuf et
        on retente, de plus en plus espace (RELANCE_V2_SEC)."""
        if etablie:
            self.relance_v2 = RELANCE_V2_SEC[0]
        delai = self.relance_v2
        self.relance_v2 = min(self.relance_v2 * 2, RELANCE_V2_SEC[1])
        minuteur = threading.Timer(delai, self._relancer_v2)
        minuteur.daemon = True
        minuteur.start()

    def _relancer_v2(self):
        if self.stop.is_set() or self.channel is None or pont2 is None:
            return
        if self.pilote_v2 is not None and self.pilote_v2.session is not None and not self.pilote_v2.fin.is_set():
            return  # une session est revenue entre-temps
        epingle = _read(os.path.join(V2_DIR, "ponts", "%s.json" % self.pi))
        if not epingle:
            return
        try:
            self._annoncer_migration(epingle["parc"])
        except pont2.ErreurPont as exc:
            log("%s : relance v2 impossible : %s" % (self.pi, exc))

    def _beat(self):
        while not self.stop.is_set():
            with self.lock:
                channel = self.channel
            if channel is None:
                return
            etat = session_state()
            if not self.send({"type": "battement", "capacites": capabilities(), "etat": etat,
                              "voix_mac": mac_voices(), "puissance": power(etat)}, v1=True):
                return
            time.sleep(BEAT_SEC)

    def run(self):
        delay = RECONNECT_SEC[0]
        while not self.stop.is_set():
            card = bridges().get(self.pi)
            if not card:
                return  # pont supprime
            try:
                if self._session(card):
                    delay = RECONNECT_SEC[0]
            except (OSError, ValueError) as exc:
                update_stats(self.pi, connecte=False, erreur="Pi injoignable (%s)" % exc.__class__.__name__)
            self.stop.wait(delay)
            delay = min(delay * 2, RECONNECT_SEC[1])


def _dossier_session(pi):
    return os.path.join(V2_DIR, "session", pi)


def _garder_v2(pi, transport, cert, port):
    """Cle de transport (0600) et certificat v2 : la machine se reconnecte apres un
    redemarrage sans repasser par la v1."""
    dossier = _dossier_session(pi)
    os.makedirs(dossier, mode=0o700, exist_ok=True)
    transport.enregistrer(os.path.join(dossier, "transport.cle"))
    _write(os.path.join(dossier, "certificat.json"), {"objet": cert, "port": port, "t": time.time()})


def _etat_v2(pi):
    """(epingle, transport, certificat, port) si ce pont tourne en v2 seule, sinon None."""
    if pont2 is None:
        return None
    epingle = _read(os.path.join(V2_DIR, "ponts", "%s.json" % pi))
    c = _read(os.path.join(_dossier_session(pi), "certificat.json"))
    chemin = os.path.join(_dossier_session(pi), "transport.cle")
    if not epingle or not c or not os.path.exists(chemin) or not os.path.exists(os.path.join(V2_DIR, "identite.pem")):
        return None
    try:
        return epingle, pont2.CleTransport.charger(chemin), c["objet"], int(c.get("port") or 47812)
    except (pont2.ErreurPont, KeyError, TypeError, ValueError):
        return None


def _basculer_v2(pi):
    nouvelle = LiaisonV2(pi)
    _links[pi] = nouvelle
    nouvelle.start()


class LiaisonV2(Link):
    """Pont en v2 seule (2026-11-16) : plus de liaison v1. Session Noise avec le
    certificat garde ; renouvele a mi-vie dans la session ; apres une longue veille
    (certificat expire), le Pi ouvre une session de renouvellement seulement."""

    def v2_terminee(self, etablie):
        pass  # la boucle de run() se reconnecte elle-meme

    def run(self):
        delay = RECONNECT_SEC[0]
        while not self.stop.is_set():
            card = bridges().get(self.pi)
            if not card:
                return  # pont supprime
            etat = _etat_v2(self.pi)
            if not etat:
                log("%s : etat v2 introuvable, retour a la liaison v1" % self.pi)
                return  # la boucle du service relancera une liaison v1
            epingle, transport, cert, port = etat
            pilote = PiloteV2(self, card, epingle, transport, cert, port)
            self.pilote_v2 = pilote
            pilote.run()  # dans ce fil : rend la main quand la session tombe
            self.pilote_v2 = None
            update_stats(self.pi, connecte=False, vu=time.time())
            if pilote.renouvele:
                delay = RECONNECT_SEC[0]
                continue  # certificat neuf : reconnexion tout de suite
            if pilote.etablie:
                delay = RECONNECT_SEC[0]
            elif card.get("hote") or card.get("ip"):
                update_stats(self.pi, erreur="Pi injoignable en v2")
            self.stop.wait(delay)
            delay = min(delay * 2, RECONNECT_SEC[1])


class PiloteV2(threading.Thread):
    """Session v2 (Noise, port 47812) a cote de la liaison v1. Tout le trafic y
    passe (Link.send essaie la v2 d'abord) ; la v1 garde l'amorce, ses battements
    de presence, et sert de secours pour un message que la v2 refuserait."""

    def __init__(self, link, card, epingle, transport, certificat, port):
        threading.Thread.__init__(self, daemon=True)
        self.link, self.pi, self.card, self.epingle = link, link.pi, card, epingle
        self.transport, self.certificat, self.port = transport, certificat, port
        self.session = None
        self.hote = None
        self.fin = threading.Event()
        self.voulu = False  # arrete par la liaison (v1 coupee, certificat remplace) : pas de relance
        self.refus_vus = set()
        self.etablie = False
        self.renouvele = False   # certificat neuf recu : se reconnecter tout de suite avec lui
        self.demande = None      # cle de transport neuve en attente de son certificat
        try:
            self.expire = int(certificat["charge"]["expire"])
        except (KeyError, TypeError, ValueError):
            self.expire = 0

    def arreter(self, voulu=True):
        self.voulu = self.voulu or voulu
        self.fin.set()
        if self.session:
            self.session.fermer()

    def envoyer(self, msg):
        """Message v1 traduit vers la liste fermee v2 et envoye. False : pas de
        session, ou message refuse (l'appelant passe par la v1)."""
        session = self.session
        if session is None or self.fin.is_set():
            return False
        try:
            session.envoyer(trad.vers_v2(msg))
            return True
        except (trad.NonTraduisible, pont2.MessageRefuse) as exc:
            if msg.get("type") not in self.refus_vus:
                self.refus_vus.add(msg.get("type"))
                log("%s : %s passe par la v1 (%s)" % (self.pi, msg.get("type"), exc))
            return False
        except pont2.ErreurPont as exc:
            log("%s : envoi v2 impossible (%s), la v1 prend le relais" % (self.pi, exc))
            self.arreter(voulu=False)
            return False

    def _battement(self):
        etat = session_state()
        mac, ips = network_identity()
        return {"type": "battement", "capacites": capabilities(), "etat": etat, "voix_mac": mac_voices(),
                "puissance": power(etat), "occupe": bool(stats(self.pi).get("en_cours")),
                "reseau": {"adresse_materielle": mac, "ips": ips}}

    def _demander_certificat(self):
        """Certificat neuf pour une cle de transport neuve, avec la preuve signee par
        notre cle d'identite (spec §4.1). A mi-vie, ou tout de suite s'il a expire."""
        import base64
        neuve = pont2.CleTransport()
        moi = pont2.Coffre.ouvrir(os.path.join(V2_DIR, "identite.pem"))
        preuve = pont2.preparer_renouvellement(moi, self.epingle["parc"], neuve.public)
        self.demande = neuve
        self.session.envoyer({"type": "certificat_demande", "id": uuid.uuid4().hex[:16],
                              "transport": base64.b64encode(neuve.public).decode(), "preuve": preuve})
        log("%s : certificat v2 demande (expire %s)" % (self.pi, time.strftime("%d/%m %H:%M", time.localtime(self.expire))))

    def _certificat_recu(self, msg):
        import base64
        neuve = self.demande
        if neuve is None:
            return
        cert = msg.get("objet")
        c = pont2.verifier_certificat(cert, self.epingle["ordonnateur"], self.epingle["parc"], pont2.Revocations(), True)
        moi = pont2.Coffre.ouvrir(os.path.join(V2_DIR, "identite.pem"))
        if c["identite"] != moi.cle_publique() or base64.b64decode(c["transport"]) != neuve.public:
            raise pont2.CertificatRefuse("ce certificat n'est pas le mien")
        _garder_v2(self.pi, neuve, cert, self.port)
        log("%s : certificat v2 renouvele, reconnexion avec lui" % self.pi)
        self.renouvele = True
        self.arreter(voulu=False)

    def run(self):
        for host in (self.card.get("hote"), self.card.get("ip")):
            if not host or self.fin.is_set():
                continue
            try:
                session = pont2.connecter("%s:%d" % (host, self.port), self.epingle["parc"], self.transport,
                                          self.certificat, self.epingle["ordonnateur"], pont2.Revocations(), True)
                self.hote = host
                self.session = session
                break
            except (pont2.ErreurPont, OSError) as exc:
                log("%s : session v2 impossible par %s (%s)" % (self.pi, host, exc))
        if not self.session:
            self.fin.set()
            update_stats(self.pi, v2=False)
            if not self.voulu:
                self.link.v2_terminee(False)
            return
        self.etablie = True
        lecteur = threading.Thread(target=self._lire, daemon=True)
        lecteur.start()
        if self.expire and self.expire <= time.time():
            # Certificat expire pendant une veille : le Pi n'ouvre qu'une session de
            # renouvellement ; rien d'autre a y faire.
            log("%s : certificat v2 expire, renouvellement" % self.pi)
            try:
                self._demander_certificat()
            except pont2.ErreurPont as exc:
                log("%s : renouvellement impossible (%s)" % (self.pi, exc))
            self.fin.wait(30)
            self.arreter(voulu=False)
            return
        log("%s : session v2 etablie (Noise, port %d), le trafic y passe" % (self.pi, self.port))
        update_stats(self.pi, v2=True, v2_depuis=time.time(), erreur_v2=None, connecte=True, depuis=time.time(),
                     erreur=None)
        _wake_workers(self.pi)  # ce qui attendait
        try:
            while not self.fin.is_set():
                if self.demande is None and self.expire and self.expire - time.time() < CERT_MI_VIE_SEC:
                    try:
                        self._demander_certificat()
                    except pont2.ErreurPont as exc:
                        log("%s : renouvellement impossible (%s)" % (self.pi, exc))
                if not self.envoyer(self._battement()):
                    break
                self.fin.wait(BEAT_SEC)
        finally:
            self.arreter(voulu=False)
            update_stats(self.pi, v2=False)
            log("%s : session v2 fermee" % self.pi)
            if not self.voulu:
                self.link.v2_terminee(True)

    def _lire(self):
        try:
            while not self.fin.is_set():
                msg = self.session.recevoir()
                if msg is None:
                    break
                if msg.get("type") == "certificat":
                    try:
                        self._certificat_recu(msg)
                    except pont2.ErreurPont as exc:
                        log("%s : certificat v2 refuse : %s" % (self.pi, exc))
                    continue
                try:
                    self.link.recu(trad.depuis_v2(msg, hote=self.hote))
                except trad.NonTraduisible as exc:
                    log("%s : message v2 ignore (%s)" % (self.pi, exc))
        except pont2.ErreurPont as exc:
            log("%s : session v2 interrompue (%s)" % (self.pi, exc))
        self.fin.set()


def _pending_for(share, fast):
    """True s'il reste un travail.json compatible avec cette file."""
    folder = os.path.join(share, "travaux")
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    caps = capabilities()
    for job_id in names:
        if job_id in _refus_travail:
            continue  # objet signe refuse : rien a attendre de ce travail
        job = _read(os.path.join(folder, job_id, "travail.json"))
        if (job and job.get("type") in caps and (job.get("type") in FAST) == fast
                and not _attributed_elsewhere(os.path.join(folder, job_id))):
            return True
    return False


# --- travaux signes v2 (spec §6, lib/pont_v2.py cote Pi) -----------------------------
# Une fois la cle v2 du Pi epinglee, on ne prend plus un travail sur la seule foi du
# travail.json du NAS : il faut travail.v2.json, signe par le Pi, et on execute le
# travail QU'IL CONTIENT, apres avoir verifie l'empreinte de chaque fichier d'entree.
# Le resultat part aussi en v2 (resultat.v2.json, signe par notre cle v2).
TRAVAIL_V2 = "travail.v2.json"
RESULTAT_V2 = "resultat.v2.json"
TERMINES = os.path.join(V2_DIR, "termines.json")  # travaux v2 deja rendus : un travail rejoue n'est pas refait
_refus_travail = set()


class TravailRefuse(Exception):
    pass


def _epingle(pi):
    return _read(os.path.join(V2_DIR, "ponts", "%s.json" % pi))


def _termines():
    data = _read(TERMINES) or {}
    limite = time.time() - 8 * 86400  # au-dela, l'objet signe est trop vieux de toute facon
    return {k: v for k, v in data.items() if isinstance(v, (int, float)) and v > limite}


def _travail_signe(pi, folder, job_id):
    """(travail, empreinte de la charge) tires de travail.v2.json, ou None si ce Pi n'a pas
    migre (la v1 vaut encore). Leve TravailRefuse si l'objet manque ou ne tient pas."""
    epingle = _epingle(pi)
    if not epingle:
        return None
    if pont2 is None:
        raise TravailRefuse("module nearfield_pont absent : impossible de verifier")
    objet = _read(os.path.join(folder, TRAVAIL_V2))
    if not objet:
        raise TravailRefuse("travail non signe")
    try:
        charge = pont2.verifier_objet(objet, epingle["ordonnateur"], "travail", epingle["parc"])
        if charge.get("id") != job_id:
            raise TravailRefuse("objet signe d'un autre travail")
        if job_id in _termines():
            raise TravailRefuse("travail deja rendu (rejeu)")
        pont2.verifier_fichiers(objet, folder)
        job = json.loads(charge["contenu"])
    except pont2.ErreurPont as exc:
        raise TravailRefuse(str(exc))
    except (KeyError, TypeError, ValueError) as exc:
        raise TravailRefuse("contenu illisible : %s" % exc)
    if not isinstance(job, dict) or job.get("id") != job_id:
        raise TravailRefuse("contenu d'un autre travail")
    import hashlib
    return job, hashlib.sha256(pont2.canonique(charge)).hexdigest()


def _rendre_v2(pi, folder, job_id, empreinte, state, message, duration, outputs):
    """resultat.v2.json, signe par notre cle v2, lie au travail par l'empreinte de sa charge."""
    epingle = _epingle(pi)
    moi = pont2.Coffre.ouvrir(os.path.join(V2_DIR, "identite.pem"))
    objet = moi.signer_objet({
        "type": "resultat", "parc": epingle["parc"], "id": "resultat-" + job_id, "travail": job_id,
        "objet_travail": empreinte, "etat": state, "message": str(message)[:4000], "duree": int(duration),
        "fichiers": {name: pont2.empreinte_fichier(os.path.join(folder, name)) for name in outputs}})
    _write(os.path.join(folder, RESULTAT_V2), objet)
    termines = _termines()
    termines[job_id] = time.time()
    _write(TERMINES, termines)


def _claim(share, fast, pi=None):
    """Prend le plus ancien travail libre de sa file (renommage atomique) :
    (id, dossier, travail, empreinte v2 ou None)."""
    folder = os.path.join(share, "travaux")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    caps = capabilities()
    for job_id in names:
        src = os.path.join(folder, job_id, "travail.json")
        job = _read(src)
        if not job or job.get("type") not in caps or (job.get("type") in FAST) != fast:
            continue
        if _attributed_elsewhere(os.path.join(folder, job_id)):
            continue  # confie a une machine plus libre
        empreinte = None
        if pi:
            try:
                signe = _travail_signe(pi, os.path.join(folder, job_id), job_id)
            except TravailRefuse as exc:
                if job_id not in _refus_travail:
                    _refus_travail.add(job_id)
                    log("%s : travail %s refuse : %s" % (pi, job_id, exc))
                continue
            if signe:
                job, empreinte = signe
                if job.get("type") not in caps or (job.get("type") in FAST) != fast:
                    continue
        dst = os.path.join(folder, job_id, "travail.pris.json")
        try:
            os.rename(src, dst)
        except OSError:
            continue  # une autre machine l'a pris
        _write(dst, dict(job, pris_par=config()["id"], pris_le=time.time()))  # pour reprendre apres une coupure
        return job_id, os.path.join(folder, job_id), job, empreinte
    return None


def _lora(job, folder):
    """Rafale LoRa : tous les facteurs d'etalement, du plus probable au moins."""
    p = job["params"]
    out = subprocess.run([GR_PYTHON, os.path.join(HOME, "lora_decode.py"), os.path.join(folder, p["fichier"]),
                          str(p["taux"]), str(p["largeur"]), str(p["sync"]), ",".join(str(s) for s in p["sfs"])],
                         capture_output=True, text=True, timeout=300).stdout
    lines = out.strip().splitlines()
    frames = json.loads(lines[-1]) if lines else []
    _write(os.path.join(folder, "trames.json"), frames)
    good = sum(1 for f in frames if f.get("crc_ok"))
    return "fait", ["trames.json"], "%d trame%s valide%s" % (good, "s" if good > 1 else "", "s" if good > 1 else "")


def _requeue_interrupted():
    """Au demarrage : un travail pris par CETTE machine et jamais termine (le
    service a ete coupe en plein calcul) est remis en file d'attente."""
    me = config()["id"]
    for pi, card in bridges().items():
        share = find_share(card)
        if not share:
            continue
        folder = os.path.join(share, "travaux")
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for job_id in names:
            taken = os.path.join(folder, job_id, "travail.pris.json")
            job = _read(taken)
            if job and job.get("pris_par") == me and not os.path.exists(os.path.join(folder, job_id, "resultat.json")):
                job.pop("pris_par", None)
                job.pop("pris_le", None)
                _write(os.path.join(folder, job_id, "travail.json"), job)
                _remove(taken)
                log("%s : travail %s interrompu, remis en file" % (pi, job_id))


def _process(job, folder):
    if job.get("type") == "lora":
        return _lora(job, folder)
    if job.get("type") == "meteor":
        import meteor
        return meteor.process(folder, job["params"], BIN_DIR, os.path.join(HOME, "calcul"))
    if job.get("type") == "decrire_images":
        import decrire_images
        return decrire_images.process_via_venv(folder, job.get("params") or {})
    if job.get("type") == "voix":
        import voix_piper
        return voix_piper.process(folder, job.get("params") or {})
    if job.get("type") == "radio_juges":
        return _radio_juges(job, folder)
    if job.get("type") == "whisper":
        import whisper_radio
        return whisper_radio.process(folder, job.get("params") or {})
    if job.get("type") in ("examen", "examen_lot", "examen_serie"):
        import examen_mac
        return examen_mac.process(folder, job.get("params") or {})
    return "echec", [], "cette machine ne sait pas faire « %s »" % job.get("type")


def _work(pi, fast):
    card = bridges().get(pi)
    share = find_share(card) if card else None
    if not share:
        update_stats(pi, erreur="disque du NAS absent") if card else None
        return
    claimed = None
    for attempt in range(40):  # jusqu'a ~20 s si un travail de CETTE file est attendu
        claimed = _claim(share, fast, pi)
        if claimed:
            break
        if not _pending_for(share, fast):
            break  # l'autre file (ex. lourd pendant un decrire_images) sort tout de suite
        if attempt in (0, 9, 19, 39):
            log("%s : claim %s en attente SMB (essai %d)" % (pi, "rapide" if fast else "lourd", attempt + 1))
        time.sleep(0.5)
    while claimed:
        job_id, folder, job, empreinte = claimed
        link = _links.get(pi)
        if link:
            link.send({"type": "pris", "travail": job_id})
        log("%s : travail %s (%s)" % (pi, job_id, job.get("type")))
        update_stats(pi, en_cours=job_id)
        started = time.time()
        # Empeche la veille pendant le calcul (sans toucher aux reglages du Mac).
        awake = None
        if sys.platform == "darwin" and not fast:
            awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        try:
            state, outputs, message = _process(job, folder)
        except Exception:
            traceback.print_exc()
            state, outputs, message = "echec", [], "erreur de calcul sur la machine compagne"
        finally:
            if awake:
                awake.terminate()
        duration = round(time.time() - started)
        conf = config()
        if empreinte:  # avant resultat.json : le Pi tient pour fini un travail qui a un resultat.json
            try:
                _rendre_v2(pi, folder, job_id, empreinte, state, message, duration, outputs)
            except Exception as exc:
                log("%s : resultat v2 de %s non signe : %s" % (pi, job_id, exc))
                state, message = "echec", "resultat v2 non signe : %s" % exc
        result = proto.sign(key(), {
            "type": "resultat", "travail": job_id, "compagnon": conf["id"], "etat": state, "message": message,
            "duree": duration, "sorties": {name: proto.file_digest(os.path.join(folder, name)) for name in outputs}})
        _write(os.path.join(folder, "resultat.json"), result)
        ok = state == "fait"
        with _stats_lock:  # deux files peuvent finir en meme temps
            data = stats(pi)
            data.update(faits=data.get("faits", 0) + ok, echecs=data.get("echecs", 0) + (not ok),
                        secondes=data.get("secondes", 0) + duration, en_cours=None,
                        dernier={"t": time.time(), "type": job.get("type"), "etat": state, "message": message,
                                 "duree": duration})
            _write(os.path.join(STATS, pi + ".json"), data)
        log("%s : %s %s (%s), %d s" % (pi, job_id, state, message, duration))
        if link:
            link.send({"type": "fini", "travail": job_id})
        claimed = _claim(share, fast, pi)


def service():
    conf = config()
    key()
    log("service demarre : %s (%s), sait faire : %s" % (conf["nom"], conf["id"], ", ".join(capabilities()) or "rien"))

    def worker(name):
        while True:
            pi = _queues[name].get()
            try:
                _work(pi, name == "rapide")
            except Exception:
                traceback.print_exc()

    try:
        _requeue_interrupted()
    except Exception:
        traceback.print_exc()
    for name in _queues:
        threading.Thread(target=worker, args=(name,), daemon=True).start()
    if "tickets" in capabilities():
        threading.Thread(target=_tickets_boucle, daemon=True).start()
    last_scan = last_beat = 0
    while True:
        current = bridges()
        for pi in current:
            if pi not in _links or not _links[pi].is_alive():
                _links[pi] = LiaisonV2(pi) if _etat_v2(pi) else Link(pi)
                _links[pi].start()
        for pi in list(_links):
            if pi not in current:
                _links.pop(pi).shutdown()
        direct = {pi for pi in current if _links.get(pi) and _links[pi].direct}
        # Liaison directe : NAS releve toutes les 5 min, en filet de securite
        # si un avis s'est perdu ; sans elle, en differe toutes les 12 h.
        every = SCAN_SEC if len(direct) == len(current) else NAS_FALLBACK_SEC
        if time.time() - last_scan > every:
            last_scan = time.time()
            for pi in current:
                _wake_workers(pi)
        if len(direct) < len(current) and time.time() - last_beat > NAS_FALLBACK_SEC:
            last_beat = time.time()
            for pi, card in current.items():
                try:
                    _nas_beat(pi, card)
                except Exception as exc:
                    log("%s : signe de vie sur le NAS impossible (%r)" % (pi, exc))
        time.sleep(LOOP_SEC)


# --- etat --------------------------------------------------------------------------------

def _when(t):
    if not t:
        return "jamais"
    day = time.strftime("%Y-%m-%d", time.localtime(t))
    today = time.strftime("%Y-%m-%d")
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    prefix = "aujourd'hui" if day == today else "hier" if day == yesterday else "le " + time.strftime(
        "%d/%m", time.localtime(t))
    return "%s à %s" % (prefix, time.strftime("%H h %M", time.localtime(t)))


def _plural(n, word):
    return "%d %s%s" % (n, word, "s" if n > 1 else "")


def _duration(sec):
    sec = int(sec or 0)
    if sec < 90:
        return _plural(sec, "seconde")
    if sec < 5400:
        return "%d minutes" % round(sec / 60)
    return "%d heures %02d" % (sec // 3600, (sec % 3600) // 60)


def service_running():
    if sys.platform == "darwin":
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        return LABEL in out
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", "nearfield-bridge"]).returncode == 0


def status_text():
    conf = config()
    caps = capabilities()
    lines = ["Service de fond : %s." % ("actif" if service_running() else "en pause"),
             "Cette machine : %s, sait faire : %s." % (conf["nom"], ", ".join(caps) or "rien pour l'instant")]
    current = bridges()
    if not current:
        lines.append("Aucun pont. Choisis « Appairer avec un Pi » pour en créer un.")
    for pi, card in sorted(current.items()):
        data = stats(pi)
        if data.get("connecte"):
            link = "connecté depuis %s" % _when(data.get("depuis"))
        else:
            link = "pas connecté" + (" (%s)" % data["erreur"] if data.get("erreur") else "")
        lines.append("Pont avec %s : %s." % (pi, link))
        done = data.get("faits", 0)
        lines.append("  %s %s, %d sans résultat, %s de calcul au total." % (
            _plural(done, "travail").replace("travails", "travaux"), "réussi" + ("s" if done > 1 else ""),
            data.get("echecs", 0), _duration(data.get("secondes"))))
        if data.get("en_cours"):
            lines.append("  Travail en cours : %s." % data["en_cours"])
        last = data.get("dernier")
        if last:
            lines.append("  Dernier travail : %s, %s, %s (%s, %s)." % (
                _when(last.get("t")), last.get("type"), "réussi" if last.get("etat") == "fait" else "sans résultat",
                last.get("message"), _duration(last.get("duree"))))
        lines.append("  Disque du NAS : %s. Créé %s." % (
            "accessible" if find_share(card) else "absent", _when(card.get("appaire"))))
    return "\n".join(lines)


def bridges_list():
    out = []
    for pi in sorted(bridges()):
        data = stats(pi)
        out.append("%s\tPont avec %s, %s" % (pi, pi, "connecté" if data.get("connecte") else "pas connecté"))
    return "\n".join(out)


def service_control(action):
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "unload" if action == "pause" else "load", PLIST],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["systemctl", "--user", "stop" if action == "pause" else "start", "nearfield-bridge"])
    time.sleep(1)
    running = service_running()
    if action == "pause":
        return "OK", ("Service en pause : le Pi ne peut plus confier de travail à cette machine."
                      if not running else "Le service tourne encore.")
    return "OK", "Service relancé." if running else "Le service n'a pas démarré."


def _print(result):
    print("\t".join(result))
    return 0 if result[0] == "OK" else 1


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "etat"
    if command == "service":
        service()
    elif command == "etat":
        print(status_text())
    elif command == "ponts":
        print(bridges_list())
    elif command == "appairage":
        sys.exit(pairing_terminal())
    elif command == "appairage-attendre":
        sys.exit(_print(pairing_wait()))
    elif command == "appairage-code":
        sys.exit(_print(pairing_decide()))
    elif command == "oublier" and len(sys.argv) > 2:
        sys.exit(_print(forget(sys.argv[2])))
    elif command in ("service-pause", "service-reprise"):
        sys.exit(_print(service_control(command.split("-")[1])))
    elif command == "service-etat":
        print("actif" if service_running() else "en pause")
    else:
        print(__doc__)
        sys.exit(2)
