"""Commande "voix" : choisir la voix de la console et son debit.

  fleches haut / bas   voix precedente / suivante, entendue dans cette voix
  Entree               choisit, puis meme chose pour le debit, puis (veilles
                       seulement) l'effet sur la voix des annonces : aucun,
                       aleatoire, echo, reverbe... (lib/effets_voix.py)
  Echap                annule, rien ne change

Les voix neuronales locales Piper (sans internet, 2026-10-18) et espeak
restent proposees. Reglages ecrits
dans console.json (voir lib/reglages.py) ; les noms de touches sont
ensuite prepares en tache de fond dans la nouvelle voix (voix.prewarm).
"""
import os
import sys


import conseils  # noqa: E402
import keys  # noqa: E402
import reglages  # noqa: E402
import voix  # noqa: E402
import voix_sources  # noqa: E402
from speak import EDGE_TTS_VOICES  # noqa: E402  (les voix deja retenues pour les annonces)
from phrases import Etiquettes, dire

UP, DOWN, RIGHT, LEFT, ENTER, ESCAPE = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D", ("\r", "\n"), "\x1b"
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
    texts = names + [dire("console.efface", touche=keys.char_name(c)) for c in chars]
    texts += list(keys.ESCAPE_NAMES.values()) + list(keys.CONTROL_NAMES.values())
    texts += [dire(k) for k in ("console.ligne_vide", "console.ligne_effacee", "arbre.recherche_effacee",
                                "arbre.recherche_cours")]
    return texts




def _choose(labels, start, speak, key_source, out, title, feature, consigne=None, prefix=""):
    index = start
    consigne = consigne or dire("voix.consigne")
    intro = (prefix + " " if prefix else "") + conseils.intro(feature, title, consigne)
    voix.prechauffer(
        [intro, consigne, dire("voix.debut_liste"), dire("commun.fin_liste"),
         dire("voix.fleches_haut_bas_entree")] + list(labels))
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
                                 "+10Hz": "un peu plus aiguë", "+20Hz": "plus aiguë"}, "hauteur de la voix",
                     par_libelle=True)  # par le libelle : "-10Hz" et "+10Hz" donnaient la meme cle


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


def _choisir_effet(choice, rate, current, key_source, out):
    """Etape effet (2026-11-13) : aucun, aleatoire, puis chaque effet. A chaque
    fleche, son nom, puis une phrase d'exemple dite avec cet effet. Rend l'id, ou
    None (Echap)."""
    import tempfile
    import threading
    import effets_voix
    import piper_out
    import tmp_ram
    values = effets_voix.CHOIX
    labels = [dire(f"voix.effet.{v}") for v in values]
    fd, exemple = tempfile.mkstemp(suffix=".wav", prefix="apercu_effet_", dir=tmp_ram.dossier())
    os.close(fd)
    pret, ok = threading.Event(), [False]
    tour, lecteur = [0], [None]

    def fabriquer():
        v = piper_out.canonical(choice) if voix.is_piper(choice) else None
        if not v or not piper_out.resolve(v):
            v = piper_out.DEFAULT_VOICE
        try:
            ok[0] = piper_out.to_file(dire("voix.effet_exemple"), exemple, voice_id=v,
                                      scale_len=piper_out.length_scale(rate))
        finally:
            pret.set()
    threading.Thread(target=fabriquer, daemon=True).start()

    def couper():
        tour[0] += 1
        proc, lecteur[0] = lecteur[0], None
        if proc is not None and proc.poll() is None:
            proc.terminate()
        voix.stop()

    def apercu(i, text):
        couper()
        mien = tour[0]

        def fil():
            voix.say_blocking(text, encore=lambda: tour[0] == mien)
            pret.wait(30)
            if tour[0] == mien and ok[0]:
                lecteur[0] = effets_voix.ecouter(exemple, effets_voix.tirer(values[i]))
        threading.Thread(target=fil, daemon=True).start()

    try:
        return _pick(values, labels, current, apercu, key_source, out, dire("voix.etape_effet"), "voix_effet")
    finally:
        couper()
        try:
            os.remove(exemple)
        except OSError:
            pass


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
    # 2 bis. effet sur la voix des annonces (pas la console ni les touches)
    if source in voix_sources.ANNONCES:
        effet = _choisir_effet(choice, new.get("debit") or r["debit"], r.get("effet") or "aucun", key_source, out)
        if effet is None:
            return inchange
        new["effet"] = effet
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


def _voix_du_mac(famille, key_source, say, out):
    """Niveau de chaque voix d'une famille du Mac. Haut et bas parcourent, gauche
    et droite changent le niveau, Entree fait entendre la voix, Echap enregistre."""
    import piper_out
    noms = [n for n in piper_out.voix_mac_connues() if voix_sources.famille_mac(n) == famille]
    if not noms:
        return dire("voix.mac_aucune")
    niveaux = voix_sources.NIVEAUX
    valeurs = voix_sources.niveaux_mac(noms)
    i = 0

    def nom(n):
        return piper_out.label(f"piper:mac#{noms[n]}").replace(", voix du Mac", "")

    def ligne(n):
        return dire("voix.preferences_ligne", fournisseur=nom(n), niveau=dire(f"voix.niveau.{valeurs[noms[n]]}"))

    consigne = dire("voix.mac_consigne")
    intro = conseils.intro("voix_du_mac", dire("voix.mac_titre", famille=dire(f"voix.fournisseur.{famille}")), consigne)
    out(intro + "\n")
    say(f"{intro} {ligne(0)}")
    for key in key_source:
        if key == ESCAPE:
            if not voix_sources.enregistrer_niveaux_mac(valeurs):
                return dire("voix.disque_nas_inaccessible_voix")
            return dire("voix.mac_enregistre")
        if key in ENTER:
            voix.preview(nom(i), f"piper:mac#{noms[i]}", voix_sources.NEUTRE_DEBIT)
            continue
        if key in (UP, DOWN):
            i = (i + (-1 if key == UP else 1)) % len(noms)
            text = ligne(i)
        elif key in (LEFT, RIGHT):
            k = niveaux.index(valeurs[noms[i]]) + (-1 if key == LEFT else 1)
            if k < 0 or k >= len(niveaux):
                text = dire("voix.niveau.deja") + " " + ligne(i)
            else:
                valeurs[noms[i]] = niveaux[k]
                text = ligne(i)
        else:
            text = consigne
        out(f"[{text}]\n")
        say(text)
    return dire("voix.mac_enregistre")


def _preferences(key_source, say, out):
    """Fournisseurs du tirage aléatoire. Haut et bas parcourent, gauche et
    droite changent le niveau. Échap enregistre."""
    import piper_out
    presentes = {voix_sources.famille_mac(n) for n in piper_out.voix_mac_connues()}
    noms = [n for n in voix_sources.FOURNISSEURS if n not in voix_sources.MAC_FAMILLES or n in presentes]
    niveaux = voix_sources.NIVEAUX
    valeurs = voix_sources.preferences()
    i = 0

    def ligne(n):
        texte = dire("voix.preferences_ligne",
                     fournisseur=dire(f"voix.fournisseur.{noms[n]}"),
                     niveau=dire(f"voix.niveau.{valeurs[noms[n]]}"))
        return f"{texte} {dire('voix.mac_entree')}" if noms[n] in voix_sources.MAC_FAMILLES else texte

    consigne = dire("voix.preferences_consigne")
    intro = conseils.intro("voix_preferences", dire("voix.preferences_aleatoires"), consigne)
    textes = [intro, consigne, dire("voix.niveau.deja")]
    textes += [ligne(n) for n in range(len(noms)) if noms[n] in voix_sources.MAC_FAMILLES]
    for n, nom in enumerate(noms):
        for niveau in niveaux:
            valeurs_ligne = dict(valeurs)
            valeurs_ligne[nom] = niveau
            textes.append(dire("voix.preferences_ligne",
                               fournisseur=dire(f"voix.fournisseur.{nom}"),
                               niveau=dire(f"voix.niveau.{niveau}")))
    voix.prechauffer(textes)
    out(intro + "\n")
    say(f"{intro} {ligne(0)}")
    for key in key_source:
        if key == ESCAPE:
            if not voix_sources.enregistrer_preferences(valeurs):
                return dire("voix.disque_nas_inaccessible_voix")
            return dire("voix.preferences_fermees")
        if key in ENTER and noms[i] in voix_sources.MAC_FAMILLES:
            text = f"{_voix_du_mac(noms[i], key_source, say, out)} {ligne(i)}"
        elif key in (UP, DOWN):
            i = (i + (-1 if key == UP else 1)) % len(noms)
            text = ligne(i)
        elif key in (LEFT, RIGHT):
            k = niveaux.index(valeurs[noms[i]]) + (-1 if key == LEFT else 1)
            if k < 0 or k >= len(niveaux):
                text = dire("voix.niveau.deja") + " " + ligne(i)
            else:
                valeurs[noms[i]] = niveaux[k]
                text = ligne(i)
        elif conseils.is_help(key):
            text = consigne
        else:
            text = consigne
        out(f"[{text}]\n")
        say(text)
    return dire("voix.preferences_fermees")


def run(key_source, say, out):
    """Commande "voix" : chaque source (console, touches du clavier, chaque
    veille) a sa voix principale (ou aleatoire), son debit et sa hauteur, sa
    voix de secours Piper (ou aleatoire) et son debit.
    Si une veille est sur « toutes les voix », la première entrée règle
    le poids de chaque fournisseur."""
    sources = list(voix_sources.SOURCES)
    pos, message = 0, ""
    while True:
        prefs = voix_sources.aleatoire_tout_actif()
        labels = ([dire("voix.preferences_aleatoires")] if prefs else []) + [
            f"{voix_sources.SOURCES[s]} : {voix_sources.resume(s)}" for s in sources]
        index = _choose(labels, pos, lambda i, text: say(text), key_source, out, dire("voix.titre_sources"),
                        "voix_sources", consigne=dire("voix.consigne_sources"), prefix=message)
        if index is None:
            return dire("voix.reglages_fermes")
        if prefs and index == 0:
            pos = 0
            message = _preferences(key_source, say, out)
            continue
        pos = index
        message = _regler(sources[index - (1 if prefs else 0)], key_source, say, out)
