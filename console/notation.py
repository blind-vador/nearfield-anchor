"""Notation d'un enregistrement radio (touche N du journal).

Grille construite avec l'utilisateur le 2026-09-30 a partir de references
eprouvees : code SINPO (UIT-R SM.1135 : signal, interference, bruit,
propagation, impression generale), ITU-T P.835 (deformation de la voix,
bruit de fond, qualite globale), dimensions NISQA (bruit, coloration,
discontinuites, volume), erreurs classiques des detecteurs de voix (debut
coupe, coupure pendant une pause, traine, bruit pris pour de la voix) et
familles de sons d'AudioSet. Chaque note garde les MESURES du systeme pour
cette capture : la commande "bilan" confronte les deux, bande par bande.

En DEGRES, par onglets (2026-10-14, demande utilisateur) : plus de choix
exclusifs. Le contenu se note en pourcentages (une prise peut etre musique
puis voix), les perturbations et le son en degres ; tout est a 0 au depart.
La note garde les degres ("degres") ET leur traduction dans l'ancienne grille
("reponses", legacy()) : le bilan, les stations probables et l'attention
continuent de lire "reponses" ; le bilan affine sa qualite avec les degres.

  Tab / Maj+Tab            onglet suivant / precedent
  fleches haut / bas       reglage precedent / suivant dans l'onglet
  fleches gauche / droite  baisse / monte la valeur
  E ou F2                  ecoute l'exemple de reference de la valeur choisie
                           (referentiel fixe, tools/build_exemples_notation.py)
  Entree                   enregistre       Echap : annule
"""
import chemins
import json
import os
import sys
import time


import noise_signatures as ns  # noqa: E402
import conseils  # noqa: E402
from phrases import dire, etiquette, nombre  # noqa: E402

NOTES_PATH = "/media/nas/nd1/server/radio_notes.json"  # jamais purge (le journal, lui, l'est a 48 h)

INTERFERENCES = [("sifflement", "Sifflement"), ("bourdonnement", "Bourdonnement"),
                 ("autre_station", "Autre station"), ("crachotements", "Crachotements")]

# Echelles : (minimum, maximum, pas, valeurs dites ou None pour un nombre)
DEGREE = (0, 4, 1, ["aucun", "léger", "moyen", "fort", "envahissant"])
PERCENT = (0, 100, 10, None)
SCALES = {
    "pct": PERCENT,
    "deg": DEGREE,
    "lis": (0, 5, 1, ["non notée", "1 sur 5", "2 sur 5", "3 sur 5", "4 sur 5", "5 sur 5"]),
    "volume": (-2, 2, 1, ["beaucoup trop faible", "un peu faible", "correct", "un peu fort", "beaucoup trop fort"]),
    "fin": (-2, 2, 1, ["coupée trop tôt", "un peu courte", "correcte", "un peu longue", "trop longue"]),
    "interet": (-2, 2, 1, ["inutile", "peu utile", "neutre", "intéressant", "à ne pas manquer"]),
    "doute": (0, 2, 1, ["sûr", "un peu hésitant", "hésitant"]),
    "coupures": (0, 4, 1, ["aucune", "rares", "quelques-unes", "nombreuses", "haché"]),
}
# Onglets : (code, nom dit, [(code, nom dit, echelle), ...])
TABS = [
    ("contenu", "Contenu", [("voix", "Voix", "pct"), ("musique", "Musique", "pct"),
                            ("tonalites", "Tonalités, bips ou DTMF", "pct"), ("donnees", "Données numériques", "pct"),
                            ("rien", "Rien d'identifiable", "pct")]),
    ("perturbations", "Perturbations", [("souffle", "Souffle", "deg"),
                                        *[(c, n, "deg") for c, n in INTERFERENCES]]),
    ("son", "Son", [("etouffe", "Étouffé", "deg"), ("metallique", "Métallique", "deg"), ("sature", "Saturé", "deg"),
                    ("volume", "Volume", "volume")]),
    ("qualite", "Qualité", [("lisibilite", "Lisibilité", "lis"), ("stabilite", "Coupures en cours", "coupures"),
                            ("debut", "Début coupé", "deg"), ("fin", "Fin", "fin")]),
    ("avis", "Avis", [("merite", "Intérêt", "interet"), ("certitude", "Doute", "doute")]),
]
CONTENT_CODES = [c for c, _, _ in TABS[0][2]]
EXAMPLES_DIR = chemins.base("exemples_notation")
F2 = ("\x1b[[B", "\x1bOQ", "\x1b[12~")
TAB, SHIFT_TAB = "\t", "\x1b[Z"


def empty_degrees():
    d = {"contenu": {c: 0 for c in CONTENT_CODES}}
    for tab, _, items in TABS[1:]:
        for code, _, _ in items:
            d[code] = 0
    return d


def _to_interf(deg):
    return "aucun" if deg <= 0 else "un_peu" if deg <= 2 else "beaucoup"


def legacy(d):
    """Traduction des degres dans l'ancienne grille (un choix par champ)."""
    pct = d.get("contenu") or {}
    contenu = max(pct, key=pct.get) if pct and max(pct.values()) > 0 else "non_precise"
    son_worst = max(("etouffe", "metallique", "sature"), key=lambda c: d.get(c, 0))
    souffle = ["aucun", "discret", "moyen", "fort", "envahissant"]
    r = {
        "contenu": contenu,
        "souffle": souffle[max(0, min(4, d.get("souffle", 0)))],
        **{code: _to_interf(d.get(code, 0)) for code, _ in INTERFERENCES},
        "son": son_worst if d.get(son_worst, 0) >= 2 else "naturel",
        "volume": "faible" if d.get("volume", 0) < 0 else "fort" if d.get("volume", 0) > 0 else "correct",
        "stabilite": "stable" if d.get("stabilite", 0) == 0 else "coupures" if d.get("stabilite", 0) <= 2 else "hache",
        "debut": "coupe" if d.get("debut", 0) >= 1 else "complet",
        "fin": "coupee" if d.get("fin", 0) < 0 else "longue" if d.get("fin", 0) > 0 else "correcte",
        "merite": "oui" if d.get("merite", 0) > 0 else "non" if d.get("merite", 0) < 0 else "bof",
        "certitude": "hesitant" if d.get("certitude", 0) >= 1 else "sur",
    }
    if d.get("lisibilite", 0) > 0:
        r["lisibilite"] = int(d["lisibilite"])
    return r


def degrees_from_legacy(r):
    """Ancienne note (choix uniques) -> degres, pour la re-noter dans la nouvelle grille."""
    d = empty_degrees()
    if r.get("contenu") in CONTENT_CODES:
        d["contenu"][r["contenu"]] = 100
    souffle = ["aucun", "discret", "moyen", "fort", "envahissant"]
    if r.get("souffle") in souffle:
        d["souffle"] = souffle.index(r["souffle"])
    for code, level in interferences(r).items():
        d[code] = {"aucun": 0, "un_peu": 2, "beaucoup": 4}.get(level, 0)
    if r.get("son") in ("etouffe", "metallique", "sature"):
        d[r["son"]] = 3
    d["volume"] = {"faible": -1, "fort": 1}.get(r.get("volume"), 0)
    d["stabilite"] = {"coupures": 2, "hache": 4}.get(r.get("stabilite"), 0)
    d["debut"] = 3 if r.get("debut") == "coupe" else 0
    d["fin"] = {"coupee": -2, "longue": 2}.get(r.get("fin"), 0)
    d["merite"] = {"oui": 1, "non": -1}.get(r.get("merite"), 0)
    d["certitude"] = 2 if r.get("certitude") == "hesitant" else 0
    if isinstance(r.get("lisibilite"), int):
        d["lisibilite"] = r["lisibilite"]
    return d


def load_notes():
    try:
        with open(NOTES_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_note(note):
    notes = [n for n in load_notes() if n.get("fichier") != note["fichier"]]  # re-noter remplace
    notes.append(note)
    tmp = NOTES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(notes, f, ensure_ascii=False)
    os.replace(tmp, NOTES_PATH)
    return notes


def interferences(reponses):
    """{type: aucun / un_peu / beaucoup} pour une note, nouvelle ou ancienne
    (ancien champ unique "brouillage" : le type nomme compte pour present)."""
    if "brouillage" in reponses and not any(code in reponses for code, _ in INTERFERENCES):
        old = reponses["brouillage"]
        return {code: ("un_peu" if code == old else "aucun") for code, _ in INTERFERENCES}
    return {code: reponses.get(code, "aucun") for code, _ in INTERFERENCES}


def _get(d, tab, code):
    return d["contenu"][code] if tab == "contenu" else d[code]


def _set(d, tab, code, value):
    if tab == "contenu":
        d["contenu"][code] = value
    else:
        d[code] = value


def _value_text(scale, value):
    low, _, _, names = SCALES[scale]
    if names is None:
        return dire("notation.pour_cent", valeur=value)
    return etiquette(f"notation.echelle.{scale}.{value - low}", names[value - low])


def _nom_onglet(t):
    """Noms dits des onglets et des champs : sections [notation.onglet_nom.*] et
    [notation.champ.*] de la langue (2026-11-08)."""
    code, name, _ = TABS[t]
    return etiquette(f"notation.onglet_nom.{code}", name)


def _nom_champ(code, name):
    return etiquette(f"notation.champ.{code}", name)


def _item_text(d, t, i):
    tab, _, items = TABS[t]
    code, name, scale = items[i]
    return dire("notation.champ_valeur", champ=_nom_champ(code, name), valeur=_value_text(scale, _get(d, tab, code)))


def _tab_text(d, t, i):
    _, _, items = TABS[t]
    return dire("notation.onglet", onglet=_nom_onglet(t), reglages=nombre(len(items), "réglage")) + " " + _item_text(d, t, i)


def _example_name(tab, code, value):
    """Fichier d'exemple de reference (ancienne grille) pour cette valeur."""
    if tab == "contenu":
        return f"contenu_{code}"
    if code in dict(INTERFERENCES):
        return f"brouillage_{code}"
    probe = empty_degrees()
    probe[code] = value
    r = legacy(probe)
    if code == "souffle":
        return f"souffle_{r['souffle']}"
    if code in ("etouffe", "metallique", "sature"):
        return f"son_{code}"
    if code == "lisibilite":
        return f"lisibilite_{value}" if value else None
    if code in ("volume", "stabilite", "debut", "fin"):
        return f"{code}_{r[code]}"
    return None


def play_example(d, t, i):
    """Touche E / F2 : exemple sonore fixe de la valeur sous le curseur."""
    tab, _, items = TABS[t]
    code, name, scale = items[i]
    name = _nom_champ(code, name)
    value = _get(d, tab, code)
    example = _example_name(tab, code, value)
    if example is None:
        return dire("notation.pas_exemple_avis", champ=name.lower())
    path = os.path.join(EXAMPLES_DIR, example + ".mp3")
    if not os.path.exists(path):
        return dire("notation.pas_exemple", champ=name.lower(), valeur=_value_text(scale, value))
    import audio_lock
    import sound_player
    import voix
    voix.say_blocking(dire("notation.exemple_de", champ=name.lower()))
    ticket = audio_lock.acquire("console", timeout=10)
    try:
        sound_player.play_sound_effect(path, timeout_sec=20, label="notation")
    finally:
        audio_lock.release(ticket)
    return _item_text(d, t, i)


# Validation rapide (touche V du journal) : "bonne prise", sans dialogue.
# Le contenu n'est pas precise (voix, tonalites...) : "non_precise", que le
# bilan ignore dans ses parts par type de contenu.
GOOD_TAKE = {"contenu": "non_precise", "lisibilite": 5, "souffle": "aucun",
             **{code: "aucun" for code, _ in INTERFERENCES}, "son": "naturel", "volume": "correct", "stabilite": "stable", "debut": "complet",
             "fin": "correcte", "merite": "oui", "certitude": "sur"}


def _good_degrees(cut=False):
    d = empty_degrees()
    d["lisibilite"], d["merite"] = 5, 2
    if cut:
        d["fin"] = -2
    return d


def _record(info, path, stamp, reponses, rapide=False, degres=None, coupee=False):
    nouvelle = not any(n.get("fichier") == os.path.basename(path) for n in load_notes())
    note = {
        "fichier": os.path.basename(path),
        "capture_le": stamp.isoformat(),
        "note_le": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "freq_hz": info.get("freq_hz"), "label": info.get("label"),
        "bande": info.get("target"), "source": info.get("source"), "mode": info.get("mode"),
        "mesures": info.get("metrics") or {},
        "empreinte": info.get("fingerprint"),
        "reponses": reponses,
    }
    if degres is not None:
        note["degres"] = degres
    if rapide:
        note["validation_rapide"] = True
    if coupee:
        note["validation_coupee"] = True
    notes = _save_note(note)
    total = len(notes)
    try:  # contenu appris par station (lib/contenus.py) : recalcule, re-noter remplace
        import contenus
        contenus.rebuild(notes)
    except Exception as exc:
        print(f"[notation] contenu par station : {exc!r}")
    try:  # "meritait : oui" pique la bande, "non" l'apaise
        import attention
        attention.stimulate(note["bande"], attention.NOTE_POINTS.get(reponses.get("merite"), 0))
    except Exception:
        pass
    station = False
    try:  # prise parfaite : station probable, recentree et revisitee (lib/stations.py)
        import stations
        station = stations.register_from_note(note)
    except Exception as exc:
        print(f"[notation] station probable : {exc!r}")
    auto = ""
    try:  # mode automatique : reglages de la bande ajustes, ou annules s'ils ont nui
        import bilan
        auto = bilan.auto_step(note["bande"])
    except Exception as exc:
        print(f"[notation] reglage automatique : {exc!r}")
    if nouvelle:
        try:  # le juge entendu sur cette frequence passe devant la prochaine fois
            import propositions
            propositions.confirmer(path)
        except Exception as exc:
            print(f"[notation] juge non retenu : {exc!r}")
    auto += _examen_contenu(path, note)
    return total, station, auto


# 2026-11-10 (demande utilisateur) : "j'ai entendu clairement des suites repetitives de
# tonalites, parfois du morse, parfois des bips que je ne connais pas". Une prise notee
# tonalites ou donnees repart a TOUS les juges (morse, DTMF, appels selectifs ZVEI/CCIR/
# EEA, FFSK, POCSAG, AX.25, numeriques), et la frequence est notee comme portant des
# donnees : elle sera reecoutee avec eux.
EXAMEN_CONTENU = ("tonalites", "donnees")


def _examen_contenu(path, note):
    contenu = (note.get("degres") or {}).get("contenu") or {}
    if not any(contenu.get(c) for c in EXAMEN_CONTENU):
        return ""
    try:
        import frequences_actives
        if note.get("freq_hz"):
            frequences_actives.noter(note["freq_hz"], "donnees", protocole="note par l'utilisateur",
                                     label=note.get("label"))
    except Exception as exc:
        print(f"[notation] frequence non notee : {exc!r}")
    try:
        import propositions
        return " " + propositions.renvoyer_flux(path, depuis_prise=True)
    except Exception as exc:
        print(f"[notation] examen des juges impossible : {exc!r}")
        return ""


def _station_text(station):
    return " " + dire("notation.station_notee") if station else ""


def validate(path, stamp, cut=False):
    """Bonne prise, en une touche : note maximale sur tous les criteres.
    cut (touche C, 2026-10-14) : meme chose, mais la fin a ete coupee trop
    tot -- ce que le bilan fait corriger (ecouter plus longtemps). Sur une
    prise deja notee, C corrige seulement la fin."""
    if not os.path.ismount("/media/nas/nd1"):
        return dire("notation.disque_absent_validation")
    previous = next((n for n in load_notes() if n.get("fichier") == os.path.basename(path)), None)
    info = ns.capture_info(path) or {}
    if cut and previous:
        if previous.get("validation_coupee") or previous.get("reponses", {}).get("fin") == "coupee":
            return dire("notation.deja_coupee")
        degres = previous.get("degres") or degrees_from_legacy(previous.get("reponses", {}))
        degres["fin"] = -2
        reponses = dict(previous.get("reponses", {}), fin="coupee")
        total, station, auto = _record(info or {k: previous.get(k) for k in ("freq_hz", "label", "source", "mode")}
                                       | {"target": previous.get("bande")}, path, stamp, reponses,
                                       rapide=previous.get("validation_rapide", False), degres=degres, coupee=True)
        return dire("notation.fin_corrigee") + auto
    if previous and previous.get("validation_rapide"):
        return dire("notation.deja_valide")
    if previous:
        return dire("notation.deja_note")
    reponses = dict(GOOD_TAKE, fin="coupee") if cut else dict(GOOD_TAKE)
    total, station, auto = _record(info, path, stamp, reponses, rapide=True, degres=_good_degrees(cut), coupee=cut)
    if cut:
        return dire("notation.valide_coupee", total=nombre(total, "note")) \
            + _station_text(station) + auto
    return dire("notation.valide", total=nombre(total, "note")) + _station_text(station) + auto


def rate(key_source, say, out, path, stamp):
    """Dialogue de notation en onglets ; rend la phrase de sortie."""
    if not os.path.ismount("/media/nas/nd1"):
        return dire("notation.disque_absent")
    info = ns.capture_info(path) or {}
    previous = next((n for n in load_notes() if n.get("fichier") == os.path.basename(path)), None)
    d = empty_degrees()
    if previous:  # reprendre la note deja donnee (ancienne grille traduite en degres)
        old = previous.get("degres") or degrees_from_legacy(previous.get("reponses", {}))
        d.update({k: v for k, v in old.items() if k != "contenu"})
        d["contenu"].update(old.get("contenu") or {})
    t, i = 0, 0
    consigne = dire("notation.consigne_onglets")
    intro = conseils.intro("notation", dire("notation.titre"), consigne)
    out(intro + "\n")
    say(intro + (" " + dire("notation.deja_note_modif") + " " if previous else " ") + _tab_text(d, t, i))
    for key in key_source:
        tab, _, items = TABS[t]
        if key == "\x1b":
            return dire("notation.annulee")
        if key in (TAB, SHIFT_TAB):
            t = (t + (1 if key == TAB else -1)) % len(TABS)
            i = 0
            text = _tab_text(d, t, i)
        elif key in ("\x1b[A", "\x1b[B"):
            j = i + (1 if key == "\x1b[B" else -1)
            if 0 <= j < len(items):
                i = j
                text = _item_text(d, t, i)
            else:
                edge = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
                text = edge + " " + _item_text(d, t, i)
        elif key in ("\x1b[C", "\x1b[D"):
            code, _, scale = items[i]
            low, high, step, _ = SCALES[scale]
            value = max(low, min(high, _get(d, tab, code) + (step if key == "\x1b[C" else -step)))
            _set(d, tab, code, value)
            text = _value_text(scale, value)
        elif key in ("\r", "\n"):
            total, station, auto = _record(info, path, stamp, legacy(d), degres=d)
            return dire("notation.enregistree", total=nombre(total, "note")) + _station_text(station) + auto
        elif key in ("e", "E") or key in F2:
            text = play_example(d, t, i)
        elif conseils.is_help(key):
            text = consigne
        else:
            text = dire("notation.aide_courte")
        out(f"[{text}]\n")
        say(text)
    return dire("notation.annulee")
