"""Commande "ignorer" (2026-10-22, demande utilisateur) : mettre de cote, pour
une duree ou pour toujours, ce que l'on entend en ce moment (ou, a defaut,
la derniere reception -- meme regle que "verrouiller") : la frequence, ou en
DMR le groupe ou l'appelant. L'ecoute en cours s'arrete aussitot (zapper).

  1. (DMR) quoi ignorer : la frequence, le groupe, l'appelant
  2. combien de temps : de 5 minutes a toujours, une heure par defaut
  fleches haut / bas parcourent, Entree valide, Echap annule, F1 les touches.

"ignorer liste" : ce qui est ignore en ce moment ; "ignorer effacer" : tout ce
qui est ignore pour une duree revient ("toujours" reste).
"""
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import ignores  # noqa: E402
import keys  # noqa: E402
import maintien  # noqa: E402
import matcher  # noqa: E402
import noise_signatures as ns  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN, ESC, ENTER = "\x1b[A", "\x1b[B", "\x1b", ("\r", "\n")


def _mhz(freq_hz):
    return f"{freq_hz / 1e6:.4f}".rstrip("0").rstrip(".").replace(".", ",")


def _cible():
    """(capture, en_cours) : ce que l'on entend, sinon la derniere reception."""
    live = ns.live_capture()
    if live:
        return live, True
    return ns.last_capture(), False


def _options(capture):
    label = capture.get("label") or ""
    opts = [("frequence", int(capture["freq_hz"]),
             dire("ignorer.option_frequence", label=label, mhz=_mhz(capture["freq_hz"])))]
    dmr = capture.get("dmr") or (capture.get("metrics") or {}).get("dmr") or {}
    if dmr.get("groupe"):
        opts.append(("groupe", str(dmr["groupe"]), dire("ignorer.option_groupe", groupe=dmr["groupe"])))
    if dmr.get("appelant"):
        opts.append(("appelant", str(dmr["appelant"]), dire("ignorer.option_appelant", appelant=dmr["appelant"])))
    return opts


def _choisir(libelles, depart, titre, key_source, say, out):
    """Index choisi, ou None (Echap)."""
    i = depart
    consigne = dire("ignorer.consigne")
    intro = conseils.intro("ignorer", titre, consigne)
    out(intro + "\n")
    say(f"{intro} {libelles[i]}")
    for key in key_source:
        if key == keys.WAKE:
            continue
        voix.stop()
        if conseils.is_help(key):
            say(consigne)
        elif key == ESC:
            return None
        elif key in ENTER:
            return i
        elif key in (UP, DOWN):
            j = i + (-1 if key == UP else 1)
            if 0 <= j < len(libelles):
                i = j
                say(libelles[i])
            else:
                say((dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")) + " " + libelles[i])
    return None


def _liste():
    actifs = ignores.actifs()
    if not actifs:
        return dire("ignorer.rien")
    parts = []
    for e in actifs:
        if e.get("jusqu_a") is None:
            reste = dire("ignorer.toujours")
        else:
            minutes = max(1, round((e["jusqu_a"] - time.time()) / 60))
            reste = dire("ignorer.encore", duree=nombre(minutes, "minute"))
        parts.append(f"{e['libelle']}, {reste}")
    return dire("ignorer.liste", nombre=nombre(len(actifs), "cible")) + " " + " ; ".join(parts) + "."


def run(line, key_source, say, out):
    mots = set(matcher.normalize(line or "").split())
    if mots & {"liste", "lister", "quoi", "lesquels"}:
        return _liste()
    if mots & {"effacer", "efface", "oublier", "plus", "vider"}:
        n = ignores.retirer_temporaires()
        return dire("ignorer.efface", nombre=nombre(n, "cible"))
    capture, en_cours = _cible()
    if not capture:
        return dire("ignorer.aucune_reception")
    opts = _options(capture)
    k = 0
    if len(opts) > 1:
        k = _choisir([o[2] for o in opts], 0, dire("ignorer.titre_quoi"), key_source, say, out)
        if k is None:
            return dire("ignorer.annule")
    type_, valeur, libelle = opts[k]
    durees = [d for d in ignores.DUREES]
    j = _choisir([dire("ignorer.duree", duree=nom) if sec else dire("ignorer.duree_toujours")
                  for sec, nom in durees], ignores.DUREE_DEFAUT,
                 dire("ignorer.titre_duree", cible=libelle), key_source, say, out)
    if j is None:
        return dire("ignorer.annule")
    sec, nom = durees[j]
    ignores.ajouter(type_, valeur, sec, libelle)
    # ce qu'on entend s'arrete tout de suite ; un verrouillage dessus est leve
    verrou = ns.lock_request()
    if verrou and type_ == "frequence" and abs(int(verrou["freq_hz"]) - int(valeur)) <= ignores.TOLERANCE_HZ:
        ns.request_unlock()
    if en_cours:
        maintien.demander_zap()
    if sec is None:
        return dire("ignorer.fait_toujours", cible=libelle)
    return dire("ignorer.fait", cible=libelle, duree=nom)
