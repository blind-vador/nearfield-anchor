"""Bilan des notes de l'utilisateur, bande par bande (commande "bilan"),
reglages proposes, appliques, annules (2026-10-02).

Confronte ce que l'utilisateur a entendu (notes du journal, touches N et V)
aux reglages audio de chaque bande (lib/reglages_audio.py) :
- une QUALITE de 0 a 1 par note, calculee sur tous les champs ;
- des PROPOSITIONS chiffrees par bande, sur les notes arrivees depuis le
  dernier changement de cette bande (au moins MIN_NOTES) : un reglage n'est
  juge que sur ce qu'on a entendu apres lui, on ne tourne pas en rond ;
- un reglage CONTRE-PRODUCTIF : qualite moyenne en baisse de REGRESSION
  apres lui (au moins MIN_AFTER notes) -> le dire ; en mode automatique,
  l'annuler tout seul.
Mode automatique : apres chaque note, les propositions de la bande sont
appliquees d'elles-memes. Regles guidees par les priorites de veille fixees
ensemble : ne jamais bloquer a tort, ne pas couper les fins, ne pas manquer
les voix faibles, continuite, pas de saturation.
"""
import time

import notation
import reglages_audio as ra

MIN_NOTES = 8
MIN_AFTER = 5
SHARE = 0.3
REGRESSION = 0.08
LEVEL = {"aucun": 0.0, "un_peu": 0.5, "beaucoup": 1.0}
SOUFFLE = {"aucun": 1.0, "discret": 0.75, "moyen": 0.5, "fort": 0.25, "envahissant": 0.0}
STABILITE = {"stable": 1.0, "coupures": 0.5, "hache": 0.0}
FIN = {"correcte": 1.0, "longue": 0.6, "coupee": 0.0}


def quality(note):
    """Qualite entendue, de 0 (inaudible) a 1 (parfait)."""
    r = note.get("reponses", {})
    parts = []
    if isinstance(r.get("lisibilite"), int):
        parts.append((r["lisibilite"] - 1) / 4)
    parts.append(SOUFFLE.get(r.get("souffle"), 0.75))
    parts.append(1 - sum(LEVEL.get(v, 0) for v in notation.interferences(r).values()) / 2)
    parts.append(1.0 if r.get("son", "naturel") == "naturel" else 0.4)
    parts.append(STABILITE.get(r.get("stabilite"), 1.0))
    parts.append(1.0 if r.get("debut", "complet") == "complet" else 0.0)
    parts.append(FIN.get(r.get("fin"), 1.0))
    parts.append(1.0 if r.get("volume", "correct") == "correct" else 0.5)
    return max(0.0, sum(parts) / len(parts))


def _weight(note):
    return 0.5 if note.get("reponses", {}).get("certitude") == "hesitant" else 1.0


def _note_time(note):
    try:
        return time.mktime(time.strptime(note["note_le"], "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, ValueError):
        return 0.0


def _radio_notes():
    """Notes de captures radio (une bande connue) ; les annonces de donnees,
    validees sans bande, ne reglent rien."""
    return [n for n in notation.load_notes() if n.get("bande")]


def _by_band(notes):
    bands = {}
    for n in notes:
        bands.setdefault(n["bande"], []).append(n)
    return bands


def _clamp(value, low, high):
    return max(low, min(high, value))


def suggestions(band_notes, settings):
    """Changements proposes {parametre: valeur} et leurs raisons."""
    total = sum(_weight(n) for n in band_notes)

    def share(test):
        return sum(_weight(n) for n in band_notes if test(n["reponses"])) / total

    def level(kind):
        return sum(_weight(n) * LEVEL.get(notation.interferences(n["reponses"])[kind], 0) for n in band_notes) / total

    changes, reasons = {}, []
    if share(lambda r: r.get("fin") == "coupee") >= SHARE:
        current = settings["attente_fin_s"] or 3.0
        changes["attente_fin_s"] = _clamp(current + 1.5, 1.5, 8.0)
        reasons.append("des fins coupées trop tôt")
    elif share(lambda r: r.get("fin") == "longue") >= 0.5:
        current = settings["attente_fin_s"] or 3.0
        changes["attente_fin_s"] = _clamp(current - 0.5, 1.5, 8.0)
        reasons.append("des fins qui traînent")
    opens_late = share(lambda r: r.get("debut") == "coupe") >= SHARE or share(lambda r: r.get("stabilite") == "hache") >= SHARE
    useless = share(lambda r: r.get("merite") == "non") >= 0.5
    weak_but_useful = (share(lambda r: r.get("merite") != "non") >= 0.8
                       and share(lambda r: isinstance(r.get("lisibilite"), int) and r["lisibilite"] <= 2) >= SHARE)
    if opens_late and not useless:
        changes["marge_detection_db"] = _clamp(settings["marge_detection_db"] - 1.0, -4.0, 6.0)
        reasons.append("des débuts coupés ou un son haché : ouvrir plus tôt")
    elif useless and not opens_late and not weak_but_useful:
        changes["marge_detection_db"] = _clamp(settings["marge_detection_db"] + 1.5, -4.0, 6.0)
        reasons.append("beaucoup d'annonces inutiles : être plus exigeant")
    if share(lambda r: r.get("son") == "sature" or r.get("volume") == "fort") >= SHARE:
        changes["gain_db"] = _clamp(settings["gain_db"] - 3.0, -12.0, 12.0)
        reasons.append("du son saturé ou trop fort")
    elif share(lambda r: r.get("volume") == "faible") >= SHARE:
        changes["gain_db"] = _clamp(settings["gain_db"] + 3.0, -12.0, 12.0)
        reasons.append("un volume trop faible")
    if share(lambda r: r.get("son") == "etouffe") >= SHARE:
        changes["passe_bas_hz"] = _clamp(settings["passe_bas_hz"] + 500, 2500, 4500)
        reasons.append("un son étouffé : garder plus d'aigus")
    if share(lambda r: r.get("souffle") in ("fort", "envahissant")) >= SHARE:
        changes["reduction_bruit_db"] = _clamp(settings["reduction_bruit_db"] + 3, 6, 18)
        reasons.append("beaucoup de souffle")
    if level("crachotements") >= SHARE:
        changes["anti_crachotements"] = _clamp(settings["anti_crachotements"] - 2, 4, 10)
        reasons.append("des crachotements")
    if level("sifflement") >= SHARE and not settings["anti_sifflement"]:
        changes["anti_sifflement"] = True
        reasons.append("du sifflement")
    if level("bourdonnement") >= SHARE and not settings["anti_bourdonnement"]:
        changes["anti_bourdonnement"] = True
        reasons.append("du bourdonnement")
    changes = {k: v for k, v in changes.items() if v != settings[k]}  # deja au bout
    return changes, reasons


def analyse():
    """Par bande : {libelle, notes, depuis, qualite, changements, raisons, regression}."""
    out = {}
    for band, notes in _by_band(_radio_notes()).items():
        since = ra.last_change(band)
        after = [n for n in notes if _note_time(n) > since]
        before = [n for n in notes if _note_time(n) <= since][-15:]
        settings = ra.get(band)
        entry = {"libelle": notes[-1].get("label") or band, "notes": len(notes), "depuis": len(after),
                 "qualite": sum(quality(n) for n in after or notes) / len(after or notes),
                 "changements": {}, "raisons": [], "regression": None}
        if since and len(after) >= MIN_AFTER and before:
            q_before = sum(quality(n) for n in before) / len(before)
            q_after = sum(quality(n) for n in after) / len(after)
            if q_after < q_before - REGRESSION:
                entry["regression"] = (q_before, q_after)
        if len(after) >= MIN_NOTES and not entry["regression"]:
            entry["changements"], entry["raisons"] = suggestions(after, settings)
        out[band] = entry
    return out


def _spoken_changes(band, changes):
    settings = ra.get(band)
    return ", ".join(f"{ra.NAMES[k]} de {ra.spoken_value(k, settings[k])} à {ra.spoken_value(k, v)}"
                     for k, v in changes.items())


def summary():
    if not notation.load_notes():
        return "Aucune note pour l'instant. Dans le journal, N pour noter un enregistrement, V pour une bonne prise."
    data = analyse()
    parts = [f"{sum(e['notes'] for e in data.values())} notes radio sur {len(data)} bandes."]
    for band, e in sorted(data.items(), key=lambda kv: -kv[1]["notes"]):
        line = f"{e['libelle']} : qualité {round(100 * e['qualite'])} sur 100"
        if e["regression"]:
            line += (f", en baisse depuis le dernier réglage ({round(100 * e['regression'][0])} avant, "
                     f"{round(100 * e['regression'][1])} après) : dis réglage précédent pour revenir")
        elif e["changements"]:
            line += f". Je propose : {_spoken_changes(band, e['changements'])}, pour {', '.join(e['raisons'])}"
        elif e["depuis"] < MIN_NOTES:
            line += f", encore {MIN_NOTES - e['depuis']} notes avant de proposer"
        else:
            line += ", rien à changer"
        parts.append(line + ".")
    pending = [b for b, e in data.items() if e["changements"]]
    if pending:
        parts.append("Dis applique les réglages pour les appliquer.")
    parts.append("Réglages automatiques : " + ("activés." if ra.load()["auto"] else "désactivés."))
    return " ".join(parts)


def apply_all(description="propositions du bilan"):
    data = analyse()
    changes = {band: e["changements"] for band, e in data.items() if e["changements"]}
    if not changes:
        return "Rien à appliquer pour l'instant."
    spoken = "; ".join(f"{data[b]['libelle']} : {_spoken_changes(b, c)}" for b, c in changes.items())
    version = ra.apply(changes, description)
    return f"Appliqué, version {version} : {spoken}. Dis réglage précédent pour revenir en arrière."


def auto_step(band):
    """Apres une note, en mode automatique : annule un reglage qui a fait
    baisser les notes de cette bande, ou applique ses propositions. Rend la
    phrase a ajouter a la reponse, ou ""."""
    if not band or not ra.load()["auto"]:
        return ""
    e = analyse().get(band)
    if not e:
        return ""
    if e["regression"]:
        what = ra.revert_band(band)
        return f" Réglage automatique annulé pour {e['libelle']} : les notes avaient baissé ({what})." if what else ""
    if e["changements"]:
        spoken = _spoken_changes(band, e["changements"])
        ra.apply({band: e["changements"]}, f"automatique, {e['libelle']}")
        return f" Réglage automatique pour {e['libelle']} : {spoken}."
    return ""


def settings_text():
    data = ra.load()
    if not data["bandes"]:
        return "Réglages audio d'origine sur toutes les bandes."
    parts = []
    for band, params in data["bandes"].items():
        parts.append(f"{band} : " + ", ".join(f"{ra.NAMES[k]} {ra.spoken_value(k, v)}" for k, v in params.items()))
    return (f"{len(data['historique'])} version{'s' if len(data['historique']) > 1 else ''} de réglages. "
            + " ; ".join(parts) + ".")


def reset_all():
    """Reapprendre de zero : reglages d'usine, empreintes de bruit et notes
    mises de cote (archivees, rien n'est detruit)."""
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    ra.reset()
    import os
    import profils_bruit
    for path in (profils_bruit.STORE_PATH, notation.NOTES_PATH):
        if os.path.exists(path):
            os.replace(path, path.replace(".json", f"_archive_{stamp}.json"))
    return ("Remis à zéro : réglages d'origine, empreintes de bruit et notes mises de côté dans des archives. "
            "Le Pi réapprend à partir des prochaines notes. Les parasites appris et les stations probables restent.")
