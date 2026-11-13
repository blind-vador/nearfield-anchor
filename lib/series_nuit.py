"""Series de la nuit (2026-11-13, demande utilisateur) : toutes les prises notees
"voix claire", regroupees par frequence, transcrites a la chaine par Whisper sur le
Mac (travail "examen_serie"), puis UNE analyse de station par frequence, sur la
concatenation de tout ce qui y a ete dit (lib/analyse_station.analyser, serie=N).

  voix claire   validation V ou C (bonne prise), ou note complete avec au moins
                VOIX_MIN % de voix et une lisibilite d'au moins LISIBLE_MIN ;
                jamais une prise chiffree, ni sans son brut (la prise habillee
                porte le carillon et l'annonce de synthese : Whisper les lirait) ;
  une fois      chaque prise n'est transcrite qu'une fois : son texte reste dans
                TEXTES et resert aux analyses suivantes de la frequence ; une
                frequence ne part que si elle a au moins une prise nouvelle ;
  concatenation les textes de la frequence, du plus ancien au plus recent, dates,
                au plus SERIE_TEXTES_MAX prises et SERIE_CARACTERES caracteres.

Lance par watchers/analyse_nuit.py (02 h 30), avec la passe "examen_lot"."""
import os
import shutil
import time

import chemins

NOTES = "/media/nas/nd1/server/radio_notes.json"
TEXTES = chemins.base("whisper_textes.json")
VOIX_MIN = 50                 # comme console/notation.avis_juge
LISIBLE_MIN = 3
PAR_FREQUENCE_MAX = 20        # prises nouvelles envoyees par frequence et par nuit
NUIT_MAX = 200
SERIE_TEXTES_MAX = 30
SERIE_CARACTERES = 8000
CONFIANCE_MIN = 0.50          # mac/analyse.py WHISPER_INTELLIGIBLE : en dessous le texte n'est pas garde
ENVOI_VALIDE_SEC = 36 * 3600
JUGES_DONNEES = ("donnees", "multimon")


def _log(texte):
    print(f"[series] {texte}", flush=True)


def _examen():
    import examen
    return examen


def voix_claire(note):
    """Vrai si la note dit une voix claire (meme regle que le oui au juge vocal)."""
    if (note.get("mesures") or {}).get("chiffre") is True:
        return False
    if (note.get("mesures") or {}).get("juge") in JUGES_DONNEES:
        return False
    if note.get("validation_rapide"):
        return True
    d = note.get("degres") or {}
    contenu = d.get("contenu") or {}
    try:
        lisible = int(d.get("lisibilite") or (note.get("reponses") or {}).get("lisibilite") or 0)
    except (TypeError, ValueError):
        lisible = 0
    return (contenu.get("voix", 0) or 0) >= VOIX_MIN and lisible >= LISIBLE_MIN


def _cle(freq_hz):
    return str(int(round(float(freq_hz) / 12500.0) * 12500))


def _notes():
    ex = _examen()
    data = ex._lire(NOTES, [])
    return data if isinstance(data, list) else []


def textes():
    return _examen()._lire(TEXTES, {})


def prises_par_frequence():
    """{cle: [ {fichier, chemin, brut, note} ] du plus ancien au plus recent}."""
    ex = _examen()
    out = {}
    for note in _notes():
        if not note.get("freq_hz") or not voix_claire(note):
            continue
        chemin = ex._chemin_journal(note.get("fichier"))
        brut = ex.source_brute(chemin) if chemin else None
        if not brut:
            continue
        out.setdefault(_cle(note["freq_hz"]), []).append(
            {"fichier": os.path.basename(chemin), "chemin": chemin, "brut": brut, "note": note})
    for liste in out.values():
        liste.sort(key=lambda p: str(p["note"].get("capture_le") or ""))
    return out


def lancer(maintenant=None):
    """Depose un travail "examen_serie" par frequence qui a des prises nouvelles.
    Rend le nombre de travaux deposes."""
    import compagnon
    maintenant = maintenant or time.time()
    if not compagnon.rank("examen_serie"):
        _log("aucune machine ne fait les series cette nuit")
        return 0
    deja = textes()
    envois = _examen()._lire(chemins.base("series_envois.json"), {})
    total = deposes = 0
    for cle, prises in sorted(prises_par_frequence().items()):
        nouvelles = [p for p in prises if p["fichier"] not in deja
                     and maintenant - envois.get(p["fichier"], 0) > ENVOI_VALIDE_SEC][-PAR_FREQUENCE_MAX:]
        if not nouvelles or total >= NUIT_MAX:
            continue
        nouvelles = nouvelles[:NUIT_MAX - total]
        derniere = nouvelles[-1]["note"]
        params = {"mode": "serie", "freq_hz": int(derniere["freq_hz"]), "label": derniere.get("label") or "",
                  "modulation": derniere.get("mode"), "juge": (derniere.get("mesures") or {}).get("juge"),
                  "fichiers": []}
        job_id, dossier, job = compagnon.new_job("examen_serie", params)
        for i, p in enumerate(nouvelles):
            nom = f"p{i:02d}{os.path.splitext(p['brut'])[1] or '.mp3'}"
            shutil.copyfile(p["brut"], os.path.join(dossier, nom))
            params["fichiers"].append({"fichier": nom, "origine": p["fichier"],
                                       "le": p["note"].get("capture_le")})
            envois[p["fichier"]] = maintenant
        job["params"] = params
        compagnon.submit(job)
        total += len(nouvelles)
        deposes += 1
        _log(f"{params['freq_hz'] / 1e6:.4f} MHz : {len(nouvelles)} prise(s) nouvelle(s) sur {len(prises)} ({job_id})")
    envois = {k: v for k, v in envois.items() if maintenant - v < 7 * 86400}
    _examen()._ecrire(chemins.base("series_envois.json"), envois)
    if not deposes:
        _log("aucune prise nouvelle a voix claire")
    return deposes


def _quand(iso):
    try:
        t = time.strptime(str(iso)[:16], "%Y-%m-%dT%H:%M")
        return time.strftime("%d/%m %H h %M", t)
    except ValueError:
        return ""


def concatener(cle, connus=None):
    """(texte, n, derniere prise) : les textes de la frequence, dates, du plus ancien
    au plus recent ; les plus anciens tombent d'abord quand c'est trop long."""
    import noms_whisper
    connus = textes() if connus is None else connus
    prises = prises_par_frequence().get(cle, [])
    morceaux = []
    for p in prises:
        t = connus.get(p["fichier"]) or {}
        propre = noms_whisper.texte_utile(t.get("texte") or "", p["note"].get("label") or "")
        if propre:
            morceaux.append((p, f"[{_quand(p['note'].get('capture_le'))}] {propre}"))
    morceaux = morceaux[-SERIE_TEXTES_MAX:]
    while len(morceaux) > 1 and sum(len(m) + 1 for _, m in morceaux) > SERIE_CARACTERES:
        morceaux.pop(0)
    if not morceaux:
        return "", 0, None
    return "\n".join(m for _, m in morceaux), len(morceaux), morceaux[-1][0]


def ranger(job, result, folder, machine=""):
    """Resultat d'une serie : textes gardes, puis une analyse pour la frequence."""
    ex = _examen()
    params = job.get("params") or {}
    if result.get("etat") != "fait":
        _log(f"{job.get('id')} en echec : {result.get('message')}")
        return None
    rendus = ex._lire(os.path.join(folder, "transcriptions.json"), [])
    connus = textes()
    gardes = 0
    for t in rendus if isinstance(rendus, list) else []:
        origine = t.get("origine")
        if not origine:
            continue
        intelligible = (t.get("confiance") or 0) >= CONFIANCE_MIN and t.get("texte")
        connus[origine] = {"texte": t.get("texte") if intelligible else "", "confiance": t.get("confiance"),
                           "statut": t.get("statut"), "le": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "freq_hz": params.get("freq_hz")}
        gardes += bool(intelligible)
    ex._ecrire(TEXTES, connus)
    cle = _cle(params.get("freq_hz") or 0)
    texte, n, derniere = concatener(cle, connus)
    freq = int(params.get("freq_hz") or cle)
    _log(f"{freq / 1e6:.4f} MHz : {len(rendus)} transcrite(s), {gardes} intelligible(s), "
         f"analyse sur {n} prise(s)")
    if not texte:
        return None
    import analyse_station
    return analyse_station.analyser(freq, params.get("modulation"), params.get("label"), params.get("juge"),
                                    texte, derniere["fichier"], machine, journal=None, serie=n)


def garder_texte(origine, transcription, freq_hz):
    """Texte d'un examen (touche E ou soir) : la serie suivante ne le refait pas."""
    if not origine or not transcription:
        return
    ex = _examen()
    connus = textes()
    ok = (transcription.get("confiance") or 0) >= CONFIANCE_MIN
    connus[origine] = {"texte": (transcription.get("texte") or "") if ok else "",
                       "confiance": transcription.get("confiance"), "statut": transcription.get("statut"),
                       "le": time.strftime("%Y-%m-%dT%H:%M:%S"), "freq_hz": freq_hz}
    ex._ecrire(TEXTES, connus)
