"""Passage systematique des sons bruts devant tous les decodeurs (2026-10-24).

radio_watch garde le son brut que dsd-fme recoit du dongle (option -6, 48 kHz,
~/autopilot/brut_dsd) avec une fiche .json : ce que le direct en a tire et
l'etiquette du candidat (reference = decode en direct, rejet = ecarte comme
bruit, analogique = voix FM, inconnu). Ici, en tache de fond, sans le dongle,
chaque son passe devant chaque "juge" (un mode de dsd-fme), l'un apres l'autre
ou deux a la fois quand le Pi est frais. Chaque verdict est ecrit aussitot dans
la fiche : apres une pause de chaleur ou un redemarrage, on reprend avec les
juges qui ne l'ont pas encore vu.

Score de confiance (juges_scores.json) : sur les sons de reference, un juge qui
reconnait le bon protocole (ou se tait quand ce n'est pas le sien) gagne en
confiance ; les plus fiables passent en premier. Les rejets et la voix FM sont
des parasites : le nombre de synchros propres qu'un juge y trouve fixe son
seuil, celui a partir duquel son decodage ne vaut plus rien.

Trouvaille : un juge decode de la voix la ou le direct n'avait rien (ou un autre
protocole) -> rangee au journal radio, et une phrase courte le dit. Jamais pour
un appel prive ou chiffre (regle utilisateur, comme en direct)."""

import glob
import json
import os
import re
import subprocess
import tempfile
import threading
import time

import radio_log
from phrases import dire
from speak import speak

BRUT_DIR = "/home/vador/autopilot/brut_dsd"
SCORES_PATH = "/home/vador/autopilot/juges_scores.json"
TAILLE_MAX = 1500 * 1024 * 1024     # au-dela, les plus vieux sons partent, juges ou non
STABLE_SEC = 5                      # fiche presente et son immobile : il est complet
TIMEOUT_SEC = 600

# juge -> protocole qu'il sait reconnaitre (None : auto-detection, plusieurs)
JUGES = {"-fa": None, "-fs": "DMR", "-fi": "NXDN", "-fn": "NXDN", "-fm": "dPMR", "-fz": "M17",
         "-f1": "P25", "-fd": "D-Star", "-fy": "YSF", "-fx": "X2-TDMA"}
PROTOCOLES = {"DMR": "DMR", "P25p1": "P25", "P25p2": "P25", "YSF": "YSF", "DSTAR": "D-Star",
              "M17": "M17", "NXDN48": "NXDN", "NXDN96": "NXDN", "NXDN": "NXDN", "dPMR": "dPMR",
              "X2-TDMA": "X2-TDMA"}
SYNC_RE = re.compile(r"Sync: [+-]([\w-]+)")
ENC_RE = re.compile(r"\b(ENC|Encrypt\w*|ALG ID|Key ID|KID)\b", re.I)
PRIVE_RE = re.compile(r"Private\w* Call|Private", re.I)

SEUIL_MIN = 2                       # comme decodeurs.DSD_MIN_CLEAN_SYNC
RATIO_MIN = 0.25                    # comme decodeurs.DSD_MIN_CLEAN_RATIO
VOIX_MIN_SEC = 1.0
PARASITES_GARDES = 300

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


# --- scores ---------------------------------------------------------------------------

def _scores():
    s = _lire(SCORES_PATH, {})
    for juge in JUGES:
        s.setdefault(juge, {"passages": 0, "justes": 0, "faux": 0, "trouvailles": 0, "parasites": []})
    return s


def seuil(score):
    """Synchros propres au-dessus desquelles le juge est cru : au-dessus de ce
    qu'il trouve dans 9 parasites sur 10 (le pire, tant qu'il y en a peu)."""
    bruit = sorted(score["parasites"])
    if not bruit:
        return SEUIL_MIN
    haut = bruit[-1] if len(bruit) < 10 else bruit[int(0.9 * (len(bruit) - 1))]
    return max(SEUIL_MIN, haut + 1)


def confiance(score):
    """Part de verdicts justes sur les sons de reference (2 justes, 2 faux au depart)."""
    return (score["justes"] + 1) / (score["justes"] + score["faux"] + 2)


def ordre(restants, scores):
    return sorted(restants, key=lambda j: confiance(scores[j]), reverse=True)


def _noter(juge, fiche, verdict):
    with _verrou:
        scores = _scores()
        s = scores[juge]
        s["passages"] += 1
        etiquette = fiche.get("etiquette")
        attendu = (fiche.get("direct") or {}).get("protocole")
        sien = JUGES[juge]
        # Parasite : rejet, voix FM, ou son de reference d'un autre protocole. Ce qui
        # compte, c'est la fausse voix : des synchros sans voix (donnees d'un relais
        # DMR dans un rejet) ne disent rien de mal du juge.
        if etiquette in ("rejet", "analogique") or (etiquette == "reference" and sien not in (None, attendu)):
            echantillon = verdict["propres"] if verdict["voix_s"] > 0 else 0
            s["parasites"] = (s["parasites"] + [echantillon])[-PARASITES_GARDES:]
        if etiquette == "reference":
            if sien is None or sien == attendu:
                bon = verdict["trouve"] and verdict["protocole"] == attendu
            else:
                bon = not verdict["trouve"]
            s["justes" if bon else "faux"] += 1
        _ecrire(SCORES_PATH, scores)
        return scores


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
               "seuil": seuil_juge, "chiffre": bool(ENC_RE.search(texte)), "prive": bool(PRIVE_RE.search(texte)),
               "t": time.time()}
    verdict["trouve"] = (propres >= seuil_juge and propres >= RATIO_MIN * max(total, 1)
                         and voix_sec >= VOIX_MIN_SEC)
    if not verdict["trouve"] and os.path.exists(voix):
        os.remove(voix)
        voix = None
    return verdict, voix


def _trouvaille(fiche, juge, verdict, voix):
    """Voix que le direct n'avait pas : au journal radio, et une phrase courte."""
    try:
        if verdict["chiffre"] or verdict["prive"]:
            print(f"[juges] {juge} : appel prive ou chiffre, rien n'est garde")
            return
        direct = fiche.get("direct") or {}
        if direct.get("valide"):
            return  # deja entendu et range en direct ; un autre protocole y serait un echo
        meilleur = max(((fiche.get("juges") or {}).values()), key=lambda v: v.get("propres", 0), default=None)
        if meilleur and meilleur.get("protocole") != verdict["protocole"] and meilleur["propres"] > verdict["propres"]:
            print(f"[juges] {juge} : echo d'un signal {meilleur.get('protocole')} plus net, ecarte")
            return
        mhz = fiche["freq_hz"] / 1e6
        session = radio_log.RadioLogSession(f"decodage differe {verdict['protocole']}")
        session.add_audio_file(voix)
        chemin = session.finalize()
        print(f"[juges] trouvaille {verdict['protocole']} par {juge} sur {mhz:.4f} MHz : {chemin}")
        with _verrou:
            scores = _scores()
            scores[juge]["trouvailles"] += 1
            _ecrire(SCORES_PATH, scores)
            f = _lire(_fiche_de(fiche["wav"]), None)
            if f is not None:
                f.setdefault("trouvailles", []).append({"juge": juge, "protocole": verdict["protocole"],
                                                        "journal": chemin})
                _ecrire(_fiche_de(fiche["wav"]), f)
        speak(dire("juges.trouvaille", protocole=verdict["protocole"],
                   freq=f"{mhz:.3f}".rstrip("0").replace(".", ",")), source="radio")
    except Exception as exc:
        print(f"[juges] trouvaille non rangee : {exc!r}")
    finally:
        if voix and os.path.exists(voix):
            os.remove(voix)


# --- la file ---------------------------------------------------------------------------

def _fiche_de(wav):
    return wav[:-4] + ".json"


def _a_juger():
    """Sons complets (fiche presente) dont un juge au moins n'a pas encore parle, plus vieux d'abord."""
    file = []
    for fiche_path in glob.glob(os.path.join(BRUT_DIR, "*.json")):
        wav = fiche_path[:-5] + ".wav"
        if not os.path.exists(wav) or time.time() - os.path.getmtime(wav) < STABLE_SEC:
            continue
        fiche = _lire(fiche_path, None)
        if fiche is None:
            continue
        restants = [j for j in JUGES if j not in fiche.get("juges", {})]
        if restants:
            file.append((os.path.getmtime(wav), wav, restants))
        else:
            _ranger(wav)
    return [(w, r) for _, w, r in sorted(file)]


def _ranger(wav):
    """Tous les juges ont parle : le son brut part (ses trouvailles sont au journal)."""
    for chemin in (wav, _fiche_de(wav)):
        try:
            os.remove(chemin)
        except OSError:
            pass


def _limiter_place():
    sons = sorted(glob.glob(os.path.join(BRUT_DIR, "*.wav")), key=os.path.getmtime)
    taille = sum(os.path.getsize(s) for s in sons)
    while sons and taille > TAILLE_MAX:
        vieux = sons.pop(0)
        taille -= os.path.getsize(vieux)
        print(f"[juges] place : {os.path.basename(vieux)} retire avant la fin des juges")
        _ranger(vieux)


def _passer(wav, juge):
    fiche_path = _fiche_de(wav)
    with _verrou:
        s = _scores()[juge]
    verdict, voix = juger(juge, wav, seuil(s))
    with _verrou:
        fiche = _lire(fiche_path, None)
        if fiche is None:
            return
        fiche.setdefault("juges", {})[juge] = verdict
        _ecrire(fiche_path, fiche)   # memorise tout de suite : reprise apres coupure
    _noter(juge, fiche, verdict)
    print(f"[juges] {os.path.basename(wav)} {juge} : {verdict['propres']}/{verdict['total']} propres, "
          f"seuil {verdict['seuil']}, voix {verdict['voix_s']} s{' -> TROUVE ' + str(verdict['protocole']) if verdict['trouve'] else ''}")
    if verdict["trouve"]:
        _trouvaille(dict(fiche, wav=wav), juge, verdict, voix)


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
        file = _a_juger()
        if not file:
            time.sleep(10)
            continue
        wav, restants = file[0]
        juges = ordre(restants, _scores())[:2 if t < DEUX_A_LA_FOIS_C else 1]
        fils = [threading.Thread(target=_passer, args=(wav, j)) for j in juges]
        for f in fils:
            f.start()
        for f in fils:
            f.join()


if __name__ == "__main__":
    main()
