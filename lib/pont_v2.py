"""Protocole de confiance v2 cote Pi (ordonnateur), en PILOTE a cote de la v1.

Specification : Prismaforge/specs/protocole-v2.md ; crypto : module Rust
nearfield_pont (pont/python). Tant que ce module n'est pas installe pour le
python des services, tout ici est INACTIF et la v1 tourne seule, inchangee.

Ce que fait la v2 (2026-11-15) :
  1. Migration sans nouvel appairage, par le canal v1 deja authentifie : le Pi
     envoie son objet "migration" (nouvelle cle v2 contresignee par sa cle v1),
     la machine repond avec le sien ; le Pi le verifie avec la cle v1 de la
     fiche de confiance, le consigne (une cle v1 ne migre qu'une fois) et
     delivre un certificat v2 de 12 h.
  2. Ecoute v2 (Noise XX, port PORT) : la machine y ouvre une session chiffree.
     Tout le trafic reel y passe (avis de travail, tickets, voix macOS, TETRA,
     numerique, battements complets) : bridge/pont_v2_traduction.py traduit
     chaque message v1 vers la liste fermee v2 et retour, nearfield_pont valide.
     La v1 reste ouverte a cote : amorce de la migration, battements de
     presence, et secours pour un message que la v2 refuse (journalise).

Etat local (carte SD, comme les fiches de confiance) : LOCAL_DIR/v2/
  identite.pem      cle d'identite v2 (logicielle sur le Pi : ordonnateur
                    TOLERE, annonce au demarrage)
  identite.puce     ... ou poignee de la cle gardee par le TPM (machine Linux avec
                    TPM 2.0 : tools/pf-tpm ; choisie a la creation, jamais de repli)
  humains.json      cles humaines connues (appareils a biometrie), lot 2 §1
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
import pont_v2_traduction as trad

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
GRACE_SEC = 30 * 86400         # certificat expire (machine en veille) : session de renouvellement seulement

_verrou = threading.Lock()
_verrou_coffre = threading.Lock()
_moi = {}            # transport et certificat du Pi
_presence_v2 = {}    # id v1 de la machine -> {depuis, vu, battements}
_sessions = {}       # id v1 de la machine -> session v2 ouverte
_recu = [None]       # rappel du pont (compagnon_serveur) : (cid, fiche, message v1)
_evenement = [None]  # rappel du pont : ("arrivee" | "depart", cid, fiche, adresse IP)
_interface = [None]  # rappel du point d'acces (lib/extensions.py) : (cid, fiche, interface_requete)
MORCEAU_INTERFACE = 36_000  # octets bruts par morceau (48 000 en base64, messages v2)
_refus_vus = set()   # types deja journalises comme passes par la v1


def actif():
    return np_ is not None


def _chemin(nom):
    return os.path.join(V2_DIR, nom)


def programme_puce():
    """Programme de la puce de cette machine, ou None (repli logiciel).
    NEARFIELD_PUCE=non : jamais de puce ; NEARFIELD_PUCE=<chemin> : ce programme."""
    choix = os.environ.get("NEARFIELD_PUCE", "")
    if choix.lower() in ("non", "0", "logiciel"):
        return None
    if choix:
        return choix
    if os.path.exists("/dev/tpmrm0"):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "pf-tpm")
        if os.path.isfile(p):
            return p
    return None


_coffre = [None]


def coffre():
    """Cle d'identite du Pi : dans le TPM s'il y en a un a la creation, sinon logicielle."""
    with _verrou_coffre:
        if _coffre[0] is None or _coffre[0][0] != V2_DIR:
            os.makedirs(V2_DIR, mode=0o700, exist_ok=True)
            _coffre[0] = (V2_DIR, np_.Coffre.identite(V2_DIR, programme_puce()))
        return _coffre[0][1]


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
    """(id v1, fiche) de la machine migree vers cet identifiant v2, ou (None, None).
    Apres une rotation, l'identite precedente vaut encore jusqu'a la premiere session
    avec la nouvelle (la machine a pu tomber entre les deux)."""
    for cid, card in compagnon.trusted().items():
        v2 = card.get("v2") or {}
        if v2.get("id") == sujet or (v2.get("precedente") or {}).get("id") == sujet:
            return cid, card
    return None, None


def _ecrire_presence():
    tmp = PRESENCE_V2 + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_presence_v2, f)
    os.replace(tmp, PRESENCE_V2)


def envoyer(cid, msg):
    """Envoie un message v1 par la session v2 de cette machine, traduit et valide.
    False (rien envoye) : pas de session, ou message refuse par la liste fermee ;
    l'appelant passe alors par la v1."""
    session = _sessions.get(cid)
    if session is None:
        return False
    try:
        session.envoyer(trad.vers_v2(msg))
        return True
    except (trad.NonTraduisible, np_.MessageRefuse) as exc:
        if msg.get("type") not in _refus_vus:
            _refus_vus.add(msg.get("type"))
            print(f"[pont v2] {msg.get('type')} passe par la v1 : {exc}", flush=True)
        return False
    except np_.ErreurPont as exc:
        print(f"[pont v2] envoi a {cid} impossible ({exc}) : la v1 prend le relais", flush=True)
        return False


def envoyer_brut(cid, msg):
    """Envoie un message v2 (deja dans la liste fermee) a cette machine ; False sans session."""
    session = _sessions.get(cid)
    if session is None:
        return False
    try:
        session.envoyer(msg)
        return True
    except np_.ErreurPont as exc:
        print(f"[pont v2] envoi a {cid} impossible : {exc}", flush=True)
        return False


def reponse_interface(reponse_a, statut, type_contenu, corps):
    """Les morceaux interface_reponse d'une reponse (corps : octets)."""
    import base64
    morceaux = [corps[i:i + MORCEAU_INTERFACE] for i in range(0, len(corps), MORCEAU_INTERFACE)] or [b""]
    return [{"type": "interface_reponse", "id": uuid.uuid4().hex[:16], "reponse_a": reponse_a, "n": n,
             "fin": n == len(morceaux) - 1, "statut": int(statut), "type_contenu": type_contenu,
             "corps": base64.b64encode(m).decode()} for n, m in enumerate(morceaux)]


def en_v2(cid):
    return cid in _sessions


def presence():
    """{id v1: {depuis, vu, battements}} des machines connectees en v2."""
    try:
        with open(PRESENCE_V2) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


ROLES_MACHINE = ("travailleur", "interface")


def _roles(card):
    """Role certifie d'une machine : celui de sa fiche (interface : appareil d'interface,
    coquille Prismaforge ; sinon travailleur)."""
    role = card.get("role")
    return [role if role in ROLES_MACHINE else "travailleur"]


def _capacites(card):
    if _roles(card) == ["interface"]:
        return []
    return [c for c in card.get("capacites") or [] if isinstance(c, str) and c][:64]


def _rotation(cid, card, msg, pair):
    """La machine passe a une nouvelle identite (sa puce) : objet "rotation" contresigne
    par l'identite de CETTE session. La fiche garde l'ancienne en "precedente"."""
    p = parc()
    v2 = card["v2"]
    if pair is None or pair["identite"] not in (v2["identite"], (v2.get("precedente") or {}).get("identite")):
        raise np_.CertificatRefuse("rotation hors d'une session de la machine")
    with _verrou:
        nonces = np_.RegistreNonces.charger(_chemin("nonces.json"))
        r = np_.verifier_rotation(msg["preuve"], pair["identite"], pair.get("niveau") or v2["niveau"], p,
                                  msg["transport"], nonces)
        nonces.enregistrer(_chemin("nonces.json"))
        serie = _serie_suivante()
    nouvelle = {"id": np_.identifiant(r["identite"]), "identite": r["identite"], "niveau": r["niveau"],
                "migre": v2.get("migre"), "appaire": v2.get("appaire"), "tourne": time.time(),
                "precedente": {"id": np_.identifiant(pair["identite"]), "identite": pair["identite"]}}
    compagnon.update_card(cid, v2={k: v for k, v in nouvelle.items() if v is not None})
    print(f"[pont v2] {card.get('nom')} : identite passee a {nouvelle['id']} ({r['niveau']})", flush=True)
    return np_.emettre_certificat(coffre(), p, card.get("nom") or nouvelle["id"], r["identite"], r["niveau"],
                                  r["transport"], _roles(card), _capacites(card), serie, TOLERANCE_LOGICIELLE)


def _renouveler(card, msg, cid=None, pair=None):
    """Certificat neuf pour la nouvelle cle de transport de la machine. Exige la preuve
    signee par sa cle d'identite v2 (celle de sa fiche) : une cle de transport volee
    ne suffit pas a se faire certifier (spec §4.1). Une preuve "rotation" fait passer
    la machine a une nouvelle identite (sa puce), sans nouvel appairage."""
    import base64
    if not msg.get("preuve"):
        raise np_.CertificatRefuse("demande de certificat sans preuve signee")
    if np_.type_de_preuve(msg["preuve"]) == "rotation":
        return _rotation(cid, card, msg, pair)
    p = parc()
    with _verrou:
        nonces = np_.RegistreNonces.charger(_chemin("nonces.json"))
        transport = np_.verifier_renouvellement(msg["preuve"], card["v2"]["identite"], p, msg["transport"], nonces)
        nonces.enregistrer(_chemin("nonces.json"))
        serie = _serie_suivante()
    return np_.emettre_certificat(coffre(), p, card.get("nom") or card["v2"]["id"], card["v2"]["identite"],
                                  card["v2"]["niveau"], transport, _roles(card), _capacites(card),
                                  serie, TOLERANCE_LOGICIELLE)


def _servir_perime(session, cid, card, adresse):
    """Certificat expire (machine restee en veille) : rien d'autre qu'un renouvellement."""
    try:
        msg = session.recevoir()
        if msg is None or msg["type"] != "certificat_demande":
            print(f"[pont v2] {card.get('nom')} : certificat expire, seul un renouvellement est permis", flush=True)
            return
        nouveau = _renouveler(card, msg, cid, session.pair)
        session.envoyer({"type": "certificat", "id": uuid.uuid4().hex[:16], "objet": nouveau})
        print(f"[pont v2] {card.get('nom')} : certificat expire renouvele ({adresse})", flush=True)
    except np_.ErreurPont as exc:
        print(f"[pont v2] {card.get('nom')} : renouvellement refuse : {exc}", flush=True)
    finally:
        session.fermer()


def _signaler(kind, cid, card, adresse):
    if _evenement[0]:
        try:
            _evenement[0](kind, cid, card, adresse.rsplit(":", 1)[0].strip("[]"))
        except Exception as exc:
            print(f"[pont v2] {kind} de {cid} : {exc!r}", flush=True)


def _servir(entrante):
    cid = None
    session = None
    arrivee = False
    try:
        transport, cert = _mon_certificat()
        session = entrante.repondre(parc(), transport, cert, coffre().cle_publique(), np_.Revocations(),
                                    TOLERANCE_LOGICIELLE, None, DELAI_POIGNEE_SEC, GRACE_SEC)
        pair = session.pair
        cid, card = _machine_de(pair["sujet"])
        if not card:  # certificat valide mais machine retiree du pont depuis
            print(f"[pont v2] {pair.get('nom')} n'est plus de confiance : refusee", flush=True)
            session.fermer()
            return
        if session.perime:
            _servir_perime(session, cid, card, entrante.adresse)
            cid = None  # session de renouvellement : ni presence ni depart
            return
        if (card["v2"].get("precedente") and pair["sujet"] == card["v2"].get("id")):
            # Premiere session avec la nouvelle identite : l'ancienne ne vaut plus rien.
            v2 = dict(card["v2"])
            v2.pop("precedente", None)
            compagnon.update_card(cid, v2=v2)
            card = compagnon.trusted().get(cid) or card
            print(f"[pont v2] {pair['nom']} : ancienne identite retiree", flush=True)
        print(f"[pont v2] {pair['nom']} connectee en v2 depuis {entrante.adresse}", flush=True)
        with _verrou:
            _presence_v2[cid] = {"depuis": time.time(), "vu": time.time(), "battements": 0}
            _ecrire_presence()
            ancienne = _sessions.get(cid)
            _sessions[cid] = session
        if ancienne is not None:
            ancienne.fermer()
        _signaler("arrivee", cid, card, entrante.adresse)
        arrivee = True
        while True:
            msg = session.recevoir()
            if msg is None:
                break
            card = compagnon.trusted().get(cid)
            if not card:
                break  # pont retire pendant la session
            if msg["type"] == "battement":
                with _verrou:
                    _presence_v2[cid]["vu"] = time.time()
                    _presence_v2[cid]["battements"] += 1
                    _ecrire_presence()
            if msg["type"] == "interface_requete":
                if _roles(card) != ["interface"] or not _interface[0]:
                    envoyer_brut(cid, reponse_interface(msg["id"], 503, "text/plain; charset=utf-8",
                                                        "L'interface de l'IDE n'est pas disponible.".encode())[0])
                else:
                    try:
                        _interface[0](cid, card, msg)
                    except Exception as exc:
                        print(f"[pont v2] {pair['nom']} : requete d'interface : {exc!r}", flush=True)
                continue
            if msg["type"] in ("validation_reponse", "humain_annonce"):
                try:
                    _validation_recue(cid, card, msg) if msg["type"] == "validation_reponse" else _humain_annonce(cid, card, msg)
                except np_.ErreurPont as exc:
                    print(f"[pont v2] {pair['nom']} : {msg['type']} refuse : {exc}", flush=True)
                continue
            if msg["type"] != "certificat_demande":
                try:
                    v1 = trad.depuis_v2(msg)
                except trad.NonTraduisible as exc:
                    print(f"[pont v2] {pair['nom']} : {exc}", flush=True)
                    continue
                if _recu[0]:
                    try:
                        _recu[0](cid, card, v1)
                    except Exception as exc:  # le pont journalise, la session continue
                        print(f"[pont v2] {pair['nom']} : traitement de {msg['type']} : {exc!r}", flush=True)
            else:
                try:
                    nouveau = _renouveler(card, msg, cid, pair)
                except np_.ErreurPont as exc:
                    print(f"[pont v2] {pair['nom']} : renouvellement refuse : {exc}", flush=True)
                    continue
                session.envoyer({"type": "certificat", "id": uuid.uuid4().hex[:16], "objet": nouveau})
                print(f"[pont v2] {pair['nom']} : certificat renouvele", flush=True)
        session.fermer()
    except np_.ErreurPont as exc:
        print(f"[pont v2] {entrante.adresse} refusee : {exc}", flush=True)
    except Exception as exc:
        print(f"[pont v2] {entrante.adresse} : {exc!r}", flush=True)
    finally:
        if cid:
            parti = False
            with _verrou:
                if _sessions.get(cid) is session:
                    _sessions.pop(cid, None)
                    _presence_v2.pop(cid, None)
                    _ecrire_presence()
                    parti = True
            if parti and arrivee:
                _signaler("depart", cid, compagnon.trusted().get(cid) or {}, entrante.adresse)


# --- cles de reception (spec §6.2) : chiffrer pour les machines du parc --------------------

def _identites_du_parc():
    """{id v2: identite} que l'on croit : nos machines migrees, et nous-memes."""
    ids = {c["v2"]["id"]: c["v2"]["identite"] for c in compagnon.trusted().values() if c.get("v2")}
    moi = coffre()
    ids[moi.id] = moi.cle_publique()
    return ids


def publier_reception(forcer=False):
    """Publie notre cle de reception de la semaine sur le NAS (au plus une fois par jour)."""
    import cles_reception
    return cles_reception.publier(V2_DIR, coffre(), parc(), compagnon.BRIDGE_DIR, forcer)


def sceller_pour_le_parc(donnees, contexte):
    """Enveloppe lisible par toutes les machines du parc qui ont publie leur cle (et nous)."""
    import cles_reception
    return cles_reception.sceller(compagnon.BRIDGE_DIR, parc(), _identites_du_parc(), coffre().id, contexte, donnees)


def ouvrir_enveloppe(enveloppe, de):
    import cles_reception
    return cles_reception.ouvrir(V2_DIR, coffre().id, de, enveloppe)


# --- cles humaines et validations (lot 2 §1) --------------------------------------------

VALIDATION_SEC = 240        # temps laisse a l'humain pour lire et valider
_validations = {}           # id de la demande -> {"cid", "evenement", "reponse"}


def humains():
    """{id: {"identite", "machine", "nom", "ajoute"}} des cles humaines connues."""
    try:
        with open(_chemin("humains.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _humain_annonce(cid, card, msg):
    """Un appareil de confiance (session v2 authentifiee) annonce sa cle humaine : objet
    "humain" signe par elle. Une seule cle humaine par machine (la nouvelle remplace)."""
    objet = msg.get("objet") or {}
    charge = objet.get("charge") or {}
    identite = charge.get("identite")
    if not isinstance(identite, dict) or identite == card["v2"]["identite"]:
        raise np_.ErreurFormat("cle humaine absente ou egale a l'identite de la machine")
    with _verrou:
        nonces = np_.RegistreNonces.charger(_chemin("nonces.json"))
        np_.verifier_objet(objet, identite, "humain", parc(), nonces)
        nonces.enregistrer(_chemin("nonces.json"))
        h = {k: v for k, v in humains().items() if v.get("machine") != cid}
        hid = np_.identifiant(identite)
        h[hid] = {"identite": identite, "machine": cid, "nom": card.get("nom") or cid, "ajoute": time.time()}
        tmp = _chemin("humains.json.tmp")
        with open(tmp, "w") as f:
            json.dump(h, f, indent=1)
        os.replace(tmp, _chemin("humains.json"))
    print(f"[pont v2] {card.get('nom')} : cle humaine {hid} enregistree", flush=True)


def _validation_recue(cid, card, msg):
    attente = _validations.get(msg.get("reponse_a"))
    if not attente or attente["cid"] != cid:
        raise np_.ErreurFormat("reponse a une demande inconnue")
    attente["reponse"] = msg
    attente["evenement"].set()


def demander_validation(sujet, charge, delai=VALIDATION_SEC):
    """Fait valider `charge` par un humain, sur un appareil connecte qui porte une cle
    humaine. Rend l'objet "validation" verifie (decision "accepte" ou "refuse").
    Leve ErreurPont : pas d'appareil, annule, delai depasse, validation invalide."""
    objet, h = _demander(sujet, charge, delai)
    np_.verifier_validation(objet, h["identite"], parc(), charge)
    return objet


def demander_arbitrage(charge, options, delai=VALIDATION_SEC):
    """L'utilisateur choisit une des `options` ; il signe la charge AVEC son choix
    (champ "choix"). Rend (validation, choix) apres verification."""
    objet, h = _demander("arbitrage", dict(charge, options=list(options)), delai)
    for choix in options:
        try:
            if np_.verifier_validation(objet, h["identite"], parc(),
                                       dict(charge, options=list(options), choix=choix)) == "accepte":
                return objet, choix
        except np_.ErreurPont:
            continue
    raise np_.ErreurPont("arbitrage signe pour aucun des choix proposes")


def _demander(sujet, charge, delai):
    connus = humains()
    fiches = compagnon.trusted()
    machines = [(hid, h) for hid, h in connus.items() if h.get("machine") in _sessions]
    # L'application Prismaforge (role interface) d'abord : son dialogue est fait pour le
    # lecteur d'ecran ; le dialogue systeme du pont nearfield ne sert que faute de mieux.
    machines.sort(key=lambda m: (fiches.get(m[1]["machine"]) or {}).get("role") != "interface")
    if not machines:
        raise np_.ErreurPont("aucun appareil avec une cle humaine n'est connecte")
    hid, h = machines[0]
    session = _sessions.get(h["machine"])
    demande = uuid.uuid4().hex[:16]
    attente = {"cid": h["machine"], "evenement": threading.Event(), "reponse": None}
    _validations[demande] = attente
    try:
        session.envoyer({"type": "validation_demande", "id": demande, "sujet": sujet, "charge": charge})
        if not attente["evenement"].wait(delai):
            raise np_.ErreurPont("pas de reponse de l'humain a temps")
        r = attente["reponse"]
        if r.get("erreur"):
            raise np_.ErreurPont("validation non donnee : " + str(r["erreur"])[:200])
        return r["objet"], h
    finally:
        _validations.pop(demande, None)


def demarrer(adresse="0.0.0.0", port=PORT, recu=None, evenement=None):
    """Ecoute v2 en tache de fond ; rend le port reel (port 0 : port libre, pour les essais).
    recu(cid, fiche, message_v1) : ce que la machine envoie, remis au code du pont.
    evenement(kind, cid, fiche, ip) : "arrivee" / "depart" d'une session v2."""
    _recu[0] = recu
    _evenement[0] = evenement
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


# --- travaux signes (spec §6) ----------------------------------------------------------
# Le NAS n'a aucune autorite : a cote du travail.json (lu par le code du Pi), le Pi
# depose travail.v2.json, objet signe par sa cle v2. Une machine migree ne prend un
# travail que si cet objet est valide (signature du Pi epinglee, parc, date, empreinte
# de chaque fichier d'entree) et n'execute que le travail QU'IL CONTIENT. Elle rend
# resultat.v2.json, signe par sa cle v2, lie a CE travail (empreinte de sa charge) :
# le Pi ne croit plus le resultat.json v1 d'une machine migree.
# La description du travail voyage en texte JSON ("contenu") : la forme canonique
# (JCS) n'admet que des entiers, et les parametres ont des nombres a virgule.

TRAVAIL_V2 = "travail.v2.json"
RESULTAT_V2 = "resultat.v2.json"
_PAS_DES_ENTREES = ("travail.json", "travail.pris.json", TRAVAIL_V2, "resultat.json", RESULTAT_V2,
                    "attribution.json")  # simple indication de repartition, ecrite apres coup


def _entrees(dossier):
    """{nom: sha256} des fichiers d'entree deja ecrits dans le dossier du travail."""
    out = {}
    for nom in sorted(os.listdir(dossier)):
        chemin = os.path.join(dossier, nom)
        if nom.startswith(".") or nom in _PAS_DES_ENTREES or os.path.islink(chemin) or not os.path.isfile(chemin):
            continue
        out[nom] = np_.empreinte_fichier(chemin)
    return out


def empreinte_charge(charge):
    import hashlib
    return hashlib.sha256(np_.canonique(charge)).hexdigest()


def signer_travail(job, dossier):
    """Depose travail.v2.json a cote du travail (rien si la v2 est inactive)."""
    if not actif():
        return None
    charge = {"type": "travail", "parc": parc(), "id": job["id"],
              "contenu": json.dumps(job, ensure_ascii=False, sort_keys=True), "fichiers": _entrees(dossier)}
    objet = coffre().signer_objet(charge)
    compagnon._write(os.path.join(dossier, TRAVAIL_V2), objet)
    return objet


def travail_signe(dossier):
    """Le travail tel que NOUS l'avons signe (travail.v2.json) : seule source du contenu
    d'un travail cote Pi ; travail.json n'est qu'une copie non signee (file d'attente)."""
    objet = compagnon._read(os.path.join(dossier, TRAVAIL_V2))
    if not objet:
        raise np_.ErreurPont("travail v2 absent")
    t = np_.verifier_objet(objet, coffre().cle_publique(), "travail", parc())
    job = json.loads(t["contenu"])
    if not isinstance(job, dict) or job.get("id") != t["id"] or t["id"] != os.path.basename(dossier.rstrip("/")):
        raise np_.ErreurPont("contenu d'un autre travail")
    return job


def resultat_signe(card, job_id, dossier):
    """Resultat v2 d'une machine migree, verifie : charge du resultat, ou leve ErreurPont.
    Le travail doit etre le notre (notre signature), le resultat signe par la cle v2 de
    LA machine, pour CE travail, et chaque fichier rendu doit avoir son empreinte."""
    travail = compagnon._read(os.path.join(dossier, TRAVAIL_V2))
    objet = compagnon._read(os.path.join(dossier, RESULTAT_V2))
    if not travail or not objet:
        raise np_.ErreurPont("resultat v2 absent")
    p = parc()
    t = np_.verifier_objet(travail, coffre().cle_publique(), "travail", p)
    r = np_.verifier_objet(objet, card["v2"]["identite"], "resultat", p)
    if t["id"] != job_id or r.get("travail") != job_id or r.get("objet_travail") != empreinte_charge(t):
        raise np_.ErreurPont("resultat d'un autre travail")
    np_.verifier_fichiers(objet, dossier)
    return r


# --- appairage v2 (spec §3), par le NAS ----------------------------------------------
# Canal NON authentifie (fichiers du NAS) : la securite vient du code a 6 chiffres que
# l'humain compare. La machine s'engage sur sa cle AVANT de connaitre notre alea : un
# intermediaire n'a qu'une chance sur un million. Fichiers dans appairage/v2/ :
#   ouvert.json                 Pi : fenetre ouverte (parc, adresse pour se connecter)
#   engagement-<n>.json         machine : engagement, nom, id, capacites
#   presentation-<n>.json       Pi : sa presentation (identite, transport, alea)
#   revelation-<n>.json         machine : sa presentation (tient l'engagement)
#   certificat-<n>.json         Pi, apres validation du code : premier certificat
#   refus-<n>.json              machine : la personne a dit non
# <n> : nom simple choisi par la machine. Rien de tout ca n'est cru sans le code.

APPAIRAGE_SEC = 180


def _dossier_appairage():
    return compagnon._path("appairage", "v2")


class AppairageV2:
    """Cote Pi, pour la console : ouvrir(), puis etat() en boucle, puis accepter()."""

    def __init__(self):
        self.accueil = None
        self.n = None
        self.machine = None   # engagement lu : nom, id, capacites
        self.revelation = None
        self.code = None
        self.erreur = None

    def ouvrir(self):
        import shutil
        dossier = _dossier_appairage()
        shutil.rmtree(dossier, ignore_errors=True)
        os.makedirs(dossier, exist_ok=True)
        compagnon._write(os.path.join(dossier, "ouvert.json"),
                         {"parc": parc(), "pi": compagnon.pi_id(), "hote": f"{compagnon.pi_id()}.local",
                          "ip": compagnon._local_ip(), "port": PORT, "t": time.time()})

    def fermer(self):
        import shutil
        shutil.rmtree(_dossier_appairage(), ignore_errors=True)

    def _lire(self, nom):
        return compagnon._read(os.path.join(_dossier_appairage(), nom))

    def etat(self):
        """"attente", "code" (self.code, self.machine), "refus" ou "erreur" (self.erreur)."""
        if self.erreur:
            return "erreur"
        if self.n is None:
            for nom in sorted(os.listdir(_dossier_appairage()) if os.path.isdir(_dossier_appairage()) else []):
                if not (nom.startswith("engagement-") and nom.endswith(".json")):
                    continue
                e = self._lire(nom) or {}
                n = nom[len("engagement-"):-5]
                try:
                    np_.nom_sur(n)
                    np_.nom_sur(str(e.get("compagnon") or ""))
                    if not isinstance(e.get("engagement"), str):
                        raise np_.ErreurFormat("engagement")
                    self.accueil = np_.Accueil(e["engagement"], coffre().cle_publique(), np_.CleTransport().public)
                except np_.ErreurPont as exc:
                    print(f"[pont v2] appairage : engagement {n} ignore ({exc})", flush=True)
                    continue
                self.n = n
                nom_machine = str(e.get("nom") or e["compagnon"])[:64]
                role = "interface" if e.get("role") == "interface" else "travailleur"
                caps = [] if role == "interface" else \
                    [c for c in e.get("capacites") or [] if isinstance(c, str) and 0 < len(c) <= 128][:64]
                self.machine = {"nom": nom_machine, "compagnon": e["compagnon"], "capacites": caps, "role": role,
                                "niveau": "materiel" if e.get("niveau") == "materiel" else "logiciel"}
                compagnon._write(os.path.join(_dossier_appairage(), f"presentation-{n}.json"),
                                 {"presentation": self.accueil.presentation, "parc": parc()})
                break
            return "attente"
        if os.path.exists(os.path.join(_dossier_appairage(), f"refus-{self.n}.json")):
            return "refus"
        if self.code is None:
            r = self._lire(f"revelation-{self.n}.json")
            if not r:
                return "attente"
            try:
                self.code = self.accueil.verifier(r.get("presentation"))
            except np_.ErreurPont as exc:
                self.erreur = str(exc)
                return "erreur"
            self.revelation = r["presentation"]
        return "code"

    def accepter(self):
        """La personne a valide le code ici : fiche de confiance v2 et premier certificat.
        Rend le nom de la machine."""
        import base64
        m, pres = self.machine, self.revelation
        cid = m["compagnon"]
        ident = pres["identite"]
        vid = np_.identifiant(ident)
        existante = compagnon.trusted().get(cid)
        if existante and (existante.get("v2") or {}).get("id") not in (None, vid):
            raise np_.AppairageRefuse(f"une autre machine porte deja le nom {cid}")
        with _verrou:
            serie = _serie_suivante()
        cert = np_.emettre_certificat(coffre(), parc(), m["nom"], ident, m["niveau"], base64.b64decode(pres["transport"]),
                                      [m["role"]], m["capacites"], serie, TOLERANCE_LOGICIELLE)
        fiche = dict(existante or {}, nom=m["nom"], capacites=m["capacites"], role=m["role"], appaire=time.time(),
                     vu=time.time(),
                     v2={"id": vid, "identite": ident, "niveau": m["niveau"], "appaire": time.time()})
        fiche.setdefault("stats", {})
        os.makedirs(compagnon.TRUST_DIR, exist_ok=True)
        compagnon._write(os.path.join(compagnon.TRUST_DIR, f"{cid}.json"), fiche)
        compagnon._write(os.path.join(_dossier_appairage(), f"certificat-{self.n}.json"),
                         {"objet": cert, "port": PORT, "parc": parc()})
        return m["nom"]
