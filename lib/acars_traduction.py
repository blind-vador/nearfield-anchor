"""Messages de donnees d'avions (ACARS, VDL2) dits en langage humain
(2026-10-03, demande utilisateur : "dire ce que l'avion a voulu dire",
expliquer de facon legere mais serieuse, la phase de vol, et a quoi
ressemble l'appareil quand on connait son modele).

Deux etages :
1. FICHE deterministe : ce qu'on sait decoder avec certitude -- type de
   trame VDL2 (accuse de reception, controle de liaison, passage de relais
   entre stations au sol avec aeroport de destination et position, ACARS
   transporte), label ACARS (table des labels surs, traduction OOOI de la
   serie Q, texte libre H1), donnees decodees par libacars (CPDLC, ADS-C),
   et l'appareil (lib/avions_info.py : modele, compagnie, trajet, phase de
   vol, allure, livree).
2. PHRASE : Gemini met la fiche en 2 ou 3 phrases a lire a voix haute, sans
   rien inventer (consigne explicite) ; repli sur une phrase fixe si Gemini
   ne repond pas ou si le plafond horaire est atteint.
"""
import json
import time

from phrases import Etiquettes, dire

GEMINI_PER_HOUR = 30

# Labels ACARS dont le sens est etabli (ARINC 618/620) ; les autres sont dits
# "message de type X" et leur contenu est explique avec prudence.
LABELS = Etiquettes("acars.label", {
    "_d": "message de service sans contenu : l'avion confirme que la liaison de données fonctionne",
    "Q0": "test de liaison",
    "SQ": "balise d'une station au sol, qui signale sa présence aux avions",
    "H1": "message vers ou depuis un terminal de bord, souvent un texte de l'équipage ou de la compagnie",
    "5Z": "message propre à la compagnie aérienne",
    "B6": "rapport automatique de position pour le contrôle aérien",
    "AA": "message de contrôle aérien par liaison de données",
    "QA": "étape de vol : sortie de la porte d'embarquement",
    "QB": "étape de vol : décollage",
    "QC": "étape de vol : atterrissage",
    "QD": "étape de vol : arrivée à la porte",
}, "sens d'un type de message ACARS")
FRAME_KINDS = Etiquettes("vdl2.trame", {
    "accuse": "accusés de réception de la liaison de données",
    "liaison": "messages de contrôle de la liaison réseau avec la station au sol",
    "relais": "passage de relais vers une autre station au sol",
    "connexion": "ouverture de la liaison avec une station au sol",
    "acars": "message ACARS",
    "controle": "message de contrôle aérien par liaison de données",
    "position": "rapport automatique de position",
    "autre": "trame de données non détaillée",
}, "nature d'une trame VDL2 captée")
PHASES = Etiquettes("avion.phase", {
    "sol": "au sol, à l'arrêt", "roulage": "en train de rouler au sol", "decollage": "en train de décoller",
    "montee": "en montée", "croisiere": "en croisière", "palier": "en palier", "descente": "en descente",
    "approche": "en approche finale",
}, "phase de vol d'un avion")

PROMPT = """Tu aides la voix d'un récepteur radio domestique à dire, à une personne aveugle, un
message de données capté d'un avion (ou d'un autre système électronique embarqué). À partir de la
fiche ci-dessous, rends un objet JSON avec trois champs, en {langue} :
- "description" : texte alternatif court de l'appareil pour une personne aveugle, sans verbe
  principal : allure, livrée, phase de vol, trajet, seulement ce que la fiche donne
  (exemple : "biréacteur moyen-courrier blanc et vert, en croisière à 10 500 mètres vers Nice") ;
- "donnees" : les données techniques du message, lues simplement, sans les expliquer
  (exemple : "passage de relais vers une station au sol, destination LFMN") ;
- "sens" : ce que l'appareil a voulu dire, en clair, en une phrase commençant par un sujet
  (exemple : "il transfère sa liaison de données vers une nouvelle station, en route vers Nice").
N'explique pas le protocole (ACARS, VDL2...) : l'utilisateur le connaît. Ton léger mais sérieux.
N'invente rien ; si le sens exact n'est pas certain, dis-le dans "sens". Si la destination annoncée
par l'avion diffère du trajet de la fiche appareil (base publique, parfois périmée), fie-toi à
l'avion et donne le nom de l'aéroport si tu connais son code OACI. Altitudes en mètres (1 pied =
0,3 mètre, arrondi à la centaine). Pas de markdown, pas d'emoji.

Fiche :
{fiche}
"""

_calls = []


def _budget():
    now = time.time()
    _calls[:] = [t for t in _calls if now - t < 3600]
    if len(_calls) >= GEMINI_PER_HOUR:
        return False
    _calls.append(now)
    return True


# --- fiches ---------------------------------------------------------------------------

def acars_meaning(msg):
    """Partie "message" d'une fiche, pour un message ACARS (acarsdec ou VDL2)."""
    label = msg.get("label") or ""
    text = (msg.get("text") or msg.get("msg_text") or "").strip()
    out = {"type": "ACARS", "label": label}
    if label in LABELS:
        out["sens"] = LABELS[label]
    if text:
        out["texte"] = text[:400]
    if msg.get("sublabel"):
        out["sous_label"] = msg["sublabel"]
    try:
        import acars_watch
        fields = acars_watch._extract_q_fields(label, text)
        if fields:
            out["etape_de_vol"] = acars_watch.Q_PHASE_VERBS.get(label)
            out["champs"] = fields
    except Exception:
        pass
    if msg.get("libacars"):
        out["donnees_decodees"] = msg["libacars"]
    return out


def _find(obj, key):
    """Premier sous-objet nomme key dans une trame dumpvdl2 imbriquee."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find(v, key)
            if found is not None:
                return found
    return None


def frame_kind(avlc):
    """Nature d'une trame AVLC (dumpvdl2) et ses details utiles."""
    if avlc.get("acars"):
        return "acars", acars_meaning(avlc["acars"])
    if _find(avlc, "cpdlc") is not None:
        return "controle", {"donnees_decodees": _find(avlc, "cpdlc")}
    if _find(avlc, "adsc") is not None:
        return "position", {"donnees_decodees": _find(avlc, "adsc")}
    xid = avlc.get("xid")
    if xid:
        params = {p.get("name"): p.get("value") for p in (xid.get("vdl_params") or [])}
        # dst_airport : destination du plan de vol, annoncee par l'avion lui-meme
        names = {"dst_airport": "destination_annoncee_par_l_avion", "ac_location": "position_de_l_avion",
                 "airport_coverage": "aeroports_couverts_par_la_station"}
        detail = {names[k]: params[k] for k in names if k in params}
        kind = "relais" if "HO" in str(xid.get("type")) else "connexion"
        return kind, dict(detail, description=xid.get("type_descr"))
    if avlc.get("frame_type") == "S":
        return "accuse", {}
    if avlc.get("x25") is not None:
        return "liaison", {}
    return "autre", {}


def substantive(kind, detail):
    """Le message dit-il quelque chose (pas seulement l'entretien de la liaison) ?"""
    if kind in ("controle", "position"):
        return True
    if kind == "relais":
        return bool(detail.get("destination_annoncee_par_l_avion") or detail.get("position_de_l_avion"))
    if kind == "acars":
        return bool(detail.get("texte") or detail.get("etape_de_vol") or detail.get("donnees_decodees")) \
            or detail.get("label") not in ("_d", "Q0", "SQ")
    return False


# --- phrases -------------------------------------------------------------------------

def _who(card):
    """ "un Airbus A320neo d'easyJet immatricule HB-AYE" (fiches, journal)."""
    parts = [dire("acars.un_modele", modele=card["modele"]) if card.get("modele") else dire("acars.un_avion")]
    if card.get("compagnie"):
        elided = card["compagnie"][:1].lower() in "aeiouyéèêh"
        parts.append(dire("acars.d_compagnie" if elided else "acars.de_compagnie", compagnie=card["compagnie"]))
    if card.get("immatriculation"):
        parts.append(dire("acars.immatricule", immatriculation=card["immatriculation"]))
    return " ".join(parts)


def sentence(card, description, donnees, sens):
    """Phrase d'un message d'avion (2026-10-03, demande utilisateur) :
    "Un appareil modele X, <description>, immatricule Y, envoie le message
    suivant : <donnees>, signifiant que <sens>." """
    appareil = dire("avion.appareil_modele", modele=card["modele"]) if card.get("modele") else dire("avion.appareil")
    sens = (sens or "").strip().rstrip(".")
    if sens:
        sens = sens[0].lower() + sens[1:]
    key = "avion.phrase_qu" if sens[:1] in "aeiouyhéèê" and sens else "avion.phrase"
    return dire(key, appareil=appareil,
                description=f", {description.strip().rstrip('.,')}" if description else "",
                immatriculation=(", " + dire("acars.immatricule", immatriculation=card["immatriculation"])
                                 if card.get("immatriculation") else ""),
                donnees=(donnees or "").strip().rstrip("."), sens=sens or dire("avion.sens_inconnu"))


def _plain_description(card):
    parts = []
    if card.get("livree"):
        parts.append(card["livree"])
    elif card.get("compagnie"):
        parts.append(dire("avion.de_la_compagnie", compagnie=card["compagnie"]))
    if card.get("phase"):
        parts.append(PHASES[card["phase"]] + (" " + dire("avion.a_metres", metres=card["altitude_m"])
                                               if card.get("altitude_m") and card["phase"] not in ("sol", "roulage")
                                               else ""))
    if card.get("depart") and card.get("arrivee"):
        parts.append(dire("avion.de_vers", depart=card["depart"], arrivee=card["arrivee"]))
    return ", ".join(parts)


def plain(card, kinds, protocole=None):
    """Meme phrase, sans Gemini."""
    return sentence(card, _plain_description(card), ", ".join(FRAME_KINDS[k] for k in kinds), None)


def explain(card, messages, protocole):
    """Phrase a dire pour un appareil et ses messages ({nature, details})."""
    kinds = list(dict.fromkeys(m["nature"] for m in messages))
    fiche = {"protocole": protocole, "appareil": card,
             "messages": [{"nature": FRAME_KINDS[m["nature"]], **m["details"]} for m in messages]}
    if card.get("phase"):
        fiche["appareil"] = dict(card, phase=PHASES[card["phase"]])
    if _budget():
        try:
            import avions_info
            import gemini
            answer, problem = gemini.ask(PROMPT.format(langue=avions_info._langue(),
                                                       fiche=json.dumps(fiche, ensure_ascii=False, default=str)[:3500]),
                                         temperature=0.4, json_mode=True)
            if isinstance(answer, str):
                answer = json.loads(answer)
            if isinstance(answer, dict) and not problem and answer.get("donnees"):
                return sentence(card, answer.get("description"), answer.get("donnees"), answer.get("sens")), fiche
            print(f"[acars_traduction] Gemini : {problem or answer}")
        except Exception as exc:
            print(f"[acars_traduction] Gemini : {exc!r}")
    return plain(card, kinds), fiche
