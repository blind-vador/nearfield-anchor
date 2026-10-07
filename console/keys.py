"""Noms parles des touches : echo de frappe et outil de reperage.

Le texte arrive par le tty (la disposition AZERTY est donc deja appliquee
par le noyau : on lit le vrai caractere, accents compris). Les touches qui
n'envoient rien au tty (Maj, Ctrl, Alt, AltGr, Windows, verrouillages...)
ne sont visibles qu'au niveau materiel : evdev, utilise seulement en mode
reperage.
"""
import os
import select
import threading
import time

import evdev

from phrases import Etiquettes, dire

CHAR_NAMES = Etiquettes("touche", {
    " ": "espace", ".": "point", ",": "virgule", ";": "point-virgule",
    ":": "deux-points", "!": "point d'exclamation", "?": "point d'interrogation",
    "'": "apostrophe", '"': "guillemet", "-": "tiret", "_": "souligné",
    "(": "parenthèse ouvrante", ")": "parenthèse fermante",
    "[": "crochet ouvrant", "]": "crochet fermant",
    "{": "accolade ouvrante", "}": "accolade fermante",
    "<": "inférieur", ">": "supérieur", "=": "égal", "+": "plus",
    "*": "étoile", "/": "barre oblique", "\\": "barre oblique inversée",
    "|": "barre verticale", "&": "et commercial", "@": "arobase",
    "#": "dièse", "$": "dollar", "%": "pour cent", "€": "euro",
    "£": "livre", "§": "paragraphe", "°": "degré", "²": "exposant deux",
    "~": "tilde", "`": "accent grave", "^": "accent circonflexe",
    "¨": "tréma", "µ": "micro", "¤": "symbole monétaire",
    "é": "e accent aigu", "è": "e accent grave", "ê": "e accent circonflexe",
    "ë": "e tréma", "à": "a accent grave", "â": "a accent circonflexe",
    "ù": "u accent grave", "û": "u accent circonflexe", "ç": "c cédille",
    "î": "i accent circonflexe", "ï": "i tréma", "ô": "o accent circonflexe",
}, "nom d'un caractère tapé", par_libelle=True)

# Sequences d'echappement de la console Linux (TERM=linux).
ESCAPE_NAMES = Etiquettes("touche", {
    "\x1b[A": "flèche haut", "\x1b[B": "flèche bas",
    "\x1b[C": "flèche droite", "\x1b[D": "flèche gauche",
    "\x1b[1~": "début", "\x1b[H": "début", "\x1b[4~": "fin", "\x1b[F": "fin",
    "\x1b[2~": "insertion", "\x1b[3~": "supprimer",
    "\x1b[5~": "page précédente", "\x1b[6~": "page suivante",
    "\x1b[[A": "F1", "\x1b[[B": "F2", "\x1b[[C": "F3", "\x1b[[D": "F4", "\x1b[[E": "F5",
    "\x1b[17~": "F6", "\x1b[18~": "F7", "\x1b[19~": "F8", "\x1b[20~": "F9",
    "\x1b[21~": "F10", "\x1b[23~": "F11", "\x1b[24~": "F12",
    "\x1b[G": "5 du pavé numérique",
}, "nom d'une touche spéciale", par_libelle=True)

CONTROL_NAMES = Etiquettes("touche", {"\t": "tabulation", "\r": "entrée", "\n": "entrée",
                 "\x7f": "retour arrière", "\x08": "retour arrière", "\x1b": "échap"}, "nom d'une touche de contrôle", par_libelle=True)

# Touches sans caractere : annoncees via evdev en mode reperage uniquement.
EVDEV_NAMES = Etiquettes("touche", {
    "KEY_LEFTSHIFT": "majuscule gauche", "KEY_RIGHTSHIFT": "majuscule droite",
    "KEY_LEFTCTRL": "contrôle gauche", "KEY_RIGHTCTRL": "contrôle droite",
    "KEY_LEFTALT": "alt", "KEY_RIGHTALT": "alt gr",
    "KEY_LEFTMETA": "touche windows", "KEY_RIGHTMETA": "touche windows droite",
    "KEY_CAPSLOCK": "verrouillage majuscules", "KEY_NUMLOCK": "verrouillage numérique",
    "KEY_SCROLLLOCK": "arrêt défilement", "KEY_COMPOSE": "menu contextuel",
    "KEY_SYSRQ": "impression écran", "KEY_PAUSE": "pause",
    "KEY_VOLUMEUP": "volume plus", "KEY_VOLUMEDOWN": "volume moins",
    "KEY_MUTE": "muet", "KEY_PLAYPAUSE": "lecture pause",
    "KEY_NEXTSONG": "piste suivante", "KEY_PREVIOUSSONG": "piste précédente",
    "KEY_STOPCD": "stop", "KEY_HOMEPAGE": "page d'accueil", "KEY_MAIL": "courrier",
    "KEY_CALC": "calculatrice", "KEY_SEARCH": "recherche", "KEY_POWER": "alimentation",
    "KEY_SLEEP": "veille", "KEY_WAKEUP": "réveil",
}, "nom d'une touche du clavier", par_libelle=True)


# Reveil de la lecture clavier sans touche : un dialogue que l'autre machine
# a termine (appairage valide sur la machine compagne) se ferme tout seul.
WAKE = "\x00reveil"
WAKE_R, WAKE_W = os.pipe()


def wake():
    os.write(WAKE_W, b"x")


def drain_wake():
    while select.select([WAKE_R], [], [], 0)[0]:
        os.read(WAKE_R, 64)


_MODIFIERS = {"ctrl": ("KEY_LEFTCTRL", "KEY_RIGHTCTRL"), "shift": ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"),
              "alt": ("KEY_LEFTALT", "KEY_RIGHTALT")}


# --- suivi permanent des modificateurs (2026-10-07) ---
# Lire l'etat du clavier APRES avoir recu la fleche ratait les appuis rapides
# (45 ms par lecture, Alt puis Ctrl verifies l'un apres l'autre : Ctrl deja
# relache). Un fil suit le clavier en continu et note, a l'instant ou une
# touche est enfoncee, les modificateurs tenus. Alt + fleche haut est
# avalee par le noyau (table de la console : KeyboardSignal) : ce fil la
# transmet lui-meme a la console (ALT_UP), par le reveil de la lecture.
ALT_UP = "\x00alt_haut"
COMBO_MAX_AGE_SEC = 1.5
_injected = []


def inject(key):
    _injected.append(key)
    wake()


def pop_injected():
    out = list(_injected)
    del _injected[:len(out)]
    return out


class _Tracker:
    NAMES = {code: name for name, codes in _MODIFIERS.items() for code in codes}

    def __init__(self):
        self.held = set()
        self.last = (0.0, frozenset())      # (instant, modificateurs) du dernier appui hors modificateur
        self.devices = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while True:
            if not self.devices:
                self.devices = _keyboard_devices()
                if not self.devices:
                    time.sleep(2)
                    continue
            ready, _, _ = select.select(self.devices, [], [], 1.0)
            for dev in ready:
                try:
                    for event in dev.read():
                        if event.type == evdev.ecodes.EV_KEY:
                            self._key(event)
                except OSError:
                    self.devices = []  # clavier debranche : on re-scannera
                    break

    def _key(self, event):
        code = evdev.ecodes.KEY.get(event.code)
        code = code[0] if isinstance(code, list) else code
        name = self.NAMES.get(code)
        if name:
            (self.held.add if event.value else self.held.discard)(name)
            return
        if event.value == 1:
            mods = frozenset(self.held)
            self.last = (time.time(), mods)
            if code == "KEY_UP" and mods == {"alt"}:
                inject(ALT_UP)

    def recent(self):
        t, mods = self.last
        return mods if time.time() - t <= COMBO_MAX_AGE_SEC else None


_tracker = None


def start_tracker():
    global _tracker
    if _tracker is None:
        _tracker = _Tracker()
        _tracker.thread.start()


def modifier_held(name):
    """Vrai si ce modificateur ("ctrl", "shift", "alt") etait tenu quand la
    derniere touche a ete enfoncee (suivi permanent), sinon s'il l'est
    maintenant : la console envoie la meme sequence pour une fleche seule
    ou avec Ctrl, son etat se lit donc sur le materiel (evdev)."""
    if _tracker is not None and _tracker.devices:
        mods = _tracker.recent()
        if mods is None:  # le fil n'a peut-etre pas encore vu l'appui
            time.sleep(0.05)
            mods = _tracker.recent()
        if mods is not None:
            return name in mods
        return name in _tracker.held
    return _held_now(name)


def _held_now(name):
    try:
        codes = {evdev.ecodes.ecodes[c] for c in _MODIFIERS[name]}
        for path in evdev.list_devices():
            dev = evdev.InputDevice(path)
            try:
                if codes & set(dev.active_keys()):
                    return True
            except OSError:
                continue
            finally:
                dev.close()
    except Exception:
        return False
    return False


def char_name(ch):
    if ch in CHAR_NAMES:
        return CHAR_NAMES[ch]
    if ch.isalpha() and ch.isupper():
        return f"{char_name(ch.lower())} majuscule"
    return ch


def sequence_name(seq):
    """Nom parle d'une saisie brute (caractere, controle ou sequence)."""
    if seq == ALT_UP:
        return dire("touches.alt_fleche_haut")
    if seq in ESCAPE_NAMES:
        return ESCAPE_NAMES[seq]
    if seq in CONTROL_NAMES:
        return CONTROL_NAMES[seq]
    if len(seq) == 2 and seq[0] == "\x1b":
        return dire("touches.alt_plus", char_name=char_name(seq[1]))
    if len(seq) == 1 and ord(seq) < 32:
        return dire("touches.controle_plus", chr=chr(ord(seq) + 96))
    if len(seq) == 1:
        return char_name(seq)
    return "touche inconnue"


def _keyboard_devices():
    devices = []
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        name = dev.name.lower()
        if "hdmi" in name or "rtl" in name:
            continue
        if evdev.ecodes.EV_KEY in dev.capabilities():
            devices.append(dev)
    return devices


class ModifierAnnouncer:
    """Thread evdev (non exclusif : le tty recoit toujours les touches)
    qui annonce les touches sans caractere, le temps du mode reperage."""

    def __init__(self, say):
        self._say = say
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        devices = _keyboard_devices()
        try:
            while not self._stop.is_set():
                if not devices:
                    self._stop.wait(2)
                    devices = _keyboard_devices()
                    continue
                ready, _, _ = select.select(devices, [], [], 0.3)
                for dev in ready:
                    try:
                        for event in dev.read():
                            if event.type != evdev.ecodes.EV_KEY or event.value != 1:
                                continue
                            code = evdev.ecodes.KEY.get(event.code)
                            if isinstance(code, list):
                                code = code[0]
                            if code in EVDEV_NAMES:
                                self._say(EVDEV_NAMES[code])
                    except OSError:
                        devices = []  # clavier debranche : on re-scannera
                        break
        finally:
            for dev in devices:
                dev.close()
