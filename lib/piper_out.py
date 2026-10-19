"""Piper (voix neuronale locale, sans reseau) : roue de secours entre les
voix Microsoft et espeak (demande utilisateur 2026-10-18 : "la voix espeak
est vraiment moche").

Binaire armv7 officiel dans PIPER_DIR, voix dans PIPER_DIR/voix (catalogue
officiel rhasspy/piper-voices : lib/piper_voix.py, commande "voix locales").
Sur ce Pi 4 deja charge par le scanner, une voix "medium" calcule a peu pres
en temps reel (mesure : 9 s pour 10 s de parole) : on la fait parler EN
FLUX (--output-raw, phrase par phrase) pour ne pas attendre la phrase
entiere ; pour un fichier (journal), on attend la fin.

Voix tiree au hasard a chaque annonce (variete voulue, comme pour les voix
Microsoft). Au-dessus de MAX_TEMP_C, Piper n'est pas lance (le calcul
ferait encore chauffer) : espeak prend le relais.
"""
import json
import os
import random
import subprocess
import threading

PIPER_DIR = "/home/vador/piper"
BINARY = os.path.join(PIPER_DIR, "piper")
VOICE_DIR = os.path.join(PIPER_DIR, "voix")
# Voix : toutes celles installees dans VOICE_DIR (catalogue officiel, voir
# lib/piper_voix.py et la commande console "voix locales"). Identifiant
# "piper:<modele>" ou "piper:<modele>#<locuteur>" pour un modele a plusieurs
# voix ; partage avec la console (choix, cache) et le cache des annonces.
# Les annonces tirent au hasard parmi les voix francaises installees.
ALIASES = {"piper:siwis": "piper:fr_FR-siwis-medium", "piper:jessica": "piper:fr_FR-upmc-medium#jessica",
           "piper:pierre": "piper:fr_FR-upmc-medium#pierre"}   # premiers noms (2026-10-18 matin)
DEFAULT_VOICE = "piper:fr_FR-siwis-medium"
MAX_SPEAKERS_PER_MODEL = 3  # MLS en a 125 : on en propose quelques-uns
ANNOUNCE_LANG = "fr_"
# Stabilite (2026-10-18 : charabia "ararhrhr" au milieu des annonces). Les
# reglages par defaut de Piper (noise_scale 0,667, noise_w 0,8) donnent
# beaucoup de variation ; les reglages partages par la communaute descendent
# a 0,5 / 0,6, et la voix MLS est livree a 0,333. On ne depasse jamais ces
# plafonds (une voix deja reglee plus bas le reste).
NOISE_SCALE_MAX = 0.5
NOISE_W_MAX = 0.6
# Voix gardees pour les annonces : qualite moyenne ou mieux, et pas MLS
# (livres audio du domaine public, locuteurs inegaux, babille plus souvent).
# Toutes restent au choix dans la console.
ANNOUNCE_EXCLUDED_MODELS = {"fr_FR-mls-medium"}
ANNOUNCE_MIN_QUALITY = ("medium", "high")
MAX_TEMP_C = 83.0
CHUNK = 4410  # 100 ms a 22050 Hz, 16 bits


def _temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except (OSError, ValueError):
        return None


def available():
    """Vrai si Piper et au moins une voix sont la, et que le Pi n'est pas brulant."""
    if not os.access(BINARY, os.X_OK) or not _voices():
        return False
    temp = _temp_c()
    if temp is not None and temp > MAX_TEMP_C:
        print(f"[piper] {temp:.0f} C : trop chaud, repli espeak")
        return False
    return True


_configs = {}


def _config(name):
    path = os.path.join(VOICE_DIR, name + ".onnx.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _configs.get(name, (None,))[0] != mtime:
        try:
            with open(path, encoding="utf-8") as f:
                _configs[name] = (mtime, json.load(f))
        except (OSError, ValueError):
            return None
    return _configs[name][1]


def installed_models():
    try:
        names = os.listdir(VOICE_DIR)
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".onnx") and os.path.exists(os.path.join(VOICE_DIR, n + ".json")))


def canonical(voice_id):
    return ALIASES.get(voice_id, voice_id)


def voice_ids(lang=ANNOUNCE_LANG):
    """Identifiants des voix installees (lang : prefixe de langue, None = toutes)."""
    ids = []
    for name in installed_models():
        if lang and not name.startswith(lang):
            continue
        speakers = list(((_config(name) or {}).get("speaker_id_map") or {}))
        if not speakers:
            ids.append(f"piper:{name}")
        else:
            step = max(1, len(speakers) // MAX_SPEAKERS_PER_MODEL)
            ids += [f"piper:{name}#{spk}" for spk in speakers[::step][:MAX_SPEAKERS_PER_MODEL]]
    return ids


def resolve(voice_id):
    """(modele, numero de locuteur ou None), ou None si la voix n'est pas installee."""
    voice_id = canonical(voice_id or "")
    if not voice_id.startswith("piper:"):
        return None
    name, _, spk = voice_id[6:].partition("#")
    cfg = _config(name)
    if cfg is None:
        return None
    if not spk:
        return name, None
    mapping = cfg.get("speaker_id_map") or {}
    if spk in mapping:
        return name, mapping[spk]
    return (name, int(spk)) if spk.isdigit() else None


def label(voice_id):
    """Nom parle d'une voix : "Siwis, qualite moyenne", "Jessica (UPMC)"... """
    voice_id = canonical(voice_id)
    name, _, spk = voice_id[6:].partition("#")
    parts = name.split("-")
    base = parts[1].replace("_", " ").capitalize() if len(parts) > 2 else name
    quality = {"x_low": "très basse", "low": "basse", "medium": "moyenne", "high": "haute"}.get(parts[-1], parts[-1])
    who = f"{spk.capitalize()} ({base})" if spk and not spk.isdigit() else (f"{base} numéro {spk}" if spk else base)
    return f"{who}, qualité {quality}"


def announce_voice_ids():
    """Voix tirees au hasard pour les annonces (voir ANNOUNCE_EXCLUDED_MODELS)."""
    ids = [v for v in voice_ids() if resolve(v)[0] not in ANNOUNCE_EXCLUDED_MODELS
           and resolve(v)[0].rsplit("-", 1)[-1] in ANNOUNCE_MIN_QUALITY]
    return ids or voice_ids()


def _noise_args(name):
    inference = (_config(name) or {}).get("inference") or {}
    scale = min(float(inference.get("noise_scale", 0.667)), NOISE_SCALE_MAX)
    width = min(float(inference.get("noise_w", 0.8)), NOISE_W_MAX)
    return ["--noise_scale", str(scale), "--noise_w", str(width)]


def _voices():
    return [resolve(v) for v in announce_voice_ids()]


def length_scale(rate):
    """Debit Microsoft ("+50%", "-10%") -> length_scale de Piper (plus petit = plus vite)."""
    try:
        pct = float(str(rate).split("~")[0].strip().rstrip("%") or 0)
    except ValueError:
        pct = 0.0
    return round(1.0 / max(0.3, 1 + pct / 100), 3)


def _pick(voice_id=None, scale_len=1.0):
    name, speaker = resolve(voice_id) or random.choice(_voices())
    model = os.path.join(VOICE_DIR, name + ".onnx")
    try:
        with open(model + ".json", encoding="utf-8") as f:
            rate = json.load(f)["audio"]["sample_rate"]
    except (OSError, ValueError, KeyError):
        rate = 22050
    args = [BINARY, "-m", model, "-q"] + _noise_args(name)
    if speaker is not None:
        args += ["-s", str(speaker)]
    if scale_len != 1.0:
        args += ["--length_scale", str(scale_len)]
    return args, rate


class Speech:
    """Une phrase en cours : piper (en flux) | relais du volume | aplay."""

    def __init__(self, text, scale=None, record_path=None, voice_id=None, scale_len=1.0, keep_path=None):
        """record_path : copie WAV de ce qui est dit (journal radio). keep_path :
        idem, mais gardee seulement si la phrase va au bout (cache)."""
        import audio_lock
        self._text = text
        args, rate = _pick(voice_id, scale_len)
        self._record = None
        self._killed = False
        self._keep = keep_path
        if keep_path:
            os.makedirs(os.path.dirname(keep_path), exist_ok=True)
            record_path = keep_path + ".part.wav"
        self._record_path = record_path
        if record_path:
            import wave
            self._record = wave.open(record_path, "wb")
            self._record.setnchannels(1)
            self._record.setsampwidth(2)
            self._record.setframerate(rate)
        self._ticket = audio_lock.ticket_courant() if scale is not None else None
        self._piper = subprocess.Popen(args + ["--output_raw"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, cwd=PIPER_DIR)
        self._piper.stdin.write(text.encode("utf-8"))
        self._piper.stdin.close()
        self._aplay = subprocess.Popen(["aplay", "-q", "-D", "default", "-r", str(rate), "-f", "S16_LE", "-c", "1"],
                                       stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._relay = threading.Thread(target=self._pump, args=(scale,), daemon=True)
        self._relay.start()

    def _pump(self, scale):
        try:
            while True:
                data = self._piper.stdout.read(CHUNK)
                if not data:
                    break
                if self._record is not None:
                    self._record.writeframes(data)
                self._aplay.stdin.write(scale(data, self._ticket) if scale else data)
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            if self._record is not None:
                self._record.close()
                if self._keep:
                    complete = not self._killed and self._piper.wait() == 0 and os.path.getsize(self._record_path) > 1000
                    try:
                        if complete:
                            os.replace(self._record_path, self._keep)
                        else:
                            os.remove(self._record_path)
                    except OSError:
                        pass
            try:
                self._aplay.stdin.close()
            except OSError:
                pass

    def poll(self):
        return self._aplay.poll()

    def wait(self, timeout=None):
        if timeout is None:
            import espeak_out
            timeout = espeak_out.duree_max(self._text, 120)
        try:
            code = self._aplay.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            return None
        try:
            self._piper.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._piper.kill()
        return code

    def kill(self):
        self._killed = True
        for proc in (self._aplay, self._piper):
            if proc.poll() is None:
                proc.kill()
        for proc in (self._aplay, self._piper):
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def say(text, scale=None, timeout=None, record_path=None, voice_id=None, scale_len=1.0):
    """Parle et attend la fin. Rend True si Piper a parle, False sinon
    (indisponible, trop chaud, erreur) : a l'appelant de passer a espeak."""
    if not text.strip() or not available():
        return False
    try:
        speech = Speech(text, scale, record_path, voice_id=voice_id, scale_len=scale_len)
        ok = speech.wait(timeout=timeout) == 0
        speech._relay.join(timeout=5)  # copie WAV fermee avant que l'appelant la lise
        return ok
    except Exception as exc:
        print(f"[piper] {exc!r}")
        return False


def to_file(text, path, timeout=None, voice_id=None, scale_len=1.0):
    """Ecrit la voix dans path (WAV). Rend True si le fichier est utilisable."""
    if not text.strip() or not available():
        return False
    args, _ = _pick(voice_id, scale_len)
    if timeout is None:
        import espeak_out
        timeout = espeak_out.duree_max(text, 120)
    tmp = _tmp(path)
    try:
        subprocess.run(args + ["-f", tmp], input=text.encode("utf-8"), capture_output=True,
                       timeout=timeout, cwd=PIPER_DIR)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 1000:
            os.replace(tmp, path)
            return True
        return False
    except Exception as exc:
        print(f"[piper] fichier : {exc!r}")
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _tmp(path):
    """Fichier temporaire propre a CE calcul : deux calculs du meme son (la
    preparation de fond et une annonce, 2026-10-18) ecrivaient le meme
    fichier en meme temps et melangeaient deux voix en charabia."""
    return f"{path}.{os.getpid()}.{threading.get_ident()}.tmp.wav"


def batch(voice_id, items, scale_len=1.0, timeout=900):
    """Fabrique plusieurs fichiers d'un coup, voix chargee une seule fois
    (preparation des caches) : items = [(texte, chemin .wav)]. Rend le
    nombre de fichiers faits."""
    items = [(t, p) for t, p in items if t.strip()]
    found = resolve(voice_id)
    if not items or found is None:
        return 0
    name, speaker = found
    args = [BINARY, "-m", os.path.join(VOICE_DIR, name + ".onnx"), "-q", "--json-input"] + _noise_args(name)
    if scale_len != 1.0:
        args += ["--length_scale", str(scale_len)]
    lines = []
    for text, path in items:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = {"text": text, "output_file": _tmp(path)}
        if speaker is not None:
            line["speaker_id"] = speaker
        lines.append(json.dumps(line, ensure_ascii=False))
    try:
        subprocess.run(["nice", "-n", "19"] + args, input=("\n".join(lines) + "\n").encode("utf-8"),
                       capture_output=True, timeout=timeout, cwd=PIPER_DIR)
    except Exception as exc:
        print(f"[piper] lot : {exc!r}")
    done = 0
    for _, path in items:
        tmp = _tmp(path)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 1000:
            os.replace(tmp, path)
            done += 1
        elif os.path.exists(tmp):
            os.remove(tmp)
    return done
