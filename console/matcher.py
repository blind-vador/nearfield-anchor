"""Correspondance texte tape -> commande.

Deterministe, sans LLM (voir ROADMAP : latence, conflit avec l'imprimante,
et surtout risque d'interpretation erronee sur un outil qui pilote du
materiel). Deux passes :
1. mots-cles : correspondance instantanee et certaine ;
2. repli flou (fuzzywuzzy) sur des phrases d'exemple, pour accepter des
   reformulations jamais vues mot pour mot.
Sous FUZZY_THRESHOLD : "commande non reconnue" plutot que deviner
(fail closed).
"""
import unicodedata

from fuzzywuzzy import fuzz

FUZZY_THRESHOLD = 65


def normalize(text):
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    cleaned = "".join(c if c.isalnum() else " " for c in text)
    return " ".join(cleaned.split())


def _contains_phrase(haystack, phrase):
    # Correspondance sur mots entiers : "pi" ne doit pas matcher "impression".
    return f" {phrase} " in f" {haystack} "


def match(text, commands):
    """Retourne (commande ou None, score)."""
    typed = normalize(text)
    if not typed:
        return None, 0
    for cmd in commands:
        for kw in cmd.keywords:
            if _contains_phrase(typed, normalize(kw)):
                return cmd, 100
    best_cmd, best_score = None, 0
    for cmd in commands:
        for example in cmd.examples:
            ex = normalize(example)
            # token_set_ratio seul vaut 100 des qu'un mot tape est inclus dans
            # l'exemple ("le" -> "le pi chauffe") : moyenne avec
            # token_sort_ratio pour penaliser les saisies trop partielles.
            score = (fuzz.token_set_ratio(typed, ex) + fuzz.token_sort_ratio(typed, ex)) // 2
            if score > best_score:
                best_cmd, best_score = cmd, score
    if best_score >= FUZZY_THRESHOLD:
        return best_cmd, best_score
    return None, best_score
