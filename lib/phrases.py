"""Phrases de l'interface, centralisees (2026-10-02).

Toutes les phrases dites ou affichees par la console passent par dire(cle,
**variables). Elles viennent d'un fichier par langue, lisible au lecteur
d'ecran : phrases/<langue>.ini (une section par phrase) :

  [journal.ferme]
  texte = Journal {nom} fermé.
  contexte = dit quand on ferme le journal
  garder = Échap, F1            (mots qui doivent rester tels quels)
  fixe = oui                    (jamais reecrite par la personnalite)
  personnalite = oui            (phrase d'interface d'un dialogue dont la famille
                                 n'est pas dans PERSONA_FAMILIES : plantes, liste
                                 noire, capteurs, transports -- 2026-10-07)

Le fichier de la langue est la VERSION NEUTRE, de reference, jamais modifiee
par le Pi. Une autre langue = un autre fichier avec les memes cles.

Personnalite (lib/personnalite.py) : chaque nuit, Gemini reecrit les phrases
selon la personnalite choisie par l'utilisateur, dans
~/autopilot/phrases/personnalite_<langue>.ini (meme format). dire() prend la
phrase du jour si elle respecte la phrase neutre (memes variables, mots
proteges presents, longueur raisonnable), sinon la neutre.
"""
import chemins
import configparser
import os
import re
import string

BASE_DIR = chemins.base("phrases")           # fichiers de langue (copies du depot)
PERSONA_DIR = chemins.base("phrases")        # phrases du jour, sur la carte SD
DEFAULT_LANG = "fr"
MAX_GROWTH = 1.6      # la place de colorer la phrase d'un personnage, pas de la rallonger (2026-10-04)
MAX_EXTRA_CHARS = 45
# Perimetre de la personnalite (2026-10-04, demande utilisateur) : seules les
# phrases d'interface courtes des dialogues de la console ; jamais les
# annonces (radio, avions, capteurs, meteo, trains, DAB+, messages...), ni les
# reponses informatives des commandes, l'aide ou le flash. Applique aussi a la
# lecture : un vieux fichier du jour ne peut plus toucher une annonce.
PERSONA_FAMILIES = {"appairage", "arbre", "archives", "commun", "conseils", "console", "interets", "journal",
                    "manuel", "nav", "notation", "observation", "personnalite", "priorites", "reglages",
                    "veilles", "voix", "maj", "vision", "simulation"}
PERSONA_MAX_LEN = 140


def rewritable(cle, entry):
    """Cette phrase peut-elle recevoir la touche de la personnalite ?"""
    return (not entry["fixe"] and (cle.split(".")[0] in PERSONA_FAMILIES or entry.get("personnalite"))
            and len(entry["texte"]) <= PERSONA_MAX_LEN)

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
                     "fixe": sec.get("fixe", "").lower() in ("oui", "yes", "1", "true"),
                     "personnalite": sec.get("personnalite", "").lower() in ("oui", "yes", "1", "true")}
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
        if word.lower() not in new_text.lower():  # "Flèche droite" vaut "flèche droite"
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
    if persona_active() and rewritable(cle, neutral):
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


def _unite_cle(mot):
    import unicodedata
    plain = unicodedata.normalize("NFKD", mot).encode("ascii", "ignore").decode().lower()
    return "unite." + re.sub(r"[^a-z0-9]+", "_", plain).strip("_")


def nombre(n, singulier, pluriel=None):
    """"1 note", "5 notes" : les nombres arrivent deja accordes dans les phrases.
    Le mot est traduit par la section [unite.<mot>] de la langue, sous la forme
    "singulier|pluriel" (jamais reecrite par la personnalite) ; sans section,
    le mot francais donne ici sert tel quel."""
    entry = base().get(_unite_cle(singulier))
    if entry and "|" in entry["texte"]:
        singulier, pluriel = (w.strip() for w in entry["texte"].split("|", 1))
    return f"{n} {singulier if abs(n) <= 1 else (pluriel or singulier + 's')}"


# --- tableaux d'etiquettes (2026-10-03) ---------------------------------------
# Les listes de choix (modes radio, champs de la grille de notation, debits de
# la voix...) gardent leurs codes dans le programme ; leurs libelles passent par
# la langue : section [<groupe>.<code>] (et [<groupe>.<code>.<valeur>] pour les
# choix d'un champ). Sans section, le libelle francais du programme sert tel
# quel. Jamais reecrits par la personnalite (ce sont des noms, pas des phrases).
# tools/phrases_convertir.py etiquettes : ecrit les sections manquantes.

import collections.abc

REGISTRE = []   # tous les tableaux, pour l'outil qui ecrit les sections


def _code(code):
    import unicodedata
    plain = unicodedata.normalize("NFKD", str(code)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", plain).strip("_") or "x"


def dire_ou(cle, defaut, **variables_):
    """dire(cle) si la langue a cette phrase, sinon le texte francais du
    programme (descriptions des commandes, ajoutees au fil de l'eau)."""
    return dire(cle, **variables_) if cle in base() else defaut


def etiquette(cle, defaut=None):
    entry = base().get(cle)
    return entry["texte"] if entry else (cle if defaut is None else defaut)


class Etiquettes(collections.abc.Mapping):
    """{code: libelle} traduit a la lecture : MODES["nfm"] -> "FM étroite"."""

    def __init__(self, groupe, libelles, contexte="", par_libelle=False):
        # par_libelle : la cle vient du libelle francais, pour les codes
        # illisibles ("\x1b[A" -> touche.fleche_haut)
        self.groupe, self._raw, self.contexte, self.par_libelle = groupe, dict(libelles), contexte, par_libelle
        REGISTRE.append(self)

    def cle(self, code):
        return f"{self.groupe}.{_code(self._raw[code] if self.par_libelle else code)}"

    def __getitem__(self, code):
        raw = self._raw[code]
        return None if raw is None else etiquette(self.cle(code), raw)  # None : rien a dire

    def __iter__(self):
        return iter(self._raw)

    def __len__(self):
        return len(self._raw)

    def sections(self):
        return [(self.cle(c), v, self.contexte) for c, v in self._raw.items() if v is not None]


class Champs(collections.abc.Sequence):
    """Grille de questions [(code, question, [(valeur, libelle)...], defaut)],
    traduite a la lecture ; les codes ne changent jamais (ils sont enregistres)."""

    def __init__(self, groupe, champs, contexte=""):
        self.groupe, self._raw, self.contexte = groupe, list(champs), contexte
        REGISTRE.append(self)

    def _cles(self, code, value=None):
        return f"{self.groupe}.{_code(code)}" + ("" if value is None else f".{_code(value)}")

    def __getitem__(self, i):
        if isinstance(i, slice):
            return [self[j] for j in range(*i.indices(len(self)))]
        code, question, options, *rest = self._raw[i]
        return (code, etiquette(self._cles(code), question),
                [(v, etiquette(self._cles(code, v), label)) for v, label in options], *rest)

    def __len__(self):
        return len(self._raw)

    def sections(self):
        out = []
        for code, question, options, *_ in self._raw:
            out.append((self._cles(code), question, self.contexte))
            out += [(self._cles(code, v), label, f"{self.contexte}, choix de {question}") for v, label in options]
        return out
