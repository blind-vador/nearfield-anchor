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

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import decodeurs
import radio_log

BRUT_DIR = "/home/vador/autopilot/brut_dsd"
A_REVOIR_DIR = os.path.join(BRUT_DIR, "a_revoir")
SCORES_PATH = "/home/vador/autopilot/juges_scores.json"
TAILLE_MAX = 1500 * 1024 * 1024     # au-dela, les plus vieux sons partent
A_REVOIR_GARDES = 120               # flux bruts non decodes gardes pour re-examen
STABLE_SEC = 5
TIMEOUT_SEC = 600
TRUST_SUR = 0.8                     # au-dessus, un juge decode tout seul au journal (confirme en live avant)

# juge -> protocole qu'il reconnait (None : auto). L'ordre du dict est l'ordre
# par defaut quand une bande n'a rien de confirme : auto, DMR, puis les rares.
JUGES = {"-fa": None, "-fs": "DMR", "-fi": "NXDN", "-fn": "NXDN", "-fm": "dPMR", "-fz": "M17",
         "-f1": "P25", "-fd": "D-Star", "-fy": "YSF", "-fx": "X2-TDMA"}
DEFAUT_ORDRE = list(JUGES)
ENC_RE = re.compile(r"\b(ENC|Encrypt\w*|ALG ID|Key ID|KID)\b", re.I)

SEUIL_MIN = 2                       # en dessous, pas meme une question (bruit pur)
RATIO_MIN = 0.25
VOIX_MIN_SEC = 1.0

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

def _duree(chemin):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", chemin],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def juger(juge, wav):
    """Fait passer un son devant un juge. Rend le verdict et le chemin de la voix
    decodee (gardee seulement si le juge propose quelque chose)."""
    voix = tempfile.mktemp(prefix="juge_", suffix=".wav")
    try:
        r = subprocess.run(["nice", "-n", "19", "dsd-fme", juge, "-i", wav, "-s", "48000", "-o", "null", "-w", voix],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=TIMEOUT_SEC)
        texte = r.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        texte = ""
    stats = decodeurs.new_stats()
    for ligne in texte.splitlines():  # meme comptage que le direct (dPMR, M17 : preuve exigee)
        decodeurs.compter_synchro(stats, ligne)
    total, propres, vus = stats["total"], stats["clean"], stats.get("vus", {})
    protocole = max(vus, key=vus.get) if vus else None
    voix_sec = _duree(voix) if os.path.exists(voix) and os.path.getsize(voix) > 44 else 0.0
    verdict = {"total": total, "propres": propres, "protocole": protocole, "voix_s": round(voix_sec, 1),
               "chiffre": bool(ENC_RE.search(texte)), "t": time.time()}
    # Une "proposition" (a confirmer) : assez de synchros propres et de la voix.
    verdict["propose"] = (not verdict["chiffre"] and propres >= SEUIL_MIN
                          and propres >= RATIO_MIN * max(total, 1) and voix_sec >= VOIX_MIN_SEC)
    if not verdict["propose"] and os.path.exists(voix):
        os.remove(voix)
        voix = None
    return verdict, voix


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
        if en_pause:
            time.sleep(20)
            continue
        _limiter_place()
        file = _a_juger(_scores())
        if not file:
            if not _reprendre_a_revoir(_scores()):
                time.sleep(10)
            continue
        cibles = file[:2] if t < DEUX_A_LA_FOIS_C else file[:1]
        fils = [threading.Thread(target=_passer, args=(w,)) for w in cibles]
        for f in fils:
            f.start()
        for f in fils:
            f.join()


if __name__ == "__main__":
    main()
