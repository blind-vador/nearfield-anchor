"""Salves de signalisation retrouvees apres coup dans un audio deja capte
(2026-11-01) : ASN / ATIS (lib/asn_dsc.py) et identifiants des radios pro
analogiques (lib/ffsk_pro.py : MDC1200, FleetSync II, MPT1327).

Pour les ecoutes ou ces decodeurs ne tournent pas en direct a cote de
multimon-ng : audio relaye par RTLSDR-Airband (airband_bridge) et audio FM
rendu par une machine compagne (demodulation deleguee, radio_watch). Quelques
dixiemes de seconde de calcul par ecoute ; jamais bloquant."""
import asn_dsc
import ffsk_pro
import veille_donnees as vd
from phrases import dire


def apres_coup(pcm_s16, taux, nom, freq_hz, son, source):
    """Decode, archive et annonce ; rend la liste des textes trouves."""
    vus = []
    try:
        trouves = [asn_dsc.interpreter(c) for c in asn_dsc.chercher(asn_dsc.demoduler(pcm_s16, taux))]
        trouves += [f"{proto}, {texte}" for proto, texte in ffsk_pro.decoder(pcm_s16, taux)]
        for texte in trouves:
            if texte and texte not in vus:
                vus.append(texte)
                print(f"[{source}] {nom} : {texte}")
                vd.archive("signalisation", {"freq_hz": freq_hz, "canal": nom, "texte": texte})
                vd.announce(dire("radio.signalisation_asn", canal=nom, texte=texte), son,
                            "message_signalisation", source, fiche={"protocole": texte.split(",")[0],
                                                                     "frequence_hz": freq_hz})
    except Exception as exc:   # jamais bloquant pour l'ecoute
        print(f"[{source}] signalisation : {exc!r}")
    return vus
