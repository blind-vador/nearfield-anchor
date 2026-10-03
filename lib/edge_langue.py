"""Langue forcee pour les voix Microsoft (edge-tts), 2026-10-03.

edge-tts 7.x ecrit en dur xml:lang='en-US' dans la requete SSML et n'offre
aucun parametre de langue. Les voix "Multilingual" (Vivienne, Remy...)
devinent alors la langue phrase par phrase : un mot anglais (easyJet,
Handoff...) et elles basculent en anglais. Correctif : la requete porte la
langue de la console (phrases.langue -> LOCALES) et le texte est entoure de
<lang xml:lang="..."> (element prevu par Microsoft pour fixer la langue
parlee d'une voix multilingue). Appele une fois au chargement de chaque
module qui synthetise (speak, prise_vocale, console/voix).
"""
LOCALES = {"fr": "fr-FR", "en": "en-US", "de": "de-DE", "es": "es-ES", "it": "it-IT", "pt": "pt-PT", "nl": "nl-NL"}
_done = {"ok": False}


def locale():
    try:
        import phrases
        return LOCALES.get(phrases.langue(), "fr-FR")
    except Exception:
        return "fr-FR"


def force_language():
    if _done["ok"]:
        return
    try:
        import edge_tts.communicate as comm
    except ImportError:
        return

    def mkssml(tc, escaped_text):
        if isinstance(escaped_text, bytes):
            escaped_text = escaped_text.decode("utf-8")
        lang = locale()
        return (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='{lang}'>"
                f"<voice name='{tc.voice}'>"
                f"<prosody pitch='{tc.pitch}' rate='{tc.rate}' volume='{tc.volume}'>"
                f"{escaped_text}"
                "</prosody>"
                "</voice>"
                "</speak>")

    comm.mkssml = mkssml
    _done["ok"] = True
