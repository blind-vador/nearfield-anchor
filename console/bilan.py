"""Bilan des notes de l'utilisateur, bande par bande (commande "bilan"),
reglages proposes, appliques, annules (2026-10-02).

Confronte ce que l'utilisateur a entendu (notes du journal, touches N et V)
aux reglages audio de chaque bande (lib/reglages_audio.py) :
- une QUALITE de 0 a 1 par note, calculee sur tous les champs ;
- des PROPOSITIONS chiffrees par bande, sur les captures FAITES depuis le
  dernier changement de cette bande (heure de capture, pas de notation) :
  un reglage n'est juge que sur ce qu'on a entendu avec lui. MIN_NOTES est
  un plancher ; on attend en plus que TOUTES les captures faites avec le
  reglage en cours soient notees (2026-10-07, demande utilisateur : des
  prises de 14 h notees a 16 h jugeaient deja un reglage plus recent) ;
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
from phrases import dire, nombre
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


def _capture_time(note):
    """Heure de la capture notee (sinon de la note, pour les plus anciennes)."""
    for key in ("capture_le", "note_le"):
        try:
            return time.mktime(time.strptime(note[key][:19], "%Y-%m-%dT%H:%M:%S"))
        except (KeyError, ValueError, TypeError):
            continue
    return 0.0


def _unrated_since(band, since, rated):
    """Captures de cette bande faites depuis since et pas encore notees
    (encore au journal : un enregistrement rejete ou purge ne compte plus)."""
    import os
    import journal
    import noise_signatures as ns
    count = 0
    for recs in journal.scan_recordings("radio").values():
        for stamp, path, text in recs:
            if not path or text is not None or stamp.timestamp() <= since or os.path.basename(path) in rated:
                continue
            if (ns.capture_info(path) or {}).get("target") == band:
                count += 1
    return count


def _radio_notes():
    """Notes de captures radio (une bande connue) ; les annonces de donnees,
    validees sans bande, ne reglent rien."""
    return [n for n in notation.load_notes() if n.get("bande")]


def _by_band(notes):
    bands = {}
    for n in notes:
        bands.setdefault(n["bande"], []).append(n)
    return bands


def _is_digital(notes):
    """Bande en voix numerique (DMR...) : decodee par dsd-fme, elle ne passe
    PAS par le nettoyage analogique (reglages_audio). Le bilan la suit mais ne
    propose aucun reglage audio -- passe-bas, souffle, gain n'ont pas de sens
    ici (2026-10-08, demande utilisateur : DMR integre au meme process, mais
    sans reglage analogique absurde)."""
    return any((n.get("mesures") or {}).get("numerique") for n in notes)


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
        reasons.append(dire("bilan.fins_coupees_trop_tot"))
    elif share(lambda r: r.get("fin") == "longue") >= 0.5:
        current = settings["attente_fin_s"] or 3.0
        changes["attente_fin_s"] = _clamp(current - 0.5, 1.5, 8.0)
        reasons.append(dire("bilan.fins_qui_trainent"))
    opens_late = share(lambda r: r.get("debut") == "coupe") >= SHARE or share(lambda r: r.get("stabilite") == "hache") >= SHARE
    useless = share(lambda r: r.get("merite") == "non") >= 0.5
    weak_but_useful = (share(lambda r: r.get("merite") != "non") >= 0.8
                       and share(lambda r: isinstance(r.get("lisibilite"), int) and r["lisibilite"] <= 2) >= SHARE)
    if opens_late and not useless:
        changes["marge_detection_db"] = _clamp(settings["marge_detection_db"] - 1.0, -4.0, 6.0)
        reasons.append(dire("bilan.debuts_coupes_son_hache"))
    elif useless and not opens_late and not weak_but_useful:
        changes["marge_detection_db"] = _clamp(settings["marge_detection_db"] + 1.5, -4.0, 6.0)
        reasons.append(dire("bilan.beaucoup_annonces_inutiles_etre"))
    if share(lambda r: r.get("son") == "sature" or r.get("volume") == "fort") >= SHARE:
        changes["gain_db"] = _clamp(settings["gain_db"] - 3.0, -12.0, 12.0)
        reasons.append(dire("bilan.son_sature_trop_fort"))
    elif share(lambda r: r.get("volume") == "faible") >= SHARE:
        changes["gain_db"] = _clamp(settings["gain_db"] + 3.0, -12.0, 12.0)
        reasons.append(dire("bilan.volume_trop_faible"))
    if share(lambda r: r.get("son") == "etouffe") >= SHARE:
        changes["passe_bas_hz"] = _clamp(settings["passe_bas_hz"] + 500, 2500, 4500)
        reasons.append(dire("bilan.son_etouffe_garder_plus"))
    if share(lambda r: r.get("souffle") in ("fort", "envahissant")) >= SHARE:
        changes["reduction_bruit_db"] = _clamp(settings["reduction_bruit_db"] + 3, 6, 18)
        reasons.append(dire("bilan.beaucoup_souffle"))
    if level("crachotements") >= SHARE:
        changes["anti_crachotements"] = _clamp(settings["anti_crachotements"] - 2, 4, 10)
        reasons.append(dire("bilan.crachotements"))
    if level("sifflement") >= SHARE and not settings["anti_sifflement"]:
        changes["anti_sifflement"] = True
        reasons.append(dire("bilan.sifflement"))
    if level("bourdonnement") >= SHARE and not settings["anti_bourdonnement"]:
        changes["anti_bourdonnement"] = True
        reasons.append(dire("bilan.bourdonnement"))
    changes = {k: v for k, v in changes.items() if v != settings[k]}  # deja au bout
    return changes, reasons


def analyse():
    """Par bande : {libelle, notes, depuis, qualite, changements, raisons, regression}."""
    out = {}
    all_notes = _radio_notes()
    rated = {n.get("fichier") for n in notation.load_notes()}
    for band, notes in _by_band(all_notes).items():
        since = ra.last_change(band)
        notes = sorted(notes, key=_capture_time)
        after = [n for n in notes if _capture_time(n) > since]
        before = [n for n in notes if _capture_time(n) <= since][-15:]
        settings = ra.get(band)
        digital = _is_digital(notes)
        entry = {"libelle": notes[-1].get("label") or band, "notes": len(notes), "depuis": len(after),
                 "qualite": sum(quality(n) for n in after or notes) / len(after or notes),
                 "changements": {}, "raisons": [], "regression": None, "a_noter": 0, "numerique": digital}
        # Bande numerique (DMR) : decodee par dsd-fme, aucun reglage analogique
        # a proposer. On la suit (qualite, nombre) sans jamais ajuster l'audio.
        if not digital:
            if since and len(after) >= MIN_AFTER and before:
                q_before = sum(quality(n) for n in before) / len(before)
                q_after = sum(quality(n) for n in after) / len(after)
                if q_after < q_before - REGRESSION:
                    entry["regression"] = (q_before, q_after)
            if len(after) >= MIN_NOTES or entry["regression"]:
                try:
                    entry["a_noter"] = _unrated_since(band, since, rated)
                except Exception as exc:  # journal illisible : on ne bloque pas le bilan
                    print(f"[bilan] captures non notees : {exc!r}")
            if len(after) >= MIN_NOTES and not entry["regression"] and not entry["a_noter"]:
                entry["changements"], entry["raisons"] = suggestions(after, settings)
        out[band] = entry
    return out


def _spoken_changes(band, changes):
    settings = ra.get(band)
    return ", ".join(dire("bilan.changement", reglage=ra.NAMES[k], avant=ra.spoken_value(k, settings[k]),
                          apres=ra.spoken_value(k, v))
                     for k, v in changes.items())


def summary():
    if not notation.load_notes():
        return dire("bilan.aucune_note_instant_journal")
    data = analyse()
    total = sum(e["notes"] for e in data.values())
    parts = [dire("bilan.total", notes=nombre(total, "note radio", "notes radio"),
                  bandes=nombre(len(data), "bande"))]
    for band, e in sorted(data.items(), key=lambda kv: -kv[1]["notes"]):
        line = dire("bilan.qualite_bande", bande=e['libelle'], qualite=round(100 * e['qualite']))
        if e.get("numerique"):
            line += dire("bilan.numerique_suivi")
        elif e["a_noter"]:
            line += dire("bilan.encore_a_noter", prises=nombre(e["a_noter"], "prise", "prises"))
        elif e["regression"]:
            line += (dire("bilan.baisse_depuis_dernier_reglage", round=round(100 * e['regression'][0]), round2=round(100 * e['regression'][1])))
        elif e["changements"]:
            line += dire("bilan.propose", v1=_spoken_changes(band, e['changements']), join=', '.join(e['raisons']))
        elif e["depuis"] < MIN_NOTES:
            line += dire("bilan.encore_notes_avant_proposer", v1=MIN_NOTES - e['depuis'])
        else:
            line += dire("bilan.rien_changer")
        parts.append(line + ".")
    pending = [b for b, e in data.items() if e["changements"]]
    if pending:
        parts.append(dire("bilan.dis_applique_reglages_appliquer"))
    parts.append(dire("bilan.reglages_auto_actives" if ra.load()["auto"] else "bilan.reglages_auto_desactives"))
    return " ".join(parts)


def apply_all(description="propositions du bilan"):
    data = analyse()
    changes = {band: e["changements"] for band, e in data.items() if e["changements"]}
    if not changes:
        return dire("bilan.rien_appliquer_instant")
    spoken = "; ".join(f"{data[b]['libelle']} : {_spoken_changes(b, c)}" for b, c in changes.items())
    version = ra.apply(changes, description)
    return dire("bilan.applique_version_dis_reglage", version=version, spoken=spoken)


def auto_step(band):
    """Apres une note, en mode automatique : annule un reglage qui a fait
    baisser les notes de cette bande, ou applique ses propositions. Rend la
    phrase a ajouter a la reponse, ou ""."""
    if not band or not ra.load()["auto"]:
        return ""
    e = analyse().get(band)
    if not e:
        return ""
    if e["a_noter"]:
        return ""  # des captures faites avec ce reglage attendent encore leur note
    if e["regression"]:
        what = ra.revert_band(band)
        return " " + dire("bilan.reglage_automatique_annule_notes", libelle=e['libelle'], what=what) if what else ""
    if e["changements"]:
        spoken = _spoken_changes(band, e["changements"])
        ra.apply({band: e["changements"]}, f"automatique, {e['libelle']}")
        return " " + dire("bilan.reglage_automatique", libelle=e['libelle'], spoken=spoken)
    return ""


def settings_text():
    data = ra.load()
    if not data["bandes"]:
        return dire("bilan.reglages_audio_origine_toutes")
    parts = []
    for band, params in data["bandes"].items():
        parts.append(f"{band} : " + ", ".join(f"{ra.NAMES[k]} {ra.spoken_value(k, v)}" for k, v in params.items()))
    return dire("bilan.versions", versions=nombre(len(data['historique']), "version"), detail=" ; ".join(parts))


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
    return (dire("bilan.remis_zero_reglages_origine"))
