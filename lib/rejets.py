"""Journal des rejets (2026-10-09) : les candidats ecartes en silence par le
scanner ("bruit, pas de signal reel" -- RNNoise ; ou "parasite connu"). Chaque
rejet garde un COURT extrait audio (RADIO_LOG_DIR/rejets/, purge 48 h comme le
reste du journal) et une entree d'index.

La console ("journal des rejets", console/rejets_cmd.py) rejoue chaque extrait
et laisse l'utilisateur trancher A L'OREILLE :
- n (noter)   : c'etait en fait un VRAI signal, ecarte a tort  -> verdict "signal"
- r (rejeter) : c'etait bien du bruit/parasite                 -> verdict "bruit"

Ces verdicts (faux rejets vs bruit confirme) sont la base pour recalibrer les
seuils et l'apprentissage, et pour la double verification par le Mac mini via
le bridge (modele neuronal plus costaud) -- cumul metadonnees + IA + humain.
"""
import fcntl
import json
import os
import time

from sound_paths import RADIO_LOG_DIR

REJETS_DIR = os.path.join(RADIO_LOG_DIR, "rejets")
INDEX_PATH = os.path.join(REJETS_DIR, "index.json")
LOCK_PATH = "/tmp/rejets.lock"
MAX_ENTRIES = 500  # borne dure : on ne garde que les plus recents (+ purge 48 h)


def _read():
    try:
        with open(INDEX_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _with_lock(fn):
    """Lecture-modification-ecriture atomique sous verrou (le scanner ajoute,
    la console etiquette -- chacun de son cote)."""
    os.makedirs(REJETS_DIR, exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read()
        result = fn(data)
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, INDEX_PATH)
        return result


def record(log_path, freq_hz, label, reason, fingerprint=None, **autres):
    """Ajoute un rejet a l'index. log_path = extrait audio deja ecrit (par
    RadioLogSession.finalize(subdir="rejets")). reason : "bruit" ou "parasite"."""
    if not log_path:
        return

    def add(data):
        data.append({
            "fichier": os.path.basename(log_path),
            "freq_hz": freq_hz,
            "label": label,
            "raison": reason,
            "le": time.time(),
            "verdict": None,           # None = pas encore trie
            "empreinte": fingerprint,  # pour recalibrage / double verif IA
            **autres,                  # mode, target, juge, rendus (garder)
        })
        del data[:-MAX_ENTRIES]
    try:
        _with_lock(add)
    except Exception as exc:
        print(f"[rejets] enregistrement impossible : {exc}")


def entries():
    """Rejets du plus recent au plus ancien, seulement ceux dont l'extrait
    existe encore (purge 48 h)."""
    out = []
    for e in _read():
        if os.path.isfile(os.path.join(REJETS_DIR, e.get("fichier", ""))):
            out.append(e)
    out.sort(key=lambda e: e.get("le", 0), reverse=True)
    return out


def path_of(fichier):
    return os.path.join(REJETS_DIR, fichier)


def set_verdict(fichier, verdict):
    """verdict : "signal" (vrai signal ecarte a tort), "bruit" (rejet correct),
    ou None (a trier). Retourne True si trouve."""
    found = [False]

    def apply(data):
        for e in data:
            if e.get("fichier") == fichier:
                e["verdict"] = verdict
                found[0] = True
    try:
        _with_lock(apply)
    except Exception as exc:
        print(f"[rejets] verdict non enregistre : {exc}")
    return found[0]


def purge(cutoff):
    """Retire de l'index les entrees plus vieilles que cutoff (epoch) ou dont
    l'extrait a disparu. Les mp3 eux-memes sont purges par radio_log."""
    def clean(data):
        kept = [e for e in data if e.get("le", 0) >= cutoff
                and os.path.isfile(os.path.join(REJETS_DIR, e.get("fichier", "")))]
        data[:] = kept
    try:
        _with_lock(clean)
    except Exception as exc:
        print(f"[rejets] purge impossible : {exc}")


# --- Garder au lieu de jeter (2026-11-05) -------------------------------------
# Tout ce que le scanner ecarte sans l'ouvrir arrive ici : on ne presume de rien,
# l'utilisateur va ecouter s'il le veut et repeche ce qui a ete ecarte a tort, et
# la passe IA du Mac (mac/analyse.py) lit ce meme dossier la nuit.

EXTRAIT_MIN_SEC = 0.5
_OCTETS = {"s16le": 2, "f32le": 4}


def _utile(son):
    if son.get("wav"):
        return os.path.isfile(son["wav"]) and os.path.getsize(son["wav"]) > 4000
    format_ = son.get("format", "s16le")
    return len(son.get("pcm") or b"") >= EXTRAIT_MIN_SEC * (son.get("taux") or 22050) * _OCTETS.get(format_, 2)


def _rendu_mp3(son, dest):
    import subprocess
    entree = (["-i", son["wav"]] if son.get("wav") else
              ["-f", son.get("format", "s16le"), "-ar", str(son.get("taux") or 22050), "-ac", "1", "-i", "-"])
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + entree
                          + ["-ac", "1", "-codec:a", "libmp3lame", "-q:a", "5", dest],
                          input=None if son.get("wav") else son["pcm"], capture_output=True, timeout=60)
    return proc.returncode == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 200


def _empreinte(son):
    import propositions
    if son.get("wav"):
        return propositions._empreinte_wav(son["wav"])
    import noise_signatures as ns
    return ns.fingerprint(son.get("pcm") or b"", son.get("taux") or 22050, son.get("format", "s16le"))


def garder(freq_hz, mode, label, raison, sons, target=None, empreinte=None):
    """Range un candidat ecarte. sons : [{juge, wav}] ou [{juge, pcm, taux, format}],
    le plus probable d'abord ; les suivants restent derriere la touche J. Son brut
    seul, sans habillage (la passe IA ne doit pas y entendre notre voix de synthese).
    Rend le chemin de l'extrait, ou None s'il n'y a rien a ecouter."""
    import radio_log
    sons = [s for s in sons or [] if _utile(s)]
    if not sons:
        return None
    premier = sons[0]
    session = radio_log.RadioLogSession(label or target or "rejet")
    if premier.get("wav"):
        session.add_audio_file(premier["wav"], contenu=True)
    else:
        session.add_raw_pcm(premier["pcm"], premier.get("taux") or 22050, premier.get("format", "s16le"))
    chemin = session.finalize(subdir="rejets")
    if not chemin:
        return None
    rendus = []
    for i, son in enumerate(sons[1:], start=1):
        nom = f"{os.path.basename(chemin)[:-4]}.p{i}.mp3"
        try:
            if _rendu_mp3(son, os.path.join(REJETS_DIR, nom)):
                rendus.append({"juge": son.get("juge"), "fichier": nom, "empreinte": _empreinte(son)})
        except Exception as exc:
            print(f"[rejets] rendu {son.get('juge')} non garde : {exc!r}")
    record(chemin, freq_hz, label, raison, empreinte, mode=mode, target=target, juge=premier.get("juge"),
           rendus=rendus)
    return chemin


def garder_fond(*args, **kwargs):
    """Meme chose sans retarder l'ecoute (pcm seulement : un wav peut disparaitre)."""
    import threading

    def travail():
        try:
            garder(*args, **kwargs)
        except Exception as exc:
            print(f"[rejets] extrait non garde : {exc!r}")
    threading.Thread(target=travail, daemon=True).start()


def confirmer_bruit(fichier, rendu=0):
    """R : ce rendu est du bruit. Le decodeur ecoute retient son empreinte, sur
    cette frequence seulement. Rend son nom, ou None s'il n'y a rien a apprendre."""
    import noise_signatures as ns
    e = next((x for x in _read() if x.get("fichier") == fichier), None)
    if not e:
        return None
    autres = e.get("rendus") or []
    source = autres[rendu - 1] if 0 < rendu <= len(autres) else e
    juge = source.get("juge") or e.get("mode")
    if not source.get("empreinte") or not e.get("freq_hz"):
        return None
    ns.add_signature({"freq_hz": e["freq_hz"], "label": e.get("label") or "", "fingerprint": source["empreinte"],
                      "juge": juge})
    return juge


def repecher(fichier):
    """Ecarte a tort : la prise retourne au journal avec sa ceremonie, ses rendus,
    et ce qu'il faut pour que V, C et les fleches apprennent. Une empreinte de
    parasite qui l'avait ecartee est oubliee. Rend le chemin, ou None."""
    import ceremonie_radio as ceremonie
    import noise_signatures as ns
    import propositions
    import radio_log
    e = next((x for x in _read() if x.get("fichier") == fichier), None)
    source = path_of(fichier)
    if not e or not os.path.isfile(source):
        return None
    juge = e.get("juge") or e.get("mode")
    session = radio_log.RadioLogSession(e.get("label") or "rejet", exiger_contenu=True)
    ceremonie.habiller(session, e.get("label"), ceremonie.modulation_dite(juge), source, e.get("freq_hz"),
                       bande_id=e.get("target"))
    chemin = session.finalize()
    if not chemin:
        return None
    ns.record_capture("rejets", e.get("freq_hz") or 0, e.get("mode") or juge, e.get("label"), e.get("target"),
                      chemin, e.get("empreinte"), {"juge": juge, "repeche": True, "raison": e.get("raison")})
    autres = [{"juge": r.get("juge"), "wav": path_of(r["fichier"])} for r in e.get("rendus") or []
              if os.path.isfile(path_of(r.get("fichier") or ""))]
    propositions.poser(chemin, e.get("freq_hz"), e.get("label"), juge, e.get("empreinte"), autres)
    if e.get("empreinte"):
        ns.oublier_ressemblantes(e.get("freq_hz") or 0, e["empreinte"], juge)
    for r in e.get("rendus") or []:
        if r.get("empreinte"):
            ns.oublier_ressemblantes(e.get("freq_hz") or 0, r["empreinte"], r.get("juge"))
    set_verdict(fichier, "signal")
    for nom in [fichier] + [r.get("fichier") for r in e.get("rendus") or []]:
        try:
            os.remove(path_of(nom))
        except OSError:
            pass
    return chemin
