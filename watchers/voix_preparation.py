"""Preparation des voix a l'avance (demande utilisateur 2026-10-18) : tout ce
qu'on sait devoir dire est fabrique une fois avec Piper et garde, pour que la
voix locale reponde aussi vite qu'un son enregistre quand les voix Microsoft
ne repondent pas (ou quand Piper est choisi pour la console). Rien n'est
refait : seulement ce qui manque, puis on attend du nouveau.

  console     noms de touches et echos (debit d'echo), puis toutes les phrases
              fixes du projet (sans variable, debit des reponses), dans la
              voix de la console si c'est Piper, sinon dans sa voix de secours,
              et seulement si Microsoft n'a pas deja le son en cache ;
  annonces    phrases repetitives (noms de bandes et canaux, messages fixes) :
              une version par voix Piper, dans le cache des annonces, sauf si
              Microsoft y a deja des echantillons.

Le Pi fait aussi tourner le scanner : priorite minimale (nice), par petits
lots, et pause tant qu'il fait plus de MAX_TEMP_C.

Machine compagne (2026-10-19) : si une machine appairee sait faire "voix"
(Mac : ~/.nearfield-bridge/voix_piper.py), tout le travail en attente lui est
confie d'un coup (memes modeles, memes reglages, ~20 fois plus vite) ;
le Pi ne calcule lui-meme que si personne n'est disponible. Seules les voix
en service sont preparees : voix des annonces, et voix de la console (ou
sa voix de secours tant que Microsoft ne repond pas). kick() (phrases du
jour reecrites par Gemini a minuit, changement de voix) relance tout de suite.
"""
import chemins
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# la console du depot (pas ~/autopilot/console, ancienne copie qui masquait les
# commandes chargees plus tard, ex. "journal proposition", 2026-11-13)
_CONSOLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "console")
if _CONSOLE not in sys.path:
    sys.path.append(_CONSOLE)

import piper_out  # noqa: E402

LOT = 25
MAX_TEMP_C = 76.0  # le scanner tient le Pi vers 75-80 C ; il coupe lui-meme a 80
PAUSE_HOT_SEC = 300
PAUSE_DONE_SEC = 3600
PAUSE_BETWEEN_SEC = 5
KICK_FLAG = "/tmp/voix_preparation_maintenant"
COMPAGNON_ATTENTE_SEC = 45 * 60   # au-dela, un travail confie sans resultat est repris par le Pi
COMPAGNON_PAUSE_SEC = 60
EXTERNES_MAX_SEC = 300   # voix d'ailleurs : la liste est refaite au plus toutes les 5 minutes
MODELES_PARTAGES = "voix_piper"   # dans le dossier du pont, sur le NAS


def _temp():
    return piper_out._temp_c() or 0.0


def _cibles(source):
    """[(voix piper, debit)] en service pour cette source (commande "voix") :
    sa voix Piper si c'est la principale, sinon sa voix de secours tant que
    Microsoft ne repond pas (toutes les voix des annonces si "aleatoire")."""
    import voix_sources as vs
    r = vs.reglage(source)
    main = r["voix"]
    if main == vs.ESPEAK:
        return []
    if main == vs.ALEATOIRE_PIPER or main in vs.MODES_MELANGES:
        return [(v, r["debit"]) for v in vs.a_preparer(main)]
    if main.startswith("piper:"):
        main = piper_out.canonical(main)
        return [(main, r["debit"])] if piper_out.resolve(main) else []
    if vs.microsoft_ok():
        return []
    sec = piper_out.canonical(r["secours"])
    if sec == vs.ALEATOIRE or not piper_out.resolve(sec) or not vs.permise(sec):
        return [(v, r["secours_debit"]) for v in piper_out.announce_voice_ids() if vs.permise(v)]
    return [(sec, r["secours_debit"])]


def console_jobs():
    """[(voix, debit, texte, chemin)] manquants pour la console (phrases) et
    les touches du clavier (echos), chacune avec ses voix en service."""
    import phrases
    import phrases_fixes  # phrase par phrase (2026-10-18) : la console reconstitue ses reponses
    import voix
    import voix_cmd
    voix.reload()
    textes = []
    for key in phrases.base():
        text = phrases.texte(key)
        if text and "{" not in text and voix._cacheable(text):
            textes.append(" ".join(text.split()))
    textes += sorted(phrases_fixes.toutes())
    wanted = [(v, r, t) for v, r in _cibles("touches") for t in voix_cmd.echo_texts()]
    wanted += [(v, r, t) for v, r in _cibles("console") for t in textes]
    jobs = []
    for v, r, text in dict.fromkeys(wanted):
        if not voix._cached(text, v, r):
            jobs.append((v, r, text, voix._cache_path(text, v, r)))
    return jobs


def announce_jobs():
    """Phrases et morceaux des annonces, chacun dans les voix Piper en service
    des veilles qui le disent (commande "voix") : voix choisie, ou toutes
    celles du tirage pour une veille en aleatoire. Un texte dont on ne sait
    pas la veille (garde a l'usage) est prepare pour toutes."""
    import annonce_morceaux
    import phrases_fixes
    import restreint
    import speak
    import voix_sources as vs
    from sound_paths import TTS_CACHE_DIR
    par_source = {src: vs.voix_piper_source(src) for src in vs.ANNONCES}
    toutes = set().union(*par_source.values()) if par_source else set()
    textes = {}
    try:
        import atomes_parole
        vocab = set(atomes_parole.vocabulaire())
    except Exception:
        vocab = set()

    def add(text, sources):
        if text and text.strip():
            textes.setdefault(text.strip(), set()).update(sources)
    for c in restreint.cibles():  # noms de bandes et de canaux annonces par le scanner
        add(c["libelle"], {"radio"})
    for text, sources in annonce_morceaux.fixed_fragments_sources().items():
        add(text, sources)
    for s in phrases_fixes.annonces():  # sans ponctuation finale, comme les morceaux
        add(s[:-1] if s[-1:] in ".!?" else s, phrases_fixes.sources_annonce(s))
    try:
        for name in os.listdir(TTS_CACHE_DIR):
            path = os.path.join(TTS_CACHE_DIR, name, speak.TEXT_FILE)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    add(f.read(), {None})
    except OSError:
        return []
    jobs = []
    for text in sorted(textes):
        sources = textes[text]
        voix = toutes if None in sources else set().union(*(par_source.get(src, set()) for src in sources))
        if text in vocab:
            # Google ne prepare pas les phrases (il bloquerait), mais le petit
            # vocabulaire des nombres et des lettres, oui : ce sont les voix distantes.
            import piper_out
            voix = set(voix) | {(v, "+0%") for v in piper_out.GOOGLE_VOIX}
        if not voix:
            continue
        speak.remember_repetitive(text)
        folder = speak._repetitive_cache_dir(text)
        for voice, rate in sorted(voix):
            path = vs.fichier_cache(folder, {"moteur": "piper", "voix": voice, "debit": rate})
            if not os.path.exists(path):
                jobs.append((voice, rate, text, path))
    return jobs


def presentation_jobs():
    """La presentation de chaque voix Piper installee (commande "voix", etape
    1), pour que le choix d'une voix soit instantane ; rien d'autre pour une
    voix qui n'est pas en service."""
    import voix
    import voix_cmd
    import voix_sources as vs
    jobs = []
    for v in piper_out.voice_ids(lang=None):
        text = voix_cmd._label(v)
        if not voix._cached(text, v, "+0%"):
            jobs.append((v, "+0%", text, voix._cache_path(text, v, "+0%")))
    for alea in (vs.ALEATOIRE, vs.ALEATOIRE_PIPER, vs.ALEATOIRE_TOUT, vs.ALEATOIRE_DISTANT):  # presente dans une voix du tirage
        text = voix_cmd._label(alea)
        for v in piper_out.announce_voice_ids():
            if vs.permise(v) and not voix._cached(text, v, "+0%"):
                jobs.append((v, "+0%", text, voix._cache_path(text, v, "+0%")))
    return jobs


def kick():
    """Relance la preparation tout de suite (appele par d'autres services)."""
    try:
        open(KICK_FLAG, "w").close()
    except OSError:
        pass


def _compagnon_en_cours():
    """Un travail "voix" confie a une machine compagne, encore sans resultat ?"""
    import compagnon
    for name in compagnon._list("travaux"):
        folder = compagnon._path("travaux", name)
        job = compagnon.lire_travail(folder)
        if job and job.get("type") == "voix" and not compagnon.termine(name):
            if time.time() - job.get("cree", 0) < COMPAGNON_ATTENTE_SEC:
                return True
    return False


def _publier_modeles(names):
    """Modeles Piper du Pi copies sur le NAS (dossier du pont) pour la compagne."""
    import shutil
    import compagnon
    dest = compagnon._path(MODELES_PARTAGES)
    os.makedirs(dest, exist_ok=True)
    for name in names:
        for ext in (".onnx", ".onnx.json"):
            src, dst = os.path.join(piper_out.VOICE_DIR, name + ext), os.path.join(dest, name + ext)
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copyfile(src, dst + ".tmp")
                os.replace(dst + ".tmp", dst)


def confier(jobs):
    """Tout le travail en attente, d'un coup, a la machine compagne."""
    import json
    import compagnon
    entries, carte, models = [], {}, set()
    for n, (voice, rate, text, path) in enumerate(jobs):
        found = piper_out.resolve(voice)
        if not found:
            continue
        name, speaker = found
        inference = (piper_out._config(name) or {}).get("inference") or {}
        models.add(name)
        entries.append({"n": n, "modele": name, "locuteur": speaker, "texte": text,
                        "length_scale": piper_out.length_scale(rate),
                        "noise_scale": min(float(inference.get("noise_scale", 0.667)), piper_out.NOISE_SCALE_MAX),
                        "noise_w": min(float(inference.get("noise_w", 0.8)), piper_out.NOISE_W_MAX)})
        carte[n] = path
    _publier_modeles(sorted(models))
    job_id, folder, job = compagnon.new_job("voix", {"modeles": sorted(models), "nombre": len(entries),
                                                      "dossier_modeles": MODELES_PARTAGES})
    with open(os.path.join(folder, "entrees.json"), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    with open(os.path.join(folder, "carte.json"), "w", encoding="utf-8") as f:
        json.dump(carte, f, ensure_ascii=False)
    compagnon.submit(job)
    print(f"[voix_preparation] {len(entries)} sons confies a la machine compagne ({job_id})")


# Phrases qu'une voix d'ailleurs n'a pas su dire (2026-10-23 : MBROLA ne rend
# rien pour la touche ".", reessayee toutes les 7 s pendant des heures) : plus
# retentees avant le prochain demarrage du service.
_ECHECS_EXTERNES = set()


def _edge_atomes():
    """Quelques mots des nombres et des lettres, dans chaque voix Microsoft.
    Le premier « 410 » n'attend pas le reseau : les mots sont deja la."""
    try:
        import atomes_parole
        import speak
        import voix_sources as vs
        import annonce_morceaux
    except Exception as exc:
        print(f"[voix_preparation] atomes : {exc!r}")
        return
    if not vs.microsoft_ok():
        return
    # Pas le debit des touches (+45 %) : un echo de frappe n'est pas assemble,
    # et cela doublerait les appels Microsoft pour rien.
    debits = {("+0%", "+0Hz")}
    for src in list(getattr(vs, "ANNONCES", ())) + ["console"]:
        try:
            r = vs.reglage(src)
            debits.add((r.get("debit") or "+0%", r.get("hauteur") or "+0Hz"))
        except Exception:
            pass
    manquants = []
    for mot in atomes_parole.vocabulaire():
        speak.remember_repetitive(mot)
        folder = speak._repetitive_cache_dir(mot)
        for voix in speak.EDGE_TTS_VOICES:
            for debit, hauteur in debits:
                choix = {"moteur": "edge", "voix": voix, "debit": debit, "hauteur": hauteur}
                path = vs.fichier_cache(folder, choix)
                if not os.path.exists(path):
                    manquants.append((mot, choix, path))
    if not manquants:
        return
    faits = 0
    for mot, choix, path in manquants[:3]:
        if annonce_morceaux._synth_edge(mot, choix, path):
            faits += 1
    print(f"[voix_preparation] {faits}/3 atomes Microsoft, {len(manquants) - faits} restants")


def run_once():
    """Un lot ; rend le nombre restant avant ce lot (-1 : travail confie, on attend)."""
    try:
        _edge_atomes()
    except Exception as exc:
        print(f"[voix_preparation] atomes Microsoft : {exc!r}")
    jobs = announce_jobs() + console_jobs() + presentation_jobs()
    if not jobs:
        return 0
    # Voix d'ailleurs (2026-10-22) : jamais confiees en lot a la compagne (pas de
    # modele a lui envoyer) ; faites sur place, sans attendre la fraicheur (rien a
    # calculer ici). Google seulement pour la presentation de la voix : des
    # centaines de phrases d'un coup lui vaudraient un blocage.
    try:
        import atomes_parole
        atomes = set(atomes_parole.vocabulaire())
    except Exception:
        atomes = set()
    externes = [j for j in jobs if piper_out.virtuelle(j[0])
                and (not piper_out.canonical(j[0]).startswith("piper:google#")
                     or j[2] in atomes or j in presentation_jobs())
                and (j[0], j[1], j[2]) not in _ECHECS_EXTERNES]
    jobs = [j for j in jobs if not piper_out.virtuelle(j[0])]
    if externes:
        voice, rate = externes[0][0], externes[0][1]
        tous = [(t, p) for v, r, t, p in externes if (v, r) == (voice, rate)]
        # Lots a la suite (2026-11-06) : refaire la liste apres chaque lot, c'est
        # 57 000 fichiers verifies et 6 s de processeur tous les 25 sons.
        debut = time.time()
        for i in range(0, len(tous), LOT):
            lot = tous[i:i + LOT]
            done = piper_out.batch(voice, lot, piper_out.length_scale(rate))
            print(f"[voix_preparation] {done}/{len(lot)} sons {voice} ({rate}), voix d'ailleurs")
            for t, chemin in lot:
                if not os.path.exists(chemin):
                    _ECHECS_EXTERNES.add((voice, rate, t))
            if not done or time.time() - debut > EXTERNES_MAX_SEC or os.path.exists(KICK_FLAG):
                break
        return len(externes) + len(jobs)
    if not jobs:
        return 0
    try:
        import compagnon
        if _compagnon_en_cours():
            return -1
        if compagnon.available("voix"):
            confier(jobs)
            return -1
    except Exception as exc:
        print(f"[voix_preparation] machine compagne : {exc!r}")
    if _temp() > MAX_TEMP_C:
        return -1  # le Pi seul : il attend d'etre plus frais
    voice, rate = jobs[0][0], jobs[0][1]
    lot = [(t, p) for v, r, t, p in jobs if (v, r) == (voice, rate)][:LOT]
    start = time.time()
    done = piper_out.batch(voice, lot, piper_out.length_scale(rate))
    print(f"[voix_preparation] {done}/{len(lot)} sons {voice} ({rate}) en {time.time() - start:.0f} s, "
          f"{len(jobs) - done} restants")
    return len(jobs)


def main():
    print("[voix_preparation] demarrage")
    while True:
        try:
            left = run_once()
        except Exception as exc:
            print(f"[voix_preparation] {exc!r}")
            left = 0
        pause = PAUSE_BETWEEN_SEC if left > 0 else (COMPAGNON_PAUSE_SEC if left < 0 else PAUSE_DONE_SEC)
        fin = time.time() + pause
        while time.time() < fin:  # kick() : on repart tout de suite
            if os.path.exists(KICK_FLAG):
                os.remove(KICK_FLAG)
                break
            time.sleep(min(15, max(0.5, fin - time.time())))


if __name__ == "__main__":
    main()
