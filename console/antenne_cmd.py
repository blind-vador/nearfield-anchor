"""Commande "antenne" (2026-10-19) : mesure de ce que recoit la cle radio
(lib/mesure_antenne.py). "antenne" : le verdict (antenne branchee ou non,
stations FM, bruit de fond par rapport a d'habitude, bandes au-dessus du
seuil en ce moment) ; "antenne detail" : chaque bande, bruit de fond, pic et
ecart au seuil de detection du scanner. Un detail demande juste apres une
mesure reprend cette mesure.

La mesure se fait en arriere-plan (2026-10-20) : elle bloquait le clavier
une a deux minutes, les touches tapees entre-temps etaient rejouees a
l'arrivee du verdict et leur echo le coupait aussitot -- verdict affiche,
jamais entendu. Le clavier reste libre, annoncer() dit le verdict.
"""
import subprocess
import sys
import threading
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


def _mesurer():
    """(resultat, None) ou (None, phrase du probleme)."""
    if _scanner_actif():
        asked = mesure_antenne.request()
        res = mesure_antenne.wait(asked, ATTENTE_SEC)
        if res is None:
            return None, dire("antenne.scanner_muet")
        return res, None
    import radio_watch
    import chaine_audio
    return mesure_antenne.measure(radio_watch.BANDS,
                                  lambda n: chaine_audio.settings(n)["marge_detection_db"]), None


def _trop_chaud():
    """Scanner en pause thermique au-dessus de la limite de la veille meteo : rien ne passera."""
    if not _scanner_actif():
        return False
    import meteo_locale
    return (meteo_locale._read_json(meteo_locale.SCANNER_STATE_PATH) or {}).get("etat") == "pause_thermique"


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
    dernier = mesure_antenne.dernier_capteur()
    silence_h = (time.time() - dernier) / 3600 if dernier else None
    if v["etat"] in ("bon", "faible") and _scanner_actif() and (silence_h is None or silence_h >= mesure_antenne.CAPTEURS_SILENCE_H):
        # la FM passe, mais rien n'est decode au-dessus : antenne mal vissee
        parts.append(dire("antenne.suspecte", stations=nombre(len(stations), "station"),
                          depuis=(dire("antenne.depuis_heures", heures=nombre(round(silence_h), "heure"))
                                  if silence_h is not None and silence_h < 72 else dire("antenne.depuis_longtemps"))))
    elif v["etat"] == "bon":
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
    if parts[0].startswith(dire("antenne.suspecte_debut")):
        d = None  # reference prise peut-etre antenne deja mal branchee : on ne la cite pas
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


def _texte(res, detail, out):
    if res.get("erreur"):
        return dire(f"antenne.{res['erreur']}")
    lignes = [_ligne_bande(b) for b in res["bandes"]]
    out("\n".join(lignes) + "\n")
    if detail:
        return " ".join(lignes)
    return _resume(res)


def run(line, out, annoncer):
    """Rend la phrase a dire tout de suite ; une mesure fraiche est faite en
    arriere-plan et son verdict passe a annoncer(texte)."""
    detail = bool({"detail", "details", "bandes", "bande"} & set(matcher.normalize(line).split()))
    res = mesure_antenne.derniere() if detail else None
    if res and not res.get("erreur") and time.time() - res.get("t", 0) <= REPRISE_SEC:
        return _texte(res, detail, out)
    if _trop_chaud():
        return dire("antenne.trop_chaud")

    def travail():
        try:
            res, probleme = _mesurer()
            texte = probleme or _texte(res, detail, out)
        except Exception as exc:  # la console ne doit pas rester sans reponse
            print(f"[antenne] {exc!r}", file=sys.stderr)
            texte = dire("antenne.rtl_power")
        annoncer(texte)
    threading.Thread(target=travail, daemon=True).start()
    return dire("antenne.demande" if _scanner_actif() else "antenne.directe")
