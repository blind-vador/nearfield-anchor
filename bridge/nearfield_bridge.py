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
SCAN_SEC = 30  # etait 300 : decrire_images restait bloque
# Sans liaison directe : fonctionnement en DIFFERE par le NAS (decision
# utilisateur 2026-10-02 : pas d'ecritures ni de lectures frequentes, les
# disques s'useraient) -> travaux releves et signe de vie depose toutes les 12 h.
NAS_FALLBACK_SEC = 12 * 3600
LOOP_SEC = 5
PAIRING_WAIT_SEC = 180
RECONNECT_SEC = (10, 120)


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
        if card and card.get("cle"):
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
        return juge, juges_radio.juger(juge, wav, dsd=RADIO_DSD, voix=voix, priorite=("nice", "-n", "10"))

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
                                    banc=bool(msg.get("banc")))
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
        # Banc (2026-11-01) : un son par modulation, chacun sous son propre type.
        banc_envoye = [m for m, chemin in sorted((res.get("banc") or {}).items())
                       if _streamer(chemin, "numerique_banc_" + m) is not None]
        link.send({"type": "numerique_resultat", "reponse_a": rid, "ok": bool(res.get("ok")),
                   "erreur": res.get("erreur"), "protocole": res.get("protocole"),
                   "meta": res.get("meta") or {}, "chiffre": bool(res.get("chiffre")),
                   "clair": bool(res.get("clair")), "voix": voix_envoyee, "audio": audio_envoye,
                   "numerique_confirme": bool(res.get("numerique_confirme")),
                   "digital_actif": bool(res.get("digital_actif")), "sync_hits": res.get("sync_hits"),
                   "voix_rms": res.get("voix_rms"), "audio_rms": res.get("audio_rms"),
                   "resume": numerique_neo.resume_fr(res), "duree": res.get("duree"),
                   "banc": banc_envoye, "banc_preuve": res.get("banc_preuve")})
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
    if share:
        _write(os.path.join(share, "revocations", conf["id"] + ".json"),
               proto.sign(key(), {"type": "oubli", "compagnon": conf["id"], "pi": pi}))
    _remove(os.path.join(BRIDGES, pi + ".json"))
    _remove(os.path.join(STATS, pi + ".json"))
    return "OK", ("Pont avec %s supprimé%s." % (pi, "" if share else
                  ", mais le disque du NAS est absent : le Pi ne le saura qu'au prochain essai"))


# --- service ----------------------------------------------------------------------------

# Deux files : les rafales LoRa (petites, a annoncer tout de suite) ne doivent
# pas attendre derriere un passage Meteor de plusieurs minutes.
FAST = ("lora", "decrire_images", "radio_juges")
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

    def send(self, msg):
        with self.lock:
            if self.channel:
                try:
                    self.channel.send(msg)
                    return True
                except OSError:
                    self.channel.close()
        return False

    def shutdown(self):
        """Pont supprime : coupe la liaison tout de suite."""
        self.stop.set()
        with self.lock:
            if self.channel:
                self.channel.close()

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
                if msg.get("type") == "travail":
                    log("%s : travail %s annonce" % (self.pi, msg.get("travail")))
                    _wake_workers(self.pi)
                elif msg.get("type") == "dire":
                    threading.Thread(target=say_to_pi, args=(self, msg), daemon=True).start()
                elif msg.get("type") == "tetra":
                    threading.Thread(target=tetra_to_pi, args=(self, msg), daemon=True).start()
                elif msg.get("type") == "numerique":
                    threading.Thread(target=numerique_to_pi, args=(self, msg), daemon=True).start()
        except (OSError, ValueError) as exc:
            log("%s : liaison coupee (%r)" % (self.pi, exc))
        finally:
            with self.lock:
                self.channel = None
            channel.close()
            update_stats(self.pi, connecte=False, vu=time.time())
        return True

    def _beat(self):
        while not self.stop.is_set():
            with self.lock:
                channel = self.channel
            if channel is None:
                return
            etat = session_state()
            if not self.send({"type": "battement", "capacites": capabilities(), "etat": etat,
                              "voix_mac": mac_voices(), "puissance": power(etat)}):
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


def _pending_for(share, fast):
    """True s'il reste un travail.json compatible avec cette file."""
    folder = os.path.join(share, "travaux")
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    caps = capabilities()
    for job_id in names:
        job = _read(os.path.join(folder, job_id, "travail.json"))
        if (job and job.get("type") in caps and (job.get("type") in FAST) == fast
                and not _attributed_elsewhere(os.path.join(folder, job_id))):
            return True
    return False


def _claim(share, fast):
    """Prend le plus ancien travail libre de sa file (renommage atomique) : (id, dossier, travail)."""
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
        dst =os.path.join(folder, job_id, "travail.pris.json")
        try:
            os.rename(src, dst)
        except OSError:
            continue  # une autre machine l'a pris
        _write(dst, dict(job, pris_par=config()["id"], pris_le=time.time()))  # pour reprendre apres une coupure
        return job_id, os.path.join(folder, job_id), job
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
    return "echec", [], "cette machine ne sait pas faire « %s »" % job.get("type")


def _work(pi, fast):
    card = bridges().get(pi)
    share = find_share(card) if card else None
    if not share:
        update_stats(pi, erreur="disque du NAS absent") if card else None
        return
    claimed = None
    for attempt in range(40):  # jusqu'a ~20 s si un travail de CETTE file est attendu
        claimed = _claim(share, fast)
        if claimed:
            break
        if not _pending_for(share, fast):
            break  # l'autre file (ex. lourd pendant un decrire_images) sort tout de suite
        if attempt in (0, 9, 19, 39):
            log("%s : claim %s en attente SMB (essai %d)" % (pi, "rapide" if fast else "lourd", attempt + 1))
        time.sleep(0.5)
    while claimed:
        job_id, folder, job = claimed
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
        claimed = _claim(share, fast)


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
    last_scan = last_beat = 0
    while True:
        current = bridges()
        for pi in current:
            if pi not in _links or not _links[pi].is_alive():
                _links[pi] = Link(pi)
                _links[pi].start()
        for pi in list(_links):
            if pi not in current:
                _links.pop(pi).shutdown()
        direct = {pi for pi in current if _links.get(pi) and _links[pi].channel is not None}
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
