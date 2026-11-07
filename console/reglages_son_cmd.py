"""Commande "reglages son" (2026-10-14, demande utilisateur) : choisir a la
main, bande par bande, le type de contenu (musique, voix, donnees, ou
automatique) et chaque couche de filtres. Ce qui est regle a la main passe
toujours avant l'apprentissage automatique (lib/reglages_audio.py, "manuel").

  Tab / Maj+Tab          bande suivante / precedente
  fleches haut / bas     parcourir : type de contenu, puis chaque couche
  Espace                 allume / coupe la couche (type de contenu : suivant)
  fleches gauche / droite  regle l'intensite (type de contenu : precedent / suivant)
  Retour arriere         rend la couche a l'automatique
  F                      reecoute la derniere capture de la bande AVEC les reglages actuels
  B                      la meme capture, brute, sans aucun filtre
  Ctrl + Z               annule le dernier changement
  Echap ferme. F1 : les touches.

Un changement vaut des la capture suivante (le scanner relit les reglages a
chaque ecoute), et pour la touche F du mode manuel.
"""
import json
import os
import subprocess
import sys
import threading


import conseils  # noqa: E402
import keys  # noqa: E402
import reglages_audio as ra  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN, LEFT, RIGHT = "\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C"
TAB, SHIFT_TAB = "\t", "\x1b[Z"
BACKSPACE = ("\x7f", "\x08")
CTRL_Z, ESC, SPACE = "\x1a", "\x1b", " "
PLAGES_PATH = "/tmp/radio_watch_plages.json"
LOG_DIR = "/media/nas/nd1/server/radio_log"
INDEX_PATH = os.path.join(LOG_DIR, ".empreintes.json")
PREVIEW_SEC = 20
# Fenetres du scanner qui ne sont pas des plages balayees (lib/stations.py, veille DMR).
EXTRA_BANDS = [("stations_wfm", "wfm", "stations FM probables"), ("stations_nfm", "nfm", "stations probables en FM étroite"),
               ("dmr", "nfm", "veille DMR")]

# (parametre, pas, minimum, maximum, valeur quand on rallume)
NUMERIC = {
    "passe_bas_hz": (250.0, 1500.0, 6000.0, 3000.0),
    "reduction_bruit_db": (2.0, 0.0, 24.0, 10.0),
    "anti_crachotements": (1.0, 0.0, 15.0, 8.0),
    "gain_db": (1.0, -12.0, 12.0, 0.0),
    # Temps et sensibilite (2026-10-14) : la valeur de depart est celle du
    # scanner quand rien n'est regle (None) ; Espace y revient.
    "attente_fin_s": (0.5, 1.0, 15.0, 3.0),
    "marge_detection_db": (1.0, -6.0, 8.0, 0.0),
    "attente_fin_numerique_s": (1.0, 4.0, 30.0, 10.0),
    "gain_numerique_db": (1.0, -12.0, 12.0, 0.0),
}
RESET_ON_SPACE = ("attente_fin_s", "marge_detection_db", "attente_fin_numerique_s", "gain_numerique_db")
TIMING_LAYERS = ["attente_fin_s", "marge_detection_db"]
DIGITAL_LAYERS = ["attente_fin_numerique_s", "gain_numerique_db"]
VOICE_LAYERS = ["filtre_voix", "passe_bas_hz", "reduction_bruit_db", "anti_crachotements", "anti_sifflement",
                "anti_bourdonnement", "gain_db"]
FM_LAYERS = ["pilote_fm", "reduction_bruit_db", "anti_crachotements", "anti_sifflement", "anti_bourdonnement", "gain_db"]


def bands():
    """[(cible, mode, libelle)] : plages du scanner, fenetres speciales, et
    toute bande deja reglee."""
    try:
        with open(PLAGES_PATH, encoding="utf-8") as f:
            plages = json.load(f)
    except (OSError, ValueError):
        plages = []
    out, seen = [], set()
    for p in plages:
        if p.get("target") and p["target"] not in seen:
            out.append((p["target"], p.get("mode", "nfm"), p.get("label") or p["target"]))
            seen.add(p["target"])
    for target, mode, label in EXTRA_BANDS:
        if target not in seen:
            out.append((target, mode, label))
            seen.add(target)
    data = ra.load()
    for target in list(data.get("manuel", {})) + list(data.get("bandes", {})):
        if target not in seen:
            out.append((target, "nfm", target.replace("_", " ")))
            seen.add(target)
    return out


def _value_text(param, value):
    if param in ("reduction_bruit_db", "anti_crachotements") and not value:
        return dire("reglages_son.coupe")
    if isinstance(value, bool):
        return dire("reglages_son.allume") if value else dire("reglages_son.coupe")
    if param == "anti_crachotements":
        return f"{value:g}, " + dire("reglages_son.plus_bas_plus_fort")
    return ra.spoken_value(param, value)


def _layers(mode, target=None):
    """DMR : seulement l'attente de fin et le volume du numerique (pas de
    filtres) ; FM etroite : la voix numerique peut y passer, ses deux reglages
    suivent ceux de l'analogique."""
    if target == "dmr":
        return list(DIGITAL_LAYERS)
    if mode == "wfm":
        return ["contenu"] + FM_LAYERS + TIMING_LAYERS
    return ["contenu"] + VOICE_LAYERS + TIMING_LAYERS + (DIGITAL_LAYERS if mode == "nfm" else [])


def _line(target, mode, param):
    m = ra.manual(target)
    if param == "contenu":
        return dire("reglages_son.ligne_contenu", contenu=ra.CONTENT_NAMES.get(m["contenu"], m["contenu"]))
    value = ra.get(target)[param]
    if param in m["couches"]:
        origin = dire("reglages_son.a_la_main")
    elif m["contenu"] != "auto" and param in ra.CONTENTS.get(m["contenu"], {}):
        origin = dire("reglages_son.par_contenu")
    else:
        origin = dire("reglages_son.automatique")
    return f"{ra.NAMES.get(param, param)} : {_value_text(param, value)}, {origin}."


def _band_text(b):
    target, mode, label = b
    m = ra.manual(target)
    n = len(m["couches"])
    extra = (", " + nombre(n, "couche réglée à la main", "couches réglées à la main")) if n else ""
    first = _layers(mode, target)[0]
    return f"{label}. " + _line(target, mode, first)[:-1] + extra + "."


def _last_capture(target):
    """(chemin du son brut, infos) de la derniere capture de cette bande, ou None."""
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            index = json.load(f)
    except (OSError, ValueError):
        return None
    best = None
    for name, info in index.items():
        if info.get("target") != target or not name.endswith(".mp3"):
            continue
        brut = os.path.join(LOG_DIR, name[:-4] + ".brut.mp3")
        if not os.path.exists(brut):  # voix numerique : souvent pas de brut, la capture elle-meme
            brut = os.path.join(LOG_DIR, name)
        if os.path.exists(brut) and (best is None or os.path.getmtime(brut) > os.path.getmtime(best[0])):
            best = (brut, info)
    return best


class Preview:
    """Reecoute d'une capture (F : filtree avec les reglages actuels, B : brute) ;
    n'importe quelle touche l'arrete."""

    def __init__(self):
        self.proc = None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None

    def play(self, target, filtered):
        found = _last_capture(target)
        if not found:
            return dire("reglages_son.pas_de_capture")
        path, info = found
        rate = 48000 if info.get("mode") == "wfm" else 22050
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-t", str(PREVIEW_SEC), "-f", "s16le", "-ac", "1",
                              "-ar", str(rate), "-"], capture_output=True, timeout=60).stdout
        if not raw:
            return dire("reglages_son.lecture_impossible")
        if filtered and target == "dmr":
            import audioop  # voix numerique : aucun filtre, seulement son volume
            gain = 10 ** ((ra.get(target).get("gain_numerique_db") or 0.0) / 20)
            raw = audioop.mul(raw, 2, gain) if gain != 1.0 else raw
        elif filtered:
            import chaine_audio
            chain = chaine_audio.nouvelle(target, info.get("freq_hz") or 0, rate, info.get("mode") or "nfm")
            step = 4096 * 2
            raw = b"".join(chain.process_s16le(raw[i:i + step])[1] for i in range(0, len(raw), step))
        self.stop()
        self.proc = subprocess.Popen(["aplay", "-q", "-D", "default", "-r", str(rate), "-f", "S16_LE", "-c", "1"],
                                     stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def feed(proc, data):
            try:
                proc.stdin.write(data)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        threading.Thread(target=feed, args=(self.proc, raw), daemon=True).start()
        return ""


def _set_journal_open(is_open):
    """Pendant le reglage, les captations du scanner s'enregistrent en silence
    (comme quand le journal est ouvert)."""
    try:
        import audio_lock
        audio_lock.set_journal_open(is_open)
    except Exception:
        pass


def run(line, key_source, say, out):
    items = bands()
    if not items:
        return dire("reglages_son.aucune_bande")
    b, i = 0, 0
    undo = []
    preview = Preview()
    consigne = dire("reglages_son.consigne")
    title = dire("reglages_son.titre")
    intro = conseils.intro("reglages_son", title, consigne)
    out(intro + "\n")
    say(f"{intro} {_band_text(items[0])}")
    _set_journal_open(True)
    try:
        for key in key_source:
            if key == keys.WAKE:
                continue
            voix.stop()
            preview.stop()
            target, mode, label = items[b]
            layers = _layers(mode, target)
            param = layers[i]
            text = None
            if conseils.is_help(key):
                text = consigne
            elif key == ESC:
                break
            elif key in (TAB, SHIFT_TAB):
                b = (b + (1 if key == TAB else -1)) % len(items)
                i = 0
                text = _band_text(items[b])
            elif key in (UP, DOWN):
                j = i + (-1 if key == UP else 1)
                if 0 <= j < len(layers):
                    i = j
                    text = _line(target, mode, layers[i])
                else:
                    edge = dire("commun.debut_liste") if j < 0 \
                        else dire("commun.fin_liste")
                    text = edge + " " + _line(target, mode, param)
            elif key in (SPACE, LEFT, RIGHT) or key in BACKSPACE:
                m = ra.manual(target)
                undo.append((target, m))
                contenu, couches = m["contenu"], dict(m["couches"])
                if param == "contenu":
                    k = ra.CONTENT_ORDER.index(contenu) if contenu in ra.CONTENT_ORDER else 0
                    if key in BACKSPACE:
                        contenu = "auto"
                    else:
                        k += -1 if key == LEFT else 1
                        contenu = ra.CONTENT_ORDER[k % len(ra.CONTENT_ORDER)]
                elif key in BACKSPACE:
                    couches.pop(param, None)
                else:
                    current = ra.get(target)[param]
                    if isinstance(current, bool):
                        couches[param] = (not current) if key == SPACE else (key == RIGHT)
                    elif key == SPACE and param in RESET_ON_SPACE:
                        couches.pop(param, None)  # retour a la valeur du scanner
                    else:
                        stepv, low, high, on_value = NUMERIC[param]
                        if current is None:
                            current = on_value
                        if key == SPACE:
                            off = low if param != "gain_db" else 0.0
                            couches[param] = on_value if current == off else off
                        else:
                            couches[param] = max(low, min(high, current + (stepv if key == RIGHT else -stepv)))
                ra.set_manual(target, contenu, couches)
                text = _line(target, mode, param)
            elif key in ("f", "F", "b", "B"):
                text = preview.play(target, filtered=key in ("f", "F"))
            elif key == CTRL_Z:
                if not undo:
                    text = dire("reglages_son.rien_a_annuler")
                else:
                    t, m = undo.pop()
                    ra.set_manual(t, m["contenu"], m["couches"])
                    text = dire("reglages_son.annule") + " " + _line(target, mode, param)
            else:
                text = dire("reglages_son.touches")
            if text:
                out(f"[{text}]\n")
                say(text)
    finally:
        preview.stop()
        _set_journal_open(False)
    return dire("reglages_son.ferme")
