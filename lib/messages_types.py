"""Types de messages techniques captes (2026-11-05).

Un scanner distribue tombe sur des messages que personne n'a prevus. La premiere
fois qu'un TYPE arrive, l'IA dit ce que c'est, une fois pour toutes ; touche I le
redit au journal. L'explication porte sur le type, pas sur le message : le contenu
d'un message prive (pager, SMS) ne part jamais chez Gemini, et les chiffres de
l'exemple sont masques pour les autres.

Fleche gauche sur un de ces messages apprend comme pour un son : ce type, sur cette
frequence, n'est plus lu (rien n'est perdu : il reste archive, et "filtres" le rend).
L'entree vit avec les empreintes de parasites, juge = le type.

La fiche d'une annonce (lib/prise_vocale.py) porte :
  type_message  la cle rendue par fiches()
  frequence_hz  sans elle, rien a apprendre
  prive         True : l'exemple n'est pas envoye a l'IA
  urgence       True : un rejet n'empeche jamais la prochaine lecture (detresse)
"""
import fcntl
import json
import os
import re
import time

import chemins
import noise_signatures as ns

PATH = chemins.base("messages_types.json")
ECRITURE = PATH + ".verrou"
IA_PAR_HEURE = 20
EXEMPLE_MAX = 240
_DEMANDES = []

_ENTETE = re.compile(r"^([A-Z][A-Z0-9_]{1,14}):\s")

PROMPT = """Un scanner radio d'un radioamateur aveugle, en {pays}, vient de capter pour la première fois ce type de message.
Protocole ou décodeur : {protocole}
Type : {type}
{exemple}
Explique en {langue}, en deux ou trois phrases à lire à voix haute : à quoi sert ce type de message, qui s'en sert, et s'il est
normal de le capter ici. S'il n'a pas de raison d'exister dans ce pays, ou si ce décodeur est connu pour sortir de fausses
trames du bruit, dis-le simplement. Si tu ne connais pas ce format, dis que tu ne le connais pas, n'invente rien. Les chiffres
masqués par # ne comptent pas. Pas de markdown, pas de liste, ne répète pas l'exemple."""


def cle_de(texte, decoupe=False):
    """Le type d'un texte decode. "FMS: d8d5..." -> "FMS". decoupe : "FLEETSYNC, flotte 1,
    poste 2 vers 3, position GPS" -> "FLEETSYNC, flotte, poste vers, position GPS" : un type
    par structure, sans ses nombres."""
    texte = (texte or "").strip()
    m = _ENTETE.match(texte)
    if m and not decoupe:
        return m.group(1)
    if decoupe:
        return re.sub(r"\s+", " ", re.sub(r"\b[\d\-]*\d[\d\-]*\b", " ", texte)).replace(" ,", ",").strip(" ,:")[:80]
    return None


def fiche(protocole, texte, freq_hz, decoupe=False, **autres):
    """La fiche d'une annonce de message, avec ce qu'il faut pour apprendre."""
    cle = cle_de(texte, decoupe) or protocole
    return dict({"protocole": protocole, "type_message": cle, "frequence_hz": freq_hz, "texte_original": texte}, **autres)


def _lire():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _noter(cle, **champs):
    """Plusieurs services annoncent : on relit sous verrou avant d'ecrire."""
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(ECRITURE, "w") as verrou:
        fcntl.flock(verrou, fcntl.LOCK_EX)
        data = _lire()
        e = data.setdefault(cle, {"premier": time.time(), "vu": 0})
        e.update(champs)
        tmp = PATH + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PATH)


def explication_connue(cle):
    return (_lire().get(cle) or {}).get("explication")


def _quota():
    maintenant = time.time()
    _DEMANDES[:] = [t for t in _DEMANDES if maintenant - t < 3600]
    if len(_DEMANDES) >= IA_PAR_HEURE:
        return False
    _DEMANDES.append(maintenant)
    return True


def _masquer(texte):
    return re.sub(r"\d", "#", (texte or "")[:EXEMPLE_MAX])


def _demander(cle, fiche_):
    import avions_info
    import gemini
    import reglages
    exemple = "" if fiche_.get("prive") else "Exemple, chiffres masqués : " + _masquer(fiche_.get("texte_original"))
    pays = (reglages.get("lieu") or {}).get("pays") or "France"
    texte, probleme = gemini.ask(PROMPT.format(pays=pays, protocole=fiche_.get("protocole") or cle, type=cle,
                                               exemple=exemple, langue=avions_info._langue()), temperature=0.2)
    if probleme or not (texte or "").strip():
        print(f"[messages_types] {cle} : pas d'explication ({probleme})")
        return None
    texte = " ".join(texte.split())
    if texte[-1] not in ".!?…":      # phrase coupee : on redemandera plutot que de la garder pour toujours
        print(f"[messages_types] {cle} : reponse coupee, pas retenue")
        return None
    return texte


def premiere_fois(fiche_):
    """L'explication a ajouter a l'annonce si ce type n'en a jamais eu, sinon None. Sans
    reponse de l'IA, rien n'est retenu : elle sera redemandee a la prochaine occurrence."""
    cle = (fiche_ or {}).get("type_message")
    if not cle:
        return None
    connu = _lire().get(cle) or {}
    _noter(cle, vu=int(connu.get("vu") or 0) + 1, dernier=time.time())
    if connu.get("explication") or not _quota():
        return None
    try:
        texte = _demander(cle, fiche_)
    except Exception as exc:     # une annonce sans explication reste une annonce
        print(f"[messages_types] {cle} : {exc!r}")
        return None
    if texte:
        _noter(cle, explication=texte)
    return texte


# --- ce que l'humain a dit ------------------------------------------------------------------

def ecarte(fiche_):
    """Ce type a-t-il ete rejete sur cette frequence ?"""
    fiche_ = fiche_ or {}
    cle, freq = fiche_.get("type_message"), fiche_.get("frequence_hz")
    if not cle or not freq or fiche_.get("urgence"):
        return False
    return any(e.get("type_message") and ns._concerne(e, freq, cle) for e in ns.load_signatures())


def rejeter(fiche_, label=""):
    """Fleche gauche : ce type, sur cette frequence seulement. Rend le nom dit, ou None."""
    cle, freq = (fiche_ or {}).get("type_message"), (fiche_ or {}).get("frequence_hz")
    if not cle or not freq:
        return None
    if not ecarte(dict(fiche_, urgence=False)):
        ns.add_message_parasite(freq, label or fiche_.get("protocole") or "", cle)
    return cle
