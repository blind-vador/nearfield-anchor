"""Frequences trouvees par une IA dans un texte (2026-11-11, plan phase 3).

Une IA (Gemini ou une autre, touche E des journaux messages/donnees, analyse
d'une station apres Whisper) lit mieux qu'une expression reguliere une frequence
mal formatee (« cent quarante-cinq six cent vingt-cinq », « 145,6250 », « QSY
sur le 25 »). Elle rend une liste JSON :
  [{"mhz": 145.625, "modulation": "FM|DMR|D-Star|...|inconnue", "role": "...",
    "phrase": "extrait du texte", "certitude": 0..1}]
On verifie chacune, puis :
  - deja connue (relais, station, frequence active) : renforcee, avec la source ;
  - inconnue : mise en SURVEILLANCE 14 jours (frequences_actives, revisitee par le
    scanner). Si elle donne une prise, elle devient une frequence active comme les
    autres ; sinon elle s'efface d'elle-meme ;
  - modulation annoncee : a priori du juge correspondant (propositions.a_priori,
    qui s'efface aussi s'il ne se confirme pas).
Chaque frequence mise en surveillance devient aussi une entree du journal
proposition (lib/revue_propositions, avec sa fiche vocale) : C la garde pour de
bon, R l'oublie et elle ne sera plus mise en surveillance (2026-11-11).
"""
import json
import os
import re
import time

import chemins

CERTITUDE_MIN = 0.5
MHZ_MIN, MHZ_MAX = 1.0, 1800.0      # ce que le dongle couvre
SURVEILLANCE_SEC = 14 * 86400       # valide par l'utilisateur le 2026-11-11
POIDS_IA = 1.0
TROUVEES = chemins.base("frequences_trouvees.json")    # {"attente": [...], "refusees": [cle]}


def _lire():
    try:
        with open(TROUVEES, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("attente", [])
            data.setdefault("refusees", [])
            return data
    except (OSError, ValueError):
        pass
    return {"attente": [], "refusees": []}


def _ecrire(data):
    tmp = TROUVEES + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, TROUVEES)


def _a_relire(freq, mhz, modulation, role, phrase, source, freq_source):
    """Entree du journal proposition (une par frequence, la plus recente garde la place)."""
    import frequences_actives as fa
    cle = fa._cle(freq)
    data = _lire()
    data["attente"] = [a for a in data["attente"] if a.get("cle") != cle]
    item = {"id": f"{int(time.time() * 1000)}", "cle": cle, "freq_hz": freq, "mhz": mhz,
            "modulation": modulation, "role": role, "phrase": phrase, "source": source,
            "freq_source": int(freq_source) if freq_source else None,
            "le": time.strftime("%Y-%m-%dT%H:%M:%S")}
    data["attente"].append(item)
    _ecrire(data)
    try:
        import revue_propositions
        revue_propositions.preparer("frequence", item)
    except Exception as exc:
        print(f"[frequences_ia] fiche vocale : {exc!r}")


def a_revoir():
    """Frequences en surveillance a accepter ou refuser. Une frequence devenue
    permanente (on y a capte quelque chose) ou effacee (14 jours sans rien)
    quitte la revue d'elle-meme."""
    import frequences_actives as fa
    actives = fa.load()
    return [a for a in _lire()["attente"] if (actives.get(a.get("cle")) or {}).get("expire")]


def _retirer(item, refuser=False):
    data = _lire()
    data["attente"] = [a for a in data["attente"] if a.get("id") != item.get("id")]
    if refuser and item.get("cle") not in data["refusees"]:
        data["refusees"].append(item.get("cle"))
    _ecrire(data)


def garder(item):
    """C : la frequence reste suivie, sans limite de temps."""
    import frequences_actives as fa
    fa.garder(item.get("freq_hz"))
    _retirer(item)


def oublier(item):
    """R : la surveillance s'arrete et cette frequence ne sera plus surveillee sur un texte."""
    import frequences_actives as fa
    fa.oublier_surveillance(item.get("freq_hz"))
    _retirer(item, refuser=True)


def _mhz(valeur):
    try:
        return float(str(valeur).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _connue(freq_hz):
    import frequences_actives as fa
    import relais
    if relais.relais_proche(freq_hz)[0] is not None:
        return "relais"
    e = fa.load().get(fa._cle(freq_hz))
    if e and set(e.get("natures") or {}) - {"annoncee"}:
        return "frequence active"
    try:
        import stations
        if any(abs(s.get("freq_hz", 0) - freq_hz) <= fa.PAS_HZ for s in stations.load().get("stations", [])):
            return "station"
    except Exception:
        pass
    return None


def ranger(trouvees, source, freq_source=None):
    """Rend {"connues": [...], "surveillees": [...], "ecartees": n} (frequences en Hz)."""
    import frequences_actives as fa
    out = {"connues": [], "surveillees": [], "ecartees": 0}
    for item in trouvees or []:
        if not isinstance(item, dict):
            out["ecartees"] += 1
            continue
        mhz = _mhz(item.get("mhz"))
        certitude = _mhz(item.get("certitude"))
        if mhz is None or not MHZ_MIN <= mhz <= MHZ_MAX or (certitude is not None and certitude < CERTITUDE_MIN):
            out["ecartees"] += 1
            continue
        freq = int(round(mhz * 1e6))
        if freq_source and abs(freq - int(freq_source)) <= fa.PAS_HZ:
            continue   # la frequence ou l'on a entendu le message : rien de neuf
        phrase = re.sub(r"\s+", " ", str(item.get("phrase") or ""))[:160]
        modulation = str(item.get("modulation") or "").strip()
        connue = _connue(freq)
        if not connue and fa._cle(freq) in _lire()["refusees"]:
            out["ecartees"] += 1     # oubliee par l'utilisateur (R au journal proposition)
            continue
        # « annoncee » est un indice de RELAIS (frequences_actives.raisons_relais) : seulement
        # pour une sortie ou une entree de relais, pas pour toute frequence citee
        # (133,760 dans un echange de controle aerien etait annonce « relais possible »).
        role = str(item.get("role") or "").lower()
        relais_cite = any(m in role for m in ("relais", "repeater", "sortie", "entree", "entrée"))
        fa.noter(freq, "annoncee", annoncee=relais_cite, texte=f"{source} : {phrase}" if phrase else source,
                 surveillance_sec=None if connue else SURVEILLANCE_SEC)
        try:
            import propositions
            juge = propositions.juge_du_protocole(modulation)
            if not juge and modulation.lower() in ("am", "fm", "nfm", "usb", "lsb", "blu"):
                juge = {"fm": "nfm", "blu": "usb"}.get(modulation.lower(), modulation.lower())
            if juge:
                propositions.a_priori(freq, juge, POIDS_IA, f"ia {source}")
        except Exception as exc:
            print(f"[frequences_ia] juge non note : {exc!r}")
        (out["connues"] if connue else out["surveillees"]).append(freq)
        if not connue:
            _a_relire(freq, mhz, modulation, role, phrase, source, freq_source)
        print(f"[frequences_ia] {source} : {mhz} MHz {modulation or ''} -> "
              + (f"connue ({connue})" if connue else "surveillance 14 jours"))
    return out


def phrase(resultat):
    """Courte phrase a dire, ou "" s'il n'y a rien."""
    from phrases import dire, nombre
    c, s = len(resultat.get("connues") or []), len(resultat.get("surveillees") or [])
    if not c and not s:
        return ""
    if c and s:
        return dire("frequences_ia.les_deux", connues=nombre(c, "déjà connue", "déjà connues"),
                    surveillees=nombre(s, "mise en surveillance", "mises en surveillance"))
    if c:
        return dire("frequences_ia.connues", nombre=nombre(c, "fréquence trouvée, déjà connue",
                                                           "fréquences trouvées, déjà connues"))
    return dire("frequences_ia.surveillees", nombre=nombre(s, "fréquence nouvelle mise en surveillance",
                                                           "fréquences nouvelles mises en surveillance"))
