"""Commande "journal des rejets" (2026-10-09) : les candidats que le scanner a
ecartes en silence ("bruit, pas de signal reel"), avec un court extrait audio
chacun (lib/rejets.py). On TRIE A L'OREILLE :

  fleches haut / bas   rejet precedent / suivant, joue aussitot (pas de
                       fioriture : juste le son, pour trier vite)
  n                    c'etait un VRAI signal, ecarte a tort (noté)
  r                    c'etait bien du bruit / un parasite (rejet confirmé)
  i                    infos : frequence, bande, raison, verdict
  Entree               rejoue      Echap ferme. F1 : les touches.

Les verdicts (faux rejet vs bruit confirme) servent au recalibrage des seuils
et a la double verification par le Mac mini (bridge).
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import rejets  # noqa: E402
import voix  # noqa: E402
from journal import Player  # noqa: E402  (meme lecteur audio que le journal)
from phrases import dire, nombre  # noqa: E402

UP, DOWN, ESC = "\x1b[A", "\x1b[B", "\x1b"
_VERDICT = {None: "rejets.verdict_aucun", "signal": "rejets.verdict_signal", "bruit": "rejets.verdict_bruit"}


def _freq(hz):
    return f"{(hz or 0) / 1e6:.4f}".rstrip("0").rstrip(".").replace(".", ",")


def _position(items, i):
    return dire("rejets.position", index=i + 1, total=len(items),
                etat=dire(_VERDICT.get(items[i].get("verdict"))))


def run(line, key_source, say, out):
    items = rejets.entries()
    i = 0
    player = Player()
    title = dire("rejets.titre", nombre=nombre(len(items), "rejet", "rejets"))
    intro = conseils.intro("rejets", title, dire("rejets.consigne"))
    out(intro + "\n")
    try:
        if not items:
            say(f"{intro} {dire('rejets.vide')}")
            for key in key_source:
                if key == ESC or not conseils.is_help(key):
                    break
                say(dire("rejets.consigne"))
            return dire("rejets.fermee")
        say(intro)
        player.play(rejets.path_of(items[0]["fichier"]))

        for key in key_source:
            voix.stop()
            text = None
            if conseils.is_help(key):
                text = dire("rejets.consigne")
            elif key == ESC:
                return dire("rejets.fermee")
            elif key in (UP, DOWN):
                j = i + (-1 if key == UP else 1)
                if 0 <= j < len(items):
                    i = j
                    player.play(rejets.path_of(items[i]["fichier"]))
                    text = _position(items, i)
                else:
                    edge = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
                    text = edge
            elif key in ("\r", "\n"):
                player.play(rejets.path_of(items[i]["fichier"]))
            elif key in ("n", "N"):
                rejets.set_verdict(items[i]["fichier"], "signal")
                items[i]["verdict"] = "signal"
                text = dire("rejets.note_signal")
            elif key in ("r", "R"):
                rejets.set_verdict(items[i]["fichier"], "bruit")
                items[i]["verdict"] = "bruit"
                text = dire("rejets.note_bruit")
            elif key in ("i", "I"):
                e = items[i]
                text = dire("rejets.info", frequence=_freq(e.get("freq_hz")), bande=e.get("label", ""),
                            raison=e.get("raison", ""), etat=dire(_VERDICT.get(e.get("verdict"))))
            else:
                text = dire("rejets.touches")
            if text:
                out(f"[{text}]\n")
                say(text)
        return dire("rejets.fermee")
    finally:
        player.close()
