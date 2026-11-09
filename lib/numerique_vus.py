"""Un mode numerique prouve sans voix a faire entendre (2026-11-05).

Un relais DMR au repos, du trafic de donnees ou du chiffre n'ont pas de voix a
jouer. Le juge les reconnaissait (3 123 synchros propres sur 467,2375 MHz), et le
resultat finissait en "rien" : l'utilisateur ne voyait que les essais AM et BLU.
On le dit au journal. Rien n'est presume : l'entree porte une fiche, la fleche
gauche fait taire ce mode sur cette frequence, et les essais analogiques continuent.

PREUVE_MIN : le bruit a sorti au plus 6 synchros propres sur 120 flux reels du
2026-11-05 ; le vrai DMR, au moins 26 en 10 s.
"""
import fcntl
import json
import os
import time

import chemins

PATH = chemins.base("numerique_vus.json")
ECRITURE = PATH + ".verrou"
PREUVE_MIN = 20
RATIO_MIN = 0.5
REDIRE_SEC = 6 * 3600
CANAL_HZ = 6250
GROUPES_DITS = 4


def prouve(propres, total):
    propres = propres or 0
    return propres >= PREUVE_MIN and propres >= RATIO_MIN * max(total or 0, 1)


def _lire():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def a_dire(freq_hz, protocole, groupes=(), chiffre=False, maintenant=None):
    """Retient cette preuve. Rend vrai la premiere fois, sur un groupe ou un chiffrement
    nouveaux, ou passe REDIRE_SEC : un relais au repos ne remplit pas le journal."""
    maintenant = maintenant or time.time()
    cle = "%d|%s" % (round(freq_hz / CANAL_HZ) * CANAL_HZ, protocole)
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(ECRITURE, "w") as verrou:
        fcntl.flock(verrou, fcntl.LOCK_EX)
        data = _lire()
        e = data.get(cle) or {"premier": maintenant, "dit": 0, "groupes": [], "chiffre": False}
        nouveaux = [g for g in groupes if g not in e["groupes"]]
        dire = bool(nouveaux) or (chiffre and not e["chiffre"]) or maintenant - e["dit"] >= REDIRE_SEC
        e["groupes"] = (e["groupes"] + nouveaux)[-50:]
        e["chiffre"] = bool(e["chiffre"] or chiffre)
        e["vu"] = maintenant
        if dire:
            e["dit"] = maintenant
        data[cle] = e
        tmp = PATH + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PATH)
    return dire


def _details(meta, chiffre):
    from phrases import dire
    meta = meta or {}
    bouts = []
    if meta.get("color_code") is not None:
        bouts.append(dire("numerique.detail_couleur", code=meta["color_code"]))
    if meta.get("nac") is not None:
        bouts.append(dire("numerique.detail_nac", nac="%X" % meta["nac"]))
    groupes = meta.get("talkgroups") or []
    if groupes:
        bouts.append(dire("numerique.detail_groupes", groupes=", ".join(str(g) for g in groupes[:GROUPES_DITS])))
    if chiffre:
        bouts.append(dire("numerique.detail_chiffre"))
    return "".join(" " + b for b in bouts)


def _apprendre_dmr(freq_hz, propres, meta, chiffre):
    """Une frequence DMR que le registre ne connait pas entre dans la veille DMR."""
    import dmr_registre
    if dmr_registre.key(freq_hz) in dmr_registre.load():
        return                  # son centre mesure reste celui de vraies sessions
    cc = (meta or {}).get("color_code")
    dmr_registre.record({"freq_hz": int(freq_hz), "chiffre": chiffre,
                         "dmr": {"cc": {} if cc is None else {cc: propres}, "relais": False, "systeme": None,
                                 "ars": [], "appels": {}}})


def annoncer(freq_hz, protocole, propres, total, meta=None, chiffre=False, tag="radio_watch"):
    """Rend True si une entree part au journal. Ne leve pas."""
    if not freq_hz or not protocole or not prouve(propres, total):
        return False
    try:
        if protocole == "DMR":
            _apprendre_dmr(freq_hz, propres, meta, chiffre)
        if not a_dire(freq_hz, protocole, (meta or {}).get("talkgroups") or (), chiffre):
            return False
        import messages_types
        import sons
        import veille_donnees as vd
        from phrases import dire
        texte = dire("numerique.prouve_sans_voix", protocole=protocole,
                     frequence=("%.4f" % (freq_hz / 1e6)).replace(".", ","), propres=propres) + _details(meta, chiffre)
        fiche = dict(messages_types.fiche(protocole, texte, int(freq_hz)), type_message="%s sans voix" % protocole)
        vd.announce(texte, sons.chemin("dmr_nouveau"), "donnees_dmr", tag, fiche=fiche)
        print("[%s] %.4f MHz : %s prouve sans voix (%s/%s synchros propres)" % (
            tag, freq_hz / 1e6, protocole, propres, total))
        return True
    except Exception as exc:
        print("[%s] numerique prouve : %r" % (tag, exc))
        return False
