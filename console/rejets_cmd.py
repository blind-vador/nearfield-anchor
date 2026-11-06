"""Commande "journal des rejets" (2026-10-09) : les candidats que le scanner a
ecartes en silence ("bruit, pas de signal reel"), avec un court extrait audio
chacun (lib/rejets.py). On TRIE A L'OREILLE :

  fleches haut / bas   rejet precedent / suivant, joue aussitot (pas de
                       fioriture : juste le son, pour trier vite)
  j                    autre demodulation du meme candidat
  n                    c'etait un VRAI signal, ecarte a tort : il est repeche au
                       journal, avec sa ceremonie, et V, C et les fleches y apprennent
  r                    c'etait bien du bruit : le decodeur ecoute retient son
                       empreinte, sur cette frequence seulement
  i                    infos : frequence, bande, raison, verdict
  Entree               rejoue      Echap ferme. F1 : les touches.

Les verdicts (faux rejet vs bruit confirme) servent au recalibrage des seuils
et a la double verification par le Mac mini (bridge).
"""
import sys


import conseils  # noqa: E402
import propositions  # noqa: E402
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


_RAISONS = {"aucune emission mesuree": "rejets.raison_sans_emission",
            "banc : aucune voix prouvee": "rejets.raison_banc_sans_voix",
            "aucune voix prouvee": "rejets.raison_sans_voix",
            "ressemble a un parasite appris": "rejets.raison_parasite",
            "eclat tres court": "rejets.raison_eclat",
            "audio FM sans voix prouvee": "rejets.raison_fm_sans_voix",
            "integrite fausse": "rejets.raison_integrite",
            "numerique prouve, pas de voix": "rejets.raison_numerique_sans_voix",
            "rien d'exploitable": "rejets.raison_rien"}


def _raison(texte):
    """Les raisons sont notees telles quelles par les ecoutes ; on les dit en francais."""
    return dire(_RAISONS[texte]) if texte in _RAISONS else texte


def run(line, key_source, say, out):
    items = rejets.entries()
    i = 0
    rendu = 0        # 0 : l'extrait principal ; sinon les autres demodulations
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
                    rendu = 0
                    player.play(rejets.path_of(items[i]["fichier"]))
                    text = _position(items, i)
                else:
                    edge = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
                    text = edge
            elif key in ("\r", "\n"):
                player.play(rejets.path_of(items[i]["fichier"]))
            elif key in ("j", "J"):
                autres = items[i].get("rendus") or []
                if autres:
                    rendu = (rendu + 1) % (len(autres) + 1)
                    e = autres[rendu - 1] if rendu else items[i]
                    player.play(rejets.path_of(e["fichier"]))
                    text = dire("rejets.rendu", juge=propositions.nom(e.get("juge")))
                else:
                    text = dire("rejets.pas_d_autre_rendu")
            elif key in ("n", "N"):
                player.stop()
                if rejets.repecher(items[i]["fichier"]):
                    items.pop(i)
                    rendu = 0
                    text = dire("rejets.repeche")
                    if not items:
                        say(text)
                        return dire("rejets.fermee")
                    i = min(i, len(items) - 1)
                else:
                    rejets.set_verdict(items[i]["fichier"], "signal")
                    items[i]["verdict"] = "signal"
                    text = dire("rejets.note_signal")
            elif key in ("r", "R"):
                rejets.set_verdict(items[i]["fichier"], "bruit")
                items[i]["verdict"] = "bruit"
                juge = rejets.confirmer_bruit(items[i]["fichier"], rendu)
                text = dire("rejets.note_bruit_appris", juge=propositions.nom(juge)) if juge else dire("rejets.note_bruit")
            elif key in ("i", "I"):
                e = items[i]
                text = dire("rejets.info", frequence=_freq(e.get("freq_hz")), bande=e.get("label", ""),
                            raison=_raison(e.get("raison", "")), etat=dire(_VERDICT.get(e.get("verdict"))))
            else:
                text = dire("rejets.touches")
            if text:
                out(f"[{text}]\n")
                say(text)
        return dire("rejets.fermee")
    finally:
        player.close()
