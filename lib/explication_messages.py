"""Touche E des journaux messages et donnees (2026-11-07) : l'IA du cloud
explique en clair ce qu'est le message et ce qu'il veut dire.

La fiche entiere (<nom>.json : texte dit, donnees brutes, appareil...) part
avec la question. La reponse est gardee dans la fiche (champ explication_ia) :
la redemander ne coute plus de requete, meme apres un redemarrage.

2026-11-11 (plan phase 3) : la reponse est du JSON. En plus de l'explication lue,
l'IA rend les frequences et modulations citees dans le message, meme mal
formatees ; lib/frequences_ia les verifie, renforce celles qu'on connait et met
les autres en surveillance 14 jours. Une reponse qui n'est pas du JSON reste lue
telle quelle.
"""
import json
import os
import re

CHAMP = "explication_ia"
FICHE_MAX = 6000      # caracteres de fiche envoyes : un resume horaire peut etre long
TIMEOUT = 45
CATEGORIES = ("messages", "donnees")

CONSIGNE = """Tu aides un radioamateur aveugle qui écoute ce scanneur. Son journal \
« {journal} » contient l'entrée ci-dessous : le texte qui lui a été lu et les données brutes \
reçues, en clair. Explique-lui en {langue} clair, comme à quelqu'un de curieux mais pas spécialiste :
- ce qu'est ce message : quel système l'a émis, qui l'envoie d'habitude et à quoi il sert ;
- ce qu'il veut dire : les champs déjà nommés dans la fiche, les codes et abréviations \
documentés publiquement (label ACARS, symbole APRS, type de balise...), les nombres dont \
l'unité est donnée ;
- l'honnêteté avant tout : beaucoup de messages contiennent des suites de chiffres au format \
propre à une compagnie ou à un fabricant. N'en tire jamais une position, une altitude, une \
vitesse ou une heure si la fiche ne le dit pas ; dis seulement que ce bloc de chiffres suit un \
format privé qu'on ne peut pas lire. Une hypothèse se dit comme telle, avec « peut-être ».
Ta réponse sera lue par une synthèse vocale : texte simple, phrases courtes, pas de liste à \
puces, pas de tableau, pas de titre, pas de mise en forme, pas d'émoticône. Présente un sigle \
par ce qu'il désigne la première fois (par exemple « ACARS, le système de messages des \
avions »). Huit phrases au plus.

Reponds en JSON avec deux cles. "explication" : le texte ci-dessus, pret a etre lu. \
"frequences" : chaque frequence radio citee dans le message, meme sans unite ou mal ecrite \
(« 145.6375 », « 1456375 » dans un champ de frequence, « QSY 25 » si le contexte rend la valeur sure), \
sous la forme {{"mhz": nombre, "modulation": "FM, AM, BLU, DMR, D-Star, C4FM, numerique ou inconnue", \
"role": "sortie, entree, rendez-vous, frequence de travail...", "phrase": "extrait du message", \
"certitude": 0 a 1}} ; liste vide s'il n'y en a pas. Ne compte pas la frequence ou le message a ete recu.

Nom du fichier : {nom}
Fiche :
{fiche}"""


def fiche_de(path):
    try:
        with open(path[:-4] + ".json", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def deja(fiche):
    return (fiche or {}).get(CHAMP) or None


def question(category, path, fiche):
    import journaux
    contenu = {k: v for k, v in fiche.items() if k not in (CHAMP, "t")}
    texte = json.dumps(contenu, ensure_ascii=False, indent=1)
    if len(texte) > FICHE_MAX:
        texte = texte[:FICHE_MAX] + "\n[... fiche coupée]"
    import avions_info
    return CONSIGNE.format(journal=journaux.NAMES.get(category, category), langue=avions_info._langue(),
                           nom=os.path.basename(path), fiche=texte)


def nettoyer(texte):
    """Ce que la voix lirait mal : gras, titres, puces, liens."""
    texte = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", texte)
    texte = re.sub(r"[*_#`]+", "", texte)
    lignes = [re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", l).strip() for l in texte.splitlines()]
    return " ".join(l for l in lignes if l).strip()


def _garder(path, explication):
    """Ecrit la reponse dans la fiche, sans toucher au reste (ecriture atomique)."""
    chemin = path[:-4] + ".json"
    fiche = fiche_de(path)
    if fiche is None:
        return
    fiche[CHAMP] = explication
    tmp = f"{chemin}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fiche, f, ensure_ascii=False)
        os.replace(tmp, chemin)
    except OSError as exc:
        print(f"[explication] non gardee : {exc!r}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def expliquer(category, path):
    """(texte, None) ou (None, raison) ; raison "fiche" : rien a expliquer."""
    fiche = fiche_de(path)
    if not fiche:
        return None, "fiche"
    if deja(fiche):
        return fiche[CHAMP], None
    import gemini
    brut, raison = gemini.ask(question(category, path, fiche), temperature=0.3, timeout=TIMEOUT, scanneur=True,
                              json_mode=True)
    texte, trouvees = lire_reponse(brut)
    texte = nettoyer(texte or "")
    if not texte:
        return None, raison or "ia"
    if trouvees:
        try:
            import frequences_ia
            res = frequences_ia.ranger(trouvees, f"message {os.path.basename(path)[:40]}",
                                       fiche.get("frequence_hz") or fiche.get("freq_hz"))
            phrase = frequences_ia.phrase(res)
            if phrase:
                texte = f"{texte} {phrase}"
        except Exception as exc:
            print(f"[explication] frequences : {exc!r}")
    _garder(path, texte)
    return texte, None


def lire_reponse(brut):
    """(explication, frequences) d'une reponse JSON ; texte libre : (texte, [])."""
    brut = (brut or "").strip()
    try:
        data = json.loads(brut)
    except ValueError:
        return brut, []
    if not isinstance(data, dict):
        return brut, []
    trouvees = data.get("frequences")
    return str(data.get("explication") or ""), trouvees if isinstance(trouvees, list) else []
