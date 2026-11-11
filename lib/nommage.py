"""Propositions de nom venues de Whisper. Rien n'est applique tout seul.

Le journal « nommage » les dit, avec un shim. C pose le nom. R le refuse :
le meme nom ne sera plus propose pour cette frequence.

2026-11-11 (plan phase 5) : une seule proposition en attente par frequence, la
plus recente remplace la precedente (18 noms s'empilaient sur 438,525, un par
indicatif entendu) ; chaque proposition garde son « pourquoi » (deux mots), ce que
l'IA a compris (E dans le journal des noms) et, pour une station deja nommee, la
mise a jour proposee (C renomme alors la station).
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


def deposer(freq_hz, mode, bande, label, nom, contexte, machine, fichier="", pourquoi=None, explication=None,
            mise_a_jour=None, ancien_nom=None):
    """Range une proposition, a la place de celle qui attendait deja pour cette
    frequence. Un refus n'est jamais repropose ; un nom deja pose n'est change
    que par une mise a jour (ancien_nom)."""
    if not freq_hz or not nom:
        return False
    if deja_refuse(freq_hz, nom):
        print(f"[nommage] {nom} deja refuse sur {freq_hz}")
        return False
    try:
        import stations
        pose = stations.name_near(freq_hz, mode) if mode else None
        if pose and not ancien_nom:
            print(f"[nommage] {freq_hz} a deja un nom")
            return False
        if pose and " ".join(pose.casefold().split()) == " ".join(nom.casefold().split()):
            return False
    except Exception as exc:
        print(f"[nommage] station illisible : {exc!r}")
    data = _lire()
    cle = _cle(freq_hz)
    data["attente"] = [a for a in data["attente"] if a.get("cle") != cle]
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
        "pourquoi": pourquoi or "",
        "explication": (explication or "")[:600],
        "mise_a_jour": (mise_a_jour or "")[:400],
        "ancien_nom": ancien_nom or "",
    })
    _ecrire(data)
    print(f"[nommage] {nom} en attente sur {cle}")
    return True


def _poser(item):
    """Applique le nom. Rend False si un nom est arrive entre-temps."""
    freq, mode, nom = item.get("freq_hz"), item.get("mode"), item.get("nom")
    try:
        import stations
        pose = stations.name_near(freq, mode) if mode else None
        if pose and not item.get("ancien_nom"):
            return False
        if pose:
            stations.rename(freq, mode, nom)     # mise a jour acceptee
        elif mode:
            stations.proposer_nom(freq, mode, nom, item.get("contexte") or "")
    except Exception as exc:
        print(f"[nommage] pose : {exc!r}")
    try:
        import noms_whisper
        canaux = noms_whisper._lire(noms_whisper.NOMS)
        entree = dict(canaux.get(item.get("cle")) or {})
        if not entree.get("nom") or item.get("ancien_nom"):
            entree["nom"] = nom
            entree["pourquoi"] = item.get("pourquoi") or entree.get("pourquoi") or ""
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
