"""Traduction entre les messages de la liaison v1 et la liste fermee v2 (2026-11-15).

Le code du pont (Pi : lib/compagnon_serveur.py ; machine : bridge/nearfield_bridge.py)
parle en messages v1 (dictionnaires). Quand une session v2 (Noise) est ouverte, on
traduit ici chaque message vers un type de la liste fermee (pont/src/messages.rs,
spec protocole-v2 §8) et retour. Le module Rust nearfield_pont revalide tout a
l'envoi ET a la reception : un champ en trop, un type inconnu ou une taille hors
limite est refuse, la session se ferme.

Ce qui ne se traduit pas leve NonTraduisible : l'appelant passe alors par la v1
(encore ouverte a cote, pour l'amorce et en secours).

Differences voulues avec la v1 :
  - "tetra" et "numerique" ne portent plus d'adresse a contacter : la machine se
    connecte au flux radio de l'ordonnateur auquel elle est liee (hote de la
    session), jamais a une adresse recue ;
  - les morceaux de son ont une piste nommee dans une liste fermee (PISTES) ;
  - "pris" porte une echeance (jusqu_a).

Fichier partage par le Pi (PYTHONPATH du service : bridge/) et la machine
compagne (copie dans ~/.nearfield-bridge/). Python 3.9 : pas de match.
"""
import re
import time
import uuid

PISTES = ("son", "tetra_voix", "numerique_voix", "numerique_audio", "numerique_flux")
_PISTE_VARIABLE = re.compile(r"^numerique_(banc|rendu)_[a-z0-9_-]{1,40}$")
PRISE_SEC = 6 * 3600  # echeance d'un "pris" : un travail long reste a la machine


class NonTraduisible(ValueError):
    """Ce message n'a pas d'equivalent v2 : il passe par la v1."""


def _id():
    return uuid.uuid4().hex[:16]


def piste_valide(piste):
    return piste in PISTES or bool(_PISTE_VARIABLE.match(str(piste)))


def _nombre(v):
    """Nombre JSON v2 (f64) rendu a la v1 : 32.0 -> 32, comme l'envoyait la v1."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _debit_pct(debit):
    """"+10%" (v1) -> 10 (v2)."""
    if debit is None or debit == "":
        return None
    try:
        return int(round(float(str(debit).strip().rstrip("%") or 0)))
    except ValueError:
        raise NonTraduisible("debit %r" % (debit,))


def _sans_none(d):
    return {k: v for k, v in d.items() if v is not None}


def _ecoute(m):
    secondes = int(m.get("secondes") or 0)
    return {"port": int(m.get("port") or 1234), "freq_hz": int(m["freq_hz"]),
            "gain": float(m.get("gain", 32)), "secondes": secondes,
            "max_secondes": int(m.get("max_secondes") or secondes), "ppm": float(m.get("ppm") or 0)}


def _puissance_libre(puissance):
    p = puissance if isinstance(puissance, dict) else {}
    try:
        return max(0, min(100, int(100 * float(p.get("disponible") or 0) / float(p["puissance"]))))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return 0


def vers_v2(m):
    """Message v1 -> message v2 (dictionnaire pour nearfield_pont.Session.envoyer)."""
    t = m.get("type")
    try:
        if t == "travail":
            return {"type": "travail_annonce", "id": _id(), "travail": m["travail"]}
        if t == "tickets":
            return {"type": "tickets", "id": _id(), "ids": list(m.get("ids") or [])}
        if t == "dire":
            return _sans_none({"type": "dire", "id": m["id"], "texte": m["texte"], "voix": m.get("voix"),
                               "debit": _debit_pct(m.get("debit"))})
        if t == "tetra":
            return dict(_ecoute(m), type="tetra_demande", id=m["id"])
        if t == "numerique":
            return _sans_none(dict(_ecoute(m), type="numerique_demande", id=m["id"], mode=m.get("mode") or "-fa",
                                   banc=bool(m.get("banc")), modes=list(m["modes"]) if m.get("modes") else None))
        if t == "battement":
            caps = [c for c in m.get("capacites") or [] if isinstance(c, str) and 0 < len(c) <= 128][:64]
            voix = [v for v in m.get("voix_mac") or [] if isinstance(v, str) and 0 < len(v) <= 128][:64]
            return _sans_none({"type": "battement", "id": _id(), "etat": "occupe" if m.get("occupe") else "libre",
                               "capacites": caps, "puissance_libre": _puissance_libre(m.get("puissance")),
                               "voix": voix, "session": m.get("etat") if isinstance(m.get("etat"), dict) else None,
                               "puissance": m.get("puissance") if isinstance(m.get("puissance"), dict) else None,
                               "reseau": m.get("reseau") if isinstance(m.get("reseau"), dict) else None})
        if t == "pris":
            return {"type": "travail_pris", "id": _id(), "travail": m["travail"],
                    "jusqu_a": int(time.time()) + PRISE_SEC}
        if t == "fini":
            return {"type": "travail_fini", "id": _id(), "travail": m["travail"]}
        if t == "son" or piste_valide(t):
            return _sans_none({"type": "morceau", "id": _id(), "flux": m["reponse_a"], "n": int(m.get("n") or 0),
                               "donnees": m.get("pcm") or "", "fin": False, "piste": t,
                               "taux": int(m["taux"]) if m.get("taux") else None})
        if t == "son_fin":
            return _sans_none({"type": "dire_fin", "id": _id(), "reponse_a": m["reponse_a"], "ok": bool(m.get("ok")),
                               "taux": int(m["taux"]) if m.get("taux") else None,
                               "erreur": str(m["erreur"])[:4000] if m.get("erreur") else None})
        if t == "tetra_resultat":
            return _sans_none({
                "type": "tetra_resultat", "id": _id(), "reponse_a": m["reponse_a"], "ok": bool(m.get("ok")),
                "erreur": str(m["erreur"])[:4000] if m.get("erreur") else None,
                "reseau": m.get("reseau") or None, "bsch": int(m.get("bsch") or 0),
                "sysinfo": int(m.get("sysinfo") or 0), "enc": m.get("enc") or None, "clair": bool(m.get("clair")),
                "mac_resource": int(m.get("mac_resource") or 0), "clip": bool(m.get("clip")),
                "voix": bool(m.get("voix")), "resume": m.get("resume") or None, "activite": m.get("activite") or None,
                "duree": float(m["duree"]) if m.get("duree") is not None else None})
        if t == "numerique_resultat":
            rendus = []
            for r in m.get("rendus") or []:
                rendus.append(_sans_none({"mode": str(r.get("mode") or ""), "protocole": r.get("protocole"),
                                          "confirme": bool(r.get("confirme")), "hits": int(r.get("hits") or 0),
                                          "rms": float(r.get("rms") or 0), "chiffre": bool(r.get("chiffre")),
                                          "voix": bool(r.get("voix"))}))
            reel = lambda k: float(m[k]) if m.get(k) is not None else None  # noqa: E731
            return _sans_none({
                "type": "numerique_resultat", "id": _id(), "reponse_a": m["reponse_a"], "ok": bool(m.get("ok")),
                "erreur": str(m["erreur"])[:4000] if m.get("erreur") else None, "protocole": m.get("protocole"),
                "meta": m.get("meta") or None, "chiffre": bool(m.get("chiffre")), "clair": bool(m.get("clair")),
                "voix": bool(m.get("voix")), "audio": bool(m.get("audio")), "flux": bool(m.get("flux")),
                "numerique_confirme": bool(m.get("numerique_confirme")),
                "digital_actif": bool(m.get("digital_actif")),
                "sync_hits": int(m["sync_hits"]) if m.get("sync_hits") is not None else None,
                "voix_rms": reel("voix_rms"), "audio_rms": reel("audio_rms"), "resume": m.get("resume") or None,
                "duree": reel("duree"), "banc": list(m["banc"]) if m.get("banc") else None,
                "banc_preuve": m.get("banc_preuve") or None, "preuve": m.get("preuve") or None,
                "rendus": rendus or None})
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, NonTraduisible):
            raise
        raise NonTraduisible("%s : %r" % (t, exc))
    raise NonTraduisible("type %r sans equivalent v2" % (t,))


def depuis_v2(m, hote=None):
    """Message v2 recu (deja valide par nearfield_pont) -> message v1 pour le code du pont.
    hote : adresse de l'ordonnateur de la session (flux radio du TETRA et du numerique)."""
    t = m["type"]
    if t == "travail_annonce":
        return {"type": "travail", "travail": m["travail"]}
    if t == "tickets":
        return {"type": "tickets", "ids": m["ids"]}
    if t == "dire":
        d = m.get("debit")
        return {"type": "dire", "id": m["id"], "texte": m["texte"], "voix": m.get("voix"),
                "debit": "%+d%%" % d if d is not None else "+0%"}
    if t in ("tetra_demande", "numerique_demande"):
        if not hote:
            raise NonTraduisible("adresse de l'ordonnateur inconnue")
        r = {"type": "tetra" if t == "tetra_demande" else "numerique", "id": m["id"], "host": hote,
             "port": m["port"], "freq_hz": m["freq_hz"], "gain": _nombre(m["gain"]), "secondes": m["secondes"],
             "max_secondes": m["max_secondes"], "ppm": _nombre(m["ppm"])}
        if t == "numerique_demande":
            r.update(mode=m["mode"], banc=bool(m.get("banc")), modes=m.get("modes"))
        return r
    if t == "battement":
        return _sans_none({"type": "battement", "capacites": m["capacites"], "voix_mac": m.get("voix") or [],
                           "etat": m.get("session"), "puissance": m.get("puissance"), "reseau": m.get("reseau")})
    if t == "travail_pris":
        return {"type": "pris", "travail": m["travail"]}
    if t == "travail_fini":
        return {"type": "fini", "travail": m["travail"]}
    if t == "morceau":
        piste = m.get("piste") or ""
        if not piste_valide(piste):
            raise NonTraduisible("piste %r" % (piste,))
        r = {"type": piste, "reponse_a": m["flux"], "n": m["n"], "pcm": m["donnees"]}
        if m.get("taux"):
            r["taux"] = m["taux"]
        return r
    if t == "dire_fin":
        return _sans_none({"type": "son_fin", "reponse_a": m["reponse_a"], "ok": m["ok"], "taux": m.get("taux"),
                           "erreur": m.get("erreur")})
    if t in ("tetra_resultat", "numerique_resultat"):
        r = {k: v for k, v in m.items() if k != "id"}
        if t == "numerique_resultat":
            r.setdefault("banc", [])
            r.setdefault("rendus", [])
            for k in ("voix_rms", "audio_rms", "duree"):
                if k in r:
                    r[k] = _nombre(r[k])
        elif "duree" in r:
            r["duree"] = _nombre(r["duree"])
        return r
    raise NonTraduisible("type %r traite par le pont v2 lui-meme" % (t,))
