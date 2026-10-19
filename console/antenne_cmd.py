"""Commande "antenne" (2026-10-19) : mesure de ce que recoit la cle radio
(lib/mesure_antenne.py). "antenne" : le verdict (antenne branchee ou non,
stations FM, bruit de fond par rapport a d'habitude, bandes au-dessus du
seuil en ce moment) ; "antenne detail" : chaque bande, bruit de fond, pic et
ecart au seuil de detection du scanner. Un detail demande juste apres une
mesure reprend cette mesure.
"""
import subprocess
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import matcher  # noqa: E402
import mesure_antenne  # noqa: E402
from phrases import dire, nombre  # noqa: E402

ATTENTE_SEC = 420
REPRISE_SEC = 600   # "antenne detail" juste apres : meme mesure


def _fr(x, decimales=1):
    s = f"{x:.{decimales}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _scanner_actif():
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "radio-watch"], timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _mesurer(say):
    if _scanner_actif():
        import meteo_locale
        etat = (meteo_locale._read_json(meteo_locale.SCANNER_STATE_PATH) or {}).get("etat")
        if etat == "pause_thermique":  # au-dessus de la limite de la veille meteo : rien ne passera
            return None, dire("antenne.trop_chaud")
        say(dire("antenne.demande"))
        asked = mesure_antenne.request()
        res = mesure_antenne.wait(asked, ATTENTE_SEC, tick=lambda: say(dire("antenne.en_cours")))
        if res is None:
            return None, dire("antenne.scanner_muet")
        return res, None
    say(dire("antenne.directe"))
    import radio_watch
    import chaine_audio
    return mesure_antenne.measure(radio_watch.BANDS,
                                  lambda n: chaine_audio.settings(n)["marge_detection_db"]), None


def _ligne_bande(b):
    if "erreur" in b:
        return dire("antenne.bande_echec", bande=b["dit"])
    cle = "antenne.bande_dessus" if b["ecart"] > 0 else "antenne.bande_dessous"
    return dire(cle, bande=b["dit"], plancher=_fr(b["plancher"]), pic=_fr(b["pic_mhz"], 3),
                ecart=_fr(abs(b["ecart"])))


def _resume(res):
    v = res["verdict"]
    fm = next((b for b in res["bandes"] if b["nom"] == mesure_antenne.FM_BAND), {})
    stations = fm.get("stations") or []
    parts = []
    if v["etat"] == "bon":
        f, db = stations[0]
        parts.append(dire("antenne.bon", stations=nombre(len(stations), "station"), frequence=_fr(f), ecart=_fr(db, 0)))
    elif v["etat"] == "faible":
        f, db = stations[0]
        parts.append(dire("antenne.faible", stations=nombre(len(stations), "station"), frequence=_fr(f), ecart=_fr(db, 0)))
    elif v["etat"] == "rien":
        parts.append(dire("antenne.rien"))
    else:
        parts.append(dire("antenne.fm_inconnue"))
    d = v["derive_plancher"]
    if not v["mesures_avant"]:
        parts.append(dire("antenne.premiere"))
    elif d is not None and d <= -mesure_antenne.DERIVE_DB:
        parts.append(dire("antenne.plancher_bas", ecart=_fr(-d)))
    elif d is not None and d >= mesure_antenne.DERIVE_DB:
        parts.append(dire("antenne.plancher_haut", ecart=_fr(d)))
    elif d is not None:
        parts.append(dire("antenne.plancher_normal"))
    dessus = sorted((b for b in res["bandes"] if b.get("ecart", -1) > 0 and b["nom"] != mesure_antenne.FM_BAND),
                    key=lambda b: -b["ecart"])
    if dessus:
        parts.append(dire("antenne.au_dessus", nombre=nombre(len(dessus), "bande"), bandes=", ".join(
            f"{b['dit']} {dire('antenne.plus_db', ecart=_fr(b['ecart']))}" for b in dessus[:3])))
    else:
        parts.append(dire("antenne.rien_au_dessus"))
    echecs = [b["dit"] for b in res["bandes"] if "erreur" in b]
    if echecs:
        parts.append(dire("antenne.echecs", bandes=", ".join(echecs)))
    parts.append(dire("antenne.detail_possible"))
    return " ".join(parts)


def run(line, say, out):
    detail = bool({"detail", "details", "bandes", "bande"} & set(matcher.normalize(line).split()))
    res = mesure_antenne.derniere() if detail else None
    if not res or res.get("erreur") or time.time() - res.get("t", 0) > REPRISE_SEC:
        res, probleme = _mesurer(say)
        if probleme:
            return probleme
    if res.get("erreur"):
        return dire(f"antenne.{res['erreur']}")
    lignes = [_ligne_bande(b) for b in res["bandes"]]
    out("\n".join(lignes) + "\n")
    if detail:
        return " ".join(lignes)
    return _resume(res)
