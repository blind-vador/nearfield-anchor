"""Cérémonie unique d'une prise envoyée au journal.

shim d'arrivée, annonce (la bande, puis la modulation), shim de transition,
le flux capté, shim de fin. Le flux a été mis en tampon pendant que la
cérémonie parlait : on le lit ensuite, rien n'est perdu.
"""

import os
import tempfile

from sound_paths import RADIO_SCANER_DIR

INCOMING = os.path.join(RADIO_SCANER_DIR, "incoming.mp3")
FREQ_OPEN = os.path.join(RADIO_SCANER_DIR, "freq_open.mp3")
FREQ_OVER = os.path.join(RADIO_SCANER_DIR, "freq_over.mp3")

_PREFIXES = ("bande", "canal")
_courant = {"bande": None, "appel": None, "groupe": None}


def _fichiers(bande_id, appel, groupe=None):
    """Arrivée et ouverture. Appel sans son : groupe de modulation, puis bande, puis général."""
    import sonneries
    return (sonneries.resoudre(bande_id, "incoming", appel, groupe) or INCOMING,
            sonneries.resoudre(bande_id, "freq_open", appel, groupe) or FREQ_OPEN)


def bande_dite(nom, prefix=None):
    """Un seul « bande » ou « canal » en tête.

    « bande bande uhf » vient d'un préfixe posé sur un nom qui commençait
    déjà par « bande » (bande L, bande 868). prefix ajoute le mot seulement
    s'il n'y est pas.
    """
    mots = (nom or "").split()
    while len(mots) >= 2 and mots[0].lower() in _PREFIXES and mots[1].lower() in _PREFIXES:
        mots.pop(0)
    nom = " ".join(mots).strip(" .")
    if not nom:
        return ""
    if prefix and nom.split()[0].lower() not in _PREFIXES:
        return f"{prefix} {nom}"
    return nom


def modulation_dite(mode):
    """Nom parlé d'une modulation ou d'un protocole (FM étroite, BLU haute, DMR)."""
    mode = " ".join((mode or "").split()).strip(" .")
    if not mode:
        return ""
    import propositions
    return propositions.nom(mode)


def texte(bande, modulation):
    """« bande UHF 406 à 430 mégahertz, FM étroite. »"""
    from phrases import dire
    bande = bande_dite(bande)
    modulation = " ".join((modulation or "").split()).strip(" .")
    if bande and modulation:
        return dire("radio.ouverture", bande=bande, modulation=modulation)
    if bande:
        return bande + "."
    if modulation:
        return modulation + "."
    return ""


def son_fin(freq_hz, bande_id=None, appel=None):
    """Son de fin : celui de la bande, le PMR 446 sur ses canaux, sinon le général."""
    import sonneries
    try:
        freq = float(freq_hz)
    except (TypeError, ValueError):
        freq = None
    if freq is not None and 446.0e6 <= freq <= 446.2e6:
        bande_id = "pmr446"
    elif bande_id is None and freq is not None:
        bande_id = sonneries.bande_pour(freq)
    return sonneries.resoudre(bande_id, "freq_over", appel) or FREQ_OVER


def _fichier_annonce(phrase):
    """Fichier WAV de l'annonce, pour une prise écrite sans être jouée."""
    import annonce_morceaux as morceaux
    path = morceaux.prepare(morceaux.whole(phrase)).wait()
    if path and os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    print(f"[ceremonie] annonce sans fichier : {phrase}")
    return None


def poser_ouverture(session, bande, modulation, bande_id=None, appel=None, groupe=None,
                    garder_annonce=False):
    """Écrit arrivée, annonce, ouverture, dans cet ordre. Rend l'annonce
    s'il faut encore la jouer."""
    if session is None and not garder_annonce:
        return None
    phrase = texte(bande, modulation)
    incoming, ouverture = _fichiers(bande_id, appel, groupe)
    annonce = _fichier_annonce(phrase) if phrase else None
    if session is not None:
        session.add_audio_file(incoming)
        if annonce:
            session.add_audio_file(annonce)
        session.add_audio_file(ouverture)
    if annonce and annonce.startswith(tempfile.gettempdir()) and not garder_annonce:
        try:
            os.remove(annonce)
        except OSError:
            pass
        annonce = None
    return annonce


def poser_fin(session, freq_hz=None, bande_id=None, appel=None):
    if session is not None:
        session.add_audio_file(son_fin(freq_hz, bande_id, appel))


def habiller(session, bande, modulation, voix, freq_hz=None, bande_id=None, appel=None):
    """La prise entière, pour un envoi au journal qui n'est pas joué en direct."""
    poser_ouverture(session, bande, modulation, bande_id, appel)
    if voix:
        session.add_audio_file(voix, contenu=True)
    poser_fin(session, freq_hz, bande_id, appel)


def ouvrir(bande, modulation, label="radio_watch", jouer=True, bande_id=None, appel=None, groupe=None):
    """Joue, ou écrit seulement, le début de la cérémonie. Le flux vient après.

    L'arrivée, l'annonce et l'ouverture sont écrites d'abord, dans cet ordre,
    puis jouées une seule fois. La lecture ne les recopie pas dans la prise.
    """
    from sound_player import get_recording_session, play_sound_effect
    import audio_lock
    import sound_player
    _courant["bande"] = bande_id
    _courant["appel"] = appel
    _courant["groupe"] = groupe
    incoming, ouverture = _fichiers(bande_id, appel, groupe)
    muet = (not jouer) or audio_lock.dropped()
    annonce = poser_ouverture(get_recording_session(), bande, modulation, bande_id, appel, groupe,
                              garder_annonce=not muet)
    if muet:
        return
    phrase = texte(bande, modulation)
    if phrase:
        print(phrase)
    sound_player.enregistrer_journal = False
    try:
        def voix():
            if annonce:
                play_sound_effect(annonce, timeout_sec=30, label=label)

        if annonce:
            sound_player.play_lead_then(incoming, voix, label=label)
        elif phrase:
            from speak import speak_with_lead_sound
            speak_with_lead_sound(incoming, phrase, label=label, repetitive=True)
        else:
            play_sound_effect(incoming, label=label)
        play_sound_effect(ouverture, label=label)
    finally:
        sound_player.enregistrer_journal = True
        if annonce and annonce.startswith(tempfile.gettempdir()):
            try:
                os.remove(annonce)
            except OSError:
                pass


def fermer(freq_hz=None, label="radio_watch", bande_id=None, appel=None):
    """Shim de fin. Écrit une fois, joué ensuite sans être recopié."""
    from sound_player import get_recording_session, play_sound_effect
    import audio_lock
    import sound_player
    if bande_id is None:
        bande_id = _courant.get("bande")
    if appel is None:
        appel = _courant.get("appel")
    son = son_fin(freq_hz, bande_id, appel)
    session = get_recording_session()
    if session is not None:
        session.add_audio_file(son)
    if audio_lock.dropped():
        return
    sound_player.enregistrer_journal = False
    try:
        play_sound_effect(son, label=label)
    finally:
        sound_player.enregistrer_journal = True
