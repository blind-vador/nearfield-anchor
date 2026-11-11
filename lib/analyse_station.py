"""Ce qu'une IA comprend d'une prise transcrite, et le nom de la station
(2026-11-11, plan phase 5).

Apres l'examen du Mac (Whisper sur le son brut, nettoye si besoin), le texte part
a Gemini (ou une autre IA, lib/gemini.py) avec ce qu'on sait du canal : bande,
modulation, juge, relais connu, indicatifs deja entendus, et la FICHE de la
station (nom, pourquoi, dernieres analyses). L'IA rend en JSON :
  explication   ce qu'elle comprend de l'echange (lu a voix haute) ;
  nom           2 a 5 mots, l'usage du canal (jamais un indicatif de passage) ;
  pourquoi      deux ou trois mots, au sens figure (« clio genante ») ;
  mise_a_jour   s'il y avait une fiche : « en sachant qu'on avait note..., je
                constate aujourd'hui..., donc je propose... » ;
  indicatifs    entendus (gardes dans la fiche, pas comme nom) ;
  frequences    citees dans l'echange (lib/frequences_ia : connues renforcees,
                nouvelles en surveillance 14 jours).
Rien n'est pose sans l'utilisateur : le nom va au journal des noms (C, R).
"""
import json
import os
import time

import chemins

FICHES = chemins.base("whisper_canaux.json")    # memoire des canaux (lib/noms_whisper.NOMS)
HISTORIQUE_MAX = 5
TIMEOUT = 60


def _lire():
    try:
        with open(FICHES, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ecrire(data):
    tmp = FICHES + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, FICHES)


def _cle(freq_hz):
    return str(int(round(float(freq_hz) / 12500.0) * 12500))


def fiche(freq_hz):
    """Ce qu'on sait du canal : nom pose, pourquoi, dernieres analyses."""
    f = dict(_lire().get(_cle(freq_hz)) or {})
    try:
        import nommage
        for item in nommage.en_attente():
            if item.get("cle") == _cle(freq_hz):
                f.setdefault("nom_propose", item.get("nom"))
                f.setdefault("pourquoi_propose", item.get("pourquoi"))
    except Exception:
        pass
    return f


def _contexte(freq_hz, mode, label, juge):
    import propositions
    rel = ""
    try:
        import relais
        f, r = relais.relais_proche(freq_hz)
        if r:
            rel = f"{r.get('nom') or 'relais'} ({r.get('mode') or 'fm'})"
    except Exception:
        pass
    indicatifs = ""
    try:
        import frequences_actives as fa
        e = fa.load().get(fa._cle(freq_hz)) or {}
        indicatifs = ", ".join(sorted(e.get("indicatifs") or {}, key=lambda k: -e["indicatifs"][k])[:6])
    except Exception:
        pass
    f = fiche(freq_hz)
    resume = {k: f.get(k) for k in ("nom", "pourquoi", "nom_propose", "pourquoi_propose") if f.get(k)}
    resume["historique"] = [{k: h.get(k) for k in ("le", "explication", "nom", "pourquoi")}
                            for h in (f.get("historique") or [])[-3:]]
    return {"bande": label or "inconnue", "modulation": propositions.nom(mode) if mode else "inconnue",
            "juge": propositions.nom(juge) if juge else "inconnu", "relais": rel or "aucun connu",
            "indicatifs": indicatifs or "aucun", "fiche": json.dumps(resume, ensure_ascii=False),
            "mhz": f"{float(freq_hz) / 1e6:.4f}"}, f


def _propre(valeur, n=None):
    texte = " ".join(str(valeur or "").split()).strip(" .,;:")
    return texte[:n] if n else texte


def analyser(freq_hz, mode, label, juge, texte, fichier, machine="", journal=None):
    """Demande l'analyse ; rend le resultat (dict) ou None. Depose le nom propose."""
    if not freq_hz or not (texte or "").strip():
        return None
    import gemini
    from phrases import dire
    ctx, ancienne = _contexte(freq_hz, mode, label, juge)
    reponse, probleme = gemini.ask(dire("station.consigne_analyse", texte=texte[:3000], **ctx),
                                   temperature=0.3, json_mode=True, timeout=TIMEOUT, scanneur=True)
    if not reponse:
        print(f"[station] {fichier} : pas d'analyse ({probleme})")
        return None
    try:
        r = json.loads(reponse)
        if not isinstance(r, dict):
            raise ValueError("pas un objet")
    except ValueError:
        print(f"[station] reponse illisible : {reponse[:200]!r}")
        return None
    res = {"le": time.strftime("%Y-%m-%dT%H:%M:%S"), "fichier": os.path.basename(fichier or ""),
           "explication": _propre(r.get("explication"), 600), "nom": _propre(r.get("nom"), 42) or None,
           "pourquoi": _propre(r.get("pourquoi"), 40) or None, "mise_a_jour": _propre(r.get("mise_a_jour"), 400) or None,
           "indicatifs": [_propre(i, 12).upper() for i in (r.get("indicatifs") or []) if _propre(i)][:8]}
    if res["nom"] and len(res["nom"]) < 3:
        res["nom"] = None
    trouvees = {"connues": [], "surveillees": []}
    try:
        import frequences_ia
        trouvees = frequences_ia.ranger(r.get("frequences") or [], "analyse de station", freq_hz)
    except Exception as exc:
        print(f"[station] frequences : {exc!r}")
    res["frequences"] = trouvees
    _memoriser(freq_hz, mode, res)
    if journal:
        try:
            with open(journal[:-4] + ".analyse.json", "w", encoding="utf-8") as f:
                json.dump(dict(res, texte=texte), f, ensure_ascii=False)
        except OSError as exc:
            print(f"[station] analyse non gardee pres de la prise : {exc!r}")
    if res["nom"] and res["nom"] != ancienne.get("nom"):
        try:
            import nommage
            nommage.deposer(freq_hz, mode, None, label, res["nom"], texte, machine, res["fichier"],
                            pourquoi=res["pourquoi"], explication=res["explication"],
                            mise_a_jour=res["mise_a_jour"], ancien_nom=ancienne.get("nom"))
        except Exception as exc:
            print(f"[station] nom non propose : {exc!r}")
    print(f"[station] {float(freq_hz) / 1e6:.4f} MHz : {res['nom']} ({res['pourquoi']}) - {res['explication'][:80]}")
    return res


def _memoriser(freq_hz, mode, res):
    """Chaque analyse entre dans l'historique de la fiche, posee ou non :
    c'est ce que la prochaine analyse relira (« en sachant qu'on avait note... »)."""
    data = _lire()
    e = data.setdefault(_cle(freq_hz), {})
    e.setdefault("historique", []).append({k: res.get(k) for k in ("le", "explication", "nom", "pourquoi")})
    e["historique"] = e["historique"][-HISTORIQUE_MAX:]
    if res.get("indicatifs"):
        vus = e.setdefault("indicatifs", {})
        for i in res["indicatifs"]:
            vus[i] = vus.get(i, 0) + 1
    if mode:
        e.setdefault("mode", mode)
    _ecrire(data)


def phrase(res, demande=True):
    """Ce qu'on dit apres un examen demande avec E."""
    from phrases import dire
    if not res:
        return ""
    def phrase_finie(t):
        t = (t or "").strip()
        return t if not t or t[-1] in ".!?…" else t + "."

    res = dict(res, explication=phrase_finie(res.get("explication")), mise_a_jour=phrase_finie(res.get("mise_a_jour")))
    morceaux = []
    if res.get("explication"):
        morceaux.append(dire("station.compris", explication=res["explication"]))
    if res.get("mise_a_jour"):
        morceaux.append(res["mise_a_jour"])
    elif res.get("nom"):
        morceaux.append(dire("station.nom_propose", nom=res["nom"], pourquoi=res.get("pourquoi") or ""))
    try:
        import frequences_ia
        f = frequences_ia.phrase(res.get("frequences") or {})
        if f:
            morceaux.append(f)
    except Exception:
        pass
    return " ".join(m for m in morceaux if m)
