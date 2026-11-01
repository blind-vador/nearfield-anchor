"""Examen en tache de fond des flux bruts radio (2026-10-25).

radio_watch garde le son brut que dsd-fme recoit du dongle (option -6, 48 kHz,
~/autopilot/brut_dsd) avec une fiche .json (bande, ce que le direct en a tire,
appel prive en clair ou chiffre). Ici, sans le dongle, on rejoue ces sons
devant les "juges" (un mode de dsd-fme chacun), SANS RIEN DETRUIRE et sans
annoncer a tort (les juges se trompent souvent) :

- La confiance (trust) de chaque juge est tenue PAR BANDE ; elle vient des
  retours de l'utilisateur a la volee (commande "juge" -> juge_live.py), pas
  d'une deduction automatique.
- Les juges passent dans l'ordre de confiance de la bande. Un juge ne decode
  tout seul au journal que si sa confiance sur cette bande est deja haute (TRUST_SUR,
  c.-a-d. que l'utilisateur l'a deja confirme bon ici) : la, c'est sur.
- Sinon, rien n'est annonce ni jete : le flux brut part dans a_revoir/ (garde),
  d'ou il revient si la bande apprend plus tard un meilleur mode.
- Un son decode en direct sert seulement au profil de la bande, il n'est pas
  rejuge. Appel chiffre : jamais garde. Appel prive EN CLAIR : garde comme le
  reste (c'est radio_watch qui applique cette regle).

Le tri vivant, lui, se fait a la volee pendant l'ecoute (juge_live.py,
commande "juge" / "juge ok" / "juge suivant")."""

import chemins
import glob
import json
import os
import shutil
import threading
import time

import juges_radio
import puissance
import radio_log

BRUT_DIR = chemins.base("brut_dsd")
A_REVOIR_DIR = os.path.join(BRUT_DIR, "a_revoir")
SCORES_PATH = chemins.base("juges_scores.json")
TAILLE_MAX = 1500 * 1024 * 1024     # au-dela, les plus vieux sons partent
A_REVOIR_GARDES = 120               # flux bruts non decodes gardes pour re-examen
STABLE_SEC = 5
TIMEOUT_SEC = 600
TRUST_SUR = 0.8                     # au-dessus, un juge decode tout seul au journal (confirme en live avant)

# juge -> protocole qu'il reconnait (None : auto). L'ordre du dict est l'ordre
# par defaut quand une bande n'a rien de confirme : auto, DMR, puis les rares.
JUGES = {"-fa": None, "-fs": "DMR", "-fi": "NXDN", "-fn": "NXDN", "-fm": "dPMR", "-fz": "M17",
         "-f1": "P25", "-fd": "D-Star", "-fy": "YSF", "-fx": "X2-TDMA",
         # 2026-11-01 : modes jamais tentes jusque-la (doctrine, scanner distribue) :
         # P25 phase 2, ProVoice, EDACS standard et EA.
         "-f2": "P25", "-fp": "ProVoice", "-fh": "EDACS", "-fe": "EDACS"}
DEFAUT_ORDRE = list(JUGES)

# Machines compagnes (2026-10-25) : un son a juger part a la machine qui finira
# le plus tot (compagnon.rank, travail "radio_juges"), meme plus lente que le Pi
# jusqu'a PREFERENCE_COMPAGNE fois : c'est la chaleur du Pi qu'on epargne. Le
# Pi ne juge lui-meme que sans machine connectee capable. Les verdicts reviennent
# dans RETOURS_DIR (deposes par compagnon_resultats) et sont appliques ici, seul
# ecrivain des fiches et des scores.
RETOURS_DIR = os.path.join(BRUT_DIR, "retours")
DUREES_PI_PATH = chemins.base("juges_durees_pi.json")
ETALON_PI_PATH = chemins.base("compagnon/puissance_etalon.json")
PREFERENCE_COMPAGNE = 3.0
DELEGUE_MAX_SEC = 900               # sans verdict au bout de 15 min, le Pi reprend la main
DELEGUES_MAX = 8                    # sons confies et pas encore revenus
APPRENTISSAGE = 0.3

PAUSE_C = 73.0
REPRISE_C = 70.0
DEUX_A_LA_FOIS_C = 65.0

_verrou = threading.Lock()


def _temperature():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except (OSError, ValueError):
        return 0.0


def _lire(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def _ecrire(chemin, data):
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)


def _fiche_de(wav):
    return wav[:-4] + ".json"


# --- confiance (trust), par bande -----------------------------------------------------

def _scores():
    s = _lire(SCORES_PATH, {})
    s.setdefault("bandes", {})
    return s


def _bande(scores, band):
    b = scores["bandes"].setdefault(band or "?", {"_protocoles": {}})
    b.setdefault("_protocoles", {})
    for juge in JUGES:
        b.setdefault(juge, {"oui": 0, "non": 0, "propositions": 0})
    return b


def trust(js):
    """Part de "oui" sur les retours de l'utilisateur (1 oui, 1 non au depart)."""
    return (js["oui"] + 1) / (js["oui"] + js["non"] + 2)


def ordre(scores, band, restants):
    """Juges a essayer, le plus digne de confiance pour cette bande d'abord."""
    b = _bande(scores, band)
    return sorted(restants, key=lambda j: (-trust(b[j]), DEFAUT_ORDRE.index(j)))


def _noter_profil(scores, band, protocole):
    if protocole:
        prof = _bande(scores, band)["_protocoles"]
        prof[protocole] = prof.get(protocole, 0) + 1


# --- un juge ---------------------------------------------------------------------------

def juger(juge, wav):
    """Fait passer un son devant un juge, sur le Pi (code commun avec les
    machines compagnes : bridge/juges_radio.py). Rend le verdict et la voix
    decodee ; apprend au passage la vitesse du Pi (secondes par Mo de son)."""
    debut = time.time()
    verdict, voix = juges_radio.juger(juge, wav, timeout=TIMEOUT_SEC)
    try:
        taille = os.path.getsize(wav) / (1024 * 1024)
        if taille > 0.05:
            _apprendre_pi((time.time() - debut) / taille)
    except OSError:
        pass
    return verdict, voix


def _apprendre_pi(s_par_mo):
    """Vitesse du Pi pour un juge, ramenee a pleine puissance comme pour les
    machines compagnes (duree x part libre du moment)."""
    part = _part_libre_pi()
    d = _lire(DUREES_PI_PATH, {})
    plein = s_par_mo * (part if part else 1.0)
    d["s_mo_juge"] = round((1 - APPRENTISSAGE) * d.get("s_mo_juge", plein) + APPRENTISSAGE * plein, 3)
    d["n"] = d.get("n", 0) + 1
    _ecrire(DUREES_PI_PATH, d)


def _part_libre_pi():
    try:
        m = puissance.mesure(ETALON_PI_PATH)
        return max(0.02, min(1.0, m["disponible"] / m["puissance"])) if m.get("puissance") else None
    except Exception as exc:
        print(f"[juges] mesure du Pi impossible : {exc!r}")
        return None


def _fin_locale(taille_mo, n_juges):
    """Secondes estimees pour que le Pi passe ces juges lui-meme, chaleur et
    charge du moment comprises ; None sans historique."""
    d = _lire(DUREES_PI_PATH, {})
    part = _part_libre_pi()
    if not d.get("s_mo_juge") or not part:
        return None
    return d["s_mo_juge"] * taille_mo * n_juges / part


def _journaliser_sur(fiche, wav, juge, verdict, voix):
    """Juge deja confirme bon sur cette bande (trust haut) : on range sa voix au
    journal radio, sans question (c'est sur). Pas d'annonce vocale."""
    try:
        mhz = fiche["freq_hz"] / 1e6
        session = radio_log.RadioLogSession(f"decodage differe {verdict['protocole']}")
        session.add_audio_file(voix)
        chemin = session.finalize()
        print(f"[juges] {os.path.basename(wav)} [{fiche.get('band')}] {juge} : {verdict['protocole']} "
              f"decode (confiance haute) -> journal {chemin}")
        with _verrou:
            sc = _scores()
            _noter_profil(sc, fiche.get("band"), verdict["protocole"])
            _ecrire(SCORES_PATH, sc)
    except Exception as exc:
        print(f"[juges] journal differe impossible : {exc!r}")
    finally:
        if voix and os.path.exists(voix):
            os.remove(voix)


# --- la file ---------------------------------------------------------------------------

def _ranger(wav):
    for chemin in (wav, _fiche_de(wav)):
        try:
            os.remove(chemin)
        except OSError:
            pass


def _mettre_a_revoir(wav, scores):
    """Aucun juge n'a rien propose : le son brut est GARDE pour un re-examen futur."""
    os.makedirs(A_REVOIR_DIR, exist_ok=True)
    fiche_path = _fiche_de(wav)
    fiche = _lire(fiche_path, {})
    fiche["revu"] = True
    tops = ordre(scores, fiche.get("band"), list(JUGES))
    fiche["top_au_rangement"] = tops[0] if tops else None
    _ecrire(fiche_path, fiche)
    for src in (wav, fiche_path):
        try:
            shutil.move(src, os.path.join(A_REVOIR_DIR, os.path.basename(src)))
        except OSError:
            pass
    gardes = sorted(glob.glob(os.path.join(A_REVOIR_DIR, "*.wav")), key=os.path.getmtime)
    for vieux in gardes[:-A_REVOIR_GARDES]:
        for chemin in (vieux, _fiche_de(vieux)):
            try:
                os.remove(chemin)
            except OSError:
                pass


def _reprendre_a_revoir(scores):
    """Repasse en file un son d'a_revoir si la bande a appris, depuis, un meilleur
    mode en tete : une vraie chance neuve de le decoder."""
    for fiche_path in sorted(glob.glob(os.path.join(A_REVOIR_DIR, "*.json")), key=os.path.getmtime):
        wav = fiche_path[:-5] + ".wav"
        if not os.path.exists(wav):
            continue
        fiche = _lire(fiche_path, None)
        if fiche is None:
            continue
        tops = ordre(scores, fiche.get("band"), list(JUGES))
        if tops and tops[0] != fiche.get("top_au_rangement") and tops[0] not in fiche.get("juges", {}):
            fiche.pop("revu", None)
            fiche["juges"] = {}
            _ecrire(fiche_path, fiche)
            for src in (wav, fiche_path):
                try:
                    shutil.move(src, os.path.join(BRUT_DIR, os.path.basename(src)))
                except OSError:
                    pass
            print(f"[juges] {os.path.basename(wav)} repris d'a_revoir (bande {fiche.get('band')} : {tops[0]} en tete)")
            return True
    return False


def _a_juger(scores):
    """Sons a traiter, plus vieux d'abord. Au passage : un son confirme part ; un
    un son decode en direct nourrit le
    profil et part ; un son qu'aucun juge n'a propose va a_revoir (garde)."""
    file = []
    for fiche_path in glob.glob(os.path.join(BRUT_DIR, "*.json")):
        wav = fiche_path[:-5] + ".wav"
        if not os.path.exists(wav) or time.time() - os.path.getmtime(wav) < STABLE_SEC:
            continue
        fiche = _lire(fiche_path, None)
        if fiche is None:
            continue
        if fiche.get("decode"):
            _ranger(wav)
            continue
        direct = fiche.get("direct") or {}
        if direct.get("valide"):
            with _verrou:
                sc = _scores()
                _noter_profil(sc, fiche.get("band"), direct.get("protocole"))
                _ecrire(SCORES_PATH, sc)
            _ranger(wav)
            continue
        restants = [j for j in JUGES if j not in fiche.get("juges", {})]
        if restants:
            file.append((os.path.getmtime(wav), wav))
        else:
            _mettre_a_revoir(wav, scores)
    return [w for _, w in sorted(file)]


def _limiter_place():
    sons = sorted(glob.glob(os.path.join(BRUT_DIR, "*.wav")), key=os.path.getmtime)
    taille = sum(os.path.getsize(s) for s in sons)
    while sons and taille > TAILLE_MAX:
        vieux = sons.pop(0)
        taille -= os.path.getsize(vieux)
        print(f"[juges] place : {os.path.basename(vieux)} retire (manque de place)")
        _ranger(vieux)


def _passer(wav):
    """Un seul juge, le plus digne de confiance encore a essayer. S'il propose une
    voix ET qu'il est deja confirme sur cette bande (trust haut), on la range au
    journal ; sinon rien n'est annonce ni jete, et le juge suivant passera au tour
    d'apres (les non essayes epuises, le flux brut ira a_revoir)."""
    fiche_path = _fiche_de(wav)
    with _verrou:
        scores = _scores()
        fiche = _lire(fiche_path, None)
        if fiche is None or fiche.get("decode"):
            return
        band = fiche.get("band")
        restants = ordre(scores, band, [j for j in JUGES if j not in fiche.get("juges", {})])
        if not restants:
            return
        juge = restants[0]
        verdict_trust = trust(_bande(scores, band)[juge])
    verdict, voix = juger(juge, wav)
    journal = verdict["propose"] and verdict_trust >= TRUST_SUR
    with _verrou:
        scores = _scores()
        _bande(scores, band)[juge]["propositions"] += 1 if verdict["propose"] else 0
        fiche = _lire(fiche_path, None)
        if fiche is None:
            if voix and os.path.exists(voix):
                os.remove(voix)
            return
        fiche.setdefault("juges", {})[juge] = {k: verdict[k] for k in ("propres", "total", "protocole", "voix_s")}
        if journal:
            fiche["decode"] = {"juge": juge, "protocole": verdict["protocole"]}
        _ecrire(fiche_path, fiche)
        _ecrire(SCORES_PATH, scores)
    if journal:
        _journaliser_sur(fiche, wav, juge, verdict, voix)
        _ranger(wav)
        return
    etat = ("propose " + str(verdict["protocole"]) + " (confiance faible, on ne tranche pas)"
            if verdict["propose"] else ("CHIFFRE" if verdict["chiffre"] else "rien"))
    print(f"[juges] {os.path.basename(wav)} [{band}] {juge} : {verdict['propres']}/{verdict['total']} propres, "
          f"voix {verdict['voix_s']} s -> {etat}")
    if voix and os.path.exists(voix):
        os.remove(voix)


# --- machines compagnes ----------------------------------------------------------------

def _delegue_frais(fiche):
    d = (fiche or {}).get("delegue")
    return bool(d and time.time() - d.get("t", 0) < DELEGUE_MAX_SEC)


def _deleguer(wav, places):
    """Confie tous les juges restants de ce son a la machine compagne qui finira
    le plus tot. Rend True si le son est (deja) entre ses mains, ou s'il attend
    une place chez elle ; False si le Pi doit le juger lui-meme (aucune machine
    connectee capable, ou elle serait vraiment trop lente)."""
    fiche = _lire(_fiche_de(wav), None)
    if fiche is None or _delegue_frais(fiche):
        return True
    try:
        import compagnon
        taille = os.path.getsize(wav) / (1024 * 1024)
        machines = [c for c in compagnon.rank("radio_juges", taille) if c.get("directe")]
    except Exception as exc:
        print(f"[juges] machines compagnes illisibles : {exc!r}")
        return False
    if not machines:
        return False
    if places <= 0:
        return True  # une machine est la, elle a deja assez de sons : on l'attend plutot que chauffer
    best = machines[0]
    restants = ordre(_scores(), fiche.get("band"), [j for j in JUGES if j not in fiche.get("juges", {})])
    local = _fin_locale(taille, len(restants))
    if ((best.get("durees") or {}).get("radio_juges") and best.get("fin_estimee") and local
            and best["fin_estimee"] > PREFERENCE_COMPAGNE * local):
        return False
    job_id, dossier, job = compagnon.new_job("radio_juges", {
        "fichier": "son.wav", "juges": restants, "brut": os.path.basename(wav),
        "band": fiche.get("band"), "freq_hz": fiche.get("freq_hz")})
    shutil.copyfile(wav, os.path.join(dossier, "son.wav"))
    compagnon.submit(job)
    with _verrou:
        fiche = _lire(_fiche_de(wav), None)
        if fiche is not None:
            fiche["delegue"] = {"travail": job_id, "t": time.time(), "machine": best.get("nom")}
            _ecrire(_fiche_de(wav), fiche)
    print(f"[juges] {os.path.basename(wav)} [{fiche and fiche.get('band')}] : {len(restants)} juges confies a "
          f"{best.get('nom')}" + (f" (Pi seul : ~{local:.0f} s)" if local else ""))
    return True


def _appliquer_retours():
    """Verdicts rendus par les machines compagnes (deposes par compagnon_resultats
    dans RETOURS_DIR/<son>/) : appliques comme si le Pi avait juge lui-meme."""
    for dossier in sorted(glob.glob(os.path.join(RETOURS_DIR, "*"))):
        if dossier.endswith(".tmp") or not os.path.isdir(dossier):
            continue
        verdicts = _lire(os.path.join(dossier, "verdicts.json"), None)
        info = _lire(os.path.join(dossier, "retour.json"), {})
        try:
            if verdicts:
                _appliquer(os.path.join(BRUT_DIR, os.path.basename(dossier) + ".wav"), verdicts, dossier,
                           info.get("machine"))
        except Exception as exc:
            print(f"[juges] retour {os.path.basename(dossier)} : {exc!r}")
        finally:
            shutil.rmtree(dossier, ignore_errors=True)


def _appliquer(wav, verdicts, dossier, machine):
    fiche_path = _fiche_de(wav)
    with _verrou:
        scores = _scores()
        fiche = _lire(fiche_path, None)
        if fiche is None or fiche.get("decode") or not os.path.exists(wav):
            return
        band = fiche.get("band")
        fiche.pop("delegue", None)
        retenu = None
        for juge in ordre(scores, band, [j for j in verdicts if j in JUGES]):
            v = verdicts[juge]
            _bande(scores, band)[juge]["propositions"] += 1 if v.get("propose") else 0
            fiche.setdefault("juges", {})[juge] = {k: v.get(k) for k in ("propres", "total", "protocole", "voix_s")}
            if retenu is None and v.get("propose") and trust(_bande(scores, band)[juge]) >= TRUST_SUR:
                retenu = juge
        if retenu:
            fiche["decode"] = {"juge": retenu, "protocole": verdicts[retenu].get("protocole"), "machine": machine}
        _ecrire(fiche_path, fiche)
        _ecrire(SCORES_PATH, scores)
    props = [f"{j} {verdicts[j].get('protocole')}" for j in verdicts if verdicts[j].get("propose")]
    print(f"[juges] {os.path.basename(wav)} [{band}] juge par {machine} : {len(verdicts)} juges -> "
          + ("propositions " + ", ".join(props) + ("" if retenu else " (confiance faible, on ne tranche pas)")
             if props else "rien"))
    if retenu:
        voix = os.path.join(dossier, "voix_%s.wav" % retenu.lstrip("-"))
        _journaliser_sur(fiche, wav, retenu, verdicts[retenu], voix)
        _ranger(wav)


def main():
    os.makedirs(BRUT_DIR, exist_ok=True)
    print("[juges] demarrage")
    en_pause = False
    while True:
        t = _temperature()
        if en_pause and t < REPRISE_C:
            en_pause = False
            print(f"[juges] reprise ({t:.1f} C)")
        elif not en_pause and t >= PAUSE_C:
            en_pause = True
            print(f"[juges] pause de chaleur ({t:.1f} C), reprise sous {REPRISE_C:.0f} C")
        _limiter_place()
        _appliquer_retours()
        file = _a_juger(_scores())
        # Chaque son va d'abord a la machine compagne la plus libre (meme pendant
        # une pause de chaleur : c'est justement la qu'elle soulage le Pi).
        places = DELEGUES_MAX - sum(1 for w in file if _delegue_frais(_lire(_fiche_de(w), None)))
        locaux = []
        for w in file:
            frais = _delegue_frais(_lire(_fiche_de(w), None))
            if not _deleguer(w, places):
                locaux.append(w)
            elif not frais:
                places -= 1
        if en_pause:
            time.sleep(20)
            continue
        if not locaux:
            if file:
                time.sleep(3)  # sons entre les mains des machines compagnes
            elif not _reprendre_a_revoir(_scores()):
                time.sleep(10)
            continue
        cibles = locaux[:2] if t < DEUX_A_LA_FOIS_C else locaux[:1]
        fils = [threading.Thread(target=_passer, args=(w,)) for w in cibles]
        for f in fils:
            f.start()
        for f in fils:
            f.join()


if __name__ == "__main__":
    main()
