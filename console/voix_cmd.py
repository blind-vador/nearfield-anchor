"""Commande "voix" : choisir la voix de la console et son debit.

  fleches haut / bas   voix precedente / suivante, entendue dans cette voix
  Entree               choisit, puis meme chose pour le debit
  Echap                annule, rien ne change

La voix locale espeak reste proposee (sans internet). Reglages ecrits
dans console.json (voir lib/reglages.py) ; les noms de touches sont
ensuite prepares en tache de fond dans la nouvelle voix (voix.prewarm).
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import keys  # noqa: E402
import reglages  # noqa: E402
import voix  # noqa: E402
from speak import EDGE_TTS_VOICES  # noqa: E402  (les voix deja retenues pour les annonces)
from phrases import Etiquettes, dire

UP, DOWN, ENTER, ESCAPE = "\x1b[A", "\x1b[B", ("\r", "\n"), "\x1b"
COUNTRIES = Etiquettes("pays", {"FR": "France", "BE": "Belgique", "CA": "Canada", "CH": "Suisse"}, "pays d'une voix")
RATES = Etiquettes("debit", {"+0%": "normal", "+15%": "un peu rapide", "+30%": "rapide",
                             "+50%": "très rapide", "+80%": "extrêmement rapide"}, "débit de la voix")
ECHO_BOOST = 30   # points de debit en plus pour les touches et la navigation
ECHO_MAX = 100


def voice_label(voice):
    if voice == voix.ESPEAK:
        return dire("voix.espeak_label")
    _, country, name = voice.split("-", 2)
    multi = "Multilingual" in name
    name = name.replace("MultilingualNeural", "").replace("Neural", "")
    return f"{name}, {COUNTRIES.get(country, country)}" + (dire("voix.multilingue") if multi else "")


def _pct(rate):
    return int(rate.strip("%").replace("+", ""))


def echo_texts():
    """Tout ce que l'echo de frappe peut dire : prepare en tache de fond."""
    chars = list("abcdefghijklmnopqrstuvwxyz0123456789") + list(keys.CHAR_NAMES)
    names = [keys.char_name(c) for c in chars] + [keys.char_name(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"]
    texts = names + [f"efface {keys.char_name(c)}" for c in chars]
    texts += list(keys.ESCAPE_NAMES.values()) + list(keys.CONTROL_NAMES.values())
    texts += ["ligne vide", "ligne effacée", "Recherche effacée.", "Pas de recherche en cours."]
    return texts




def _choose(labels, start, speak, key_source, out, title, feature):
    index = start
    intro = conseils.intro(feature, title, dire("voix.consigne"))
    out(intro + "\n")
    speak(index, f"{intro} {labels[index]}")
    for key in key_source:
        if key == ESCAPE:
            return None
        if key in ENTER:
            return index
        if key in (UP, DOWN):
            wanted = index + (-1 if key == UP else 1)
            index = min(max(wanted, 0), len(labels) - 1)
            edge = "" if wanted == index else (dire("voix.debut_liste") + " " if key == UP else dire("commun.fin_liste") + " ")
            text = edge + labels[index]
        elif conseils.is_help(key):
            text = dire("voix.consigne")
        else:
            text = dire("voix.fleches_haut_bas_entree")
        out(f"[{text}]\n")
        speak(index, text)
    return None


def run(key_source, say, out):
    current = reglages.get("console")
    voices = EDGE_TTS_VOICES + [voix.ESPEAK]
    labels = [voice_label(v) for v in voices]
    start = voices.index(current["voix"]) if current["voix"] in voices else 0
    rate = current.get("debit", "+20%")
    index = _choose(labels, start, lambda i, text: voix.preview(text, voices[i], rate), key_source, out,
                    dire("voix.choix_voix_console_chaque"), "voix")
    if index is None:
        return dire("voix.annule_voix_inchangee")
    voice = voices[index]
    if voice == voix.ESPEAK:
        new_rate = rate
    else:
        rate_labels = list(RATES.values())
        rates = list(RATES)
        start = rates.index(rate) if rate in rates else 0
        chosen = _choose(rate_labels, start,
                         lambda i, text: voix.preview(dire("voix.apercu_debit", debit=text), voice, rates[i]),
                         key_source, out, dire("voix.debit_lecture"), "voix_debit")
        if chosen is None:
            return dire("voix.annule_voix_inchangee_2")
        new_rate = rates[chosen]
    echo_rate = f"+{min(ECHO_MAX, _pct(new_rate) + ECHO_BOOST)}%"
    settings = dict(reglages.get("console"), voix=voice, debit=new_rate, debit_echo=echo_rate)
    if not reglages.save("console", settings):
        return dire("voix.disque_nas_inaccessible_voix")
    voix.reload()
    if voice == voix.ESPEAK:
        return dire("voix.voix_locale_espeak_enregistree")
    todo = voix.prewarm(echo_texts())
    text = dire("voix.voix_enregistree", voice_label=voice_label(voice))
    if todo:
        text += (" " + dire("voix.prepare_sons_touches_arriere", todo=todo, max=max(1, todo // 120), v1='s' if todo >= 240 else ''))
    return text
