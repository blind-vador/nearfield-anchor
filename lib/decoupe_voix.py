"""Decouper un long texte pour une voix qui borne la taille d'une requete (2026-11-21).

Google Traduction prend 180 caracteres au plus ; edge-tts coupe lui-meme a 4096 octets. Coupe au
dernier espace venu, une phrase s'arretait au milieu (« Le message indique que » ... longue pause ...
« cet avion approche de sa destination ») : chaque morceau est dit comme une phrase entiere, avec sa
chute et son silence. Ici, dans l'ordre : fins de phrase et retours a la ligne ; dans une phrase trop
longue, une ponctuation (virgule, point-virgule, deux-points, tiret, parenthese) ; sinon avant un mot
qui ouvre une proposition (qui, que, et, mais...) ; sinon a un espace qui ne laisse pas un petit mot
en fin de morceau (le, de, que...). Les phrases courtes voisines sont regroupees jusqu'a la taille.
"""
import re

_FIN = re.compile(r"(?<=[.!?…])\s+|\s*\n+\s*")
_PONCT = (",", ";", ":", " —", " –", " -", ")", "»")
OUVRANTS = ("qui", "que", "qu'", "qu’", "dont", "où", "et", "mais", "ou", "donc", "car", "puis", "lorsque",
            "quand", "parce", "afin", "alors", "tandis", "sinon", "avant", "après", "pendant", "comme", "si")
FAIBLES = {"le", "la", "les", "l'", "l’", "un", "une", "des", "du", "de", "d'", "d’", "à", "au", "aux", "en",
           "que", "qu'", "qu’", "qui", "et", "ou", "ce", "cet", "cette", "ces", "son", "sa", "ses", "mon", "ma",
           "mes", "ton", "ta", "tes", "notre", "nos", "votre", "vos", "leur", "leurs", "par", "pour", "sur",
           "sous", "dans", "avec", "sans", "ne", "n'", "se", "s'", "il", "elle", "ils", "elles", "on", "nous",
           "vous", "je", "j'", "tu", "y", "dont", "où", "très", "plus", "moins", "the", "a", "an", "of", "to"}


def phrases(texte):
    return [p.strip() for p in _FIN.split(str(texte or "")) if p and p.strip()]


def _dernier_mot(t):
    return t.rsplit(None, 1)[-1].lower() if t.strip() else ""


def _coupe(phrase, taille, minimum):
    """Indice ou couper une phrase trop longue (le morceau gauche fait au plus `taille`)."""
    fenetre = phrase[:taille + 1]
    meilleur = -1
    for p in _PONCT:                                   # apres une ponctuation
        i = fenetre.rfind(p)
        if i >= minimum and i + len(p) <= taille:
            meilleur = max(meilleur, i + len(p))
    if meilleur > 0:
        return meilleur
    for m in reversed(list(re.finditer(r"\s(%s)(?=[\s'’])" % "|".join(re.escape(o) for o in OUVRANTS), fenetre, re.I))):
        i = m.start()
        while i >= minimum and _dernier_mot(fenetre[:i]) in FAIBLES:     # « ... et » | « que ... » : avant « et »
            i = fenetre.rfind(" ", 0, i)
        if i >= minimum:                               # avant le mot qui ouvre la suite
            return i
    for m in reversed(list(re.finditer(r"\s", fenetre))):
        if m.start() < minimum:
            break
        mot = fenetre[:m.start()].rsplit(None, 1)[-1].lower() if fenetre[:m.start()].strip() else ""
        if mot not in FAIBLES:                         # jamais « ... indique que » | « cet avion »
            return m.start()
    i = fenetre.rfind(" ")
    return i if i > 0 else taille


def morceaux(texte, taille, minimum=None, regrouper=True, mesure=len):
    """Les morceaux a dire, chacun de `mesure` au plus `taille` (len : caracteres ; ou octets)."""
    minimum = taille // 3 if minimum is None else minimum
    out = []
    for phrase in phrases(texte):
        while mesure(phrase) > taille:
            limite = taille
            while mesure(phrase[:limite]) > taille and limite > 1:     # octets : multi-octets
                limite -= max(1, (mesure(phrase[:limite]) - taille) // 2)
            i = _coupe(phrase, limite, min(minimum, limite // 2))
            gauche, phrase = phrase[:i].strip(), phrase[i:].strip()
            if gauche:
                out.append(gauche)
        if phrase:
            out.append(phrase)
    if not regrouper:
        return out
    groupes = []
    for m in out:
        if groupes and mesure(groupes[-1] + " " + m) <= taille:
            groupes[-1] += " " + m
        else:
            groupes.append(m)
    return groupes


def octets(t):
    return len(t.encode("utf-8"))


EDGE_MAX_OCTETS = 3000     # edge-tts coupe lui-meme a 4096 octets, au dernier espace : on coupe avant, en phrases


async def edge_enregistrer(edge_tts, texte, voix, chemin, debit="+0%", hauteur="+0Hz"):
    """Le texte dit par une voix Microsoft dans `chemin` (MP3) ; un long texte part en plusieurs requetes
    coupees entre deux phrases, mises bout a bout dans le meme fichier."""
    if octets(texte) <= EDGE_MAX_OCTETS:
        await edge_tts.Communicate(texte, voix, rate=debit, pitch=hauteur).save(chemin)
        return
    with open(chemin, "wb") as f:
        for m in morceaux(texte, EDGE_MAX_OCTETS, mesure=octets):
            async for bloc in edge_tts.Communicate(m, voix, rate=debit, pitch=hauteur).stream():
                if bloc.get("type") == "audio":
                    f.write(bloc["data"])
