"""Couche DISPATCHER au-dessus de la couche decode : pour UNE ecoute, choisit
LOCAL (le Pi demodule, comme aujourd'hui) ou DISTANT (la machine compagne
demodule a sa place). Idee utilisateur 2026-10-31.

Principe : le distant n'entre en action QUE si le Pi en a besoin -- c'est
lib/offload (gradient thermique) qui tranche. Sinon tout reste local. Quand il
delegue, la compagne sert de DEMODULATEUR COMPLET sur le flux rtl_tcp du Pi :
FM analogique classique ET numerique divers, en meme temps (dsd-neo `-fa -8`
rend le numerique decode + l'audio analogique brut sur un seul flux -- prouve
en direct 2026-10-31). On garde ainsi la multi-detection d'UNE fenetre, au lieu
de fragmenter par mode. Mode-AGNOSTIQUE (rien a voir avec le tempo).

Gracieux (doctrine) : pas de compagne libre, ou echec du distant -> on reste
local. On n'EXIGE jamais la compagne.

Patron d'appel (cote radio_watch, cablage a venir) :

    d = demod.plan(poids_sec)
    if d["deleguer"]:
        # le Pi sert son dongle par rtl_tcp (comme _tetra_window), puis :
        res = demod.distant(freq_hz, gain, sec, host, port, mode=mode_dsd,
                            max_secondes=plafond)
        if res.get("ok") and (res.get("voix_wav") or res.get("audio_wav")):
            ... presenter res ...  # voix numerique OU audio FM remontes
        else:
            ... repli local ...
    else:
        ... demodulation locale habituelle (prise unique FM + numerique) ...

`poids_sec` = estimation des secondes de decodage (p.ex. tempo.sonde() / dwell).
`mode_dsd` = flag dsd-neo que le Pi impose selon ce qu'il delegue (-fa auto,
-fs relais DMR connu...) ; voir bridge/numerique_neo.MODES_OK.
"""
CAPACITE = "numerique"   # capacite declaree par la compagne (decodeur plein, bridge/numerique_neo)
DEFAUT_MODE = "-fa"


def plan(poids_sec, temp_c=None):
    """Decision de routage d'une ecoute de poids `poids_sec`. Rend le dict de
    offload.deleguer : {deleguer, compagne, temp_c, seuil, raison}. Ne leve pas
    (pas d'offload -> local)."""
    try:
        import offload
        return offload.deleguer(CAPACITE, poids_sec, temp_c)
    except Exception as exc:
        print(f"[demod] offload indisponible ({exc!r}) -> local")
        return {"deleguer": False, "compagne": None, "temp_c": temp_c, "seuil": None, "raison": "local_sans_offload"}


def distant(freq_hz, gain, secondes, host, port=1234, ppm=0, max_secondes=None, mode=DEFAUT_MODE, banc=False, modes=None):
    """Delegue l'ecoute a la compagne (prise NUMERIQUE_SOCKET -> liaison ->
    dsd-neo). Le Pi doit DEJA servir rtl_tcp sur host:port et avoir tune le
    CENTRE RECENTRE. Rend le dict resultat (ok, protocole, voix_wav, ...), ou
    {"ok": False, "erreur": ...}. Ne leve pas."""
    try:
        import numerique_distant
    except Exception as exc:
        return {"ok": False, "erreur": "client distant absent (%r)" % exc}
    res = numerique_distant.decoder(freq_hz, gain, secondes, host=host, port=port, ppm=ppm,
                                    max_secondes=max_secondes, mode=mode, banc=banc, modes=modes)
    if not res.get("ok"):
        print(f"[demod] distant {freq_hz / 1e6:.4f} MHz : {res.get('erreur')}")
    return res
