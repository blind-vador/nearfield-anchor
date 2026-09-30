"""Bilan des notes de l'utilisateur, bande par bande (commande "bilan").

Confronte ce que l'utilisateur a entendu (notes du journal, touche N) a ce
que le systeme avait mesure, et PROPOSE des reglages -- sans jamais rien
appliquer de lui-meme (decision prise avec l'utilisateur le 2026-09-30).
Regles guidees par les priorites de veille fixees ensemble : ne jamais
bloquer a tort, ne pas couper les fins, ne pas manquer les voix faibles,
continuite, pas de saturation ; souffle, traine et attente secondaires.
"""
from collections import Counter, defaultdict

import notation

MIN_NOTES = 8        # en dessous : on resume, on ne propose rien
SHARE = 0.3          # un defaut signale sur 30 % des notes d'une bande merite attention


def _pct(count, total):
    return round(100 * count / total)


def _suggestions(band_notes):
    # Les notes "hesitant" comptent pour moitie.
    weight = lambda note: 0.5 if note["reponses"].get("certitude") == "hesitant" else 1.0
    total = sum(weight(x) for x in band_notes)
    share = lambda field, value: sum(weight(x) for x in band_notes if x["reponses"].get(field) == value) / total

    tips = []
    if share("fin", "coupee") >= SHARE:
        tips.append("des fins sont coupées trop tôt : allonger le délai avant de couper pour cette bande")
    elif share("fin", "longue") >= 0.5 and share("fin", "coupee") == 0:
        tips.append("les fins traînent souvent, sans aucune fin coupée : on peut raccourcir un peu le délai")
    if share("debut", "coupe") >= SHARE:
        tips.append("des débuts sont coupés : allonger la mise en mémoire avant l'annonce")
    if share("stabilite", "hache") >= SHARE:
        tips.append("le son est souvent haché : assouplir le silencieux sur cette bande")
    if share("son", "sature") >= SHARE or share("volume", "fort") >= SHARE:
        tips.append("son saturé ou trop fort : baisser le gain ou l'amplification")
    elif share("volume", "faible") >= SHARE:
        tips.append("volume trop faible : augmenter l'amplification")
    if share("son", "etouffe") >= SHARE:
        tips.append("son étouffé : élargir le filtre de bande vocale sur cette bande")
    useless = share("merite", "non")
    airband = sum(1 for x in band_notes if x.get("source") == "airband") > len(band_notes) / 2
    if useless >= 0.5 and airband:
        # Canaux RTLSDR-Airband : pas de seuil de voix, c'est le silencieux qui decide.
        tips.append("beaucoup d'annonces inutiles : rendre le silencieux plus exigeant sur ce canal, "
                    "ou apprendre ces signaux comme parasites")
    elif useless >= 0.5:
        probs = [x["mesures"].get("prob_declenchement") for x in band_notes
                 if x["reponses"].get("merite") == "non" and x["mesures"].get("prob_declenchement")]
        if probs and sorted(probs)[len(probs) // 2] < 0.70:
            tips.append("beaucoup d'annonces inutiles, déclenchées de justesse : relever un peu le seuil de voix pour "
                        "cette bande")
        else:
            tips.append("beaucoup d'annonces inutiles : relever la marge de détection de cette bande")
    if share("contenu", "voix") > 0 and useless < 0.2 and share("lisibilite", 1) + share("lisibilite", 2) >= SHARE:
        tips.append("des voix peu lisibles mais utiles : ne surtout pas durcir les filtres ici")
    return tips


def summary():
    notes = notation.load_notes()
    if not notes:
        return "Aucune note pour l'instant. Dans le journal, appuie sur N pour noter un enregistrement."
    by_band = defaultdict(list)
    for note in notes:
        by_band[note.get("label") or note.get("bande") or "bande inconnue"].append(note)
    parts = [f"{len(notes)} notes sur {len(by_band)} bandes."]
    for label, band_notes in sorted(by_band.items(), key=lambda kv: -len(kv[1])):
        n = len(band_notes)
        merite = Counter(x["reponses"].get("merite") for x in band_notes)
        line = (f"{label} : {n} note{'s' if n > 1 else ''}, "
                f"méritait l'annonce : {_pct(merite['oui'], n)} % oui, {_pct(merite['non'], n)} % non.")
        if n < MIN_NOTES:
            line += f" Encore {MIN_NOTES - n} notes avant des propositions."
        else:
            tips = _suggestions(band_notes)
            line += (" Propositions : " + " ; ".join(tips) + ".") if tips else " Rien à changer."
        parts.append(line)
    return " ".join(parts)
