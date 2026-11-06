"""Textes transportes par les radios numeriques (DMR, D-Star, YSF, M17,
NXDN, dPMR...) que dsd-fme decode en plus de la voix : nom transmis par
l'emetteur (alias), SMS et textes, positions GPS (2026-10-03). Chacun est dit
au fil de l'eau comme une prise vocale du journal "messages"
(lib/prise_vocale.py), avec l'indicatif et la ville de l'emetteur quand la
base publique les connait (lib/radioamateurs.py)."""
import os

import veille_donnees as vd
from phrases import dire

SOUND = "/media/nas/nd1/server/radio_scaner/decoding_numeric.mp3"
REPEAT_SEC = 3600          # le meme alias ou le meme texte du meme emetteur : redit apres 1 h
_memory = vd.Memory("/tmp/messages_numeriques.json")


def _who(src):
    if not src:
        return dire("numerique.emetteur_inconnu")
    try:
        import radioamateurs
        ham = radioamateurs.dmr_id(src)
    except Exception:
        ham = None
    if ham and ham.get("indicatif"):
        where = ham.get("ville") or ham.get("pays")
        return dire("numerique.indicatif_ville", indicatif=vd.spell(ham["indicatif"]), ville=where) if where \
            else dire("numerique.indicatif", indicatif=vd.spell(ham["indicatif"]))
    return dire("numerique.identifiant", numero=src)


def found(kind, value, stats):
    """Un texte trouve dans la sortie de dsd-fme : annonce s'il est nouveau.
    kind : alias, texte ou gps."""
    src = stats.get("src")
    if not _memory.due(f"{kind}|{src}|{value[:80]}", REPEAT_SEC):
        return
    _memory.save()
    protocol = stats.get("protocol") or dire("radio.numerique")
    freq = stats.get("freq_hz")
    if kind != "gps":  # alias ou SMS : une frequence, un relais, un indicatif ?
        try:
            import relais
            relais.depuis_texte(value, freq, "dmr " + kind, sur=True)
        except Exception as exc:
            print(f"[messages_numeriques] texte non lu pour les relais : {exc!r}")
    mhz = f"{freq / 1e6:.4f}".rstrip("0").replace(".", ",") if freq else None
    fiche = {"protocole": protocol, "frequence_hz": freq, "source": src, "groupe": stats.get("tgt"),
             "genre": kind, "texte_original": value, "prive": True,
             "type_message": f"{protocol}, {kind}"}

    def text():  # dans la file d'annonces : recherche de l'indicatif sans bloquer le decodage
        who = _who(src)
        fiche["source"] = who
        key = {"alias": "numerique.alias", "texte": "numerique.texte", "gps": "numerique.gps"}[kind]
        spoken = value
        if kind == "gps":
            try:
                lat, lon = (float(x) for x in value.split(","))
                spoken = vd.where(lat, lon)
            except (ValueError, TypeError):
                pass
        said = dire(key, protocole=protocol, qui=who, valeur=spoken)
        if mhz:
            said += " " + dire("numerique.frequence", frequence=mhz)
        return said

    vd.announce(text, SOUND, f"message_numerique_{protocol}", "decodeurs", fiche=fiche)
