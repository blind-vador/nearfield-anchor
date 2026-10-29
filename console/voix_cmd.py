"""Commande "voix" : choisir la voix de la console et son debit.

  fleches haut / bas   voix precedente / suivante, entendue dans cette voix
  Entree               choisit, puis meme chose pour le debit
  Echap                annule, rien ne change

Les voix neuronales locales Piper (sans internet, 2026-10-18) et espeak
restent proposees. Reglages ecrits
dans console.json (voir lib/reglages.py) ; les noms de touches sont
ensuite prepares en tache de fond dans la nouvelle voix (voix.prewarm).
"""
import sys


import conseils  # noqa: E402
import keys  # noqa: E402
import reglages  # noqa: E402
import voix  # noqa: E402
import voix_sources  # noqa: E402
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
    if voix.is_piper(voice):
        moteur = voix.piper_out.canonical(voice)[6:].partition("#")[0]
        if moteur in ("mac", "google", "mbrola"):  # voix d'ailleurs (2026-10-22)
            return dire(f"voix.{moteur}_label", nom=voix.piper_out.label(voice))
        return dire("voix.piper_label", nom=voix.piper_out.label(voice))
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




def _choose(labels, start, speak, key_source, out, title, feature, consigne=None, prefix=""):
    index = start
    consigne = consigne or dire("voix.consigne")
    intro = (prefix + " " if prefix else "") + conseils.intro(feature, title, consigne)
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
            text = consigne
        else:
            text = dire("voix.fleches_haut_bas_entree")
        out(f"[{text}]\n")
        speak(index, text)
    return None


PITCHES = Etiquettes("hauteur", {"-20Hz": "plus grave", "-10Hz": "un peu plus grave", "+0Hz": "normale",
                                 "+10Hz": "un peu plus aiguë", "+20Hz": "plus aiguë"}, "hauteur de la voix")


def _label(voice):
    if voice == voix_sources.ALEATOIRE:
        return dire("voix.aleatoire_microsoft_label")
    if voice == voix_sources.ALEATOIRE_PIPER:
        return dire("voix.aleatoire_piper_label")
    if voice == voix_sources.ALEATOIRE_TOUT:
        return dire("voix.aleatoire_tout_label")
    if voice == voix_sources.ALEATOIRE_DISTANT:
        return dire("voix.aleatoire_distant_label")
    text = voice_label(voice)
    if not voix.is_piper(voice) and voice != voix.ESPEAK and not voix.cloud_ok():
        text += ", " + dire("voix.microsoft_absent")
    return text


def _avec_actuel(values, labels, current):
    """Le reglage actuel est toujours propose, meme hors de la liste habituelle
    (un debit "+45%") : Entree sur la position de depart ne change rien."""
    values, labels = list(values), list(labels)
    if current and current not in values:
        values.insert(0, current)
        labels.insert(0, dire("voix.actuel", valeur=current))
    return values, labels


def _pick(values, labels, current, preview, key_source, out, title, feature):
    """Une etape : rend la valeur choisie, ou None (Echap)."""
    start = values.index(current) if current in values else 0
    i = _choose(labels, start, preview, key_source, out, title, feature)
    return None if i is None else values[i]


def _regler(source, key_source, say, out):
    """Etapes 1 a 4 pour une source ; rien n'est enregistre si on sort par Echap."""
    nom = voix_sources.SOURCES[source]
    r = voix_sources.reglage(source)
    new = dict(r)
    inchange = dire("voix.source_inchangee", source=nom)
    # 1. voix principale
    # Les quatre "aleatoire" (2026-10-22), puis les voix une a une.
    voices = [voix_sources.ALEATOIRE_TOUT, voix_sources.ALEATOIRE_DISTANT, voix_sources.ALEATOIRE_PIPER,
              voix_sources.ALEATOIRE] + voix.piper_out.voice_ids(lang=None) \
        + EDGE_TTS_VOICES + [voix.ESPEAK]
    current = voix.piper_out.canonical(r["voix"])
    # Presentation au debit neutre : elle est preparee d'avance pour chaque voix.
    choice = _pick(voices, [_label(v) for v in voices], current,
                   lambda i, text: voix.preview(text, voices[i], "+0%"), key_source, out,
                   dire("voix.etape_principale", source=nom), "voix_principale")
    if choice is None:
        return inchange
    new["voix"] = choice
    # 2. debit et hauteur (la hauteur : voix Microsoft seulement)
    if choice != voix.ESPEAK:
        rates, labels = _avec_actuel(RATES, RATES.values(), r["debit"])
        rate = _pick(rates, labels, r["debit"],
                     lambda i, text: voix.preview(dire("voix.apercu_debit", debit=text), choice, rates[i]),
                     key_source, out, dire("voix.etape_debit"), "voix_debit")
        if rate is None:
            return inchange
        new["debit"] = rate
        if not voix_sources.locale(choice):
            pitches, labels = _avec_actuel(PITCHES, PITCHES.values(), r.get("hauteur", "+0Hz"))
            pitch = _pick(pitches, labels, r.get("hauteur", "+0Hz"),
                          lambda i, text: voix.preview(dire("voix.apercu_hauteur", hauteur=text), choice, rate,
                                                       pitches[i]),
                          key_source, out, dire("voix.etape_hauteur"), "voix_hauteur")
            if pitch is None:
                return inchange
            new["hauteur"] = pitch
    # 3 et 4. voix de secours (Piper) et son debit, si la principale passe par Microsoft
    if not voix_sources.locale(choice):
        secours = [voix_sources.ALEATOIRE] + voix.piper_out.voice_ids(lang=None)
        sec = _pick(secours, [dire("voix.aleatoire_label") if v == voix_sources.ALEATOIRE else _label(v)
                              for v in secours], voix.piper_out.canonical(r["secours"]),
                    lambda i, text: voix.preview(text, secours[i], r["secours_debit"]), key_source, out,
                    dire("voix.etape_secours"), "voix_secours")
        if sec is None:
            return inchange
        new["secours"] = sec
        rates, labels = _avec_actuel(RATES, RATES.values(), r["secours_debit"])
        srate = _pick(rates, labels, r["secours_debit"],
                      lambda i, text: voix.preview(dire("voix.apercu_debit", debit=text), sec, rates[i]),
                      key_source, out, dire("voix.etape_secours_debit"), "voix_debit")
        if srate is None:
            return inchange
        new["secours_debit"] = srate
    if not voix_sources.enregistrer(source, new):
        return dire("voix.disque_nas_inaccessible_voix")
    if source in ("console", "touches"):  # console.json garde la voix de la console (autres lecteurs)
        console = dict(reglages.get("console"))
        if source == "console":
            console.update(voix=new["voix"], debit=new["debit"], voix_secours=new["secours"])
        else:
            console.update(debit_echo=new["debit"])
        reglages.save("console", console)
    voix.reload()
    try:  # les phrases de la nouvelle voix se preparent (machine compagne, sinon le Pi)
        import voix_preparation
        voix_preparation.kick()
    except Exception:
        pass
    if source == "touches":
        voix.prewarm(echo_texts())
    return dire("voix.source_enregistree", source=nom, resume=voix_sources.resume(source))


def run(key_source, say, out):
    """Commande "voix" : chaque source (console, touches du clavier, chaque
    veille) a sa voix principale (ou aleatoire), son debit et sa hauteur, sa
    voix de secours Piper (ou aleatoire) et son debit."""
    sources = list(voix_sources.SOURCES)
    pos, message = 0, ""
    while True:
        labels = [f"{voix_sources.SOURCES[s]} : {voix_sources.resume(s)}" for s in sources]
        index = _choose(labels, pos, lambda i, text: say(text), key_source, out, dire("voix.titre_sources"),
                        "voix_sources", consigne=dire("voix.consigne_sources"), prefix=message)
        if index is None:
            return dire("voix.reglages_fermes")
        pos = index
        message = _regler(sources[index], key_source, say, out)
