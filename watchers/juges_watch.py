"""Examen en tache de fond des flux bruts radio (2026-10-25).

radio_watch garde le son brut que dsd-fme recoit du dongle (option -6, 48 kHz,
~/autopilot/brut_dsd) avec une fiche .json (bande, ce que le direct en a tire,
appel prive en clair ou chiffre). Ici, sans le dongle, on rejoue ces sons
devant les "juges" (un mode de dsd-fme chacun), SANS RIEN DETRUIRE et sans
annoncer a tort (les juges se trompent souvent) :

- La confiance (trust) de chaque juge est tenue PAR BANDE ; elle vient des
  retours de l'utilisateur (commande "juge", et depuis le 2026-11-05 V, C et les
  fleches du journal), pas d'une deduction automatique.
- Les juges passent dans l'ordre de confiance. Elle classe, elle ne decide plus :
  toute proposition va au journal (2026-11-05), c'est l'humain qui dit si c'etait
  le bon juge. Il fallait 0,8 de confiance sur un score que rien ne nourrissait :
  aucune proposition seule n'etait jamais arrivee jusqu'a lui.
- Aucun juge n'a rien propose : le flux brut part dans a_revoir/ (garde), d'ou il
  revient si la bande apprend plus tard un meilleur mode.
- Un son decode en direct sert seulement au profil de la bande, il n'est pas
  rejuge. Appel chiffre : jamais garde. Appel prive EN CLAIR : garde comme le
  reste (c'est radio_watch qui applique cette regle).

Le tri vivant, lui, se fait a la volee pendant l'ecoute (juge_live.py,
commande "juge" / "juge ok" / "juge suivant")."""

import chemins
import glob
import json
import os
import re
import shutil
import tempfile
import threading
import time

import juges_radio
import puissance
import radio_log
import tickets_partages as tickets
import tickets_pi
from sound_paths import RADIO_SCANER_DIR

BRUT_DIR = chemins.base("brut_dsd")
A_REVOIR_DIR = os.path.join(BRUT_DIR, "a_revoir")
SCORES_PATH = chemins.base("juges_scores.json")
TAILLE_MAX = 1500 * 1024 * 1024     # au-dela, les plus vieux sons partent
A_REVOIR_GARDES = 120               # flux bruts non decodes gardes pour re-examen
STABLE_SEC = 5
TIMEOUT_SEC = 600

# juge -> protocole qu'il reconnait (None : auto). L'ordre du dict est l'ordre
# par defaut quand une bande n'a rien de confirme : auto, DMR, puis les rares.
JUGES = {"-fa": None, "-fs": "DMR", "-fi": "NXDN", "-fn": "NXDN", "-fm": "dPMR", "-fz": "M17",
         "-f1": "P25", "-fd": "D-Star", "-fy": "YSF", "-fx": "X2-TDMA",
         # 2026-11-01 : modes jamais tentes jusque-la (doctrine, scanner distribue) :
         # P25 phase 2, ProVoice, EDACS standard et EA.
         "-f2": "P25", "-fp": "ProVoice", "-fh": "EDACS", "-fe": "EDACS"}
DEFAUT_ORDRE = list(JUGES)


def _restants(fiche):
    """Juges encore à passer. Un envoi Contrôle+R n'en demande qu'un."""
    fait = set((fiche or {}).get("juges") or {})
    if (fiche or {}).get("etiquette") == "decodeur":
        seul = fiche.get("decodeur")
        return [seul] if seul in JUGES and seul not in fait else []
    return [j for j in JUGES if j not in fait]

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


BONUS_DEBIT = 0.3   # un juge compatible avec le debit symbole vu sur le son passe devant


def ordre(scores, band, restants, freq_hz=None, debit=None):
    """Juges a essayer, le plus souvent valide sur cette frequence d'abord,
    puis le plus digne de confiance pour la bande. debit (2026-11-11, plan phase 6) :
    rythme symbole estime a l'aveugle sur le son (lib/debit_symbole) ; les juges
    compatibles passent devant, aucun n'est retire."""
    b = _bande(scores, band)
    try:
        import propositions
        souvenir = {j: propositions.rang(freq_hz, j) for j in restants}
    except Exception:
        souvenir = {}
    compatibles = ()
    if debit:
        import debit_symbole
        compatibles = debit_symbole.juges_compatibles(debit)
    return sorted(restants, key=lambda j: (
        -(souvenir.get(j, 0.5) + (BONUS_DEBIT if j in compatibles else 0)), -trust(b[j]),
        DEFAUT_ORDRE.index(j) if j in JUGES else 50))


def _debit(wav, fiche):
    """Debit symbole du son, estime une fois et garde dans sa fiche."""
    if "debit_symbole" not in fiche:
        import debit_symbole
        debit, rapport = debit_symbole.estimer_fichier(wav)
        fiche["debit_symbole"] = debit
        fiche["debit_rapport"] = round(rapport, 1)
        if debit:
            print(f"[juges] {os.path.basename(wav)} : rythme de {debit} bauds (raie x{rapport:.0f})")
            if debit == 1200 and fiche.get("freq_hz"):
                try:   # AFSK 1200 : les juges de donnees de cette frequence passent devant
                    import propositions
                    propositions.a_priori(fiche["freq_hz"], "donnees", 1.0, "debit 1200 bauds")
                except Exception as exc:
                    print(f"[juges] a priori donnees : {exc!r}")
        try:
            _ecrire(_fiche_de(wav), fiche)
        except OSError:
            pass
    return fiche.get("debit_symbole")


def ordre_son(scores, wav, fiche, restants):
    return ordre(scores, fiche.get("band"), restants, fiche.get("freq_hz"), _debit(wav, fiche))


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
    """Un juge propose : sa voix va au journal radio, l'humain dira. Pas d'annonce
    vocale."""
    try:
        mhz = fiche["freq_hz"] / 1e6
        import ceremonie_radio as ceremonie
        session = radio_log.RadioLogSession(f"decodage differe {verdict['protocole']}")
        ceremonie.habiller(session, fiche.get("band"), verdict.get("protocole"), voix, fiche.get("freq_hz"))
        chemin = session.finalize()
        print(f"[juges] {os.path.basename(wav)} [{fiche.get('band')}] {juge} : {verdict['protocole']} "
              f"propose -> journal {chemin}")
        if chemin:  # sans cela V, C et les fleches n'apprenaient rien de cette prise
            import noise_signatures as ns
            ns.record_capture("juges", fiche.get("freq_hz") or 0, juge, fiche.get("band"), fiche.get("band"), chemin,
                              None, {"numerique": True, "juge": juge, "protocole": verdict.get("protocole"),
                                     "doute": verdict.get("doute")})
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
    tops = ordre(scores, fiche.get("band"), list(JUGES), fiche.get("freq_hz"))
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


_revoir_vu = None  # etat des fichiers au dernier examen d'a_revoir qui n'a rien repris


def _mtime(chemin):
    try:
        return os.path.getmtime(chemin)
    except OSError:
        return 0


def _reprendre_a_revoir(scores):
    """Repasse en file un son d'a_revoir si la bande a appris, depuis, un meilleur
    mode en tete : une vraie chance neuve de le decoder. Rien de neuf dans les
    scores ni dans le dossier : rien a examiner (2026-11-06 : 120 sons reexamines
    toutes les 10 s, soit 17 % d'un coeur pour rien)."""
    global _revoir_vu
    import propositions
    cle = (_mtime(SCORES_PATH), _mtime(propositions.SCORES), _mtime(A_REVOIR_DIR))
    if cle == _revoir_vu:
        return False
    for fiche_path in sorted(glob.glob(os.path.join(A_REVOIR_DIR, "*.json")), key=os.path.getmtime):
        wav = fiche_path[:-5] + ".wav"
        if not os.path.exists(wav):
            continue
        fiche = _lire(fiche_path, None)
        if fiche is None:
            continue
        tops = ordre(scores, fiche.get("band"), list(JUGES), fiche.get("freq_hz"))
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
    _revoir_vu = cle
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
        restants = _restants(fiche)
        if restants:
            file.append((os.path.getmtime(wav), wav))
        elif fiche.get("etiquette") == "decodeur":
            _ranger(wav)
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


def _au_rejet(fiche, juge, voix, raison):
    """Ecoutable au journal des rejets : c'est l'humain qui dit."""
    try:
        import rejets
        rejets.garder(fiche.get("freq_hz"), juge, fiche.get("band"), raison, [{"juge": juge, "wav": voix}],
                      target=fiche.get("band"))
    except Exception as exc:
        print(f"[juges] rejet non garde : {exc!r}")


def _voix_rejetee(freq_hz, chemin, juge=None):
    """La sortie de ce juge ressemble à une empreinte déjà rejetée ici."""
    if not chemin or not os.path.exists(chemin):
        return False, None
    try:
        import noise_signatures as ns
        return ns.matches_wav(freq_hz, chemin, juge)
    except Exception as exc:
        print(f"[juges] comparaison d'empreinte : {exc!r}")
        return False, None


def _passer(wav):
    """Un seul juge, le plus digne de confiance encore a essayer. S'il propose une
    voix, on la range au journal ; sinon le juge suivant passera au tour d'apres
    (les non essayes epuises, le flux brut ira a_revoir)."""
    fiche_path = _fiche_de(wav)
    with _verrou:
        scores = _scores()
        fiche = _lire(fiche_path, None)
        if fiche is None or fiche.get("decode"):
            return
        band = fiche.get("band")
        juge_force = fiche.get("decodeur") if fiche.get("etiquette") == "decodeur" else None
        restants = ordre_son(scores, wav, fiche, _restants(fiche))
        if not restants and not juge_force:
            return
        juge = juge_force or restants[0]
    if juge_force:
        if juge_force not in JUGES:
            _ranger(wav)
            return
        import juges_radio
        verdict, voix = juges_radio.juger(juge_force, wav, timeout=TIMEOUT_SEC, garder=True)
        with _verrou:
            fiche2 = _lire(fiche_path, None)
            if fiche2 is not None:
                fiche2.setdefault("juges", {})[juge_force] = {
                    k: verdict.get(k) for k in ("propres", "total", "protocole", "voix_s")}
                _ecrire(fiche_path, fiche2)
        _clore_decodeur(wav, fiche, juge_force, verdict, voix)
        return
    verdict, voix = juger(juge, wav)
    if verdict.get("propose") and voix:
        pareil, sim = _voix_rejetee(fiche.get("freq_hz"), voix, juge)
        if pareil:
            print(f"[juges] {os.path.basename(wav)} {juge} : sortie trop proche d'un rejet "
                  f"({sim:.2f}), probablement pas le bon juge")
            _au_rejet(fiche, juge, voix, "ressemble a un parasite appris")
            verdict = dict(verdict, propose=False)
    journal = verdict["propose"]
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
    etat = "CHIFFRE" if verdict["chiffre"] else "rien"
    _preuve_sans_voix(fiche, {juge: verdict})
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
    restants = ordre_son(_scores(), wav, fiche, _restants(fiche))
    if not restants:
        return True
    local = _fin_locale(taille, len(restants))
    if ((best.get("durees") or {}).get("radio_juges") and best.get("fin_estimee") and local
            and best["fin_estimee"] > PREFERENCE_COMPAGNE * local):
        return False
    try:
        job_id, dossier, job = compagnon.new_job("radio_juges", {
            "fichier": "son.wav", "juges": restants, "brut": os.path.basename(wav),
            "band": fiche.get("band"), "freq_hz": fiche.get("freq_hz"),
            "garder_voix": fiche.get("etiquette") in ("renvoi", "decodeur")})
        shutil.copyfile(wav, os.path.join(dossier, "son.wav"))
        compagnon.submit(job)
    except OSError as exc:   # NAS absent ou refuse : le Pi garde le son pour lui
        print(f"[juges] {os.path.basename(wav)} : travail non depose ({exc!r})")
        return False
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


def _journaliser_plusieurs(fiche, juges, verdicts, dossier):
    """Plusieurs juges ont trouve : un seul enregistrement, tous les rendus."""
    import noise_signatures as ns
    import propositions
    rendus = []
    for j in juges:
        voix = os.path.join(dossier, "voix_%s.wav" % j.lstrip("-"))
        if os.path.exists(voix) and os.path.getsize(voix) > 44:
            rendus.append((j, voix, verdicts[j]))
    if len(rendus) < 2:
        return False
    j0, v0, verd0 = rendus[0]
    import ceremonie_radio as ceremonie
    session = radio_log.RadioLogSession(
        f"decodage differe {verd0.get('protocole') or j0}", exiger_contenu=True)
    ceremonie.habiller(session, fiche.get("band"), verd0.get("protocole") or ceremonie.modulation_dite(j0), v0,
                       fiche.get("freq_hz"))
    chemin = session.finalize()
    if not chemin:
        return False
    empreinte = None
    try:
        import wave
        with wave.open(v0) as w:
            pcm = w.readframes(w.getnframes())
            if w.getnchannels() == 2:
                import audioop
                pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
            empreinte = ns.fingerprint(pcm, w.getframerate(), "s16le")
    except Exception:
        pass
    propositions.poser(chemin, fiche.get("freq_hz"), fiche.get("band"), j0, empreinte, [
        {"juge": j, "wav": voix} for j, voix, _ in rendus[1:]])
    ns.record_capture("juges", fiche.get("freq_hz") or 0, j0, fiche.get("band"), fiche.get("band"),
                      chemin, empreinte, {"numerique": True, "juge": j0})
    print(f"[juges] {len(rendus)} rendus ranges au journal ({chemin})")
    return True


def _preuve_sans_voix(fiche, verdicts):
    """Le flux prouve un mode numerique et personne n'a de voix a rendre : on le dit
    (lib/numerique_vus). C'est ici que passe ce que le direct, moins sensible, a manque."""
    import numerique_vus
    prouves = [v for j, v in verdicts.items() if j in JUGES and v.get("protocole")
               and numerique_vus.prouve(v.get("propres"), v.get("total"))]
    if not prouves:
        return None
    v = max(prouves, key=lambda x: x.get("propres") or 0)
    numerique_vus.annoncer((fiche or {}).get("freq_hz"), v["protocole"], v["propres"], v.get("total"),
                           None, bool(v.get("chiffre")), "juges")
    return v["protocole"]


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
        forcer = fiche.get("etiquette") == "decodeur"
        for juge in ordre(scores, band, [j for j in verdicts if j in JUGES], fiche.get("freq_hz")):
            v = verdicts[juge]
            if v.get("propose") and not forcer:
                voix = os.path.join(dossier, "voix_%s.wav" % juge.lstrip("-"))
                pareil, sim = _voix_rejetee(fiche.get("freq_hz"), voix, juge)
                if pareil:
                    print(f"[juges] {os.path.basename(wav)} {juge} : sortie trop proche d'un rejet "
                          f"({sim:.2f}), probablement pas le bon juge")
                    _au_rejet(fiche, juge, voix, "ressemble a un parasite appris")
                    v = dict(v, propose=False)
                    verdicts[juge] = v
            _bande(scores, band)[juge]["propositions"] += 1 if v.get("propose") else 0
            fiche.setdefault("juges", {})[juge] = {k: v.get(k) for k in ("propres", "total", "protocole", "voix_s")}
            if retenu is None and v.get("propose"):
                retenu = juge
        if retenu:
            fiche["decode"] = {"juge": retenu, "protocole": verdicts[retenu].get("protocole"), "machine": machine}
        _ecrire(fiche_path, fiche)
        _ecrire(SCORES_PATH, scores)
    if fiche.get("etiquette") in ("renvoi", "decodeur"):
        _clore_renvoi(wav, fiche, verdicts, dossier)
        return
    props = [f"{j} {verdicts[j].get('protocole')}" for j in verdicts if verdicts[j].get("propose")]
    sans_voix = None if props else _preuve_sans_voix(fiche, verdicts)
    print(f"[juges] {os.path.basename(wav)} [{band}] juge par {machine} : {len(verdicts)} juges -> "
          + ("propositions " + ", ".join(props) if props else f"{sans_voix} prouve, sans voix" if sans_voix else "rien"))
    proposes = [j for j in ordre(_scores(), band, [j for j in verdicts if j in JUGES], fiche.get("freq_hz"))
                if verdicts[j].get("propose")]
    if len(proposes) >= 2 and _journaliser_plusieurs(fiche, proposes, verdicts, dossier):
        _ranger(wav)
        return
    if retenu:
        voix = os.path.join(dossier, "voix_%s.wav" % retenu.lstrip("-"))
        _journaliser_sur(fiche, wav, retenu, verdicts[retenu], voix)
        _ranger(wav)



def _dire_renvoi(phrase):
    if not phrase:
        return
    print(f"[juges] {phrase}")
    try:
        import speak
        speak.speak(phrase)
    except Exception as exc:
        print(f"[juges] annonce impossible : {exc!r}")


def _clore_decodeur(wav, fiche, juge, verdict, voix):
    """Un seul décodeur a répondu : on garde sa sortie et on le dit."""
    import tempfile
    import propositions
    dossier = tempfile.mkdtemp(prefix="decodeur_")
    try:
        if voix and os.path.isfile(voix) and os.path.getsize(voix) > 44:
            shutil.copyfile(voix, os.path.join(dossier, "voix_%s.wav" % str(juge).lstrip("-")))
        phrase = propositions.rendre_decodeur(wav, fiche, juge, verdict, dossier)
    finally:
        shutil.rmtree(dossier, ignore_errors=True)
        if voix and os.path.isfile(voix):
            try:
                os.remove(voix)
            except OSError:
                pass
    _dire_renvoi(phrase)
    _ranger(wav)


def _clore_renvoi(wav, fiche, verdicts, dossier):
    """Tous les juges ont repondu : on depose les sons sur la prise et on le dit.
    Un décodeur forcé ne dépose que sa propre sortie."""
    import propositions
    if (fiche or {}).get("etiquette") == "decodeur":
        juge = fiche.get("decodeur")
        phrase = propositions.rendre_decodeur(wav, fiche, juge, (verdicts or {}).get(juge), dossier)
    else:
        phrase = propositions.rendre_renvoi(wav, fiche, verdicts, dossier)
    _dire_renvoi(phrase)
    _ranger(wav)


def _renvois_deja_finis():
    """Un renvoi termine avant cette version : on le rend maintenant."""
    import propositions
    for dossier in (A_REVOIR_DIR, BRUT_DIR):
        if not os.path.isdir(dossier):
            continue
        for fiche_path in sorted(glob.glob(os.path.join(dossier, "renvoi_*.json"))):
            fiche = _lire(fiche_path, {})
            if fiche.get("etiquette") != "renvoi":
                continue
            juges = fiche.get("juges") or {}
            if any(j not in juges for j in JUGES):
                continue
            wav = fiche_path[:-5] + ".wav"
            _clore_renvoi(wav, fiche, juges, None)


# --- tickets partages (2026-11-04, bridge/tickets_partages.py) ---------------------------
# Quand une machine reliee sait les traiter, un son part en ticket plutot qu'en
# travail confie : le Pi et les compagnes s'y partagent les epreuves, les
# propositions sont repassees par une autre machine, et le resultat retombe
# dans _appliquer (rendus multiples, confiance, empreintes de rejet, renvois).
TICKET_MAX_SEC = 3600               # au-dela on conclut avec ce qu'on a ; le reste repartira
CONTRE_MAX_SEC = 300                # la contre-epreuve controle la machine : elle ne retient pas un rendu
MESSAGES_DITS_MAX = 3               # par ticket, comme en direct ; tous restent archives
BLU_ATTENTE_SEC = 40                # radio_watch attend lui-meme ses tickets BLU jusque-la
BLU_MAX_SEC = 300                   # un texte ou une voix BLU n'a plus d'interet au-dela
PI_RETARD_SEC = 300                 # le Pi ne reprend une epreuve libre qu'ensuite
CLOS_GARDE_SEC = 600
DONNEES_DIR = "tickets"             # archive veille_donnees de ce que trouvent les epreuves de donnees
DESACCORDS_PATH = chemins.base("tickets_desaccords.json")
SON_MESSAGE = os.path.join(RADIO_SCANER_DIR, "decoding_morse.mp3")     # comme radio_watch


def _epreuves_de(fiche, restants):
    """Tous les decodeurs qui savent lire un son, sauf ce qui a deja ete passe.
    Un envoi Controle+R ne demande qu'un juge."""
    eps = [tickets.epreuve_dsd(j) for j in restants]
    if fiche.get("etiquette") != "decodeur" and not fiche.get("donnees_faites"):
        eps += list(tickets.DONNEES_SON)
    return eps


def _ticket_frais(fiche):
    t = (fiche or {}).get("ticket")
    return bool(t and tickets.existe(tickets_pi.RACINE, t["id"]))


def _ouvrir_ticket(wav, places):
    """Pose le ticket de ce son. True si le son est (deja) en ticket ou attend une
    place ; False si le Pi doit faire autrement (NAS absent, rien a faire)."""
    fiche = _lire(_fiche_de(wav), None)
    if fiche is None:
        return True
    if _ticket_frais(fiche):
        return True
    if places <= 0:
        return True
    eps = _epreuves_de(fiche, ordre_son(_scores(), wav, fiche, _restants(fiche)))
    if not eps:
        return False
    garder = fiche.get("etiquette") in ("renvoi", "decodeur")
    try:
        tid = tickets_pi.ouvrir({"son.wav": wav}, {
            "origine": "brut", "brut": os.path.basename(wav), "band": fiche.get("band"),
            "freq_hz": fiche.get("freq_hz"), "garder": garder, "contre": not garder}, eps)
    except OSError as exc:
        print(f"[juges] ticket impossible (NAS ?) : {exc!r}")
        return False
    with _verrou:
        fiche = _lire(_fiche_de(wav), None)
        if fiche is not None:
            fiche["ticket"] = {"id": tid, "t": time.time()}
            _ecrire(_fiche_de(wav), fiche)
    print(f"[juges] {os.path.basename(wav)} [{fiche and fiche.get('band')}] : ticket {tid}, {len(eps)} epreuves")
    return True


def _noter_desaccords(tid, desaccords, avis):
    if not desaccords:
        return
    d = _lire(DESACCORDS_PATH, {})
    for ep, par_machine in desaccords.items():
        noms = {m: (avis.get(m) or {}).get("nom") or m for m in par_machine}
        print(f"[juges] ticket {tid} : {ep} DESACCORD entre {', '.join(noms.values())} -> annule")
        for nom in noms.values():
            d[nom] = d.get(nom, 0) + 1
    _ecrire(DESACCORDS_PATH, d)


def _dire_message(mode, texte, dits, freq=None):
    """Lu comme en direct, sauf ce que le direct a deja lu ; rend le nouveau compte."""
    import messages_dits
    import messages_types
    import veille_donnees as vd
    from phrases import dire
    if dits >= MESSAGES_DITS_MAX or messages_dits.deja_dit(texte):
        return dits
    fiche = messages_types.fiche(mode, texte, freq, decoupe=mode == "ffsk", urgence="tresse" in texte)
    nom = re.split("[,:]", fiche["type_message"])[0] if mode in ("multimon", "ffsk") else mode
    import journaux
    vd.announce(dire("radio.message_donnees", nom=nom, msg=texte), SON_MESSAGE, journaux.etiquette_decodage(texte),
                "juges", fiche=fiche)
    return dits + 1


def _integrite_au_rejet(tid, freq, band, textes):
    """Un message dont le decodeur dit lui-meme qu'il ne tient pas n'est pas lu : son flux
    va au journal des rejets, comme tout ce qu'on ecarte. Rien n'est appris : l'humain dira."""
    import decodeurs
    faux = [t for t in textes if decodeurs.integrite_fausse(t)]
    son = os.path.join(tickets_pi.RACINE, tickets.DOSSIER, tid, "son.wav")
    if not faux or not os.path.exists(son):
        return
    try:
        import rejets
        rejets.garder(freq, "multimon", band, "integrite fausse", [{"juge": "multimon", "wav": son}], target=band)
    except Exception as exc:
        print(f"[juges] rejet d'integrite non garde : {exc!r}")


def _donnees_du_ticket(tid, ticket, c, avis):
    """Ce que le ticket apprend de la frequence, voix ou non (trafic non vocal), et les
    messages : lus et archives, le texte peut aussi dire une frequence ou un relais."""
    import frequences_actives
    import relais
    import veille_donnees as vd
    infos = ticket.get("infos") or {}
    freq, band = infos.get("freq_hz"), infos.get("band")
    for ep, v in c["verdicts"].items():
        total, propres = v.get("total") or 0, v.get("propres") or 0
        if tickets.juge_de(ep) and propres >= juges_radio.SEUIL_MIN and propres >= juges_radio.RATIO_MIN * max(total, 1):
            frequences_actives.noter(freq, "chiffre" if v.get("chiffre") else "numerique",
                                     protocole=v.get("protocole"), label=band)
        for texte in v.get("douteux") or []:   # filtre comme bruit : garde quand meme, pas lu
            vd.archive(DONNEES_DIR, {"freq_hz": freq, "bande": band, "epreuve": ep, "texte": texte, "ticket": tid,
                                     "filtre": True})
        _integrite_au_rejet(tid, freq, band, v.get("douteux") or [])
    dits = 0
    for ep in c["etablies"] + c["seules"]:
        v = c["verdicts"].get(ep) or {}
        for texte in v.get("messages") or []:
            vd.archive(DONNEES_DIR, {"freq_hz": freq, "bande": band, "epreuve": ep, "texte": texte, "ticket": tid,
                                     "confirme": ep in c["etablies"], "doute": v.get("doute")})
            print(f"[juges] ticket {tid} : {ep} {'confirme' if ep in c['etablies'] else 'seul'} : {texte}")
            frequences_actives.noter(freq, "donnees", protocole=ep, label=band, texte=texte)
            relais.depuis_texte(texte, freq, "ticket " + ep, label=band)
            dits = _dire_message(ep.split("@")[0], texte, dits, freq)
        image = tickets.rendu_de(tickets_pi.RACINE, tid, avis, ep) if ep.startswith("sstv") else None
        if image:
            shutil.copy(image, os.path.join(vd.ARCHIVE_DIR, f"sstv_{tid}{os.path.splitext(image)[1]}"))


def _appliquer_blu(tid, ticket, c, avis):
    """Ticket BLU que radio_watch n'a pas pu attendre, ou son texte FT8/FT4 : le texte
    est annonce comme en direct, une voix FreeDV non presentee va au journal (une
    seule prise, les autres rendus derriere J)."""
    infos = ticket.get("infos") or {}
    freq, band = infos.get("freq_hz"), infos.get("band")
    import ceremonie_radio as ceremonie
    import noise_signatures as ns
    import propositions
    import veille_donnees as vd
    voix_prise = tickets.a_marque(tickets_pi.RACINE, tid, "voix_prise")   # radio_watch l'a deja presentee
    dits = 0
    voix = []
    for ep in tickets.freedv_retenues(c["etablies"] + c["seules"]):
        v = c["verdicts"].get(ep) or {}
        nom, _, modulation = ep.partition("@")
        for ligne in v.get("messages") or []:
            mode, _, texte = ligne.partition(": ")
            vd.archive(mode.lower(), {"freq_hz": freq, "mode": mode, "texte": texte, "modulation": modulation})
            dits = _dire_message(mode, texte, dits, freq)
        chemin = tickets.rendu_de(tickets_pi.RACINE, tid, avis, ep) if nom.startswith("freedv-") else None
        if chemin and not voix_prise:
            voix.append((nom, v, chemin))
    if not voix:
        return
    nom, v, chemin = voix[0]
    session = radio_log.RadioLogSession(f"decodage differe {v.get('protocole')}", exiger_contenu=True)
    ceremonie.habiller(session, band, ceremonie.modulation_dite(nom), chemin, freq)
    prise = session.finalize()
    if prise:
        ns.record_capture("juges", freq or 0, nom, band, band, prise, None,
                          {"numerique": True, "juge": nom, "protocole": v.get("protocole")})
        propositions.poser(prise, freq, band, nom, None, [{"juge": n, "wav": w} for n, _, w in voix[1:]])
    print(f"[juges] ticket {tid} : {v.get('protocole')} -> journal {prise}")


def _appliquer_brut(tid, ticket, c, avis):
    infos = ticket.get("infos") or {}
    wav = os.path.join(BRUT_DIR, infos.get("brut") or "")
    dossier = tempfile.mkdtemp(prefix="ticket_")
    try:
        machines = ", ".join(sorted({a.get("nom") or m for m, a in avis.items()})) or "personne"
        verdicts = {}
        for ep, v in c["verdicts"].items():
            juge = tickets.juge_de(ep)
            if not juge:
                continue
            voix = tickets.rendu_de(tickets_pi.RACINE, tid, avis, ep)
            if voix:
                shutil.copyfile(voix, os.path.join(dossier, "voix_%s.wav" % juge.lstrip("-")))
            elif v.get("propose"):
                v = dict(v, propose=False)      # voix perdue ou empreinte fausse
            verdicts[juge] = v
        with _verrou:
            fiche = _lire(_fiche_de(wav), None)
            if fiche is not None:
                fiche.pop("ticket", None)
                if not any(tickets.juge_de(e) is None for e in c["manque"]):
                    fiche["donnees_faites"] = True
                _ecrire(_fiche_de(wav), fiche)
        if verdicts:
            _appliquer(wav, verdicts, dossier, machines)
        elif (_lire(_fiche_de(wav), None) or {}).get("etiquette") in ("renvoi", "decodeur"):
            _clore_renvoi(wav, _lire(_fiche_de(wav), {}), {}, dossier)
    finally:
        shutil.rmtree(dossier, ignore_errors=True)


def _tickets_un_tour(en_pause, temperature):
    """Conclut ce qui est fini, puis le Pi prend sa part ; rend le nombre de
    tickets encore ouverts."""
    tickets.nettoyer(tickets_pi.RACINE, CLOS_GARDE_SEC)
    ouverts = tickets.ouverts(tickets_pi.RACINE)
    if not ouverts:
        return 0
    cles, mach, outils = tickets_pi.cles(), tickets_pi.machines(), tickets_pi.outils()
    repassable = tickets_pi.contre_possible(mach)
    restent = 0
    for tid in ouverts:
        ticket, avis = tickets.lire(tickets_pi.RACINE, tid, cles)
        if not ticket:
            continue
        infos = ticket.get("infos") or {}
        age = time.time() - ticket.get("cree", 0)
        blu = infos.get("origine") == "blu"

        def possible(ep, auteur):
            return age <= CONTRE_MAX_SEC and repassable(ep, auteur)

        c = tickets.conclure(ticket, avis, possible, forcer=age > (BLU_MAX_SEC if blu else TICKET_MAX_SEC))
        if c["fini"] and (not blu or age > BLU_ATTENTE_SEC):
            try:
                _noter_desaccords(tid, c["desaccords"], avis)
                if blu:
                    _appliquer_blu(tid, ticket, c, avis)
                else:
                    _donnees_du_ticket(tid, ticket, c, avis)
                    _appliquer_brut(tid, ticket, c, avis)
            except Exception as exc:
                print(f"[juges] ticket {tid} : {exc!r}")
            tickets.clore(tickets_pi.RACINE, tid, tickets_pi.cle(), tickets_pi.moi(),
                          {"etablies": c["etablies"], "seules": c["seules"], "desaccords": list(c["desaccords"])})
            continue
        restent += 1
        if en_pause:
            continue

        def pour_moi(ep):
            # Ce qu'une compagne reliee sait faire reste a elle ; le Pi ne reprend
            # qu'apres PI_RETARD_SEC, ou si elle n'est plus la.
            return age > PI_RETARD_SEC or not any(tickets_pi.sait(m, ep) for m in mach)

        n = 2 if temperature < DEUX_A_LA_FOIS_C else 1
        fait = tickets.travailler(tickets_pi.RACINE, tid, tickets_pi.moi(), tickets_pi.moi(), tickets_pi.cle(), cles,
                                  outils, n, pour_moi=pour_moi)
        if fait:
            _, avis = tickets.lire(tickets_pi.RACINE, tid, cles)
            if any(v.get("propose") for v in (avis.get(tickets_pi.moi(), {}).get("verdicts") or {}).values()):
                tickets_pi.signaler(tid)     # une compagne a une contre-epreuve a faire
    return restent


def main():
    """Une panne du NAS (2026-11-04 : 877 redemarrages en 3 h) n'arrete plus le service."""
    while True:
        try:
            _boucle()
        except OSError as exc:
            print(f"[juges] {exc!r}, nouvel essai dans 60 s")
            time.sleep(60)


def _boucle():
    os.makedirs(BRUT_DIR, exist_ok=True)
    print("[juges] demarrage")
    try:
        _renvois_deja_finis()
    except Exception as exc:
        print(f"[juges] renvois en attente : {exc!r}")
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
        ouverts = _tickets_un_tour(en_pause, t)
        en_tickets = bool(tickets_pi.machines())
        file = _a_juger(_scores())
        # Chaque son va d'abord a la machine compagne la plus libre (meme pendant
        # une pause de chaleur : c'est justement la qu'elle soulage le Pi).
        places = DELEGUES_MAX - sum(1 for w in file if _delegue_frais(_lire(_fiche_de(w), None))
                                    or _ticket_frais(_lire(_fiche_de(w), None)))
        locaux = []
        for w in file:
            f = _lire(_fiche_de(w), None)
            frais = _delegue_frais(f) or _ticket_frais(f)
            if (en_tickets or _ticket_frais(f)) and _ouvrir_ticket(w, places):
                pass
            elif not _deleguer(w, places):
                locaux.append(w)
            elif not frais:
                places -= 1
        if en_pause:
            time.sleep(20)
            continue
        if not locaux:
            if ouverts:
                time.sleep(2)  # tickets en cours : le Pi a sa part (contre-epreuves, conclusions)
            elif file:
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
