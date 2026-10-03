import json
import socket
import subprocess
import time

from phrases import Etiquettes, dire

# --- Veille ACARS (donnees numeriques avion/compagnie) ---
# Ajoute en session (2026-08-31, demande explicite de l'utilisateur) --
# libacars + acarsdec (TLeconte) compiles depuis les sources sur ce Pi (ni
# l'un ni l'autre n'est empaquete Debian), installes dans /usr/local/bin.
# Frequences europeennes standard groupees a moins de 200 kHz d'ecart pour
# tenir dans une seule capture RTL-SDR (131.525/131.550/131.725 MHz) --
# 136.900/136.975 MHz existent aussi mais sont trop loin (~5.4 MHz) pour
# rentrer dans la meme capture avec ce dongle, laissees de cote pour
# l'instant plutot que d'ajouter un second passage dedie.
ACARS_FREQUENCIES_MHZ = ["131.525", "131.550", "131.725"]
ACARS_UDP_PORT = 23457  # local uniquement, distinct du port dsd-fme (23456)
ACARS_DEDUP_WINDOW_SEC = 300  # meme evenement (vol+phase, ou vol+texte) pas repete avant 5 min
ACARS_DEDUP_PURGE_FACTOR = 4  # purge les entrees > 4x la fenetre pour ne pas grossir indefiniment

# --- Traduction OOOI serie Q -> phrase francaise vulgarisee ---
# Demande explicite de l'utilisateur (2026-08-31) : plutot que d'ignorer
# les messages structures, les "traduire" en une phrase humaine. Portage
# DIRECT des extracteurs de champs de label.c (source officielle
# d'acarsdec, memes decalages d'octets exacts) -- pas une supposition.
# Perimetre volontairement limite a la serie Q (protocole OOOI moderne,
# la plus probable en trafic reel europeen actuel) -- les labels
# numeriques/legacy (10,11,12,15,17,20,21,26,2N,2Z,33,39,44,45,80,83,8D,
# 8E,8S, egalement geres par acarsdec en interne, cf. label.c) restent
# hors perimetre pour l'instant : aucun exemple reel capte cette session
# pour valider leur format avant de les annoncer a voix haute -- a
# etendre plus tard si du vrai trafic les confirme.
#
# sa=aeroport/station actuel, da=destination (codes bruts, PAS forcement
# IATA/ICAO standard -- format propre a chaque compagnie, aucune table de
# correspondance code->ville disponible localement pour l'instant),
# heures gout/woff/won/gin/eta au format brut HHMM.
Q_FIELD_SPECS = {
    "Q1": [("sa", 0, 4), ("gout", 4, 8), ("woff", 8, 12), ("won", 12, 16), ("gin", 16, 20), ("da", 24, 28)],
    "Q2": [("sa", 0, 4), ("eta", 4, 8)],
    "QA": [("sa", 0, 4), ("gout", 4, 8)],
    "QB": [("sa", 0, 4), ("woff", 4, 8)],
    "QC": [("sa", 0, 4), ("won", 4, 8)],
    "QD": [("sa", 0, 4), ("gin", 4, 8)],
    "QE": [("sa", 0, 4), ("gout", 4, 8), ("da", 8, 12)],
    "QF": [("sa", 0, 4), ("woff", 4, 8), ("da", 8, 12)],
    "QG": [("sa", 0, 4), ("gout", 4, 8), ("gin", 8, 12)],
    "QH": [("sa", 0, 4), ("gout", 4, 8)],
    "QK": [("sa", 0, 4), ("won", 4, 8), ("da", 8, 12)],
    "QL": [("da", 0, 4), ("gin", 8, 12), ("sa", 13, 17)],
    "QM": [("da", 0, 4), ("sa", 8, 12)],
    "QN": [("da", 4, 8), ("eta", 8, 12)],
    "QP": [("sa", 0, 4), ("da", 4, 8), ("gout", 8, 12)],
    "QQ": [("sa", 0, 4), ("da", 4, 8), ("woff", 8, 12)],
    "QR": [("sa", 0, 4), ("da", 4, 8), ("won", 8, 12)],
    "QS": [("sa", 0, 4), ("da", 4, 8), ("gin", 8, 12)],
    "QT": [("sa", 0, 4), ("da", 4, 8), ("gout", 8, 12), ("gin", 12, 16)],
}

# Verbe/evenement vulgarise par label -- un evenement principal par
# label (Q1 combine tout, traite a part comme "compte-rendu complet").
Q_PHASE_VERBS = Etiquettes("acars.evenement", {
    "Q1": "compte-rendu de vol complet",
    "Q2": "heure d'arrivée estimée mise à jour",
    "QA": "sortie de porte",
    "QB": "décollage",
    "QC": "atterrissage",
    "QD": "arrivée à la porte",
    "QE": "sortie de porte",
    "QF": "décollage",
    "QG": "sortie de porte",
    "QH": "sortie de porte",
    "QK": "atterrissage",
    "QL": "arrivée à la porte",
    "QM": "arrivée",
    "QN": "heure d'arrivée estimée",
    "QP": "sortie de porte",
    "QQ": "décollage",
    "QR": "atterrissage",
    "QS": "arrivée à la porte",
    "QT": "sortie de porte",
}, "étape d'un vol confirmée par message ACARS", par_libelle=True)


def _extract_q_fields(label, txt):
    """Retourne un dict des champs non vides pour un label serie Q connu
    (voir Q_FIELD_SPECS), ou None si le label n'est pas gere ou le texte
    trop court pour l'offset attendu."""
    specs = Q_FIELD_SPECS.get(label)
    if not specs or not txt:
        return None
    result = {}
    for name, start, end in specs:
        if len(txt) < end:
            continue
        value = txt[start:end].strip()
        if value:
            result[name] = value
    return result or None


SOUND_EFFECTS_DIR = "/media/nas/nd1/server/radio_scaner"
ACARS_SOUND = f"{SOUND_EFFECTS_DIR}/decoding_numeric.mp3"


# Historique de deduplication, en memoire seulement (remis a zero a chaque
# redemarrage du service, comme le reste des traces glissantes du projet).
_recent_events = {}


def _identity(msg):
    return msg.get("flight") or msg.get("tail") or "inconnu"


def _should_announce(key, window=ACARS_DEDUP_WINDOW_SEC):
    now = time.time()
    last = _recent_events.get(key)
    if last is not None and now - last < window:
        return False
    _recent_events[key] = now
    stale_before = now - ACARS_DEDUP_WINDOW_SEC * ACARS_DEDUP_PURGE_FACTOR
    stale = [k for k, t in _recent_events.items() if t < stale_before]
    for k in stale:
        del _recent_events[k]
    return True


def _process_message(msg):
    """Tout message ACARS est dit au fil de l'eau, en langage humain
    (lib/acars_traduction.py), comme une prise vocale du journal "messages"
    (lib/prise_vocale.py). Seule une retransmission identique est ecartee
    (ACARS_DEDUP_WINDOW_SEC). Rend True si c'est un vrai contenu."""
    import acars_traduction as tr
    import avions_info
    import veille_donnees as vd
    identity = _identity(msg)
    detail = tr.acars_meaning(msg)
    content = tr.substantive("acars", detail)
    key = (identity, msg.get("label", ""), (msg.get("text") or "")[:80])
    if not _should_announce(key):
        return False
    fiche = {"protocole": "ACARS", "source": identity, "frequence_hz": _freq_hz(msg), "details": detail}

    def text():  # dans la file d'annonces : base d'avions et Gemini sans bloquer l'ecoute
        card = avions_info.card(registration=(msg.get("tail") or "").lstrip(".") or None, callsign=msg.get("flight"))
        fiche["appareil"] = card
        if content:
            return tr.explain(card, [{"nature": "acars", "details": detail}], "ACARS")[0]
        return tr.plain(card, ["acars"], "ACARS")

    vd.announce(text, ACARS_SOUND, f"message_avion_ACARS_{identity}", "acars_watch", fiche=fiche)
    return content


def _freq_hz(msg):
    try:
        return float(msg.get("freq")) * 1e6
    except (TypeError, ValueError):
        return None


def scan_window(dwell_sec=30):
    """Une passe d'ecoute ACARS, meme discipline de partage du dongle que
    airband_bridge.scan_window : lance acarsdec sur les frequences
    surveillees, ecoute le flux JSON UDP local pendant dwell_sec, annonce
    ce qui merite de l'etre, arrete proprement. Retourne True si au moins
    une annonce reelle a eu lieu pendant cette passe."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", ACARS_UDP_PORT))
    except OSError as exc:
        print(f"[acars_watch] impossible d'ouvrir le port UDP local : {exc}")
        sock.close()
        return False
    sock.settimeout(1.0)

    try:
        # -r <device> <freqs...> doit etre EN DERNIER sur la ligne de
        # commande -- les frequences sont des arguments positionnels
        # consommes jusqu'a la fin de argv. Les mettre avant d'autres
        # options fait que ces options sont lues comme des frequences
        # supplementaires ("Frequencies too far apart", bug trouve en
        # testant l'integration).
        proc = subprocess.Popen(
            ["acarsdec", "-e", "-j", f"127.0.0.1:{ACARS_UDP_PORT}", "-o", "0",
             "-r", "0"] + ACARS_FREQUENCIES_MHZ,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError:
        print("[acars_watch] binaire acarsdec introuvable")
        sock.close()
        return False

    announced_any = False
    try:
        time.sleep(1.5)  # laisse acarsdec initialiser le dongle avant de lire
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            print(f"[acars_watch] acarsdec s'est arrete immediatement : {stderr[-300:]}")
            return False

        deadline = time.time() + dwell_sec
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            try:
                msg = json.loads(data.decode(errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if _process_message(msg):
                announced_any = True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        sock.close()

    return announced_any
