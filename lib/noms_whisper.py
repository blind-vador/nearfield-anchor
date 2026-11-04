"""Noms et contexte poses le soir, a partir du texte Whisper d'une prise validee.
Le nom n'ecrase jamais un nom deja donne. Une prise chiffree n'arrive pas ici.
"""
import json
import os
import re
import time

import chemins

FAITS = chemins.base("whisper_faits.json")
NOMS = chemins.base("whisper_canaux.json")
ENVOYES_SEC = 6 * 3600

_VIDES = {
    "euh", "alors", "oui", "ouais", "allo", "allô", "bon", "ben", "hein",
    "voila", "voilà", "ok", "donc", "et", "le", "la", "les", "un", "une",
    "de", "des", "du", "au", "aux", "a", "à", "je", "tu", "il", "on", "nous",
    "c'est", "cest", "est", "vraiment", "trop", "pas", "que", "qui", "en",
    "pour", "dans", "sur", "avec", "entre", "voit", "voir", "the", "you", "and",
}
_ANNONCE = ("bande", "band", "radio", "radioamateur", "amateur", "megahertz", "mégahertz", "fm", "am")
_OTAN = {
    "alpha", "bravo", "charlie", "charli", "delta", "echo", "foxtrot", "foxtrotte",
    "golf", "hotel", "hôtel", "india", "juliett", "juliette", "kilo", "lima",
    "mike", "november", "novembre", "oscar", "papa", "quebec", "québec", "romeo",
    "sierra", "tango", "uniform", "victor", "whiskey", "whisky", "xray", "yankee", "zulu",
}
_HALLU = (
    "sous-titre", "sous titre", "amara.org", "thanks for watching",
    "merci d'avoir regard", "abonne-toi", "abonnez-vous",
)


def _lire(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ecrire(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def deja(fichier):
    if not fichier:
        return True
    data = _lire(FAITS)
    if fichier in (data.get("faits") or {}):
        return True
    quand = (data.get("envoyes") or {}).get(fichier)
    return bool(quand and time.time() - quand < ENVOYES_SEC)


def marquer_envoye(fichier):
    data = _lire(FAITS)
    data.setdefault("envoyes", {})[fichier] = time.time()
    _ecrire(FAITS, data)


def marquer_fait(fichier):
    data = _lire(FAITS)
    data.setdefault("faits", {})[fichier] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (data.get("envoyes") or {}).pop(fichier, None)
    _ecrire(FAITS, data)


def oublier_envoi(fichier):
    if not fichier:
        return
    data = _lire(FAITS)
    (data.get("envoyes") or {}).pop(fichier, None)
    _ecrire(FAITS, data)


def _mots(texte):
    return re.findall(r"[0-9A-Za-zÀ-ÿ']+", (texte or "").lower())


def texte_utile(texte, label=""):
    """Enleve les hallucinations et l'annonce du scanner (« bande … »)."""
    label_mots = set(_mots(label))
    gardes = []
    for phrase in re.split(r"[.!?]+", texte or ""):
        phrase = " ".join(phrase.split())
        if not phrase:
            continue
        bas = phrase.lower()
        if any(h in bas for h in _HALLU):
            continue
        if ("canal station" in bas) or (bas.startswith("band") and "radio" in bas):
            continue
        mots = [m for m in _mots(phrase) if m not in _VIDES]
        if len(mots) < 2:
            continue
        annonce = sum(m in label_mots or m in _ANNONCE for m in mots)
        if annonce >= max(2, len(mots) - 1):
            continue
        gardes.append(phrase)
    return " ".join(gardes).strip()


def nom_propose(texte):
    """Deux mots assez longs, dans l'ordre dit. Pas l'alphabet radio, pas l'annonce."""
    mots = []
    for mot in re.findall(r"[0-9A-Za-zÀ-ÿ']+", texte or ""):
        bas = mot.lower()
        if bas in _VIDES or bas in _ANNONCE or bas in _OTAN or len(mot) < 5:
            continue
        mots.append(mot)
    if len(mots) < 2:
        return None
    return (mots[0] + " " + mots[1])[:42]


def _cle(freq_hz):
    return str(int(round(float(freq_hz) / 12500.0) * 12500))


def mhz(freq_hz):
    return f"{float(freq_hz) / 1e6:.4f}".rstrip("0").replace(".", ",")


def pres(freq_hz):
    """Nom pose par Whisper pour cette frequence, ou None."""
    if not freq_hz:
        return None
    return (_lire(NOMS).get(_cle(freq_hz)) or {}).get("nom")


def annoncer(freq_hz, mode, bande, label, texte, fichier, machine=""):
    """Depose une proposition de nom. Elle n'est posee que si l'utilisateur
    la confirme dans le journal nommage."""
    propre = texte_utile(texte, label)
    marquer_fait(fichier)
    if not propre or not freq_hz:
        print("[whisper] %s : rien d'utilisable" % fichier)
        return None
    try:  # un relais qui s'annonce : frequence, tonalite, indicatif
        import relais
        relais.depuis_texte(re.sub(r"m[ée]gahertz", "MHz", propre, flags=re.I), freq_hz, "whisper", mode, label, sur=True)
    except Exception as exc:
        print("[whisper] texte non lu pour les relais : %r" % (exc,))
    nom = nom_propose(propre)
    if not nom:
        print("[whisper] %s : pas de nom a proposer" % fichier)
        return None
    import nommage
    nommage.deposer(freq_hz, mode, bande, label, nom, propre, machine, fichier)
    return None
