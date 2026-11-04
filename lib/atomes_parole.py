"""Mots preenregistres pour les trous du cache (2026-11-01).

Un nombre n'est plus une phrase nouvelle a chaque valeur : 410 se dit en
collant des morceaux deja en cache, « 400 » puis « dix », fabriques
une fois par les voix distantes (Microsoft, Mac, Google) et par les voix
locales. Les centaines, les milliers, les heures et les minutes sont
ecrits en chiffres : « 6000 » se lit « si mille », « six mille » se lit
« siss mille ». « et un » est un seul mot : « vingt » puis « et » puis
« un » sonne comme une liste. Une lettre seule, un indicatif d'avion ou
une donnee qui mele lettres et chiffres (hors mot) se dit en alphabet
radio international : AFR410 -> alfa, foxtrotte, roméo, puis 400, dix.

Le reste de la phrase (mots, noms, phrases fixes) n'est pas decoupe.
"""
import re
import unicodedata

# Pauses lues par annonce_morceaux (pas des signes a prononcer).
COLLE_NOMBRE = "\x1c"   # entre « vingt » et « et un » : aucun silence, un seul groupe
COLLE_LETTRE = "\x1d"   # entre deux lettres : le meme ecart que « cinquante » / « et un »
COLLE_SUITE = "\x1e"    # nombre colle au mot qui suit (« une » + « heure »)

UNITES = [
    "zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf",
    "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize",
]
_DIZAINES = ("vingt", "trente", "quarante", "cinquante", "soixante")
# 17, 18, 19 d'un seul tenant : « dix » puis « sept » ne sonne pas « dissett ».
_COMPOSES = (
    "dix-sept", "dix-huit", "dix-neuf",
    "quatre-vingts", "quatre-vingt-dix",
)

# Graphie francaise : « Foxtrot » est lu « foxtrot », « foxtrotte » est lu foxtrotte.
ICAO = {
    "A": "alfa", "B": "bravo", "C": "charli", "D": "delta", "E": "écho",
    "F": "foxtrotte", "G": "golf", "H": "hôtel", "I": "indya", "J": "juliette",
    "K": "kilo", "L": "lima", "M": "maïk", "N": "novembre", "O": "oscar",
    "P": "papa", "Q": "québec", "R": "roméo", "S": "sièra", "T": "tango",
    "U": "uniforme", "V": "victor", "W": "ouiski", "X": "iksrè", "Y": "yanki",
    "Z": "zoulou",
}
# Anciennes graphies anglaises, ramenées a la forme dite.
_RADIO = {v.lower(): v for v in ICAO.values()}
_RADIO.update({
    "alpha": "alfa", "charlie": "charli", "echo": "écho", "foxtrot": "foxtrotte",
    "hotel": "hôtel", "india": "indya", "juliett": "juliette", "juliet": "juliette",
    "mike": "maïk", "november": "novembre", "quebec": "québec", "romeo": "roméo",
    "sierra": "sièra", "uniform": "uniforme", "whisky": "ouiski", "whiskey": "ouiski",
    "x-ray": "iksrè", "xray": "iksrè", "yankee": "yanki", "zulu": "zoulou",
})
# Le « s » de « cents » / « vingts » ne s'entend pas : meme mot en cache.
_ALIAS = {"cents": "cent", "vingts": "vingt"}
# « et un » d'un bloc : trois clips (« vingt », « et », « un ») sonnent
# « vingt, et, un ». « une » et « premier » accordent le 1 selon le mot d'apres.
_ACCORDS = ("et un", "et une", "et onze", "une", "premier")
_MOTS_NOMBRE = set(UNITES) | set(_DIZAINES) | set(_COMPOSES) | set(_ACCORDS) | {
    "cent", "mille", "et", "million", "millions", "milliard", "milliards",
    "virgule", "moins", "zéro",
}
# Devant ces mots, « un » se dit « une » (et « et un » se dit « et une »).
_FEMININ = {
    "heure", "heures", "minute", "minutes", "seconde", "secondes",
    "notification", "notifications", "entrée", "entrées", "nouvelle", "nouvelles",
}
_MOIS = {
    "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
    "septembre", "octobre", "novembre", "décembre",
}
# Le nombre se colle a ce qui le nomme, sans le silence d'une virgule.
_APRES_NOMBRE = _FEMININ | _MOIS | {
    "mètre", "mètres", "kilomètre", "kilomètres", "degré", "degrés",
    "millimètre", "millimètres", "centimètre", "centimètres",
}
# Un de ces mots, seul, est deja un nombre (« mille », « vingt »). « un » ou
# « et » seuls restent des mots de la phrase (« un avion », « et a destination »).
_FORTS = _MOTS_NOMBRE - set(UNITES) - set(_ACCORDS) - {"et", "moins", "virgule"}
_LIAISONS = {"tiret", "barre"}
# « km2 », « 100Hz » : une unite, pas un indicatif.
_UNITES_MESURE = {
    "km", "m", "cm", "mm", "h", "db", "hz", "mhz", "ghz", "khz",
    "kg", "g", "kb", "mb", "go", "mo", "ko",
}

_PONCT = ".,:;!?…"
_NOMBRE = re.compile(r"-?\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d+)?|-?\d+(?:[,.]\d+)?")
_CODE = re.compile(
    r"[A-Z]{1,2}(?:-[A-Z0-9]{2,6})+"                       # F-HBOD, F-HABC
    r"|(?:[A-Za-z]{1,12}\d|\d+[A-Za-z])[A-Za-z0-9-]{0,14}"  # AFR410, F4ABC-9, B12
)
_LETTRE = re.compile(r"[A-Z]")
# Bande ou modulation en capitales : toujours l'alphabet radio, qu'il y ait
# un chiffre (P25 l'était déjà) ou non (VHF, BLU, FM ne l'étaient pas).
_SIGLES = {
    "AM", "FM", "CW", "HF", "CB", "BLU", "SSB", "USB", "LSB",
    "NFM", "WFM", "VHF", "UHF", "SHF", "LPD", "PMR", "DMR",
    "DAB", "GSM", "AIS", "APRS", "FSK", "SSTV", "RTTY",
    "DSTAR", "NXDN", "POCSAG", "GSM-R",
}
_SIGLE_ALT = "|".join(sorted(_SIGLES, key=len, reverse=True))
def _centaine(n):
    """100, 200, ... 900, écrits en chiffres. « six cent » est lu « siss cent » ;
    « 600 » est lu « si cent »."""
    return str(int(n) * 100)


def _millier(k):
    """1000, 2000, ... 10000, écrits en chiffres. Au-delà de dix mille, None.
    « six mille » est lu « siss mille » ; « 6000 » est lu « si mille »."""
    if 1 <= k <= 10:
        return str(int(k) * 1000)
    return None


def phrase_heure(n):
    """Une des 24 heures, en chiffres : « 1 heure », « 21 heures ». None hors de 0..23."""
    n = int(n)
    if 0 <= n <= 23:
        return f"{n} heure" if n <= 1 else f"{n} heures"
    return None


def phrase_minute(n):
    """Une des 60 minutes, en chiffres : « 1 minute », « 45 minutes ». None hors de 0..59."""
    n = int(n)
    if 0 <= n <= 59:
        return f"{n} minute" if n <= 1 else f"{n} minutes"
    return None


_COMPOSES_GARDES = re.compile(
    r"quatre-vingt-dix|quatre-vingts|quatre-vingt|dix-sept|dix-huit|dix-neuf",
    re.IGNORECASE,
)


def sans_separateurs(text):
    """Le tiret, la barre et les traits ne se prononcent pas.
    Les nombres composés (dix-sept, quatre-vingts) gardent leur trait d'union."""
    if not text:
        return text
    gardes = []

    def garder(m):
        gardes.append(m.group(0))
        return f"\ue000{len(gardes) - 1}\ue001"

    text = _COMPOSES_GARDES.sub(garder, text)
    text = re.sub(r"(?i)(?<!\w)(?:tirets?|barres?)(?!\w)", " ", text)
    text = re.sub(r"[-‐‑‒–—―/_|]+", " ", text)

    def remettre(m):
        return gardes[int(m.group(1))]

    text = re.sub(r"\ue000(\d+)\ue001", remettre, text)
    return re.sub(r"[ \t]{2,}", " ", text)


def vocabulaire():
    """Mots a fabriquer une fois par voix : nombres, heures, minutes,
    milliers, et les lettres de l'alphabet radio."""
    mots = set(UNITES) | set(_DIZAINES) | set(_COMPOSES) | set(_ACCORDS) | {
        "cent", "mille", "et", "million", "millions", "milliard", "milliards",
        "virgule", "moins",
    } | {_centaine(n) for n in range(1, 10)} | {_millier(k) for k in range(1, 11)}
    mots |= {phrase_heure(h) for h in range(24)}
    mots |= {phrase_minute(m) for m in range(60)}
    mots |= set(ICAO.values())
    return sorted(mots)


def normaliser(text):
    return unicodedata.normalize("NFC", text or "")


def mots_entier(n):
    """410 -> [« 400 », « dix »]. Francais de France. Les centaines et les
    milliers ronds sont des chiffres : la voix les lit avec la bonne liaison."""
    n = int(n)
    if n < 0:
        return ["moins"] + mots_entier(-n)
    if n < 17:
        return [UNITES[n]]
    if n < 20:
        return [("dix-sept", "dix-huit", "dix-neuf")[n - 17]]
    if n < 70:
        nom = _DIZAINES[n // 10 - 2]
        u = n % 10
        if u == 0:
            return [nom]
        if u == 1:
            return [nom, "et un"]
        return [nom] + mots_entier(u)
    if n < 80:
        reste = n - 60
        if reste == 11:
            return ["soixante", "et onze"]
        return ["soixante"] + mots_entier(reste)
    if n < 100:
        reste = n - 80
        if reste == 0:
            return ["quatre-vingts"]
        if reste == 10:
            return ["quatre-vingt-dix"]
        return ["quatre-vingts"] + mots_entier(reste)
    if n < 1000:
        centaines, reste = divmod(n, 100)
        base = [_centaine(centaines)]
        return base if reste == 0 else base + mots_entier(reste)
    if n < 1_000_000:
        milliers, reste = divmod(n, 1000)
        bloc = _millier(milliers)
        base = [bloc] if bloc else mots_entier(milliers) + ["mille"]
        return base if reste == 0 else base + mots_entier(reste)
    if n < 1_000_000_000_000:
        if n >= 1_000_000_000:
            gros, reste = divmod(n, 1_000_000_000)
            mot = "milliard" if gros == 1 else "milliards"
        else:
            gros, reste = divmod(n, 1_000_000)
            mot = "million" if gros == 1 else "millions"
        base = mots_entier(gros) + [mot]
        return base if reste == 0 else base + mots_entier(reste)
    return [UNITES[int(d)] for d in str(abs(n))]


def _mots_chaine_entier(s):
    """« 007 » -> zéro zéro sept ; « 410 » -> 400, dix."""
    neg = s.startswith("-")
    s = s[1:] if neg else s
    zeros = 0
    i = 0
    while i < len(s) - 1 and s[i] == "0":
        zeros += 1
        i += 1
    mots = ["zéro"] * zeros + mots_entier(int(s[i:] or "0"))
    return (["moins"] + mots) if neg else mots


def mots_nombre(tok):
    """Un jeton chiffre, ou None si ce n'en est pas un. « 12,5 » -> douze virgule cinq."""
    tok = tok.replace("\u00a0", " ").strip()
    if re.fullmatch(r"-?\d{1,3}(?: \d{3})+", tok):
        return _mots_chaine_entier(tok.replace(" ", ""))
    m = re.fullmatch(r"(-?\d+)[,.](\d+)", tok)
    if m:
        return _mots_chaine_entier(m.group(1)) + ["virgule"] + [UNITES[int(d)] for d in m.group(2)]
    if re.fullmatch(r"-?\d+", tok):
        return _mots_chaine_entier(tok)
    return None


def _est_unite(tok):
    lettres = re.findall(r"[A-Za-z]+", tok)
    return bool(lettres) and all(x.lower() in _UNITES_MESURE for x in lettres)


def mots_indicatif(code):
    """« AFR410 » -> alfa foxtrotte roméo, 400, dix.
    « F-HBOD » -> foxtrotte, hôtel, bravo, oscar, delta.
    « F4ABC-9 » -> foxtrotte, quatre, alfa, bravo, charli, neuf.
    Le tiret et la barre ne se prononcent pas."""
    out = []
    for part in re.findall(r"[A-Za-z]+|\d+|[-/]", code.strip()):
        if part in "-/":
            continue
        elif part.isdigit():
            out.extend((m, "nombre") for m in _mots_chaine_entier(part))
        else:
            for ch in part.upper():
                if ch in ICAO:
                    out.append((ICAO[ch], "lettre"))
    return out


def _canon_mot(tok):
    """Mot d'atome reconnu dans un texte deja ecrit en toutes lettres, ou None."""
    nu = tok.strip(_PONCT)
    if not nu:
        return None
    radio = _RADIO.get(nu.lower())
    if radio:
        return radio, "lettre"
    bas = _ALIAS.get(nu.lower(), nu.lower())
    if bas in _COMPOSES:
        return bas, "nombre"
    if bas in _LIAISONS:
        return None
    if bas in _MOTS_NOMBRE:
        return bas, "nombre"
    if "-" in nu:
        bouts = nu.split("-")
        canons = [_canon_mot(b) for b in bouts]
        if bouts and all(c and c[1] == "nombre" for c in canons):
            return [c[0] for c in canons], "nombre"
    return None


def _tokens(text):
    return [t for t in re.findall(r"\S+", text) if t.strip(_PONCT)]


def _seulement_atomes(text):
    toks = _tokens(text)
    if not toks:
        return False
    return all(_canon_mot(t) for t in toks)


def _morceaux_mot(tok):
    """[(mot, genre)] d'un jeton deja en toutes lettres."""
    got = _canon_mot(tok)
    if not got:
        return []
    mots, genre = got
    if isinstance(mots, list):
        return [(m, genre) for m in mots]
    return [(mots, genre)]


def _run_utile(morceaux):
    """Une suite de mots d'atome merite d'etre collee (pas « un » ou « et » seuls)."""
    if any(g == "lettre" for _, g in morceaux):
        return True
    nombres = [m for m, g in morceaux if g == "nombre"]
    if len(nombres) >= 2:
        return True
    return bool(nombres) and nombres[0] in _FORTS


def _depuis_mots(text):
    out = []
    for tok in _tokens(text):
        out.extend(_morceaux_mot(tok))
    return out


# La ponctuation collee au chiffre est avalee avec lui, puis jetee :
# « 410. » ne doit pas poser un silence de fin de phrase au milieu du nombre.
# œ n'est pas dans À-ÿ : sans lui, le N de « Nœud » devenait November.
_LET = "A-Za-zÀ-ÿŒœÆæ"
_SCAN = re.compile(
    rf"(?<![{_LET}0-9])("
    r"(?:-?\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d+)?|-?\d+(?:[,.]\d+)?)[.,:;!?…]*"
    r"|[A-Z]{1,2}(?:-[A-Z0-9]{2,6})+"
    rf"|(?:[A-Za-z]{{1,12}}\d|\d+[A-Za-z])[A-Za-z0-9-]{{0,14}}"
    rf"|{_SIGLE_ALT}"
    r"|[A-Z]"
    rf")(?![{_LET}0-9])"
)
# Indicatif déjà épelé (« F 4 A B C ») ou collé (« F4ABC-7 », « AFR410 »).
_APPELLATIF = re.compile(
    rf"(?<![{_LET}0-9])(?:[A-Z]|\d)(?:\s+(?:[A-Z]|\d|tiret|barre)){{2,}}"
    rf"|(?<![{_LET}0-9])[A-Za-z]{{1,2}}\d[A-Za-z0-9-]{{1,10}}(?![{_LET}0-9])"
)
# « axe X » : X est la lettre de l'axe, pas un indicatif (X-ray).
_APRES_AXE = re.compile(r"(?i)(?:axes|axe|but[ée]es?)\s*$")


def _coeur_nombre(tok):
    """Retire la ponctuation finale, pas la virgule decimale de « 12,5 »."""
    return tok.replace("\u00a0", " ").rstrip(_PONCT)


def _elision(text, start, tok):
    """« C'est », « N'est », « L'avion » : une élision, pas Charli ni Novembre."""
    if not _LETTRE.fullmatch(tok):
        return False
    apres = text[start + len(tok):]
    return bool(apres) and apres[0] in "'’" and len(apres) > 1 and apres[1].isalpha()


def _lettre_laisee(text, start, tok):
    """X, Y, Z apres « axe » ou devant « : » restent des lettres, pas X-ray."""
    if not _LETTRE.fullmatch(tok):
        return False
    avant = text[:start]
    if _APRES_AXE.search(avant):
        return True
    if tok in "XYZ":
        apres = text[start + len(tok):].lstrip()
        if not apres or apres[0] in ".:":
            return True
        if re.search(r"(?:^|[:.,;]\s*)$", avant):
            return True
    return False


def _pieces_code(tok):
    coeur = _coeur_nombre(tok)
    if mots_nombre(coeur) is not None and _NOMBRE.fullmatch(coeur):
        return [(m, "nombre") for m in mots_nombre(coeur)]
    if _est_unite(tok):
        return None
    if tok.upper() in _SIGLES:
        return mots_indicatif(tok.upper())
    if _LETTRE.fullmatch(tok):
        return [(ICAO[tok], "lettre")]
    if _CODE.fullmatch(tok):
        return mots_indicatif(tok)
    return None


def _promouvoir(pieces):
    """« Foxtrot quatre Alpha » : « quatre », entre deux lettres radio, devient un atome.
    « sept mille » au milieu d'une phrase reste une phrase."""
    out = []
    i = 0
    while i < len(pieces):
        texte, genre = pieces[i]
        if genre != "prose":
            out.append((texte, genre))
            i += 1
            continue
        j = i
        bloc = []
        while j < len(pieces) and pieces[j][1] == "prose":
            bloc.append(pieces[j][0])
            j += 1
        brut = "".join(bloc)
        avant = out[-1][1] if out else None
        apres = pieces[j][1] if j < len(pieces) else None
        morceaux = _depuis_mots(brut) if brut.strip() else []
        # « novembre » dans une date reste le mois : la lettre N est déjà
        # « novembre », les deux ne doivent pas se confondre ici.
        if morceaux and all(m.lower() in _MOIS for m, _ in morceaux):
            out.append((brut, "prose"))
            i = j
            continue
        if morceaux and _seulement_atomes(brut) and (avant in ("lettre", "nombre") or apres in ("lettre", "nombre") or _run_utile(morceaux)):
            # Un « et » colle au nombre seulement s'il est dans la suite, pas tout seul
            # entre deux bouts de phrase. _run_utile / voisin atome couvrent les deux cas.
            if avant in ("lettre", "nombre") or apres in ("lettre", "nombre") or _run_utile(morceaux):
                out.extend(morceaux)
            else:
                out.append((brut, "prose"))
        else:
            if brut:
                out.append((brut, "prose"))
        i = j
    return [(t, g) for t, g in out if str(t).strip()]


def _premier_mot(texte):
    m = re.search(r"[A-Za-zÀ-ÿ]+", texte or "")
    return m.group(0).lower() if m else ""


def _dernier_mot(texte):
    mots = re.findall(r"[A-Za-zÀ-ÿ]+", texte or "")
    return mots[-1].lower() if mots else ""


def liaison(texte):
    """Le mot qui suit un nombre se colle a lui (heure, mètre, novembre)."""
    return _premier_mot(texte) in _APRES_NOMBRE


def _fusionner(run):
    """« et » + « un » -> « et un », un seul enregistrement."""
    out = []
    i = 0
    while i < len(run):
        if run[i] == "et" and i + 1 < len(run) and run[i + 1] in ("un", "onze"):
            out.append("et " + run[i + 1])
            i += 2
        else:
            out.append(run[i])
            i += 1
    return out


def _est_phrase_compte(mot):
    """« 3 heures », « 1 minute » : déjà une phrase, pas un morceau de nombre."""
    return (mot or "").endswith((" heure", " heures", " minute", " minutes"))


def _accorder(pieces):
    """« 1 novembre » -> premier ; « 21 heures » reste « 21 heures » ;
    « 3 heures 1 » -> la minute seule se dit « une »."""
    out = []
    i = 0
    while i < len(pieces):
        if pieces[i][1] != "nombre":
            out.append(pieces[i])
            i += 1
            continue
        j = i
        while j < len(pieces) and pieces[j][1] == "nombre":
            if j > i and _est_phrase_compte(pieces[j][0]):
                break
            j += 1
            if _est_phrase_compte(pieces[j - 1][0]):
                break
        run = _fusionner([m for m, _ in pieces[i:j]])
        precedent = _dernier_mot(out[-1][0]) if out else ""
        suivant = _premier_mot(pieces[j][0]) if j < len(pieces) and pieces[j][1] == "prose" else ""
        if suivant in _MOIS and run == ["un"]:
            run = ["premier"]
        elif suivant in _FEMININ:
            if run and run[-1] == "et un":
                run[-1] = "et une"
            elif run and run[-1] == "un":
                run[-1] = "une"
        elif run == ["un"] and precedent in ("heure", "heures"):
            run = ["une"]
        out.extend((m, "nombre") for m in run)
        i = j
    return out


def decouper(text):
    """[(morceau, genre)] genre « prose », « nombre » ou « lettre ».
    Sans chiffre, sans indicatif et sans mot de l'alphabet radio : un seul
    morceau, la phrase telle quelle."""
    text = normaliser(text)
    if not text or not text.strip():
        return []
    if _seulement_atomes(text):
        mots = _depuis_mots(text)
        if mots and _run_utile(mots):
            return _accorder(mots)
        if mots and any(g == "lettre" for _, g in mots):
            return _accorder(mots)
    pieces = []
    pos = 0
    for m in _SCAN.finditer(text):
        tok = m.group(1)
        if _lettre_laisee(text, m.start(), tok) or _elision(text, m.start(), tok):
            continue
        fin = m.end()
        coeur = _coeur_nombre(tok)
        if re.fullmatch(r"\d{1,2}", coeur):
            suite = re.match(r"\s+(heure|heures|minute|minutes)\b", text[fin:], re.I)
            if suite:
                n = int(coeur)
                mot = suite.group(1).lower()
                phrase = phrase_heure(n) if mot.startswith("heure") else phrase_minute(n)
                if phrase:
                    if m.start() > pos:
                        pieces.append((text[pos:m.start()], "prose"))
                    pieces.append((phrase, "nombre"))
                    pos = fin + suite.end()
                    continue
        got = _pieces_code(tok)
        if not got:
            continue
        if m.start() > pos:
            pieces.append((text[pos:m.start()], "prose"))
        pieces.extend(got)
        pos = fin
    if pos < len(text):
        pieces.append((text[pos:], "prose"))
    if not pieces:
        propre = sans_separateurs(_a_plat(text))
        return [(propre, "prose")] if propre.strip() else []
    propres = []
    for texte, genre in _promouvoir(pieces):
        if genre == "prose":
            texte = sans_separateurs(_a_plat(texte))
        nu = texte.strip()
        if not nu:
            continue
        if genre == "prose" and all(c in _PONCT for c in nu) and propres and propres[-1][1] == "nombre":
            continue
        propres.append((texte, genre))
    return _accorder(propres) or [(sans_separateurs(_a_plat(text)), "prose")]


def _a_plat(texte):
    """Un « à » isolé, même au milieu d'une annonce envoyée d'un bloc.
    « déjà » garde le sien : la lettre n'y est pas un mot."""
    return re.sub(r"(?<![\w])[àÀ](?![\w])", "a", texte or "")


def indicatifs_en_clair(text):
    """Pour un texte dit d'un bloc : l'indicatif est déjà en mots radio.
    Les nombres ordinaires (« 11 degrés ») restent des chiffres."""
    text = normaliser(text)
    if not text:
        return text
    out, pos = [], 0
    for m in _SCAN.finditer(text):
        tok = m.group(1)
        if _lettre_laisee(text, m.start(), tok) or _elision(text, m.start(), tok):
            continue
        got = _pieces_code(tok)
        if not got or not any(g == "lettre" for _, g in got):
            continue
        out.append(text[pos:m.start()])
        out.append(" ".join(mot for mot, _ in got))
        pos = m.end()
    out.append(text[pos:])
    return _a_plat(sans_separateurs("".join(out)))


def a_decouper(text):
    """Vrai si decouper sort autre chose que la phrase d'un seul tenant."""
    parts = decouper(text)
    if len(parts) != 1:
        return bool(parts)
    return parts[0][1] != "prose"


# Au-dela, ce n'est plus une annonce a la volee : flash info, flash trafic,
# flash meteo. Le texte part entier au moteur, qui le genere d'un bloc.
_COURT_MAX = 320


def annonce_courte(text):
    """Une phrase courte (heure, altitude, indicatif). Un flash en fait plusieurs.
    Un indicatif APRS ou LoRa reste découpé, même avec une seconde phrase :
    sinon les lettres ne passent pas par l'alphabet radio."""
    body = (text or "").strip()
    if not body:
        return False
    phrases = len(re.findall(r"[.!?…]", body))
    if _APPELLATIF.search(body) and phrases <= 3 and len(body) <= 500:
        return True
    if phrases > 1 or len(body) > _COURT_MAX:
        return False
    return True
