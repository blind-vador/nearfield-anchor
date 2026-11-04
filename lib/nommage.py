"""Propositions de nom venues de Whisper. Rien n'est applique tout seul.

Le journal « nommage » les dit, avec un shim. C pose le nom. R le refuse :
le meme nom ne sera plus propose pour cette frequence.
"""
import json
import os
import time

import chemins

PATH = chemins.base("nommage.json")
_MODES = {
    "nfm": "FM étroite", "wfm": "FM large", "am": "AM",
    "usb": "BLU haute", "lsb": "BLU basse",
}


def _lire():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("attente", [])
            data.setdefault("rejets", [])
            return data
    except (OSError, ValueError):
        pass
    return {"attente": [], "rejets": []}


def _ecrire(data):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, PATH)


def _cle(freq_hz):
    return str(int(round(float(freq_hz) / 12500.0) * 12500))


def _refus(freq_hz, nom):
    return _cle(freq_hz) + "|" + " ".join((nom or "").casefold().split())


def modulation(mode):
    mode = " ".join((mode or "").split()).strip(" .")
    return _MODES.get(mode.lower(), mode)


def bande_dite(bande, label):
    texte = " ".join((label or "").split()).strip(" .")
    if texte:
        return texte
    return " ".join((bande or "").replace("_", " ").split())


def en_attente():
    return list(_lire().get("attente") or [])


def deja_refuse(freq_hz, nom):
    if not freq_hz or not nom:
        return True
    return _refus(freq_hz, nom) in (_lire().get("rejets") or [])


def deposer(freq_hz, mode, bande, label, nom, contexte, machine, fichier=""):
    """Range une proposition. N'ecrase pas un nom deja pose, ni un refus."""
    if not freq_hz or not nom:
        return False
    if deja_refuse(freq_hz, nom):
        print(f"[nommage] {nom} deja refuse sur {freq_hz}")
        return False
    try:
        import stations
        if mode and stations.name_near(freq_hz, mode):
            print(f"[nommage] {freq_hz} a deja un nom")
            return False
    except Exception as exc:
        print(f"[nommage] station illisible : {exc!r}")
    data = _lire()
    cle = _cle(freq_hz)
    for item in data["attente"]:
        if item.get("cle") == cle and " ".join((item.get("nom") or "").casefold().split()) == " ".join(nom.casefold().split()):
            return False
    data["attente"].append({
        "id": f"{int(time.time())}-{len(data['attente'])}",
        "cle": cle,
        "freq_hz": freq_hz,
        "mode": mode or "",
        "bande": bande_dite(bande, label),
        "modulation": modulation(mode),
        "nom": nom,
        "contexte": (contexte or "")[:400],
        "machine": machine or "compagne",
        "fichier": fichier or "",
        "le": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    _ecrire(data)
    print(f"[nommage] {nom} en attente sur {cle}")
    return True


def _poser(item):
    """Applique le nom. Rend False si un nom est arrive entre-temps."""
    freq, mode, nom = item.get("freq_hz"), item.get("mode"), item.get("nom")
    try:
        import stations
        if mode and stations.name_near(freq, mode):
            return False
        if mode:
            stations.proposer_nom(freq, mode, nom, item.get("contexte") or "")
    except Exception as exc:
        print(f"[nommage] pose : {exc!r}")
    try:
        import noms_whisper
        canaux = noms_whisper._lire(noms_whisper.NOMS)
        entree = dict(canaux.get(item.get("cle")) or {})
        if not entree.get("nom"):
            entree["nom"] = nom
        if item.get("contexte"):
            entree["contexte"] = item["contexte"]
        entree["le"] = time.strftime("%Y-%m-%d")
        if mode:
            entree["mode"] = mode
        canaux[item.get("cle")] = entree
        noms_whisper._ecrire(noms_whisper.NOMS, canaux)
    except Exception as exc:
        print(f"[nommage] memoire : {exc!r}")
    return True


def _retirer(data, item):
    data["attente"] = [a for a in data["attente"] if a.get("id") != item.get("id")]


def confirmer(item):
    data = _lire()
    _retirer(data, item)
    _ecrire(data)
    return _poser(item)


def refuser(item):
    data = _lire()
    _retirer(data, item)
    marque = _refus(item.get("freq_hz"), item.get("nom"))
    if marque not in data["rejets"]:
        data["rejets"].append(marque)
    _ecrire(data)
