"""Mode manuel du scanner : dialogue au clavier, la radio joue en direct.

  fleches haut / bas       plage precedente / suivante
  fleches gauche / droite  un pas de frequence (ou canal) en moins / en plus
  page haut / page bas     dix pas
  Entree                   redit la plage et la frequence
  Echap                    ferme le dialogue ; le scanner reste fixe sur la
                           frequence (avec silencieux) jusqu'a "decroche"

Les plages viennent du scanner lui-meme (/tmp/radio_watch_plages.json,
ecrit au demarrage de radio_watch.py) : aucune liste en double a tenir a
jour ici.
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import noise_signatures as ns  # noqa: E402

BIG_STEP = 10
INTRO = ("Mode manuel, balayage suspendu. Flèches haut et bas : changer de plage. "
         "Gauche et droite : ajuster la fréquence. Page haut et page bas : dix pas. "
         "Entrée : relire. Échap : fermer.")


def spoken_mhz(freq_hz):
    text = f"{freq_hz / 1e6:.4f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + " mégahertz"


class Tuner:
    def __init__(self, plages, start=None):
        self.plages = plages
        self.index = 0
        self.freq = None
        if start:
            for i, plage in enumerate(plages):
                if plage["target"] == start.get("target") and self._contains(plage, start["freq_hz"]):
                    self.index, self.freq = i, start["freq_hz"]
                    break
        if self.freq is None:
            self.freq = self._first(self.plage)

    @property
    def plage(self):
        return self.plages[self.index]

    @staticmethod
    def _first(plage):
        return plage["channels"][0] if plage["channels"] else plage["low_hz"]

    @staticmethod
    def _contains(plage, freq):
        if plage["channels"]:
            return freq in plage["channels"]
        return plage["low_hz"] <= freq <= plage["high_hz"]

    def change_plage(self, delta):
        self.index = (self.index + delta) % len(self.plages)
        self.freq = self._first(self.plage)
        return self.describe(with_plage=True)

    def step(self, count):
        """Deplace de count pas ; rend (texte, bord atteint)."""
        plage = self.plage
        if plage["channels"]:
            channels = plage["channels"]
            pos = channels.index(self.freq) + count
            clamped = min(max(pos, 0), len(channels) - 1)
            self.freq = channels[clamped]
            edge = clamped != pos
        else:
            wanted = self.freq + count * plage["step_hz"]
            self.freq = min(max(wanted, plage["low_hz"]), plage["high_hz"])
            edge = self.freq != wanted
        if edge:
            return ("Début de plage, " if count < 0 else "Fin de plage, ") + spoken_mhz(self.freq)
        return spoken_mhz(self.freq)

    def describe(self, with_plage=True):
        text = spoken_mhz(self.freq)
        return f"{self.plage['label']}, {text}" if with_plage else text

    def target(self):
        """Description compatible avec une "derniere capture" (verrouillage)."""
        plage = self.plage
        return {"source": plage["source"], "freq_hz": self.freq, "mode": plage["mode"],
                "label": plage["label"], "target": plage["target"], "log_path": None,
                "fingerprint": None, "time": 0}


KEYS = {
    "\x1b[A": ("plage", -1), "\x1b[B": ("plage", 1),
    "\x1b[D": ("step", -1), "\x1b[C": ("step", 1),
    "\x1b[5~": ("step", BIG_STEP), "\x1b[6~": ("step", -BIG_STEP),
}


def run(key_source, say, out):
    """Boucle du dialogue ; rend la phrase de sortie a dire par la console."""
    plages = ns.read_plages()
    if not plages:
        return "Le scanner radio n'a pas publié sa liste de plages. Vérifie qu'il tourne."
    tuner = Tuner(plages, ns.last_capture())
    ns.request_manual(tuner.target())
    out(INTRO + "\n")
    say(INTRO + " " + tuner.describe())
    for key in key_source:
        if key == "\x1b":
            break
        action = KEYS.get(key)
        if action and action[0] == "plage":
            text = tuner.change_plage(action[1])
        elif action:
            text = tuner.step(action[1])
        elif key in ("\r", "\n"):
            text = tuner.describe()
        else:
            text = "Flèches, entrée, ou échap pour fermer."
        ns.request_manual(tuner.target())
        out(f"[{text}]\n")
        say(text)
    ns.request_lock(tuner.target())
    return (f"Mode manuel fermé. Le scanner reste fixé sur {tuner.describe()}. "
            "Tape décroche pour reprendre le balayage.")
