"""Mode manuel du scanner : dialogue au clavier, la radio joue en direct.

  fleches haut / bas       plage precedente / suivante ; en tete, "stations
                           reperees" (stations probables, lib/stations.py)
  fleches gauche / droite  un pas de frequence (ou canal) en moins / en plus ;
                           dans les stations reperees : station precedente /
                           suivante, frequence et mode regles tout seuls
  V                        bonne reception : comme V dans le journal (note,
                           station probable, reglages automatiques)
  page haut / page bas     dix pas
  F                        filtre on/off : filtre de bande vocale + silencieux,
                           comme en reception normale (coupe sifflements et souffle)
  Entree                   redit la plage et la frequence
  Echap                    ferme le dialogue ; le scanner reste fixe sur la
                           frequence (avec silencieux) jusqu'a "decroche"

Les plages viennent du scanner lui-meme (/tmp/radio_watch_plages.json,
ecrit au demarrage de radio_watch.py) : aucune liste en double a tenir a
jour ici.
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import datetime  # noqa: E402

import noise_signatures as ns  # noqa: E402
import conseils  # noqa: E402
import notation  # noqa: E402

BIG_STEP = 10
TITLE = "Mode manuel, balayage suspendu."
CONSIGNE = ("Flèches haut et bas : changer de plage ; en tête, les stations repérées. "
            "Gauche et droite : ajuster la fréquence, ou passer d'une station repérée à l'autre. "
            "Page haut et page bas : dix pas. F : filtre et silencieux. V : bonne réception, comme dans le journal. "
            "Entrée : relire. Échap : fermer. F1 redit ces touches.")
MODES = {"nfm": "FM étroite", "wfm": "FM large", "am": "AM"}


def stations_plage():
    """Plage "stations reperees" : stations probables, de la plus suivie a la
    moins suivie ; chaque canal garde son mode, sa bande, son libelle."""
    try:
        import stations
        found = sorted(stations.load()["stations"], key=lambda s: -stations.interest(s))
    except Exception:
        return None
    if not found:
        return None
    return {"source": "rtl_fm", "target": "stations", "mode": found[0]["mode"], "label": "stations repérées",
            "low_hz": None, "high_hz": None, "step_hz": None, "channels": [s["freq_hz"] for s in found],
            "stations": found}


def spoken_mhz(freq_hz):
    text = f"{freq_hz / 1e6:.4f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + " mégahertz"


class Tuner:
    def __init__(self, plages, start=None):
        self.plages = plages
        self.filtered = False
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

    def station(self):
        """Station reperee sous le curseur, ou None."""
        stations = self.plage.get("stations")
        if not stations or self.freq not in self.plage["channels"]:
            return None
        return stations[self.plage["channels"].index(self.freq)]

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

    def toggle_filter(self):
        self.filtered = not self.filtered
        if not self.filtered:
            return "Filtre désactivé."
        if self.plage["mode"] == "wfm":
            return "Filtre activé, mais sans effet sur la radio FM."
        return "Filtre activé."

    def describe(self, with_plage=True):
        text = spoken_mhz(self.freq)
        station = self.station()
        if station:
            import stations
            n = self.plage["channels"].index(self.freq) + 1
            text = (f"station repérée {n} sur {len(self.plage['channels'])}, {text}, "
                    f"{MODES.get(station['mode'], station['mode'])}, {station.get('label') or station.get('bande')}")
            habits = stations.habits(station)
            if habits:
                text += f", souvent le {stations.JOURS[habits[0][0]]} vers {habits[0][1]} heures"
            return text
        return f"{self.plage['label']}, {text}" if with_plage else text

    def target(self):
        """Description compatible avec une "derniere capture" (verrouillage).
        Station reperee : son propre mode, sa bande et son libelle."""
        plage = self.plage
        station = self.station()
        if station:
            return {"source": "rtl_fm", "freq_hz": self.freq, "mode": station["mode"],
                    "label": station.get("label") or "station repérée", "target": station.get("bande"),
                    "log_path": None, "fingerprint": None, "time": 0, "filter": self.filtered}
        return {"source": plage["source"], "freq_hz": self.freq, "mode": plage["mode"],
                "label": plage["label"], "target": plage["target"], "log_path": None,
                "fingerprint": None, "time": 0, "filter": self.filtered}

    def validate(self):
        """V : bonne reception, meme effet que V dans le journal (note "tout au
        vert", station probable creee ou renforcee, reglages automatiques)."""
        t = self.target()
        stamp = datetime.datetime.now()
        name = f"manuel_{int(t['freq_hz'])}_{stamp:%Y%m%d_%H%M%S}.mp3"  # pas d'enregistrement, juste un nom
        info = {"freq_hz": t["freq_hz"], "label": t["label"], "target": t["target"], "source": "manuel",
                "mode": t["mode"], "metrics": {}, "fingerprint": None}
        total, station, auto = notation._record(info, name, stamp, dict(notation.GOOD_TAKE), rapide=True)
        return (f"Validé, bonne réception. {total} note{'s' if total > 1 else ''} au total."
                + notation._station_text(station) + auto)


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
    probable = stations_plage()
    if probable:
        plages = [probable] + plages
    tuner = Tuner(plages, ns.last_capture())
    ns.request_manual(tuner.target())
    intro = conseils.intro("manuel", TITLE, CONSIGNE)
    out(intro + "\n")
    say(intro + " " + tuner.describe())
    for key in key_source:
        if key == "\x1b":
            break
        action = KEYS.get(key)
        if action and action[0] == "plage":
            text = tuner.change_plage(action[1])
        elif action:
            text = tuner.step(action[1])
        elif key in ("f", "F"):
            text = tuner.toggle_filter()
        elif key in ("v", "V"):
            text = tuner.validate()
        elif key in ("\r", "\n"):
            text = tuner.describe() + (", filtre activé" if tuner.filtered else "")
        elif conseils.is_help(key):
            text = CONSIGNE
        else:
            text = "Flèches, F, V, entrée, ou échap pour fermer. F1 pour l'aide."
        ns.request_manual(tuner.target())
        out(f"[{text}]\n")
        say(text)
    ns.request_lock(tuner.target())
    return (f"Mode manuel fermé. Le scanner reste fixé sur {tuner.describe()}. "
            "Tape décroche pour reprendre le balayage.")
