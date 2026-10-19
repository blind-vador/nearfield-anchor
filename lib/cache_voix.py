"""Inventaire des caches de voix (commande "phrases", 2026-10-19) : chaque
phrase deja calculee, dans quelle voix, pour quelle veille, avec de quoi
l'ecouter, l'effacer, la valider ou la recalculer. Piper tire au hasard une
partie de sa prononciation : une phrase bafouillee, gardee en cache, serait
rejouee telle quelle a chaque fois ; on la recalcule jusqu'a ce qu'elle soit
bonne, puis on la valide.

Deux caches :
  console   ~/autopilot/voix_cache/<voix>/<md5(debit|texte)>.wav (console/voix.py)
            le texte n'est pas dans le nom : retrouve par index.tsv (note a
            chaque calcul depuis 2026-10-19) ou en recalculant l'empreinte
            des phrases connues ;
  annonces  tts_cache/<phrase>/texte.txt + un fichier par voix
            (<voix Microsoft>[@debit].mp3, piper-<voix>[@debit].wav).

Une phrase validee n'est jamais effacee par le menage du cache ni par
l'effacement d'une voix entiere ; la recalculer retire la validation.
"""
import hashlib
import json
import os
import re
import time

from sound_paths import TTS_CACHE_DIR

CONSOLE_DIR = "/home/vador/autopilot/voix_cache"
INDEX = "index.tsv"
TEXT_FILE = "texte.txt"
VALIDEES_PATH = "/home/vador/autopilot/voix_validees.json"
NEUTRE = "+0%"
ANNONCE_RE = re.compile(r"^(?P<voix>.+?)(?:@(?P<debit>[+-]\d+%)(?P<hauteur>[+-]\d+Hz)?)?\.(?:wav|mp3)$")
DEBITS_ESSAYES = [f"{p:+d}%" for p in range(-50, 105, 5)]


class Entree:
    """Un son en cache : chemin, texte (None si inconnu), voix ("piper:..."
    ou voix Microsoft), debit, hauteur, cache ("console" ou "annonces")."""
    __slots__ = ("chemin", "texte", "voix", "debit", "hauteur", "cache", "source", "famille")

    def __init__(self, chemin, texte, voix, debit, hauteur, cache):
        self.chemin, self.texte, self.voix = chemin, texte, voix
        self.debit, self.hauteur, self.cache = debit or NEUTRE, hauteur, cache
        self.source, self.famille = None, None


# --- validation ---------------------------------------------------------------------------

def validees():
    try:
        with open(VALIDEES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _ecrire_validees(data):
    tmp = VALIDEES_PATH + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, VALIDEES_PATH)


def valider(entree, oui=True):
    data = validees()
    if oui:
        data[entree.chemin] = {"texte": entree.texte, "voix": entree.voix, "t": time.time()}
    else:
        data.pop(entree.chemin, None)
    _ecrire_validees(data)


def est_validee(chemin, data=None):
    return chemin in (validees() if data is None else data)


# --- index du cache console ---------------------------------------------------------------

def empreinte(texte, debit):
    return hashlib.md5(f"{debit}|{texte}".encode("utf-8")).hexdigest()


_notes = set()


def noter(dossier, texte, debit):
    """Appele par console/voix.py a chaque son du cache : retrouver son texte."""
    d = empreinte(texte, debit)
    if (dossier, d) in _notes:
        return
    _notes.add((dossier, d))
    try:
        os.makedirs(dossier, exist_ok=True)
        with open(os.path.join(dossier, INDEX), "a", encoding="utf-8") as f:
            f.write(f"{d}\t{debit}\t{' '.join(texte.split())}\n")
    except OSError:
        pass


def _lire_index(dossier):
    found = {}
    try:
        with open(os.path.join(dossier, INDEX), encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t", 2)
                if len(parts) == 3:
                    found[parts[0]] = (parts[2], parts[1])
    except OSError:
        pass
    return found


def _debits_connus():
    debits = set(DEBITS_ESSAYES) | {NEUTRE}
    try:
        import reglages
        import voix_sources
        console = reglages.get("console") or {}
        debits |= {console.get("debit"), console.get("debit_echo")}
        for source in voix_sources.SOURCES:
            r = voix_sources.reglage(source)
            debits |= {r["debit"], r["secours_debit"], f"{r['debit']}~{r['hauteur']}"}
    except Exception:
        pass
    return {d for d in debits if d}


def _textes_connus(extra=()):
    import phrases
    import phrases_fixes
    textes = set(extra)
    for key in phrases.base():
        t = " ".join((phrases.texte(key) or "").split())
        if t and "{" not in t:
            textes.add(t)
        for s in phrases_fixes.split(t):
            if "{" not in s:
                textes.add(s)
    return textes


def _voix_console(nom_dossier):
    return "piper:" + nom_dossier[len("piper_"):] if nom_dossier.startswith("piper_") else nom_dossier


# --- inventaire ----------------------------------------------------------------------------

def inventaire(textes_en_plus=()):
    """Toutes les entrees des deux caches, avec veille et famille de phrases."""
    entrees = []
    reverse = None
    if os.path.isdir(CONSOLE_DIR):
        for nom in sorted(os.listdir(CONSOLE_DIR)):
            dossier = os.path.join(CONSOLE_DIR, nom)
            if not os.path.isdir(dossier):
                continue
            index = _lire_index(dossier)
            for f in os.listdir(dossier):
                if not f.endswith(".wav") or ".tmp" in f:
                    continue
                d = f[:-4]
                if d not in index:
                    if reverse is None:  # empreintes des phrases connues, calculees une fois
                        reverse = {}
                        textes = _textes_connus(textes_en_plus)
                        for debit in _debits_connus():
                            for t in textes:
                                reverse[empreinte(t, debit)] = (t, debit)
                    if d in reverse:
                        index[d] = reverse[d]
                texte, debit = index.get(d, (None, None))
                hauteur = None
                if debit and "~" in debit:
                    debit, hauteur = debit.split("~", 1)
                entrees.append(Entree(os.path.join(dossier, f), texte, _voix_console(nom), debit, hauteur, "console"))
    if os.path.isdir(TTS_CACHE_DIR):
        for nom in os.listdir(TTS_CACHE_DIR):
            dossier = os.path.join(TTS_CACHE_DIR, nom)
            try:
                with open(os.path.join(dossier, TEXT_FILE), encoding="utf-8") as fh:
                    texte = fh.read().strip()
                fichiers = os.listdir(dossier)
            except OSError:
                continue
            for f in fichiers:
                m = ANNONCE_RE.match(f)
                if not m or f == TEXT_FILE or ".tmp" in f:
                    continue
                voix = m["voix"]
                voix = "piper:" + voix[len("piper-"):] if voix.startswith("piper-") else voix
                entrees.append(Entree(os.path.join(dossier, f), texte, voix, m["debit"], m["hauteur"], "annonces"))
    _classer(entrees)
    return entrees


def _classer(entrees):
    """Veille (source de voix) et famille de phrases de chaque entree."""
    import phrases
    import phrases_fixes
    familles = {}
    for key in phrases.base():
        t = " ".join((phrases.texte(key) or "").split())
        fam = key.split(".")[0]
        for s in [t] + phrases_fixes.split(t):
            if s and "{" not in s:
                familles.setdefault(s, set()).add((fam, phrases_fixes.source_de(key)))
    try:
        import annonce_morceaux
        morceaux = annonce_morceaux.fixed_fragments_sources()
    except Exception:
        morceaux = {}
    for e in entrees:
        if e.texte is None:
            e.source, e.famille = "inconnues", None
            continue
        t = " ".join(e.texte.split())
        trouve = sorted(familles.get(t, ()), key=lambda p: (p[1] is None, p))
        if trouve:
            e.famille, src = trouve[0]
            e.source = src or ("console" if e.cache == "console" else "annonces")
        elif t in morceaux:
            e.source = sorted(s for s in morceaux[t] if s)[0] if any(morceaux[t]) else "annonces"
            e.famille = "morceaux"
        else:
            e.source = "console" if e.cache == "console" else "annonces"
            e.famille = "autres"


# --- actions ---------------------------------------------------------------------------------

def effacer(entrees, garder_validees=True):
    """Efface ces sons. Rend (effaces, gardes parce que valides)."""
    data = validees()
    faits = gardes = 0
    for e in entrees:
        if garder_validees and e.chemin in data:
            gardes += 1
            continue
        try:
            os.remove(e.chemin)
            faits += 1
        except OSError:
            pass
        data.pop(e.chemin, None)
    _ecrire_validees(data)
    return faits, gardes


def recalculer(e):
    """Nouveau calcul de ce son (Piper tire une autre prononciation a chaque
    fois ; Microsoft s'il repond). Rend (True, None) ou (False, raison)."""
    if e.texte is None:
        return False, "texte_inconnu"
    import piper_out
    if e.voix.startswith("piper:"):
        if piper_out.resolve(e.voix) is None:
            return False, "voix_absente"
        tmp = e.chemin + ".nouveau.wav"
        ok = piper_out.to_file(e.texte, tmp, voice_id=e.voix, scale_len=piper_out.length_scale(e.debit))
        if not ok:
            return False, "calcul_rate"
        os.replace(tmp, e.chemin)
    elif e.cache == "annonces":
        import annonce_morceaux
        tmp = e.chemin + ".nouveau.mp3"
        if not annonce_morceaux._synth_edge(e.texte, {"voix": e.voix, "debit": e.debit,
                                                      "hauteur": e.hauteur or "+0Hz"}, tmp):
            return False, "microsoft_absent"
        os.replace(tmp, e.chemin)
    else:
        try:
            import voix  # console/voix.py (dialogue lance depuis la console)
        except ImportError:
            return False, "microsoft_absent"
        rk = e.debit + (f"~{e.hauteur}" if e.hauteur else "")
        mp3 = voix._synthesize(e.texte, e.voix, rk)
        if not mp3:
            return False, "microsoft_absent"
        voix._store(mp3, e.chemin)
    valider(e, oui=False)
    return True, None

