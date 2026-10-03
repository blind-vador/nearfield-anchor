"""Traduction en francais parle des stations APRS : type de station (d'apres
son symbole APRS, sinon son suffixe) et commentaire libre, ecrit par et pour
des radioamateurs (reglages de relais, tensions, logiciels, telemetrie
compressee...).

Principe : on traduit ce qu'on sait lire (frequences, tonalites, portee,
tension, altitude, sigles courants), on retire ce qui n'a pas de sens a
l'oral (telemetrie compressee, adresses web et mail, numeros de version,
codes techniques), et on garde le reste tel quel.
"""
import re

from phrases import Etiquettes, dire

# Symboles APRS, table principale ("/").
PRIMARY = Etiquettes("aprs.type", {
    "-": "station fixe", ">": "voiture", "[": "piéton", "_": "station météo", "#": "relais numérique",
    "r": "relais radio", "v": "fourgon", "k": "camion", "u": "camion", "b": "vélo", "<": "moto",
    "R": "camping-car", "Y": "voilier", "s": "bateau", "O": "ballon", "'": "petit avion", "^": "avion",
    "X": "hélicoptère", "y": "station fixe", "j": "véhicule tout-terrain", "U": "bus", "a": "ambulance",
    "f": "camion de pompiers", "P": "police", "&": "passerelle", "`": "antenne parabolique",
    "$": "téléphone", "l": "ordinateur portable", "n": "nœud radio", "h": "hôpital", "+": "Croix-Rouge",
}, "type de station APRS", par_libelle=True)
# Table secondaire ("\\" ou une lettre/chiffre en surimpression).
SECONDARY = Etiquettes("aprs.type", {"#": "relais numérique", "&": "passerelle", "_": "station météo", "0": "nœud radio",
             "a": "station", "-": "station fixe", ">": "véhicule", "r": "relais radio"}, "type de station APRS", par_libelle=True)
OVERLAY = Etiquettes("aprs.type", {"L": "LoRa", "I": "internet", "D": "numérique", "E": "EchoLink", "R": "réception"}, "technique d'une station APRS", par_libelle=True)
SSID = Etiquettes("aprs.type", {"5": "smartphone", "7": "portatif", "9": "mobile", "13": "station météo", "14": "camion",
        "8": "bateau", "11": "ballon ou aéronef"}, "type de station APRS", par_libelle=True)

FREQ = r"(\d{2,4}(?:\.\d{1,5})?)\s?MHz"
# Frequence sans unite, dans les bandes radioamateur 2 m et 70 cm ("145.500").
BARE_FREQ = r"(?<![\d.])(14[4-8]\.\d{3,4}|4[34]\d\.\d{3,4})(?![\d.])"
WORDS = [
    (r"\bDigi\s*\+\s*i[Gg]ate\b", "relais numérique et passerelle internet"),
    (r"(?i)\bigate\b", "passerelle internet"),
    (r"\ben F/", "en France"),
    (r"/P\b", " en portable"),
    (r"\bDIGI[ _]NED\b|\bDIGI\b|\bDigi\b|\bdigi\b", "relais numérique"),
    (r"\bRX ONLY\b", "réception seulement"),
    (r"\bRF-IS\b", "de la radio vers internet"),
    (r"\bWX\b", "météo"),
    (r"\bADRASEC\s*(\d{2})\b", r"radioamateurs de la sécurité civile, département \1"),
    (r"\bAdrasec\s*(\d{2})\b", r"radioamateurs de la sécurité civile, département \1"),
    (r"\bD[ée]?p(?:ar)?t\.?\s*(\d{2})\b", r"département \1"),
    (r"\bsysop\b", "responsable"),
    (r"\bBBS\b", "messagerie radio"),
    (r"\bNODE\b", "nœud"),
    (r"\bQRV\b", "à l'écoute"),
    (r"^\.?73\b|\b73\b", "salutations"),
    (r"\bscan-veille\b", "veille"),
    (r"\bMobile\b", "mobile"),
]
# Logiciels et materiels : sans interet a l'oral, retires avec leur version.
SOFTWARE = (r"\b(UIDIGI|UI-?View32|UIV32|BPQ32|MMDVM|Pi-Star\w*|DMRGateway|EurakMon|AGR|WX3IN1|ws2300|"
            r"WMR200|Oregon Scient\.?|ANYTONE|AT-D878UV|FTM400|Linux)\b([\s-]*V?\d+(\.\w+)*)?")
EARLY_REMOVE = [
    r"\|[^|]{2,}\|",                         # telemetrie compressee (base 91)
    r"\{[^}]*\}",                             # {UIV32} : logiciel
    r"\b(https?://|www\.|github\.com)\S+",    # adresses web
    r"\S+@\S+\.\w+",                          # adresses mail
    r"\b\d{1,3}(\.\d{1,3}){3}\b",             # adresses IP
]
REMOVE = [
    r"\bRPT\b",
    r"\bPHG\w{4,5}\b",                        # code puissance-hauteur-gain
    r"\b[A-R]{2}\d{2}[a-x]{2}\b",             # carre locator (JN25ns)
    r"\bR\d+x\b",                             # canal de relais
    r"APRS for\s*", r"\bInfo:\s*",
    r"\b[A-Za-z]{1,2}\d{1,2}[a-z]?\b(?![\w/.-])",  # petits codes (B3, TU2k), pas des indicatifs
    r"[<>=]{2,}",                             # >>> <<< <=>
]


def _num(text):
    value = float(text)
    out = f"{value:.4f}".rstrip("0").rstrip(".")
    return out.replace(".", ",").replace("-", "moins ")


def _frequencies(text):
    """Frequences, decalage, tonalite et portee d'un relais -> phrases."""
    found = []
    m = re.search(r"(\d{3}\.\d{2,5})\s*<=>\s*(\d{3}\.\d{2,5})", text)
    if m:
        found.append(dire("aprs.lien_entre_megahertz", v1=_num(m.group(1)), v3=_num(m.group(2))))
        text = text.replace(m.group(0), " ")
    freqs = re.findall(FREQ, text) or re.findall(BARE_FREQ, text)
    if freqs:
        main = freqs[0]
        part = dire("aprs.frequence_megahertz", v1=_num(main))
        offset = (re.search(FREQ + r"\s+([+-]\d+\.\d+)\s?MHz", text)
                  or re.search(r"(?<!\S)()([+-]\d\.\d+)(?!\S)", text))
        if offset and float(offset.group(2)) != 0:
            part += dire("aprs.decalage_megahertz", v1=_num(offset.group(2)))
        found.append(part)
    tone = re.search(r"\bT(\d{3})\b|tone\s*(\d+)\s*Hz|CTCSS\s*(\d+)\s*Hz", text)
    if tone:
        found.append(dire("aprs.tonalite_acces_hertz", int=int(next((g for g in tone.groups() if g)))))
    rng = re.search(r"\bR(\d+)k\b", text)
    if rng:
        found.append(dire("aprs.portee_kilometres", group=rng.group(1)))
    mode = re.search(r"\((D-Star|DMR|C4FM)\)", text)
    if mode:
        found.insert(0, f"relais vocal {mode.group(1)}")
    for pattern in (FREQ + r"(\s+[+-]\d+\.\d+\s?MHz)?", BARE_FREQ, r"\bT\d{3}\b", r"tone\s*\d+\s*Hz",
                    r"CTCSS\s*\d+\s*Hz", r"\bR\d+k\b", r"\((D-Star|DMR|C4FM)\)", r"\bVoice\b",
                    r"\b(\d{1,2}cm|[1-9]m|10m)\b", r"(?<!\S)[+-]\d\.\d+(?!\S)"):
        text = re.sub(pattern, " ", text)
    return found, text


def _measures(text):
    found = []
    volts = re.search(r"\bU=\s*(\d+[.,]\d+)\s*V\b|(?<![\w.])(\d{1,2}[.,]\d{1,2})V\b", text)
    if volts:
        found.append(f"tension {_num((volts.group(1) or volts.group(2)).replace(',', '.'))} volts")
    temp = re.search(r"(?<![\w.])(-?\d{1,2}\.\d)C\b", text)
    if temp:
        found.append(dire("aprs.temperature_degres", v1=_num(temp.group(1))))
    alt = re.search(r"\bAlt:\s*(\d+)\s*m\b|\b(\d{3,4})m\b", text)
    if alt:
        found.append(dire("aprs.altitude_metres", v1=alt.group(1) or alt.group(2)))
    for pattern in (r"\bU=\s*\d+[.,]\d+\s*V\b", r"(?<![\w.])-?\d+([.,]\d+)?(V|A|PV|Ah|Wh|W|C|%)(?!\w)",
                    r"\bAlt:\s*\d+\s*m\b", r"\b\d{3,4}m\b", r"\b\d+\.\d+\s*MHz\b"):
        text = re.sub(pattern, " ", text)
    return found, text


def comment(raw, callsign=""):
    """Commentaire APRS -> texte francais parlable (vide si rien de lisible).
    callsign : indicatif de la station, retire s'il est repete."""
    text = raw or ""
    # Mic-E : octets de telemetrie colles au debut et a la fin du commentaire.
    text = re.sub(r"^[`'][^\w\s]{0,2}", "", text)
    text = re.sub(r"[_\]\[=%\"^~]{1,2}$", "", text)
    for pattern in EARLY_REMOVE:
        text = re.sub(pattern, " ", text)
    text = text.replace("_", " ")
    base = callsign.split("-")[0].strip()
    if base:
        text = re.sub(rf"\b{re.escape(base)}(-\w+)?\b", " ", text)
    freqs, text = _frequencies(text)
    text = re.sub(SOFTWARE, " ", text, flags=re.IGNORECASE)
    for pattern in REMOVE:
        text = re.sub(pattern, " ", text)
    measures, text = _measures(text)
    for pattern, repl in WORDS:
        text = re.sub(pattern, repl, text)
    text = re.sub(r"(?<!département )(?<!\S)\d{1,2}(?!\S)", " ", text)   # nombres isoles sans unite
    text = re.sub(r"\b(relais numérique|passerelle internet)( \1)+", r"\1", text)
    text = re.sub(r"\s+[-–:,.&/]+(?=\s|$)", " ", text)       # ponctuation orpheline
    text = re.sub(r"(?<=\w)-(?=\s|$)", "", text)
    text = re.sub(r"\bsalutations de\s*(?=[,.]|$)", "salutations", text)
    text = re.sub(r"([,.])[,.]+", r"\1", text)
    text = re.sub(r"^[\s\W]+|[\s,;:&/-]+$", "", " ".join(text.split()))
    if text and sum(c.isalpha() for c in text) < 3:
        text = ""
    parts = [p for p in [text] + freqs + measures if p]
    return ", ".join(parts)


# Sigles compris tels quels a l'oral (ne rendent pas un commentaire "bizarre").
KNOWN_TOKENS = {"APRS", "LORA", "VHF", "UHF", "HF", "DMR", "YSF", "C4FM", "SNCF", "OK", "GPS", "FM", "AM",
                "RX", "TX", "WX", "SMS", "USB", "PC", "QRZ"}
CALLSIGN = re.compile(r"^[A-Z0-9]{1,2}\d[A-Z]{1,4}(-\w+)?$")


def is_weird(raw, translated):
    """Vrai si le commentaire reste obscur apres traduction : sigle inconnu,
    code melant lettres et chiffres, ou commentaire non vide entierement
    vide par la traduction (on n'a rien su en tirer)."""
    raw = (raw or "").strip()
    if sum(c.isalnum() for c in raw) < 3:
        return False  # telemetrie Mic-E pure : rien a expliquer
    if not translated:
        return True
    for token in re.findall(r"[A-Za-z0-9][\w-]*", translated):
        if token.upper() in KNOWN_TOKENS or CALLSIGN.match(token):
            continue
        if re.fullmatch(r"[A-Z]{2,4}", token):  # sigle court ; MORVAN, ALLIER sont des lieux
            return True
        if re.search(r"[A-Za-z]", token) and re.search(r"\d", token):
            return True
    return False


def station_type(packet):
    """ "relais numérique LoRa", "voiture", "mobile"... ou ""."""
    table, symbol = packet.get("symbol_table"), packet.get("symbol")
    if symbol:
        if table == "/":
            kind = PRIMARY.get(symbol, "")
        else:
            kind = SECONDARY.get(symbol, "")
            tech = OVERLAY.get(table)
            if kind and tech and tech not in kind:
                kind = f"{kind} {tech}"
            elif symbol == "a":  # "station" seule : rien a dire
                kind = ""
        if kind:
            return kind
    call = packet.get("from") or ""
    if packet.get("format") == "object":
        return ""
    return SSID.get(call.rsplit("-", 1)[-1], "") if "-" in call else ""
