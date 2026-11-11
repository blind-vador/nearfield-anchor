"""Examen des prises radio par la passe d'analyse du Mac (touche E du journal
radio, et passe de la nuit).

2026-11-11 : refondu en TRAVAUX du pont (lib/compagnon.py). L'ancienne file
partagee (radio_log/a_examiner.json, lue par un agent launchd du Mac) ne
tournait plus depuis le 11 octobre : macOS refuse l'acces aux volumes reseau aux
taches launchd d'arriere-plan, et le Mac ne pouvait meme plus lire ce fichier
(reecrit par renommage sous un client SMB qui le gardait ouvert). Desormais :
  - touche E : le son brut de la prise est copie dans un travail "examen"
    (file rapide du pont) ;
  - la nuit : un travail "examen_lot" (watchers/analyse_nuit.py) ;
  - le pont lance mac/analyse.py, qui n'ecrit que dans le dossier du travail ;
  - le Pi range lui-meme les propositions (ranger(), appele par
    compagnon_resultats) : un seul ecrivain pour propositions.json.
"""
import fcntl
import json
import os
import shutil
import time

import chemins
from sound_paths import RADIO_LOG_DIR

ANCIENNE_FILE = os.path.join(RADIO_LOG_DIR, "a_examiner.json")
AUDIO_DIR = os.path.join(RADIO_LOG_DIR, "propositions_audio")
DEMANDES = chemins.base("examen_demandes.json")
LOCK_PATH = "/tmp/examen.lock"
DEMANDE_VALIDE_SEC = 36 * 3600   # comme compagnon_resultats.STALE_SEC : au-dela, on peut redemander


def _lire(path, defaut):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def _ecrire(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _chemin_journal(fichier):
    """Chemin complet d'une prise : tel quel s'il existe, sinon dans radio_log ou ses rejets."""
    if fichier and os.path.isabs(fichier) and os.path.isfile(fichier):
        return fichier
    nom = os.path.basename(fichier or "")
    for cand in (os.path.join(RADIO_LOG_DIR, nom), os.path.join(RADIO_LOG_DIR, "rejets", nom)):
        if os.path.isfile(cand):
            return cand
    return None


def source_brute(chemin):
    """Le son a examiner : le brut capte (sans carillon ni annonce de synthese,
    que la passe prendrait pour de la voix), sinon la prise elle-meme."""
    if chemin.endswith(".mp3"):
        brut = chemin[:-4] + ".brut.mp3"
        if os.path.isfile(brut):
            return brut
    return chemin


def _relatif(chemin):
    try:
        rel = os.path.relpath(chemin, RADIO_LOG_DIR)
    except ValueError:
        return os.path.basename(chemin)
    return os.path.basename(chemin) if rel.startswith("..") else rel


def _infos_prise(chemin):
    """Frequence, modulation, bande et juge de la prise (pour l'analyse de station)."""
    try:
        import noise_signatures as ns
        info = ns.capture_info(chemin) or {}
    except Exception:
        info = {}
    m = info.get("metrics") or {}
    return {"freq_hz": info.get("freq_hz"), "mode": m.get("modulation_banc") or info.get("mode"),
            "label": info.get("label") or "", "juge": m.get("juge")}


def demander(fichier, soir=False, infos=None):
    """Touche E du journal radio (soir : choisie par watchers/whisper_soir, rien n'est
    dit). Rend "demande", "deja", "sans_machine" ou "absent"."""
    chemin = _chemin_journal(fichier)
    if not chemin:
        return "absent"
    nom = os.path.basename(chemin)
    import compagnon
    try:
        os.makedirs(os.path.dirname(DEMANDES), exist_ok=True)
        with open(LOCK_PATH, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            demandes = _lire(DEMANDES, {})
            t = (demandes.get(nom) or {}).get("le", 0)
            if time.time() - t < DEMANDE_VALIDE_SEC:
                return "deja"
            if not any("examen" in (c.get("capacites") or []) for c in compagnon.companions()):
                return "sans_machine"
            src = source_brute(chemin)
            ext = os.path.splitext(src)[1] or ".mp3"
            params = {"mode": "clip", "fichier": "son" + ext, "origine": nom, "brut": _relatif(src),
                      "soir": bool(soir), **{k: v for k, v in (infos or _infos_prise(chemin)).items() if v}}
            job_id, dossier, job = compagnon.new_job("examen", params)
            shutil.copyfile(src, os.path.join(dossier, "son" + ext))
            compagnon.submit(job)
            demandes[nom] = {"le": time.time(), "travail": job_id}
            for cle in [k for k, v in demandes.items() if time.time() - v.get("le", 0) > 7 * 86400]:
                del demandes[cle]
            _ecrire(DEMANDES, demandes)
    except OSError as exc:
        print(f"[examen] demande impossible : {exc!r}")
        return "absent"
    print(f"[examen] {nom} confie ({job_id})")
    return "demande"


def lancer_nuit(heures=24, maximum=300):
    """Passe de la nuit : un seul travail "examen_lot" a la fois."""
    import compagnon
    if any(j.get("type") == "examen_lot" for j in compagnon.open_jobs()):
        print("[examen] passe de nuit deja en attente")
        return None
    job_id, _dossier, job = compagnon.new_job("examen_lot", {"mode": "lot", "heures": heures, "max": maximum})
    compagnon.submit(job)
    print(f"[examen] passe de nuit confiee ({job_id})")
    return job_id


def reprendre_ancienne_file():
    """Les demandes restees dans l'ancienne file partagee repartent en travaux."""
    anciennes = _lire(ANCIENNE_FILE, None)
    if anciennes is None:
        return 0
    n = 0
    for e in anciennes if isinstance(anciennes, list) else []:
        if demander(e.get("fichier")) == "demande":
            n += 1
    try:
        os.replace(ANCIENNE_FILE, ANCIENNE_FILE + ".reprise")
    except OSError as exc:
        print(f"[examen] ancienne file non retiree : {exc!r}")
    print(f"[examen] ancienne file : {n} demande(s) reprise(s)")
    return n


def ranger(job, result, folder):
    """Resultat d'un travail examen ou examen_lot : sons nettoyes vers
    radio_log/propositions_audio, propositions au journal proposition.
    Rend le nombre de propositions rangees."""
    import propositions_reglages as pr
    params = job.get("params") or {}
    origine = params.get("origine")
    if origine:
        demandes = _lire(DEMANDES, {})
        if origine in demandes:
            demandes[origine]["fini"] = time.time()
            _ecrire(DEMANDES, demandes)
    if result.get("etat") != "fait":
        print(f"[examen] {job.get('id')} en echec : {result.get('message')}")
        return 0
    props = _lire(os.path.join(folder, "propositions.json"), [])
    os.makedirs(AUDIO_DIR, exist_ok=True)
    n = 0
    for prop in props if isinstance(props, list) else []:
        for t in prop.get("traitements") or []:
            src = os.path.join(folder, t.get("audio") or "")
            if not t.get("audio") or not os.path.isfile(src):
                continue
            dest = os.path.join(AUDIO_DIR, os.path.basename(src))
            shutil.copyfile(src, dest)
            t["audio"] = os.path.relpath(dest, RADIO_LOG_DIR)
        if params.get("brut"):
            prop["brut"] = params["brut"]
        pr.ajouter(prop)
        n += 1
    print(f"[examen] {job.get('id')} : {n} proposition(s) rangee(s)")
    transcription = _lire(os.path.join(folder, "transcription.json"), {})
    if job.get("type") != "examen" or not origine:
        return n
    resume = props[0].get("resume") if n else None
    machine = ""
    try:
        import compagnon
        machine = (compagnon.trusted().get(result.get("compagnon"), {}) or {}).get("nom") or ""
    except Exception:
        pass
    import threading
    threading.Thread(target=_suite, args=(params, transcription, resume, machine), daemon=True).start()
    return n


def _suite(params, transcription, resume, machine):
    """Apres l'examen : analyse de la station si Whisper a compris quelque chose,
    puis une seule annonce (examen demande) ; le soir, rien n'est dit."""
    from phrases import dire
    origine = params.get("origine")
    texte = (transcription or {}).get("texte") or ""
    res = None
    if texte and params.get("freq_hz"):
        try:
            import noms_whisper
            propre = noms_whisper.texte_utile(texte, params.get("label") or "")
            if propre:
                import analyse_station
                res = analyse_station.analyser(params["freq_hz"], params.get("mode"), params.get("label"),
                                               params.get("juge"), propre, origine, machine,
                                               journal=_chemin_journal(origine))
        except Exception as exc:
            print(f"[examen] analyse de station : {exc!r}")
    if params.get("soir"):
        try:
            import noms_whisper
            noms_whisper.marquer_fait(origine)
        except Exception:
            pass
        return
    morceaux = [dire("examen.pret", resume=resume) if resume else dire("examen.rien")]
    statut = (transcription or {}).get("statut")
    if statut == "inaudible":
        morceaux.append(dire("examen.inaudible"))
    elif res:
        import analyse_station
        morceaux.append(analyse_station.phrase(res))
    elif texte:
        morceaux.append(dire("examen.rien_compris"))
    import veille_donnees as vd
    from sound_paths import RADIO_SCANER_DIR
    vd.announce(" ".join(m for m in morceaux if m), os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3"),
                "examen", "compagnon")
