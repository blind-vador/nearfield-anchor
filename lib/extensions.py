"""Point d'acces local des extensions (Prismaforge, specs/lot2-fondations.md §5).

Le logiciel IA de PrismaLab (et tout autre outil) se branche sur l'ordonnateur par une
PRISE UNIX LOCALE, jamais par le reseau :
  LOCAL_DIR/v2/extensions/prise   (dossier 0700, prise 0600, proprietaire du service ;
                                   sous Linux on verifie aussi l'utilisateur de l'appelant)

Trames : 4 octets (longueur, gros-boutiste) puis un objet JSON UTF-8 de 256 Kio au plus.

Ouverture :
  <- {"type": "bonjour", "version": 2, "parc": P, "defi": <base64 32 octets>}
  -> {"type": "presentation", "objet": <objet signe "extension_ouverture" {defi}>}
       signe par la cle d'identite de l'extension (enrolee) ; ou, pour une extension
       neuve, une seule fois :
  -> {"type": "enrolement_demande", "nom": "...", "identite": {...}, "capacites": [...]}
       l'ordonnateur fait valider l'enrolement par l'humain (cle humaine, biometrie),
       repond {"type": "enrole", "id": ...} ou {"type": "erreur", ...} et ferme.
  <- {"type": "accueil", "id": ..., "nom": ..., "capacites": [...]}

Requetes (LISTE FERMEE ; chacune porte un "id", la reponse le rappelle en
"reponse_a" : {"type": "reponse", ...} ou {"type": "erreur", "raison": ...}) :
  ticket_proposer  {brouillon}             controle, garde le brouillon ; rend son empreinte
  ticket_valider   {ticket}                validation humaine puis ticket signe par l'ordonnateur
  ticket_lire      {ticket}                ticket, nombre d'entrees, clos ou non
  tickets_lister   {}                      resume de chaque ticket emis (titre, mode, parent,
                                           entrees, clos, pause), du plus recent au plus ancien
  journal_ecrire   {entree}                entree signee PAR L'EXTENSION, chainee et verifiee
  journal_lire     {ticket, depuis}        entrees verifiees
  artefact_deposer {ticket, nom, contenu}  fichier attache au ticket (un diff par exemple), 180 Kio
                                           au plus, jamais reecrit ; rend son empreinte, qu'une
                                           entree de journal SIGNEE reference (donnees.artefacts
                                           {nom: empreinte}) pour qu'il fasse foi
  artefact_lire    {ticket, nom}           contenu, empreinte, et l'entree de journal qui le
                                           reference (n) ou null
  travail_deposer  {ticket, capacite, params}  travail d'un type connu, dans ce qu'autorise le ticket
  contre_epreuve   {ticket, travail, capacite, params}  verification confiee par l'ORDONNATEUR a une
                                           autre machine que l'auteur (ou, seule machine, a un
                                           agent en contexte neutre), notee au journal
  evenements       {}                      s'abonner : {"type": "evenement", "genre": ...}
  interface_servir {}                      (capacite "interface") devenir le front de l'IDE : les
                                           requetes des coquilles arrivent en evenements
                                           {"type": "interface_requete", requete, appareil,
                                           methode, chemin, corps}
  interface_reponse {requete, statut, type_contenu, corps (base64)}  la reponse, relayee a
                                           la coquille par sa session Noise (morceaux)

Modes (lot2 §6) : un ticket est "lax" (sa validation couvre ses sous-tickets, dans ses
bornes) ou "strict" (chaque sous-ticket, c.-a-d. chaque bloc, est valide par l'humain).
Controle mecanique : une entree "progression" ou "resultat" peut declarer
donnees.fichiers (chemins touches) et donnees.consomme {secondes, cout_centimes} ; hors
des zones ou au-dela du budget, l'ordonnateur arrete le ticket (entree "cloture" signee
par lui). Desaccord : le premier met le ticket en pause et demande l'arbitrage de
l'architecte (points, raisons de chacun, impact ; il choisit redacteur, verificateur,
troisieme avis ou arret) ; le second de suite arrete le ticket.
Aucune commande arbitraire, aucun acces aux cles de l'ordonnateur ; enroler une extension
et lancer un plan exigent une validation humaine de moins de 5 minutes. Retirer une
extension se fait sur le Pi : python3 lib/extensions.py retirer <nom>.

Sons (2026-11-16) : le Pi sonne (validation qui attend, ticket lance ou arrete, entree de
journal) ; la parole est sur l'appareil de l'utilisateur (application Prismaforge, VoiceOver),
qui montre le texte exact du journal.

Etat (carte SD) : LOCAL_DIR/v2/extensions/fiches.json, LOCAL_DIR/v2/tickets/.
"""
import base64
import hashlib
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import uuid

import compagnon
import pont_v2

np_ = pont_v2.np_

MAX_TRAME = 256 * 1024
MAX_PARAMS = 16 * 1024
NOM_SUR = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SURVEILLANCE_SEC = 10

_verrou = threading.Lock()
_enrolement = threading.Lock()
_abonnes = set()        # connexions abonnees aux evenements
_travaux = {}           # id du travail -> (ticket, id de l'extension), en attente de fin
_nonces = None
ANNONCES = True         # les essais les coupent


def _dire(cle, **variables):
    try:
        from phrases import dire
        return dire(cle, **variables)
    except Exception:
        return None


def _annoncer(texte, son="journal_entree"):
    """Le Pi SONNE (2026-11-16 : la parole est sur l'appareil de l'utilisateur, avec son
    lecteur d'écran ; deux voix qui se chevauchent sont incompréhensibles). Le texte est
    journalisé ; l'application Prismaforge le montre et l'annonce (fenêtre IDE, validations)."""
    if not ANNONCES:
        return
    if texte:
        print(f"[extensions] {texte}", flush=True)
    try:
        import sons
        sons.jouer(son)
    except Exception as exc:
        print(f"[extensions] son {son} : {exc!r}", flush=True)


def _valider(sujet, charge, quoi):
    """Validation humaine annoncee sur le Pi (« le Mac te demande… ») puis demandee."""
    _annoncer(_dire("extensions.validation_demandee", quoi=quoi), "validation_attente")
    return pont_v2.demander_validation(sujet, charge)


def _dossier(*parts):
    return os.path.join(pont_v2.V2_DIR, *parts)


def chemin_prise():
    return _dossier("extensions", "prise")


def _lire_json(chemin, defaut=None):
    try:
        with open(chemin) as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def _ecrire_json(chemin, valeur, exclusif=False):
    os.makedirs(os.path.dirname(chemin), mode=0o700, exist_ok=True)
    if exclusif:
        fd = os.open(chemin, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(valeur, f)
        return
    tmp = chemin + ".tmp"
    with open(tmp, "w") as f:
        json.dump(valeur, f, indent=1)
    os.replace(tmp, chemin)


def fiches():
    """{id: {"nom", "identite", "capacites", "validation", "ajoute"}} des extensions enrolees."""
    return _lire_json(_dossier("extensions", "fiches.json"), {}) or {}


def _cles_connues():
    """Signataires admis dans un journal : extensions, ordonnateur, cles humaines."""
    cles = {i: f["identite"] for i, f in fiches().items()}
    moi = pont_v2.coffre()
    cles[moi.id] = moi.cle_publique()
    for hid, h in pont_v2.humains().items():
        cles[hid] = h["identite"]
    return cles


def _humaines():
    return [h["identite"] for h in pont_v2.humains().values()]


# --- trames -------------------------------------------------------------------------------

class Refus(Exception):
    """Requete refusee : la raison est renvoyee a l'extension (et journalisee)."""


def _recevoir(conn):
    entete = b""
    while len(entete) < 4:
        morceau = conn.recv(4 - len(entete))
        if not morceau:
            return None
        entete += morceau
    n = struct.unpack(">I", entete)[0]
    if n == 0 or n > MAX_TRAME:
        raise Refus("trame trop grande")
    donnees = b""
    while len(donnees) < n:
        morceau = conn.recv(min(65536, n - len(donnees)))
        if not morceau:
            return None
        donnees += morceau
    try:
        msg = json.loads(donnees.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refus("JSON illisible")
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise Refus("objet JSON avec un type attendu")
    return msg


class Connexion:
    def __init__(self, conn):
        self.conn = conn
        self.envoi = threading.Lock()
        self.fiche = None
        self.id = None

    def envoyer(self, msg):
        donnees = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        if len(donnees) > MAX_TRAME:
            raise Refus("reponse trop grande")
        with self.envoi:
            self.conn.sendall(struct.pack(">I", len(donnees)) + donnees)


def _evenement(genre, **donnees):
    msg = dict(donnees, type="evenement", genre=genre, t=time.time())
    for c in list(_abonnes):
        try:
            c.envoyer(msg)
        except OSError:
            _abonnes.discard(c)


# --- requetes -----------------------------------------------------------------------------

def _nom_sur(nom, quoi):
    if not isinstance(nom, str) or not NOM_SUR.match(nom):
        raise Refus("%s : nom simple attendu (minuscules, chiffres, _ et -)" % quoi)
    return nom


def _ticket(tid):
    _nom_sur(tid, "ticket")
    t = _lire_json(_dossier("tickets", tid, "ticket.json"))
    if not t:
        raise Refus("ticket inconnu : %s" % tid)
    return t


def _entrees(tid):
    dossier = _dossier("tickets", tid, "journal")
    try:
        noms = sorted((int(n[:-5]) for n in os.listdir(dossier) if re.fullmatch(r"\d+\.json", n)))
    except OSError:
        return []
    return [_lire_json(os.path.join(dossier, "%d.json" % n)) for n in noms]


def _journal_verifie(ticket, entrees):
    return np_.verifier_journal(ticket, entrees, pont_v2.parc(), _cles_connues())


def _clos(entrees_verifiees):
    return bool(entrees_verifiees) and entrees_verifiees[-1].get("genre") == "cloture"


def _en_pause(entrees_verifiees):
    """Un desaccord pas encore tranche (aucune entree "validation" d'arbitrage apres)."""
    pause = False
    for e in entrees_verifiees:
        if e.get("genre") == "desaccord":
            pause = True
        elif e.get("genre") == "validation" and (e.get("donnees") or {}).get("arbitrage") is not None:
            pause = False
    return pause


def _brouillon_de(ticket):
    """Le brouillon porte par un ticket emis (charge sans les champs de l'objet)."""
    c = dict(ticket["charge"])
    for k in ("v", "emis", "nonce", "validation", "couvert_par"):
        c.pop(k, None)
    return c


def _ecrire_ordonnateur(tid, genre, texte, donnees=None):
    """Entree de journal signee par l'ordonnateur lui-meme (contre-epreuve, arbitrage, arret)."""
    t = _ticket(tid)
    with _verrou:
        anciennes = _entrees(tid)
        e = np_.ecrire_journal(pont_v2.coffre(), pont_v2.parc(), t, anciennes[-1] if anciennes else None,
                               genre, texte, donnees)
        n = e["charge"]["n"]
        _ecrire_json(_dossier("tickets", tid, "journal", "%d.json" % n), e, exclusif=True)
    _evenement("journal", ticket=tid, n=n, journal_genre=genre, texte=texte)
    _annoncer(texte, "ticket_arrete" if genre == "cloture" else "journal_entree")
    return n


def _arreter(tid, raison):
    print(f"[extensions] ticket {tid} arrete : {raison}", flush=True)
    return _ecrire_ordonnateur(tid, "cloture", "Ticket arrêté : %s" % raison, {"arret": raison[:500]})


def ticket_proposer(cx, req):
    b = req.get("brouillon")
    if not isinstance(b, dict) or b.get("type") != "ticket" or b.get("parc") != pont_v2.parc():
        raise Refus("brouillon de ticket de ce parc attendu")
    _nom_sur(b.get("id"), "ticket")
    empreinte = np_.controler_brouillon(b)
    if b.get("parent"):
        _controler_bloc(b)
    hors = set(b.get("capacites") or []) - set(_capacites_du_parc())
    if hors:
        raise Refus("capacites inconnues du parc : %s" % ", ".join(sorted(hors)))
    if os.path.exists(_dossier("tickets", b["id"], "ticket.json")):
        raise Refus("ce ticket est deja emis")
    _ecrire_json(_dossier("tickets", "brouillons", b["id"] + ".json"), dict(b, _par=cx.id))
    return {"empreinte": empreinte}


def _controler_bloc(b):
    """Sous-ticket : parent emis, ni clos ni en pause, bornes (crate), et budget cumule des
    blocs deja emis au plus celui du parent."""
    ptid = _nom_sur((b.get("parent") or {}).get("ticket"), "ticket parent")
    parent = _ticket(ptid)
    if (b["parent"] or {}).get("empreinte") != np_.empreinte_objet(parent):
        raise Refus("empreinte du ticket parent")
    e = _journal_verifie(parent, _entrees(ptid))
    if _clos(e) or _en_pause(e):
        raise Refus("ticket parent clos ou en pause")
    pb = _brouillon_de(parent)
    np_.controler_sous_ticket(pb, b)
    blocs = _lire_json(_dossier("tickets", ptid, "blocs.json"), []) or []
    for cle in ("secondes", "cout_centimes"):
        total = sum(x["budget"][cle] for x in blocs if x["id"] != b["id"]) + b["budget"][cle]
        if total > pb["budget"][cle]:
            raise Refus("budget des blocs au-dela de celui du ticket parent (%s)" % cle)
    return parent, pb


def _valider_bloc(tid, b):
    parent, pb = _controler_bloc(b)
    validation = None
    if pb["mode"] == "strict" or b["mode"] == "strict":
        quoi = _dire("extensions.bloc_quoi", titre=b.get("titre", tid), parent=pb.get("titre", ""))
        validation = _valider("ticket", b, quoi)
        if np_.verifier_validation(validation, validation_cle(validation), pont_v2.parc(), b) != "accepte":
            raise Refus("bloc refuse par l'humain")
    ticket = np_.emettre_sous_ticket(pont_v2.coffre(), b, parent, validation, _humaines())
    with _verrou:
        blocs = _lire_json(_dossier("tickets", pb["id"], "blocs.json"), []) or []
        blocs.append({"id": tid, "budget": b["budget"], "valide": validation is not None})
        _ecrire_json(_dossier("tickets", pb["id"], "blocs.json"), blocs)
    return ticket


def ticket_valider(cx, req):
    tid = _nom_sur(req.get("ticket"), "ticket")
    b = _lire_json(_dossier("tickets", "brouillons", tid + ".json"))
    if not b or b.pop("_par", None) != cx.id:
        raise Refus("pas de brouillon de cette extension pour ce ticket")
    if b.get("parent"):
        ticket = _valider_bloc(tid, b)
        _ecrire_json(_dossier("tickets", tid, "ticket.json"), ticket, exclusif=True)
        try:
            os.remove(_dossier("tickets", "brouillons", tid + ".json"))
        except OSError:
            pass
        _evenement("ticket_emis", ticket=tid, titre=b.get("titre"), parent=b["parent"]["ticket"])
        if "couvert_par" in ticket["charge"]:
            _annoncer(_dire("extensions.bloc_lance", titre=b.get("titre", tid)), "ticket_lance")
        else:
            _annoncer(_dire("extensions.ticket_emis", titre=b.get("titre", tid)), "ticket_lance")
        return {"ticket": ticket}
    try:
        validation = _valider("ticket", b, _dire("extensions.ticket_quoi", titre=b.get("titre", tid)))
        if np_.verifier_validation(validation, validation_cle(validation), pont_v2.parc(), b) != "accepte":
            raise Refus("ticket refuse par l'humain")
    except (Refus, np_.ErreurPont) as exc:
        _annoncer(_dire("extensions.ticket_refuse", titre=b.get("titre", tid), raison=str(exc)), "ticket_arrete")
        raise
    ticket = np_.emettre_ticket(pont_v2.coffre(), b, validation, _humaines())
    _ecrire_json(_dossier("tickets", tid, "ticket.json"), ticket, exclusif=True)
    try:
        os.remove(_dossier("tickets", "brouillons", tid + ".json"))
    except OSError:
        pass
    _evenement("ticket_emis", ticket=tid, titre=b.get("titre"))
    _annoncer(_dire("extensions.ticket_emis", titre=b.get("titre", tid)), "ticket_lance")
    return {"ticket": ticket}


def validation_cle(validation):
    """Cle humaine qui a signe cette validation (parmi les connues)."""
    h = pont_v2.humains().get(validation.get("signataire"))
    if not h:
        raise Refus("validation signee par une cle humaine inconnue")
    return h["identite"]


def ticket_lire(cx, req):
    t = _ticket(req.get("ticket"))
    e = _journal_verifie(t, _entrees(req["ticket"]))
    return {"ticket": t, "entrees": len(e), "clos": _clos(e), "pause": _en_pause(e)}


def tickets_lister(cx, req):
    try:
        noms = [n for n in os.listdir(_dossier("tickets")) if n != "brouillons" and NOM_SUR.match(n)]
    except OSError:
        noms = []
    sortie = []
    for tid in noms:
        t = _lire_json(_dossier("tickets", tid, "ticket.json"))
        if not t:
            continue
        try:
            e = _journal_verifie(t, _entrees(tid))
        except np_.ErreurPont:
            continue  # journal invalide : pas montre (incident deja journalise ailleurs)
        c = t["charge"]
        sortie.append({"id": tid, "titre": c.get("titre"), "mode": c.get("mode"),
                       "parent": (c.get("parent") or {}).get("ticket"), "emis": c.get("emis"),
                       "entrees": len(e), "clos": _clos(e), "pause": _en_pause(e),
                       "dernier": (e[-1].get("texte") if e else None)})
    sortie.sort(key=lambda x: x.get("emis") or 0, reverse=True)
    return {"tickets": sortie[:200]}


def _ticket_verifie(tid):
    """Ticket emis et verifie (avec son parent pour un bloc) : rend son brouillon."""
    t = _ticket(tid)
    c = t["charge"]
    if c.get("parent"):
        return np_.verifier_sous_ticket(t, _ticket(c["parent"]["ticket"]), pont_v2.coffre().cle_publique(),
                                        pont_v2.parc(), _humaines())
    return np_.verifier_ticket(t, pont_v2.coffre().cle_publique(), pont_v2.parc(), _humaines())


def _controle_mecanique(b, charge):
    """Fichiers touches dans les zones, consommation dans le budget ; rend la raison d'un
    depassement, ou None."""
    d = charge.get("donnees") or {}
    fichiers = d.get("fichiers") or []
    if not isinstance(fichiers, list):
        return "liste de fichiers illisible"
    for f in fichiers:
        if not isinstance(f, str) or not np_.dans_les_zones(b["zones"], f):
            return "fichier hors des zones du ticket : %s" % str(f)[:200]
    c = d.get("consomme") or {}
    for cle, nom in (("secondes", "temps"), ("cout_centimes", "coût")):
        v = c.get(cle, 0)
        if not isinstance(v, (int, float)) or v > b["budget"][cle]:
            return "budget dépassé (%s)" % nom
    return None


def journal_ecrire(cx, req):
    entree = req.get("entree")
    if not isinstance(entree, dict) or entree.get("signataire") != cx.id:
        raise Refus("entree signee par cette extension attendue")
    tid = (entree.get("charge") or {}).get("ticket")
    t = _ticket(tid)
    with _verrou:
        anciennes = _entrees(tid)
        _journal_verifie(t, anciennes + [entree])  # chaine, numeros, signatures, dates, cloture
        n = entree["charge"]["n"]
        try:
            _ecrire_json(_dossier("tickets", tid, "journal", "%d.json" % n), entree, exclusif=True)
        except FileExistsError:
            raise Refus("entree %d deja ecrite (fourche refusee)" % n)
    c = entree["charge"]
    _evenement("journal", ticket=tid, n=n, journal_genre=c.get("genre"), texte=c.get("texte"))
    _annoncer(c.get("texte"))  # l'utilisateur entend exactement ce qui est enregistre
    reponse = {"n": n}
    if c.get("genre") in ("progression", "resultat"):
        raison = _controle_mecanique(_ticket_verifie(tid), c)
        if raison:
            _arreter(tid, raison)
            reponse["arrete"] = raison
    elif c.get("genre") == "desaccord":
        e = _journal_verifie(t, _entrees(tid))
        desaccords = [x for x in e if x.get("genre") == "desaccord"]
        if len(desaccords) >= 2:
            _arreter(tid, "deuxième désaccord sur ce ticket")
            reponse["arrete"] = "deuxieme desaccord"
        else:
            reponse["pause"] = True
            threading.Thread(target=_arbitrer, args=(tid, n, c), daemon=True).start()
    return reponse


ARBITRAGES = ("redacteur", "verificateur", "troisieme_avis", "arreter")


def _arbitrer(tid, n, entree):
    """Le premier desaccord : l'architecte tranche, avec les points, les raisons de chacun
    et l'impact. Sa validation (le choix compris) est gardee au journal."""
    t = _ticket(tid)
    d = entree.get("donnees") or {}
    charge = {"type": "arbitrage", "parc": pont_v2.parc(), "id": "arbitrage-%s-%d" % (tid, n), "ticket": tid,
              "titre": t["charge"].get("titre", tid), "desaccord": entree.get("texte") or "",
              "points": d.get("points") or [], "impact": d.get("impact") or ""}
    try:
        _annoncer(_dire("extensions.validation_demandee",
                        quoi=_dire("extensions.arbitrage_quoi", titre=charge["titre"])), "validation_attente")
        validation, choix = pont_v2.demander_arbitrage(charge, ARBITRAGES)
    except np_.ErreurPont as exc:
        _evenement("arbitrage", ticket=tid, choix=None, raison=str(exc))
        print(f"[extensions] arbitrage de {tid} non donne : {exc}", flush=True)
        return  # le ticket reste en pause ; l'extension peut redemander par un nouveau desaccord
    _ecrire_ordonnateur(tid, "validation", _dire("extensions.arbitrage_" + choix) or choix,
                        {"arbitrage": n, "choix": choix, "validation": validation})
    _evenement("arbitrage", ticket=tid, choix=choix)
    if choix == "arreter":
        _arreter(tid, "décision de l'architecte")


def journal_lire(cx, req):
    t = _ticket(req.get("ticket"))
    depuis = req.get("depuis", 0)
    if not isinstance(depuis, int) or depuis < 0:
        raise Refus("depuis : entier positif")
    _journal_verifie(t, _entrees(req["ticket"]))
    return {"entrees": _entrees(req["ticket"])[depuis:depuis + 200]}


MAX_ARTEFACT = 180 * 1024
NOM_ARTEFACT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _nom_artefact(nom):
    if not isinstance(nom, str) or not NOM_ARTEFACT.match(nom) or ".." in nom:
        raise Refus("nom d'artefact : minuscules, chiffres, . _ et -, 64 au plus")
    return nom


def artefact_deposer(cx, req):
    tid = req.get("ticket")
    _ticket_actif(tid)
    nom = _nom_artefact(req.get("nom"))
    try:
        contenu = base64.b64decode(req.get("contenu") or "", validate=True)
    except ValueError:
        raise Refus("contenu : base64 attendu")
    if len(contenu) > MAX_ARTEFACT:
        raise Refus("artefact trop gros (180 Kio au plus)")
    chemin = _dossier("tickets", tid, "artefacts", nom)
    os.makedirs(os.path.dirname(chemin), mode=0o700, exist_ok=True)
    try:
        fd = os.open(chemin, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise Refus("cet artefact existe deja (jamais reecrit)")
    with os.fdopen(fd, "wb") as f:
        f.write(contenu)
    return {"empreinte": hashlib.sha256(contenu).hexdigest(), "taille": len(contenu)}


def artefact_lire(cx, req):
    tid = req.get("ticket")
    t = _ticket(tid)
    nom = _nom_artefact(req.get("nom"))
    try:
        with open(_dossier("tickets", tid, "artefacts", nom), "rb") as f:
            contenu = f.read(MAX_ARTEFACT + 1)
    except OSError:
        raise Refus("artefact inconnu")
    empreinte = hashlib.sha256(contenu).hexdigest()
    reference = None
    for e in _journal_verifie(t, _entrees(tid)):
        if ((e.get("donnees") or {}).get("artefacts") or {}).get(nom) == empreinte:
            reference = e["n"]
    return {"nom": nom, "contenu": base64.b64encode(contenu).decode(), "empreinte": empreinte,
            "reference": reference}


def _capacites_du_parc():
    caps = set()
    for card in compagnon.trusted().values():
        caps.update(c for c in card.get("capacites") or [] if isinstance(c, str))
    return sorted(caps)


def _params_bornes(p):
    if not isinstance(p, dict):
        raise Refus("params : objet attendu")
    if len(json.dumps(p)) > MAX_PARAMS:
        raise Refus("params trop gros")

    def prof(v, n):
        if n > 6:
            return False
        if isinstance(v, dict):
            return all(isinstance(k, str) and prof(x, n + 1) for k, x in v.items())
        if isinstance(v, list):
            return all(prof(x, n + 1) for x in v)
        return v is None or isinstance(v, (str, int, float, bool))
    if not prof(p, 0):
        raise Refus("params trop profonds ou de type inconnu")


def _ticket_actif(tid):
    t = _ticket(tid)
    e = _journal_verifie(t, _entrees(tid))
    if _clos(e):
        raise Refus("ticket clos")
    if _en_pause(e):
        raise Refus("ticket en pause : un desaccord attend l'arbitrage de l'architecte")
    return t


def _deposer(cx, tid, t, capacite, params, extra=None):
    if capacite not in (t["charge"].get("capacites") or []):
        raise Refus("le ticket n'autorise pas la capacite %s" % capacite)
    if capacite not in _capacites_du_parc():
        raise Refus("aucune machine du parc ne sait faire %s" % capacite)
    _params_bornes(params)
    job_id, _dossier_travail, job = compagnon.new_job(capacite, params)
    job["ticket"] = tid
    job["extension"] = cx.id
    job.update(extra or {})
    compagnon.submit(job)
    _travaux[job_id] = (tid, cx.id)
    with _verrou:
        liste = _lire_json(_dossier("tickets", tid, "travaux.json"), []) or []
        liste.append({"id": job_id, "capacite": capacite, "t": time.time()})
        _ecrire_json(_dossier("tickets", tid, "travaux.json"), liste)
    return job_id


def travail_deposer(cx, req):
    tid = req.get("ticket")
    t = _ticket_actif(tid)
    capacite = _nom_sur(req.get("capacite"), "capacite")
    return {"travail": _deposer(cx, tid, t, capacite, req.get("params") or {})}


def _auteur(job_id):
    """(id, fiche) de la machine qui a rendu ce travail : signataire de son resultat v2,
    VERIFIE (signature, lien au travail, fichiers) ; (None, None) sinon."""
    dossier = compagnon._path("travaux", job_id)
    objet = compagnon._read(os.path.join(dossier, "resultat.v2.json")) or {}
    cid, fiche = compagnon._machine_v2(objet.get("signataire"), compagnon.trusted())
    if not fiche:
        return None, None
    try:
        pont_v2.resultat_signe(fiche, job_id, dossier)
    except np_.ErreurPont:
        return None, None
    return cid, fiche


def contre_epreuve(cx, req):
    """L'ORDONNATEUR choisit qui verifie : une autre machine que l'auteur (la plus tot
    disponible) ; seule machine capable : la meme, en contexte neutre (nouvel agent, sans
    canal vers le redacteur). Le choix est signe au journal."""
    tid = req.get("ticket")
    t = _ticket_actif(tid)
    travail = _nom_sur(req.get("travail"), "travail")
    if travail not in {x["id"] for x in _lire_json(_dossier("tickets", tid, "travaux.json"), []) or []}:
        raise Refus("ce travail n'appartient pas a ce ticket")
    auteur, fiche = _auteur(travail)
    if not auteur:
        raise Refus("travail sans resultat signe : rien a contre-eprouver")
    capacite = _nom_sur(req.get("capacite"), "capacite")
    autres = [c for c in compagnon.rank(capacite) if c["id"] != auteur]
    meme = not autres
    nom = (autres[0].get("nom") if autres else fiche.get("nom")) or "?"
    params = dict(req.get("params") or {}, contre_epreuve_de=travail, contexte_neutre=True)
    extra = {"contre_epreuve": travail}
    if not meme:
        extra["exclure"] = [auteur]
    job_id = _deposer(cx, tid, t, capacite, params, extra)
    texte = (_dire("extensions.contre_epreuve_meme", nom=nom) if meme
             else _dire("extensions.contre_epreuve_autre", nom=nom))
    n = _ecrire_ordonnateur(tid, "contre_epreuve", texte,
                            {"travail": travail, "verification": job_id, "verificateur": nom,
                             "meme_machine": meme})
    return {"travail": job_id, "verificateur": nom, "meme_machine": meme, "n": n}


def evenements(cx, req):
    _abonnes.add(cx)
    return {}


# --- interface de l'IDE (lot 3) : le front est une extension -----------------------------

INTERFACE_DELAI_SEC = 30
MAX_INTERFACE = 192 * 1024       # corps d'une reponse (base64) : tient dans une trame de la prise
_front = [None]                  # connexion de l'extension qui sert l'interface
_requetes_interface = {}         # requete -> {"cid", "id", "t"}


def interface_servir(cx, req):
    if "interface" not in (cx.fiche or {}).get("capacites", []):
        raise Refus("cette extension n'a pas la capacite interface")
    _front[0] = cx
    print(f"[extensions] {cx.fiche['nom']} sert l'interface de l'IDE", flush=True)
    return {}


def recevoir_requete_interface(cid, card, msg):
    """Requete d'une coquille (session Noise, role interface) : au front, ou 503."""
    front = _front[0]
    if front is None:
        for m in pont_v2.reponse_interface(msg["id"], 503, "text/plain; charset=utf-8",
                                           "L'interface de l'IDE n'est pas démarrée.".encode()):
            pont_v2.envoyer_brut(cid, m)
        return
    rid = uuid.uuid4().hex[:16]
    _requetes_interface[rid] = {"cid": cid, "id": msg["id"], "t": time.time()}
    try:
        front.envoyer({"type": "evenement", "genre": "interface_requete", "requete": rid,
                       "appareil": {"id": cid, "nom": card.get("nom") or cid},
                       "methode": msg["methode"], "chemin": msg["chemin"], "corps": msg.get("corps")})
    except OSError:
        _front[0] = None
        _requetes_interface.pop(rid, None)
        for m in pont_v2.reponse_interface(msg["id"], 503, "text/plain; charset=utf-8", b"front injoignable"):
            pont_v2.envoyer_brut(cid, m)


def interface_reponse(cx, req):
    if cx is not _front[0]:
        raise Refus("cette extension ne sert pas l'interface")
    attente = _requetes_interface.pop(req.get("requete"), None)
    if not attente:
        raise Refus("requete d'interface inconnue ou expiree")
    statut = req.get("statut")
    if not isinstance(statut, int) or not 100 <= statut <= 599:
        raise Refus("statut invalide")
    type_contenu = str(req.get("type_contenu") or "application/octet-stream")[:128]
    corps_b64 = req.get("corps") or ""
    if not isinstance(corps_b64, str) or len(corps_b64) > MAX_INTERFACE:
        raise Refus("corps trop gros")
    try:
        corps = base64.b64decode(corps_b64, validate=True)
    except ValueError:
        raise Refus("corps : base64 attendu")
    for m in pont_v2.reponse_interface(attente["id"], statut, type_contenu, corps):
        if not pont_v2.envoyer_brut(attente["cid"], m):
            raise Refus("la coquille n'est plus connectee")
    return {}


def _expirer_interface():
    """Requetes sans reponse du front : 504 a la coquille."""
    for rid, a in list(_requetes_interface.items()):
        if time.time() - a["t"] > INTERFACE_DELAI_SEC:
            _requetes_interface.pop(rid, None)
            for m in pont_v2.reponse_interface(a["id"], 504, "text/plain; charset=utf-8", b"pas de reponse du front"):
                pont_v2.envoyer_brut(a["cid"], m)


REQUETES = {
    "ticket_proposer": ticket_proposer,
    "ticket_valider": ticket_valider,
    "ticket_lire": ticket_lire,
    "tickets_lister": tickets_lister,
    "journal_ecrire": journal_ecrire,
    "journal_lire": journal_lire,
    "travail_deposer": travail_deposer,
    "artefact_deposer": artefact_deposer,
    "artefact_lire": artefact_lire,
    "contre_epreuve": contre_epreuve,
    "evenements": evenements,
    "interface_servir": interface_servir,
    "interface_reponse": interface_reponse,
}
LONGUES = {"ticket_valider"}  # attendent l'humain : traitees a part, la connexion continue


# --- ouverture ----------------------------------------------------------------------------

def _appelant_autorise(conn):
    """Sous Linux : l'appelant est le meme utilisateur que le service (ou root)."""
    if not hasattr(socket, "SO_PEERCRED"):
        return True  # macOS : les droits 0600 de la prise suffisent
    _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return uid in (0, os.getuid())


def _enroler(cx, msg):
    nom = _nom_sur(msg.get("nom"), "nom")
    identite = msg.get("identite")
    caps = msg.get("capacites") or []
    if not isinstance(caps, list) or len(caps) > 64:
        raise Refus("capacites : 64 noms simples au plus")
    for c in caps:
        _nom_sur(c, "capacite")
    try:
        eid = np_.identifiant(identite)
    except np_.ErreurPont as exc:
        raise Refus("identite : %s" % exc)
    if any(f.get("nom") == nom for f in fiches().values()):
        raise Refus("une extension porte deja ce nom")
    if not _enrolement.acquire(blocking=False):
        raise Refus("un enrolement est deja en cours")
    try:
        charge = {"type": "extension", "parc": pont_v2.parc(), "id": "extension-" + nom, "nom": nom,
                  "identite": identite, "capacites": sorted(set(caps)), "empreinte": eid}
        validation = _valider("extension", charge, _dire("extensions.extension_quoi", nom=nom))
        if np_.verifier_validation(validation, validation_cle(validation), pont_v2.parc(), charge) != "accepte":
            raise Refus("enrolement refuse par l'humain")
        with _verrou:
            f = fiches()
            f[eid] = {"nom": nom, "identite": identite, "capacites": charge["capacites"], "validation": validation,
                      "ajoute": time.time()}
            _ecrire_json(_dossier("extensions", "fiches.json"), f)
        print(f"[extensions] {nom} enrolee ({eid}), capacites : {', '.join(charge['capacites']) or 'aucune'}",
              flush=True)
        _annoncer(_dire("extensions.enrolee", nom=nom), "appairage_reussi")
        return eid
    finally:
        _enrolement.release()


def _ouvrir(cx):
    global _nonces
    defi = os.urandom(32)
    cx.envoyer({"type": "bonjour", "version": 2, "parc": pont_v2.parc(), "defi": base64.b64encode(defi).decode()})
    msg = _recevoir(cx.conn)
    if msg is None:
        return False
    if msg["type"] == "enrolement_demande":
        try:
            cx.envoyer({"type": "enrole", "id": _enroler(cx, msg)})
        except (Refus, np_.ErreurPont) as exc:
            cx.envoyer({"type": "erreur", "raison": str(exc)})
        return False
    if msg["type"] != "presentation":
        raise Refus("presentation attendue")
    objet = msg.get("objet") or {}
    fiche = fiches().get(objet.get("signataire"))
    if not fiche:
        raise Refus("extension inconnue")
    with _verrou:
        if _nonces is None:
            _nonces = np_.RegistreNonces()
        charge = np_.verifier_objet(objet, fiche["identite"], "extension_ouverture", pont_v2.parc(), _nonces)
    if charge.get("defi") != base64.b64encode(defi).decode():
        raise Refus("defi different")
    cx.fiche, cx.id = fiche, objet["signataire"]
    cx.envoyer({"type": "accueil", "id": cx.id, "nom": fiche["nom"], "capacites": fiche["capacites"]})
    print(f"[extensions] {fiche['nom']} connectee", flush=True)
    return True


def _traiter(cx, req):
    rid = req.get("id")
    try:
        if not isinstance(rid, str) or not 0 < len(rid) <= 64:
            raise Refus("id de requete attendu")
        fonction = REQUETES.get(req["type"])
        if fonction is None:
            raise Refus("type de requete inconnu : %s" % str(req["type"])[:64])
        reponse = fonction(cx, req)
        cx.envoyer(dict(reponse, type="reponse", reponse_a=rid))
    except (Refus, np_.ErreurPont, OSError, KeyError, TypeError, ValueError) as exc:
        raison = str(exc) or exc.__class__.__name__
        try:
            cx.envoyer({"type": "erreur", "reponse_a": rid, "raison": raison[:500]})
        except OSError:
            pass


def _servir(conn):
    cx = Connexion(conn)
    try:
        if not _appelant_autorise(conn):
            raise Refus("appelant d'un autre utilisateur")
        conn.settimeout(30)
        if not _ouvrir(cx):
            return
        conn.settimeout(None)
        while True:
            req = _recevoir(conn)
            if req is None:
                break
            if req["type"] in LONGUES:
                threading.Thread(target=_traiter, args=(cx, req), daemon=True).start()
            else:
                _traiter(cx, req)
    except (Refus, np_.ErreurPont) as exc:
        try:
            cx.envoyer({"type": "erreur", "raison": str(exc)[:500]})
        except OSError:
            pass
        print(f"[extensions] connexion refusee : {exc}", flush=True)
    except (OSError, socket.timeout):
        pass
    finally:
        _abonnes.discard(cx)
        if _front[0] is cx:
            _front[0] = None
        if cx.fiche:
            print(f"[extensions] {cx.fiche['nom']} deconnectee", flush=True)
        conn.close()


def _surveiller():
    """Previent les abonnes quand un travail depose par une extension est fini."""
    while True:
        time.sleep(SURVEILLANCE_SEC)
        _expirer_interface()
        if not _travaux:
            continue
        try:
            for job, resultat, _dossier_travail in compagnon.finished_jobs():
                if job.get("id") in _travaux:
                    tid, _eid = _travaux.pop(job["id"])
                    _evenement("travail_fini", ticket=tid, travail=job["id"], etat=resultat.get("etat"),
                               message=str(resultat.get("message") or "")[:1000])
        except Exception as exc:
            print(f"[extensions] surveillance des travaux : {exc!r}", flush=True)


def demarrer():
    """Ouvre la prise (dossier 0700, prise 0600) et sert les extensions dans des fils."""
    dossier = _dossier("extensions")
    os.makedirs(dossier, mode=0o700, exist_ok=True)
    os.chmod(dossier, 0o700)
    prise = chemin_prise()
    if os.path.lexists(prise):
        os.remove(prise)
    serveur = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ancien = os.umask(0o177)
    try:
        serveur.bind(prise)
    finally:
        os.umask(ancien)
    os.chmod(prise, 0o600)
    serveur.listen(8)

    def boucle():
        while True:
            try:
                conn, _ = serveur.accept()
            except OSError:
                return  # prise fermee (arret du service, fin d'essai)
            threading.Thread(target=_servir, args=(conn,), daemon=True).start()
    pont_v2._interface[0] = recevoir_requete_interface
    threading.Thread(target=boucle, daemon=True, name="extensions").start()
    threading.Thread(target=_surveiller, daemon=True, name="extensions-travaux").start()
    print(f"[extensions] point d'acces ouvert : {prise}", flush=True)
    return serveur


# --- client minimal (essais, et exemple pour les extensions) -------------------------------

class Client:
    """Client de reference : `Client(coffre).ouvrir()` puis `requete(type, **champs)`."""

    def __init__(self, coffre, prise=None):
        self.coffre, self.prise = coffre, prise or chemin_prise()
        self.conn = None
        self.bonjour = None

    def _connecter(self):
        self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.conn.connect(self.prise)
        self.bonjour = _recevoir(self.conn)

    def _envoyer(self, msg):
        donnees = json.dumps(msg).encode("utf-8")
        self.conn.sendall(struct.pack(">I", len(donnees)) + donnees)

    def enroler(self, nom, capacites):
        self._connecter()
        self._envoyer({"type": "enrolement_demande", "nom": nom, "identite": self.coffre.cle_publique(),
                       "capacites": capacites})
        r = _recevoir(self.conn)
        self.conn.close()
        return r

    def ouvrir(self):
        self._connecter()
        defi = self.bonjour["defi"]
        objet = self.coffre.signer_objet({"type": "extension_ouverture", "parc": self.bonjour["parc"],
                                          "id": "ouverture-" + uuid.uuid4().hex[:16], "defi": defi})
        self._envoyer({"type": "presentation", "objet": objet})
        return _recevoir(self.conn)

    def requete(self, type_, **champs):
        rid = uuid.uuid4().hex[:16]
        self._envoyer(dict(champs, type=type_, id=rid))
        while True:
            r = _recevoir(self.conn)
            if r is None or r.get("reponse_a") == rid:
                return r

    def recevoir(self):
        return _recevoir(self.conn)

    def fermer(self):
        if self.conn:
            self.conn.close()


def _cli(args):
    if args[:1] == ["liste"]:
        for eid, f in sorted(fiches().items(), key=lambda x: x[1]["nom"]):
            print(f"{f['nom']}\t{eid}\t{', '.join(f['capacites'])}")
        return 0
    if args[:1] == ["retirer"] and len(args) == 2:
        with _verrou:
            f = fiches()
            garde = {i: x for i, x in f.items() if x["nom"] != args[1]}
            if len(garde) == len(f):
                print("Pas d'extension de ce nom.")
                return 1
            _ecrire_json(_dossier("extensions", "fiches.json"), garde)
        print(f"Extension {args[1]} retiree : elle ne peut plus se connecter.")
        return 0
    print("usage : extensions.py liste | retirer <nom>")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
