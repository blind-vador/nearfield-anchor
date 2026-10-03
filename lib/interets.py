"""Centres d'interet de l'utilisateur (2026-10-03) : chaque piqure du
scanner (lib/attention.py) est ponderee par l'interet REEL de la cible.

Demande utilisateur : la radio FM commerciale a toujours du trafic, donc la
plus forte piqure, alors qu'elle sert surtout a verifier que le scanner
marche ; un autre utilisateur voudra privilegier les talkies 446, un autre
les avions. Les cibles sont regroupees en categories parlees ; chaque
categorie a un niveau (aucun, faible, normal, fort, passion) qui multiplie
la piqure. Le plancher d'exploration (toute bande revue au moins toutes les
30 min) et l'apprentissage des stations ne changent pas : seule la priorite
donnee par la piqure suit l'interet.

Reglage "interets" (NAS, comme les autres) : {categorie: niveau}. Commande
console "interets" (console/interets_cmd.py).
"""
import time

from phrases import Etiquettes

LEVELS = ["aucun", "faible", "normal", "fort", "passion"]
WEIGHTS = {"aucun": 0.1, "faible": 0.4, "normal": 1.0, "fort": 1.8, "passion": 2.5}
LEVEL_NAMES = Etiquettes("interet.niveau", {l: l for l in LEVELS}, "niveau d'intérêt pour une catégorie d'écoute")

# categorie -> (cibles du scanner, niveau par defaut). Prefixes acceptes ("relais_").
CATEGORIES = {
    "talkies": (["pmr446", "lpd433"], "normal"),
    "aviation": (["aviation_atis", "aviation_st_etienne", "aviation_tour", "aviation_sol", "aviation_approche",
                  "aviation", "milair", "acars", "vdl2", "adsb"], "normal"),
    "radioamateurs": (["radioamateur_", "relais_", "aprs", "lora"], "normal"),
    "cb": (["cb_am"], "normal"),
    "meteo": (["capteurs", "sondes"], "normal"),
    "secours": (["pagers", "balises"], "normal"),
    "marine": (["marine_vhf"], "normal"),
    "professionnels": (["uhf_business", "vhf_business", "vhf_80", "uhf_410", "vhf_low", "pmse"], "normal"),
    "fm": (["fm_diffusion"], "faible"),   # bandes de verification : du trafic tout le temps
}
CATEGORY_NAMES = Etiquettes("interet.categorie", {
    "talkies": "talkies-walkies 446 et LPD",
    "aviation": "aviation : tour, approche, messages et avions en direct",
    "radioamateurs": "radioamateurs : bandes, relais, APRS et LoRa",
    "cb": "CB 27 mégahertz",
    "meteo": "météo : capteurs sans fil et ballons-sondes",
    "secours": "secours : pagers et balises de détresse",
    "marine": "VHF marine",
    "professionnels": "radios professionnelles et micros sans fil",
    "fm": "radio FM commerciale",
}, "catégorie d'écoute du scanner, dans la commande intérêts")
CACHE_SEC = 30
_cache = {"t": 0.0, "niveaux": None}


def defaults():
    return {cat: level for cat, (_, level) in CATEGORIES.items()}


def levels(fresh=False):
    """{categorie: niveau}, relu sur le NAS au plus toutes les CACHE_SEC."""
    if fresh or _cache["niveaux"] is None or time.time() - _cache["t"] > CACHE_SEC:
        found = defaults()
        try:
            import reglages
            saved = reglages.get("interets") or {}
            found.update({c: l for c, l in saved.items() if c in CATEGORIES and l in WEIGHTS})
        except Exception as exc:
            print(f"[interets] reglages illisibles : {exc!r}")
        _cache.update(t=time.time(), niveaux=found)
    return _cache["niveaux"]


def category_of(target):
    for cat, (targets, _) in CATEGORIES.items():
        if any(target == t or (t.endswith("_") and target.startswith(t)) for t in targets):
            return cat
    return None


def weight(target):
    cat = category_of(target)
    return WEIGHTS[levels()[cat]] if cat else 1.0


def save(new_levels):
    import reglages
    ok = reglages.save("interets", {c: l for c, l in new_levels.items() if c in CATEGORIES and l in WEIGHTS})
    _cache["niveaux"] = None
    return ok
