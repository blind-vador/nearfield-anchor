"""Journal des propositions (2026-10-09) : suggestions de reglages que la passe
d'analyse du Mac mini (classification + debruitage/isolation de voix, bien plus
costaud que le RNNoise du Pi) depose pour revue HUMAINE le matin.

Principe (garde-fou anti-hallucination) : le Mac ne remplace JAMAIS l'audio par
du son genere. Il a le droit de "creuser fort" (debruitage agressif) pour faire
apparaitre une voix, MAIS c'est l'utilisateur qui tranche a l'oreille dans le
dialogue "journal proposition" (console/proposition_cmd.py) : c = confirme (le
reglage est applique au Pi), r = refuse. Une proposition porte un extrait AVANT
et, si l'IA a nettoye, un extrait APRES, pour comparer.

Chaque proposition :
  id, le (epoch), origine ("ia" | "metadonnees" | "note"), confiance 0..1,
  resume (texte pret a dire -- donnee, pas une cle de phrase),
  avant / apres (chemins d'extraits audio, optionnels),
  action : dict applique a la confirmation. Types geres cote Pi :
    {"kind": "parasite_off", "cle": [label, bucket]}   -> desactive un filtre
    {"kind": "interet", "categorie": c, "niveau": n}    -> change un interet
    {"kind": "filtre", ...}                             -> (a venir) reglage de
        nettoyage ; non applique automatiquement pour l'instant (note "manuel").
  etat : "attente" | "confirme" | "refuse".
"""
import fcntl
import json
import os
import time

NAS_ROOT = "/media/nas/nd1"
STORE_NAS = f"{NAS_ROOT}/server/radio_log/propositions.json"
STORE_LOCAL = "/home/vador/autopilot/pi_console/propositions.json"
LOCK_PATH = "/tmp/propositions.lock"
MAX_ENTRIES = 300


def _path():
    return STORE_NAS if os.path.ismount(NAS_ROOT) else STORE_LOCAL


def _read():
    try:
        with open(_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _with_lock(fn):
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read()
        result = fn(data)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _path())
        return result


def add(resume, action, origine="ia", confiance=0.5, avant=None, apres=None):
    """Depose une proposition (appele par la passe Mac via le bridge, ou en
    local). Retourne l'id attribue."""
    pid = f"{int(time.time()*1000)}"

    def put(data):
        data.append({"id": pid, "le": time.time(), "origine": origine,
                     "confiance": confiance, "resume": resume, "avant": avant,
                     "apres": apres, "action": action or {}, "etat": "attente"})
        del data[:-MAX_ENTRIES]
    _with_lock(put)
    return pid


def entries(include_traitees=False):
    """A traiter (etat "attente") d'abord, de la plus confiante a la moins ;
    puis, si include_traitees, les deja vues."""
    data = _read()
    attente = sorted([p for p in data if p.get("etat") == "attente"],
                     key=lambda p: -p.get("confiance", 0))
    if not include_traitees:
        return attente
    return attente + [p for p in data if p.get("etat") != "attente"]


def set_etat(pid, etat):
    def apply(data):
        for p in data:
            if p.get("id") == pid:
                p["etat"] = etat
    _with_lock(apply)


def apply_action(prop):
    """Applique l'action d'une proposition confirmee. Retourne (ok, detail)
    ou detail est un court texte (donnee) decrivant ce qui a ete fait, ou la
    raison de l'echec."""
    action = prop.get("action") or {}
    kind = action.get("kind")
    try:
        if kind == "parasite_off":
            import noise_signatures
            noise_signatures.set_group_active(action["cle"], False)
            mhz = f"{action.get('freq_hz', 0) / 1e6:.4f}".rstrip("0").rstrip(".").replace(".", ",")
            return True, f"filtre {mhz} mégahertz désactivé"
        if kind == "interet":
            import interets
            niveaux = dict(interets.levels(fresh=True))
            niveaux[action["categorie"]] = action["niveau"]
            if not interets.save(niveaux):
                return False, "NAS inaccessible"
            return True, f"{action['categorie']} réglé sur {action['niveau']}"
        # Types non appliques automatiquement (reglages de nettoyage fins) :
        # confirmes mais a appliquer plus tard, cote Pi.
        return False, "à appliquer manuellement"
    except Exception as exc:
        return False, f"échec : {exc}"
