"""Materiau charge sur l'imprimante et presets d'impression.

Simple : on declare PLA / PETG / ABS / TPU ; les temperatures et le
comportement (test filament, retrait...) suivent. Les choix de tranchage
(couche, remplissage, supports, adherence) sont memorises pour le
prochain job et expliques a voix haute dans le questionnaire.
"""
import reglages

# Temperatures buse / plateau + besoin d'un petit test d'extrusion avant job.
MATERIAUX = {
    "PLA":  {"buse": 200, "plateau": 60,  "test_filament": True,  "nom": "P.L.A."},
    "PETG": {"buse": 240, "plateau": 80,  "test_filament": True,  "nom": "P.E.T.G."},
    "ABS":  {"buse": 240, "plateau": 100, "test_filament": True,  "nom": "A.B.S."},
    "TPU":  {"buse": 220, "plateau": 50,  "test_filament": False, "nom": "T.P.U. souple"},
}

# Alias de reconnaissance vocale / frappe
ALIAS = {
    "pla": "PLA", "p.l.a": "PLA", "p.l.a.": "PLA", "plastique": "PLA",
    "petg": "PETG", "p.e.t.g": "PETG", "p.e.t.g.": "PETG",
    "abs": "ABS", "a.b.s": "ABS", "a.b.s.": "ABS",
    "tpu": "TPU", "t.p.u": "TPU", "t.p.u.": "TPU", "souple": "TPU", "flexible": "TPU",
}

# Choix du questionnaire (id -> valeur stockee)
COUCHES = [
    ("rapide", 0.28),   # plus epais = moins de detail, plus vite
    ("normale", 0.20),
    ("fine", 0.12),
]
REMPLISSAGES = [
    ("leger", 10),
    ("normal", 20),
    ("solide", 40),
]
ADHERENCES = ["rien", "skirt", "brim", "raft"]


def get_settings():
    return reglages.get("imprimante")


def save_settings(data):
    reglages.save("imprimante", data)


def materiau_courant():
    s = get_settings()
    code = (s.get("materiau") or "PLA").upper()
    if code not in MATERIAUX:
        code = "PLA"
    return code, MATERIAUX[code]


def set_materiau(code):
    code = ALIAS.get(code.lower().strip(), code.upper().strip())
    if code not in MATERIAUX:
        return None
    s = get_settings()
    s["materiau"] = code
    s["test_filament"] = MATERIAUX[code]["test_filament"]
    save_settings(s)
    return code, MATERIAUX[code]


def parse_materiau_line(line):
    """Extrait un code materiau depuis une phrase libre, ou None."""
    import matcher
    words = matcher.normalize(line or "").split()
    # Prefer explicit tokens
    for w in words:
        if w in ALIAS:
            return ALIAS[w]
        if w.upper() in MATERIAUX:
            return w.upper()
    joined = " ".join(words)
    for alias, code in ALIAS.items():
        if alias in joined:
            return code
    return None


def resume_materiau():
    from phrases import dire
    code, preset = materiau_courant()
    return dire(
        "imprimante.materiau_actuel",
        nom=preset["nom"],
        buse=preset["buse"],
        plateau=preset["plateau"],
    )


def job_params_from_settings():
    """Params concrets pour annonces / futur trancheur."""
    s = get_settings()
    code, preset = materiau_courant()
    couche_id = s.get("couche") or "normale"
    remp_id = s.get("remplissage") or "normal"
    couche_mm = next((v for k, v in COUCHES if k == couche_id), 0.2)
    remp_pct = next((v for k, v in REMPLISSAGES if k == remp_id), 20)
    return {
        "materiau": code,
        "nom_materiau": preset["nom"],
        "buse": preset["buse"],
        "plateau": preset["plateau"],
        "test_filament": bool(s.get("test_filament", preset["test_filament"])),
        "couche": couche_id,
        "couche_mm": couche_mm,
        "remplissage": remp_id,
        "remplissage_pct": remp_pct,
        "supports": bool(s.get("supports")),
        "adherence": s.get("adherence") or "skirt",
        "buse_mm": float(s.get("buse_mm") or 0.4),
    }
