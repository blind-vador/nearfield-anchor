"""Passage des sons bruts radio devant les decodeurs, conditionne a la bande (2026-10-25).

radio_watch garde le son brut que dsd-fme recoit du dongle (option -6, 48 kHz,
~/autopilot/brut_dsd) avec une fiche .json : la bande, ce que le direct en a
tire, et si c'etait un appel prive (en clair) ou chiffre. Ici, en tache de fond
et sans le dongle, on rejoue ces sons devant les "juges" (un mode de dsd-fme
chacun).

Principe (demande de l'utilisateur, 2026-10-25) :
- Les scores sont tenus PAR BANDE. Une bande souvent numerique passe d'abord au
  numerique : les juges sont essayes dans l'ordre de ce qui a deja marche sur
  cette bande (sinon l'ordre par defaut, auto et DMR en tete).
- On s'arrete au PREMIER juge qui decode : seul l'echec d'un juge en appelle un
  autre. On ne rejuge jamais un son deja decode (par le direct ou par un juge).
- Un son qu'aucun juge n'a decode n'est pas jete : il part dans a_revoir/, d'ou
  il peut etre repris plus tard, quand la bande a appris un mode qu'on n'avait
  pas encore essaye dessus.
- Un son decode par le direct sert seulement a apprendre que la bande est
  numerique (profil), il n'est pas rejuge.

Trouvaille : un juge decode une voix que le direct avait manquee -> rangee au
journal radio, et une phrase courte le dit. Jamais pour un appel chiffre ; un
appel prive EN CLAIR (simple adressage point a point, audio non chiffre) est
garde comme le reste.

Seuils fixes par l'utilisateur (2026-10-25) : les juges se trompent (du dPMR
pris pour du DMR, un son hache pris pour de la voix). L'utilisateur ecoute
l'entree "decodage differe" au journal radio puis note, par la commande
"juges" : "juges faux" (ce juge ne sera plus cru en dessous de ce niveau sur
cette bande) ou "juges bon". Un seuil note a la main (seuil_u) prime sur celui
que le juge deduirait seul des parasites -- comme le reste du systeme radio, ce
sont les retours de l'utilisateur qui affinent les reglages."""

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import radio_log
from phrases import dire
from speak import speak

BRUT_DIR = "/home/vador/autopilot/brut_dsd"
A_REVOIR_DIR = os.path.join(BRUT_DIR, "a_revoir")
SCORES_PATH = "/home/vador/autopilot/juges_scores.json"
RETOURS_PATH = "/home/vador/autopilot/juges_retours.json"   # decodages differes annonces, pour les retours de l'utilisateur
RETOURS_GARDES = 40
TAILLE_MAX = 1500 * 1024 * 1024     # au-dela, les plus vieux sons partent, juges ou non
A_REVOIR_GARDES = 80                # sons non decodes gardes pour re-examen (les plus vieux partent)
STABLE_SEC = 5                      # fiche presente et son immobile : il est complet
TIMEOUT_SEC = 600

# juge -> protocole qu'il sait reconnaitre (None : auto-detection, plusieurs).
# L'ordre de ce dict est l'ordre par defaut quand une bande n'a rien appris :
# auto d'abord, puis DMR, les plus courants avant les rares.
JUGES = {"-fa": None, "-fs": "DMR", "-fi": "NXDN", "-fn": "NXDN", "-fm": "dPMR", "-fz": "M17",
         "-f1": "P25", "-fd": "D-Star", "-fy": "YSF", "-fx": "X2-TDMA"}
DEFAUT_ORDRE = list(JUGES)
PROTOCOLES = {"DMR": "DMR", "P25p1": "P25", "P25p2": "P25", "YSF": "YSF", "DSTAR": "D-Star",
              "M17": "M17", "NXDN48": "NXDN", "NXDN96": "NXDN", "NXDN": "NXDN", "dPMR": "dPMR",
              "X2-TDMA": "X2-TDMA"}
SYNC_RE = re.compile(r"Sync: [+-]([\w-]+)")
ENC_RE = re.compile(r"\b(ENC|Encrypt\w*|ALG ID|Key ID|KID)\b", re.I)

SEUIL_MIN = 2                       # comme decodeurs.DSD_MIN_CLEAN_SYNC
RATIO_MIN = 0.25                    # comme decodeurs.DSD_MIN_CLEAN_RATIO
VOIX_MIN_SEC = 1.0
PARASITES_GARDES = 200

# Chaleur : le scanner en direct passe avant. Pause plus tot que la sienne (~75 C) ;
# le Pi tourne deja vers 70 C quand le scanner travaille.
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


# --- scores, par bande ----------------------------------------------------------------

def _scores():
    s = _lire(SCORES_PATH, {})
    s.setdefault("bandes", {})
    return s


def _bande(scores, band):
    b = scores["bandes"].setdefault(band or "?", {"_protocoles": {}})
    b.setdefault("_protocoles", {})
    for juge in JUGES:
        b.setdefault(juge, {"succes": 0, "essais": 0, "parasites": []})
    return b


def seuil(js):
    """Synchros propres au-dessus desquelles ce juge est cru sur cette bande.
    Un seuil note a la main par l'utilisateur (seuil_u) prime ; sinon on le
    deduit des parasites (fausses voix) : le pire tant qu'il y en a peu, le 9e
    decile au-dela."""
    if js.get("seuil_u") is not None:
        return js["seuil_u"]
    bruit = sorted(js["parasites"])
    if not bruit:
        return SEUIL_MIN
    haut = bruit[-1] if len(bruit) < 10 else bruit[int(0.9 * (len(bruit) - 1))]
    return max(SEUIL_MIN, haut + 1)


def ordre(scores, band, restants):
    """Juges a essayer, le plus prometteur pour cette bande d'abord : d'abord ce
    qui y a deja decode (succes), puis l'ordre par defaut (auto et DMR en tete)."""
    b = _bande(scores, band)
    def cle(j):
        js = b[j]
        taux = js["succes"] / (js["essais"] + 1)
        return (-js["succes"], -taux, DEFAUT_ORDRE.index(j))
    return sorted(restants, key=cle)


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


def juger(juge, wav, seuil_juge):
    """Fait passer un son devant un juge. Rend le verdict et le chemin de la voix."""
    voix = tempfile.mktemp(prefix="juge_", suffix=".wav")
    try:
        r = subprocess.run(["nice", "-n", "19", "dsd-fme", juge, "-i", wav, "-s", "48000", "-o", "null", "-w", voix],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=TIMEOUT_SEC)
        texte = r.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        texte = ""
    total = propres = 0
    vus = {}
    for ligne in texte.splitlines():
        m = SYNC_RE.search(ligne)
        if not m:
            continue
        total += 1
        if "ERR" not in ligne:
            propres += 1
            p = PROTOCOLES.get(m.group(1), m.group(1))
            vus[p] = vus.get(p, 0) + 1
    protocole = max(vus, key=vus.get) if vus else None
    voix_sec = _duree(voix) if os.path.exists(voix) and os.path.getsize(voix) > 44 else 0.0
    verdict = {"total": total, "propres": propres, "protocole": protocole, "voix_s": round(voix_sec, 1),
               "seuil": seuil_juge, "chiffre": bool(ENC_RE.search(texte)), "t": time.time()}
    verdict["trouve"] = (propres >= seuil_juge and propres >= RATIO_MIN * max(total, 1)
                         and voix_sec >= VOIX_MIN_SEC)
    if not verdict["trouve"] and os.path.exists(voix):
        os.remove(voix)
        voix = None
    return verdict, voix


def _trouvaille(fiche, wav, juge, verdict, voix):
    """Voix que le direct n'avait pas : au journal radio, et une phrase courte."""
    try:
        mhz = fiche["freq_hz"] / 1e6
        mhz_txt = f"{mhz:.3f}".rstrip("0").rstrip(".").replace(".", ",")
        session = radio_log.RadioLogSession(f"decodage differe {verdict['protocole']}")
        session.add_audio_file(voix)
        chemin = session.finalize()
        print(f"[juges] trouvaille {verdict['protocole']} par {juge} sur {mhz:.4f} MHz : {chemin}")
        with _verrou:
            f = _lire(_fiche_de(wav), None)
            if f is not None:
                f.setdefault("trouvailles", []).append({"juge": juge, "protocole": verdict["protocole"],
                                                        "journal": chemin})
                _ecrire(_fiche_de(wav), f)
        _ajouter_retour({"t": time.time(), "mhz": mhz_txt, "band": fiche.get("band"), "juge": juge,
                         "protocole": verdict["protocole"], "propres": verdict["propres"], "journal": chemin})
        speak(dire("juges.trouvaille", protocole=verdict["protocole"],
                   freq=f"{mhz:.3f}".rstrip("0").replace(".", ",")), source="radio")
    except Exception as exc:
        print(f"[juges] trouvaille non rangee : {exc!r}")
    finally:
        if voix and os.path.exists(voix):
            os.remove(voix)


# --- retours de l'utilisateur ---------------------------------------------------------

def _retours():
    return _lire(RETOURS_PATH, [])


def _ajouter_retour(rec):
    with _verrou:
        r = _retours()
        rec["id"] = (r[-1]["id"] + 1) if r else 1
        r.append(rec)
        _ecrire(RETOURS_PATH, r[-RETOURS_GARDES:])


def derniers_retours_texte(n=5):
    """Lu par la commande "juges" : les derniers decodages differes a noter."""
    r = _retours()[-n:]
    if not r:
        return "Aucun décodage différé récent à noter."
    bouts = []
    for x in reversed(r):
        deja = f", déjà noté {x['retour']}" if x.get("retour") else ""
        bouts.append(f"numéro {x['id']}, {x['protocole']} sur {x['mhz']} mégahertz{deja}")
    return ("Décodages différés récents : " + " ; ".join(bouts)
            + ". Dis « juges faux » ou « juges bon », avec le numéro, le dernier sinon.")


def retour_utilisateur(num, bon):
    """Retour de l'utilisateur sur un decodage differe : fixe a la main le seuil
    du juge sur cette bande (prime sur la deduction automatique)."""
    with _verrou:
        r = _retours()
        if not r:
            return "Aucun décodage différé à noter."
        rec = next((x for x in r if x["id"] == num), None) if num else r[-1]
        if rec is None:
            return f"Je ne trouve pas le décodage différé numéro {num}."
        scores = _scores()
        js = _bande(scores, rec.get("band"))[rec["juge"]]
        if bon:
            js["seuil_u"] = max(SEUIL_MIN, min(rec["propres"], js.get("seuil_u") or rec["propres"]))
            msg = (f"Noté bon : {rec['protocole']} sur {rec['mhz']} mégahertz. "
                   f"À partir de {js['seuil_u']} synchronisations propres, ce décodage compte comme vraie voix.")
        else:
            js["seuil_u"] = max(rec["propres"] + 1, js.get("seuil_u") or 0)
            msg = (f"Noté faux : {rec['protocole']} sur {rec['mhz']} mégahertz. "
                   f"Ce juge ne sera plus cru en dessous de {js['seuil_u']} synchronisations propres sur cette bande.")
        rec["retour"] = "bon" if bon else "faux"
        _ecrire(SCORES_PATH, scores)
        _ecrire(RETOURS_PATH, r)
        return msg


# --- la file ---------------------------------------------------------------------------

def _ranger(wav):
    """Le son a fini son parcours : il part (ses trouvailles sont au journal)."""
    for chemin in (wav, _fiche_de(wav)):
        try:
            os.remove(chemin)
        except OSError:
            pass


def _mettre_a_revoir(wav, scores):
    """Aucun juge n'a decode : on garde le son brut pour un re-examen futur,
    avec le mode qui est en tete de la bande au moment ou on le range (quand ce
    mode changera, on saura qu'il vaut la peine de le repasser)."""
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
    """Repasse en file un son d'a_revoir si la bande a appris, depuis qu'on l'a
    range, un autre mode en tete : il y a une vraie chance neuve de le decoder."""
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
            fiche["juges"] = {}          # on redonne sa chance a tous, dans le nouvel ordre
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
    """Sons a traiter, plus vieux d'abord. Au passage : un son deja decode (par
    le direct ou un juge) est retire sans etre rejuge, apres avoir nourri le
    profil de sa bande ; un son que tous les juges ont manque part a_revoir."""
    file = []
    for fiche_path in glob.glob(os.path.join(BRUT_DIR, "*.json")):
        wav = fiche_path[:-5] + ".wav"
        if not os.path.exists(wav) or time.time() - os.path.getmtime(wav) < STABLE_SEC:
            continue
        fiche = _lire(fiche_path, None)
        if fiche is None:
            continue
        direct = fiche.get("direct") or {}
        if fiche.get("decode"):
            _ranger(wav)
            continue
        if direct.get("valide"):         # decode en direct : sert au profil, pas rejuge
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
        print(f"[juges] place : {os.path.basename(vieux)} retire avant la fin des juges")
        _ranger(vieux)


def _passer(wav):
    """Un seul juge : le plus prometteur encore a essayer pour la bande de ce
    son. S'il decode, le son est range (pas rejuge) ; sinon on note et le juge
    suivant sera essaye au tour d'apres."""
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
        seuil_juge = seuil(_bande(scores, band)[juge])
    verdict, voix = juger(juge, wav, seuil_juge)
    chiffre = verdict["chiffre"]
    with _verrou:
        scores = _scores()
        b = _bande(scores, band)[juge]
        b["essais"] += 1
        fiche = _lire(fiche_path, None)
        if fiche is None:
            if voix and os.path.exists(voix):
                os.remove(voix)
            return
        fiche.setdefault("juges", {})[juge] = {k: verdict[k] for k in ("propres", "total", "protocole", "voix_s")}
        # Appel chiffre : on ne garde rien (regle utilisateur) ; un appel prive EN
        # CLAIR (adressage, audio non chiffre) passe comme le reste.
        decode = verdict["trouve"] and not chiffre
        if decode:
            b["succes"] += 1
            _noter_profil(scores, band, verdict["protocole"])
            fiche["decode"] = {"juge": juge, "protocole": verdict["protocole"]}
        elif verdict["trouve"]:
            # fausse voix retenue (ou son chiffre) : nourrit le seuil de ce juge sur la bande
            ech = verdict["propres"] if verdict["voix_s"] > 0 else 0
            b["parasites"] = (b["parasites"] + [ech])[-PARASITES_GARDES:]
        _ecrire(fiche_path, fiche)
        _ecrire(SCORES_PATH, scores)
    print(f"[juges] {os.path.basename(wav)} [{band}] {juge} : {verdict['propres']}/{verdict['total']} propres, "
          f"seuil {seuil_juge}, voix {verdict['voix_s']} s"
          + (" CHIFFRE" if chiffre else "")
          + (f" -> DECODE {verdict['protocole']}" if decode else ""))
    if decode:
        _trouvaille(dict(fiche, wav=wav), wav, juge, verdict, voix)
        _ranger(wav)                     # decode : on ne le rejuge pas
    elif voix and os.path.exists(voix):
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
        # Un juge a la fois par son (on s'arrete au premier succes) ; deux sons
        # differents en parallele quand le Pi est frais, pour tenir les coeurs.
        cibles = file[:2] if t < DEUX_A_LA_FOIS_C else file[:1]
        fils = [threading.Thread(target=_passer, args=(w,)) for w in cibles]
        for f in fils:
            f.start()
        for f in fils:
            f.join()


if __name__ == "__main__":
    main()
