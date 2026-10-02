"""Phrases de l'interface, centralisees (2026-10-02).

Toutes les phrases dites ou affichees par la console passent par dire(cle,
**variables). Elles viennent d'un fichier par langue, lisible au lecteur
d'ecran : phrases/<langue>.ini (une section par phrase) :

  [journal.ferme]
  texte = Journal {nom} fermé.
  contexte = dit quand on ferme le journal
  garder = Échap, F1            (mots qui doivent rester tels quels)
  fixe = oui                    (jamais reecrite par la personnalite)

Le fichier de la langue est la VERSION NEUTRE, de reference, jamais modifiee
par le Pi. Une autre langue = un autre fichier avec les memes cles.

Personnalite (lib/personnalite.py) : chaque nuit, Gemini reecrit les phrases
selon la personnalite choisie par l'utilisateur, dans
~/autopilot/phrases/personnalite_<langue>.ini (meme format). dire() prend la
phrase du jour si elle respecte la phrase neutre (memes variables, mots
proteges presents, longueur raisonnable), sinon la neutre.
"""
import configparser
import os
import re
import string

BASE_DIR = "/home/vador/autopilot/phrases"           # fichiers de langue (copies du depot)
PERSONA_DIR = "/home/vador/autopilot/phrases"        # phrases du jour, sur la carte SD
DEFAULT_LANG = "fr"
MAX_GROWTH = 1.8      # une phrase du jour au plus 1,8 fois plus longue (synthese vocale)
MAX_EXTRA_CHARS = 90

_cache = {}
_settings = {"t": 0.0, "data": {}}
SETTINGS_TTL_SEC = 30  # reglages de la console relus au plus toutes les 30 s


def _console_settings():
    import time
    if time.time() - _settings["t"] > SETTINGS_TTL_SEC:
        try:
            import reglages
            _settings["data"] = reglages.get("console") or {}
        except Exception:
            _settings["data"] = {}
        _settings["t"] = time.time()
    return _settings["data"]


def langue():
    return (_console_settings().get("langue") or DEFAULT_LANG).lower()


def _read(path):
    """{cle: {texte, contexte, garder, fixe}} d'un fichier, relu s'il change."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        print(f"[phrases] fichier illisible {path} : {exc}")
        return {}
    data = {}
    for key in parser.sections():
        sec = parser[key]
        data[key] = {"texte": " ".join(sec.get("texte", "").split()),
                     "contexte": sec.get("contexte", ""),
                     "garder": [w.strip() for w in sec.get("garder", "").split(",") if w.strip()],
                     "fixe": sec.get("fixe", "").lower() in ("oui", "yes", "1", "true")}
    _cache[path] = (mtime, data)
    return data


def base(lang=None):
    return _read(os.path.join(BASE_DIR, f"{lang or langue()}.ini"))


def persona_path(lang=None):
    return os.path.join(PERSONA_DIR, f"personnalite_{lang or langue()}.ini")


def variables(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def valid(entry, new_text):
    """La phrase du jour respecte-t-elle la neutre ? (rend une raison, ou None)"""
    if not new_text or "\n" in new_text:
        return "vide ou sur plusieurs lignes"
    if variables(new_text) != variables(entry["texte"]):
        return "variables differentes"
    for word in entry["garder"]:
        if word not in new_text:
            return f"mot protege absent : {word}"
    if len(new_text) > max(len(entry["texte"]) * MAX_GROWTH, len(entry["texte"]) + MAX_EXTRA_CHARS):
        return "trop longue"
    if re.search(r"[*#_`~<>\[\]]|[\U0001F000-\U0001FFFF]", new_text):
        return "symboles ou emoji"
    try:
        new_text.format(**{v: "x" for v in variables(new_text)})
    except (ValueError, IndexError, KeyError):
        return "accolades mal formees"
    return None


def persona_active():
    return bool((_console_settings().get("personnalite") or {}).get("active"))


def texte(cle, lang=None):
    """Gabarit de la phrase : celle du jour si active et valide, sinon la neutre."""
    neutral = base(lang).get(cle)
    if neutral is None:
        print(f"[phrases] cle inconnue : {cle}")
        return None
    if persona_active() and not neutral["fixe"]:
        daily = _read(persona_path(lang)).get(cle)
        if daily and valid(neutral, daily["texte"]) is None:
            return daily["texte"]
    return neutral["texte"]


def dire(cle, **variables_):
    """La phrase prete a dire. Une cle inconnue ou une variable manquante ne
    plante jamais la console : repli sur la neutre, puis sur le nom de la cle."""
    template = texte(cle)
    if template is None:
        return cle
    try:
        return template.format(**variables_)
    except (KeyError, IndexError, ValueError):
        neutral = base().get(cle, {}).get("texte", cle)
        try:
            return neutral.format(**variables_)
        except (KeyError, IndexError, ValueError):
            return neutral


def nombre(n, singulier, pluriel=None):
    """"1 note", "5 notes" : les nombres arrivent deja accordes dans les phrases."""
    return f"{n} {singulier if n <= 1 else (pluriel or singulier + 's')}"
