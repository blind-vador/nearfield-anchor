"""Protocole de confiance v2 cote Pi (ordonnateur), en PILOTE a cote de la v1.

Specification : Prismaforge/specs/protocole-v2.md ; crypto : module Rust
nearfield_pont (pont/python). Tant que ce module n'est pas installe pour le
python des services, tout ici est INACTIF et la v1 tourne seule, inchangee.

Ce que fait le pilote (2026-11-15) :
  1. Migration sans nouvel appairage, par le canal v1 deja authentifie : le Pi
     envoie son objet "migration" (nouvelle cle v2 contresignee par sa cle v1),
     la machine repond avec le sien ; le Pi le verifie avec la cle v1 de la
     fiche de confiance, le consigne (une cle v1 ne migre qu'une fois) et
     delivre un certificat v2 de 12 h.
  2. Ecoute v2 (Noise XX, port PORT) : la machine y ouvre une session et y
     envoie ses battements. Le trafic reel (travaux, voix, TETRA...) reste sur
     la v1 tant que la liste fermee des messages v2 ne le couvre pas.

Etat local (carte SD, comme les fiches de confiance) : LOCAL_DIR/v2/
  identite.pem      cle d'identite v2 (logicielle sur le Pi : ordonnateur
                    TOLERE, annonce au demarrage)
  migrations.json   cles v1 deja migrees -> identifiant v2
  nonces.json       registre anti-rejeu
  serie.json        dernier numero de serie de certificat
"""
import json
import os
import threading
import time
import uuid

import compagnon

try:
    import nearfield_pont as np_
except ImportError:  # module non installe : v2 inactive
    np_ = None

PORT = 47812
V2_DIR = os.path.join(compagnon.LOCAL_DIR, "v2")
PRESENCE_V2 = "/tmp/compagnons_v2.json"
DELAI_POIGNEE_SEC = 10
RENOUVELLEMENT_SEC = 6 * 3600  # le certificat du Pi (12 h) est refait a mi-vie
TOLERANCE_LOGICIELLE = True    # pas de puce sur le Pi : ordonnateur logiciel tolere

_verrou = threading.Lock()
_moi = {}            # transport et certificat du Pi
_presence_v2 = {}    # id v1 de la machine -> {depuis, vu, battements}


def actif():
    return np_ is not None


def _chemin(nom):
    return os.path.join(V2_DIR, nom)


def coffre():
    os.makedirs(V2_DIR, mode=0o700, exist_ok=True)
    return np_.Coffre.charger_ou_creer(_chemin("identite.pem"), "p256")


def parc():
    """Identifiant du parc v2 : celui de la cle d'identite v2 du Pi."""
    return coffre().id


def _serie_suivante():
    chemin = _chemin("serie.json")
    try:
        with open(chemin) as f:
            n = int(json.load(f).get("serie", 0)) + 1
    except (OSError, ValueError):
        n = 1
    tmp = chemin + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"serie": n}, f)
    os.replace(tmp, chemin)
    return n


def objet_migration():
    """Annonce de la cle v2 du Pi, contresignee par sa cle v1 (compagnon.KEY_PATH)."""
    ancien = np_.Coffre.ouvrir(compagnon.KEY_PATH)
    return np_.preparer_migration(ancien, coffre(), parc(), compagnon.pi_id())


def recevoir_migration(cid, card, objet):
    """Migration d'une machine appairee en v1 : verifiee avec la cle v1 de SA fiche.
    Consigne la migration et rend son certificat v2 (leve ErreurPont si refus)."""
    p = parc()
    with _verrou:
        nonces = np_.RegistreNonces.charger(_chemin("nonces.json"))
        m = np_.verifier_migration(objet, p, {"alg": "ed25519", "pub": card["cle"]}, nonces)
        migrations = np_.RegistreMigrations.charger(_chemin("migrations.json"))
        migrations.consigner(m)  # refuse une autre identite pour la meme cle v1
        migrations.enregistrer(_chemin("migrations.json"))
        nonces.enregistrer(_chemin("nonces.json"))
        serie = _serie_suivante()
    if not m.get("transport"):
        raise np_.MigrationRefusee("cle de transport absente")
    import base64
    capacites = [c for c in (card.get("capacites") or []) if isinstance(c, str) and 0 < len(c) <= 128][:64]
    cert = np_.emettre_certificat(coffre(), p, m["nom"], m["identite"], m["niveau"],
                                  base64.b64decode(m["transport"]), ["travailleur"], capacites, serie,
                                  TOLERANCE_LOGICIELLE)
    compagnon.update_card(cid, v2={"id": np_.identifiant(m["identite"]), "identite": m["identite"],
                                   "niveau": m["niveau"], "migre": time.time()})
    return cert


# --- ecoute v2 ------------------------------------------------------------------------

def _mon_certificat():
    """Cle de transport et certificat d'ordonnateur du Pi, refaits a mi-vie."""
    with _verrou:
        if not _moi or time.time() - _moi["t"] > RENOUVELLEMENT_SEC:
            transport = np_.CleTransport()
            c = coffre()
            cert = np_.emettre_certificat(c, parc(), compagnon.pi_id(), c.cle_publique(), c.niveau,
                                          transport.public, ["ordonnateur"], [], _serie_suivante(),
                                          TOLERANCE_LOGICIELLE)
            _moi.update(transport=transport, certificat=cert, t=time.time())
        return _moi["transport"], _moi["certificat"]


def _machine_de(sujet):
    """(id v1, fiche) de la machine migree vers cet identifiant v2, ou (None, None)."""
    for cid, card in compagnon.trusted().items():
        if (card.get("v2") or {}).get("id") == sujet:
            return cid, card
    return None, None


def _ecrire_presence():
    tmp = PRESENCE_V2 + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_presence_v2, f)
    os.replace(tmp, PRESENCE_V2)


def presence():
    """{id v1: {depuis, vu, battements}} des machines connectees en v2."""
    try:
        with open(PRESENCE_V2) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _servir(entrante):
    cid = None
    try:
        transport, cert = _mon_certificat()
        session = entrante.repondre(parc(), transport, cert, coffre().cle_publique(), np_.Revocations(),
                                    TOLERANCE_LOGICIELLE, None, DELAI_POIGNEE_SEC)
        pair = session.pair
        cid, card = _machine_de(pair["sujet"])
        if not card:  # certificat valide mais machine retiree du pont depuis
            print(f"[pont v2] {pair.get('nom')} n'est plus de confiance : refusee", flush=True)
            session.fermer()
            return
        print(f"[pont v2] {pair['nom']} connectee en v2 depuis {entrante.adresse}", flush=True)
        with _verrou:
            _presence_v2[cid] = {"depuis": time.time(), "vu": time.time(), "battements": 0}
            _ecrire_presence()
        while True:
            msg = session.recevoir()
            if msg is None:
                break
            if not compagnon.trusted().get(cid):
                break  # pont retire pendant la session
            if msg["type"] == "battement":
                with _verrou:
                    _presence_v2[cid]["vu"] = time.time()
                    _presence_v2[cid]["battements"] += 1
                    _ecrire_presence()
            elif msg["type"] == "certificat_demande":
                import base64
                m = {"nom": card.get("nom") or cid, "identite": card["v2"]["identite"],
                     "niveau": card["v2"]["niveau"]}
                nouveau = np_.emettre_certificat(coffre(), parc(), m["nom"], m["identite"], m["niveau"],
                                                 base64.b64decode(msg["transport"]), ["travailleur"],
                                                 [c for c in card.get("capacites") or [] if c][:64],
                                                 _serie_suivante(), TOLERANCE_LOGICIELLE)
                session.envoyer({"type": "certificat", "id": uuid.uuid4().hex[:16], "objet": nouveau})
        session.fermer()
    except np_.ErreurPont as exc:
        print(f"[pont v2] {entrante.adresse} refusee : {exc}", flush=True)
    except Exception as exc:
        print(f"[pont v2] {entrante.adresse} : {exc!r}", flush=True)
    finally:
        if cid:
            with _verrou:
                _presence_v2.pop(cid, None)
                _ecrire_presence()


def demarrer(adresse="0.0.0.0", port=PORT):
    """Ecoute v2 en tache de fond ; rend le port reel (port 0 : port libre, pour les essais)."""
    ecouteur = np_.Ecouteur(f"{adresse}:{port}")
    print("[pont v2] ecoute sur le port %d ; ordonnateur LOGICIEL tolere (pas de puce), parc %s"
          % (ecouteur.port, parc()), flush=True)
    with _verrou:
        _presence_v2.clear()
        _ecrire_presence()

    def boucle():
        while True:
            try:
                entrante = ecouteur.accepter()
            except Exception as exc:
                print(f"[pont v2] ecoute : {exc!r}", flush=True)
                time.sleep(1)
                continue
            threading.Thread(target=_servir, args=(entrante,), daemon=True).start()

    threading.Thread(target=boucle, daemon=True).start()
    return ecouteur.port
