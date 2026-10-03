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

GEMINI_PER_HOUR = 20

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

PROMPT = """Tu es la voix d'un récepteur radio domestique. Il vient de capter des messages de données
envoyés par un avion (ou un autre système électronique embarqué) à des stations au sol. À partir de la
fiche ci-dessous, dis en 2 ou 3 phrases courtes, à lire à voix haute pour une personne aveugle :
qui est l'appareil et à quoi il ressemble si c'est indiqué, où il en est de son vol, et ce que ses
messages veulent dire en clair. Explique simplement les sigles. Ton léger mais sérieux.
N'invente rien qui ne soit pas dans la fiche ; si le sens exact d'un message n'est pas certain,
dis-le simplement. Ne parle de la phase de vol que si la fiche la donne. Altitudes en mètres
(convertis les pieds : 1 pied = 0,3 mètre, arrondi à la centaine). Commence directement par
l'appareil, sans formule d'introduction. Pas de markdown, pas d'emoji, pas de liste, pas de codes
bruts épelés inutilement.
Langue de la réponse : {langue}.

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
        detail = {k: params[k] for k in ("dst_airport", "ac_location", "airport_coverage") if k in params}
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
        return bool(detail.get("dst_airport") or detail.get("ac_location"))
    if kind == "acars":
        return bool(detail.get("texte") or detail.get("etape_de_vol") or detail.get("donnees_decodees")) \
            or detail.get("label") not in ("_d", "Q0", "SQ")
    return False


# --- phrases -------------------------------------------------------------------------

def _who(card):
    parts = [dire("acars.un_modele", modele=card["modele"]) if card.get("modele") else dire("acars.un_avion")]
    if card.get("compagnie"):
        elided = card["compagnie"][:1].lower() in "aeiouyéèêh"
        parts.append(dire("acars.d_compagnie" if elided else "acars.de_compagnie", compagnie=card["compagnie"]))
    if card.get("immatriculation"):
        parts.append(dire("acars.immatricule", immatriculation=card["immatriculation"]))
    return " ".join(parts)


def plain(card, kinds):
    """Phrase fixe, sans Gemini."""
    text = dire("acars.capte", qui=_who(card), quoi=", ".join(FRAME_KINDS[k] for k in kinds))
    if card.get("phase") and card.get("altitude_m") and not card.get("phase", "").startswith(("sol", "roulage")):
        text += " " + dire("acars.phase_altitude", phase=PHASES[card["phase"]], metres=card["altitude_m"])
    elif card.get("phase"):
        text += " " + dire("acars.phase", phase=PHASES[card["phase"]])
    if card.get("depart") and card.get("arrivee"):
        text += " " + dire("acars.trajet", depart=card["depart"], arrivee=card["arrivee"])
    return text


def explain(card, messages):
    """Phrase a dire pour un appareil et ses messages ({nature, details})."""
    kinds = list(dict.fromkeys(m["nature"] for m in messages))
    fiche = {"appareil": card, "messages": [{"nature": FRAME_KINDS[m["nature"]], **m["details"]} for m in messages]}
    if card.get("phase"):
        fiche["appareil"] = dict(card, phase=PHASES[card["phase"]])
    if _budget():
        try:
            import avions_info
            import gemini
            text, problem = gemini.ask(PROMPT.format(langue=avions_info._langue(),
                                                     fiche=json.dumps(fiche, ensure_ascii=False, default=str)[:3500]),
                                       temperature=0.4)
            if text and not problem:
                return " ".join(text.split()), fiche
            print(f"[acars_traduction] Gemini : {problem}")
        except Exception as exc:
            print(f"[acars_traduction] Gemini : {exc!r}")
    return plain(card, kinds), fiche
