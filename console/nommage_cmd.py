"""Journal des noms proposes par une machine compagne.

  fleches haut / bas   proposition precedente / suivante
  c                    confirme : le nom est pose sur la station
  r                    refuse : ce nom ne sera plus propose pour cette frequence
  e                    ce que l'IA a compris de l'echange (2026-11-11)
  Echap ferme. F1 : les touches.
"""
import conseils
import keys
import nommage
import voix
from phrases import dire
from sound_player import play_sound_effect

UP, DOWN, ESC = "\x1b[A", "\x1b[B", "\x1b"


def _shim():
    try:
        import sons
        chemin = sons.chemin("nommage")
    except Exception:
        chemin = None
    if chemin:
        try:
            play_sound_effect(chemin, label="nommage")
        except Exception as exc:
            print(f"[nommage] shim : {exc!r}")


def _phrase(item):
    mhz = f"{float(item.get('freq_hz') or 0) / 1e6:.4f}".rstrip("0").replace(".", ",")
    if item.get("ancien_nom"):
        return dire("nommage.annonce_maj", ancien=item["ancien_nom"], nom=item.get("nom") or "",
                    pourquoi=item.get("pourquoi") or "", bande=item.get("bande") or "", mhz=mhz)
    if item.get("pourquoi"):
        return dire("nommage.annonce_pourquoi", machine=item.get("machine") or "compagne", nom=item.get("nom") or "",
                    pourquoi=item["pourquoi"], bande=item.get("bande") or "", mhz=mhz,
                    modulation=item.get("modulation") or "")
    return dire(
        "nommage.annonce",
        machine=item.get("machine") or "compagne",
        nom=item.get("nom") or "",
        bande=item.get("bande") or "",
        mhz=f"{float(item.get('freq_hz') or 0) / 1e6:.4f}".rstrip("0").replace(".", ","),
        modulation=item.get("modulation") or "",
    )


def _dire(item):
    _shim()
    return _phrase(item)


def run(line, key_source, say, out):
    items = nommage.en_attente()
    consigne = dire("nommage.consigne")
    titre = dire("nommage.titre", nombre=str(len(items)))
    intro = conseils.intro("nommage", titre, consigne)
    try:
        voix.prechauffer([
            consigne, titre, dire("nommage.vide"), dire("nommage.ferme"),
            dire("nommage.confirme", nom="nom"), dire("nommage.refuse", nom="nom"),
            dire("nommage.deja"), dire("nommage.plus_rien"),
        ], echo=True)
    except Exception:
        pass
    if not items:
        say(f"{intro} {dire('nommage.vide')}")
        return dire("nommage.ferme")
    i = 0
    say(f"{intro} {_dire(items[0])}")
    for key in key_source:
        if key == keys.WAKE:
            continue
        voix.stop()
        text = None
        if conseils.is_help(key):
            text = consigne
        elif key == ESC:
            break
        elif key in (UP, DOWN):
            pas = -1 if key == UP else 1
            n = i + pas
            if 0 <= n < len(items):
                i = n
                text = _dire(items[i])
            else:
                text = dire("commun.debut_liste") if n < 0 else dire("commun.fin_liste")
        elif key in ("c", "C"):
            item = items.pop(i)
            if nommage.confirmer(item):
                text = dire("nommage.confirme", nom=item.get("nom") or "")
            else:
                text = dire("nommage.deja")
            if not items:
                say(text + " " + dire("nommage.plus_rien"))
                return dire("nommage.ferme")
            i = min(i, len(items) - 1)
            text = text + " " + _dire(items[i])
        elif key in ("e", "E"):
            item = items[i]
            text = (" ".join(x for x in (item.get("mise_a_jour"), item.get("explication")) if x)
                    or dire("nommage.sans_explication"))
        elif key in ("r", "R"):
            item = items.pop(i)
            nommage.refuser(item)
            text = dire("nommage.refuse", nom=item.get("nom") or "")
            if not items:
                say(text + " " + dire("nommage.plus_rien"))
                return dire("nommage.ferme")
            i = min(i, len(items) - 1)
            text = text + " " + _dire(items[i])
        if text:
            out(text + "\n")
            say(text)
    return dire("nommage.ferme")
