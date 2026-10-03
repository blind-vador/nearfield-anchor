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
import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402

import noise_signatures as ns  # noqa: E402
import conseils  # noqa: E402
import notation  # noqa: E402
from phrases import Etiquettes, dire, nombre  # noqa: E402

BIG_STEP = 10
MODES = Etiquettes("mode", {"nfm": "FM étroite", "wfm": "FM large", "am": "AM"}, "mode de réception radio")


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
    return {"source": "rtl_fm", "target": "stations", "mode": found[0]["mode"], "label": dire("manuel.plage_stations"),
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
        now = self.describe() if plage.get("stations") else spoken_mhz(self.freq)
        if edge:
            if plage.get("stations"):
                return dire("manuel.debut_liste" if count < 0 else "manuel.fin_liste", suite=now)
            return dire("manuel.debut_plage" if count < 0 else "manuel.fin_plage", suite=now)
        return now

    def toggle_filter(self):
        self.filtered = not self.filtered
        if not self.filtered:
            return dire("manuel.filtre_off")
        if self.plage["mode"] == "wfm":
            return dire("manuel.filtre_fm")
        return dire("manuel.filtre_on")

    def describe(self, with_plage=True):
        text = spoken_mhz(self.freq)
        station = self.station()
        if station:
            import stations
            n = self.plage["channels"].index(self.freq) + 1
            text = dire("manuel.station", numero=n, total=len(self.plage["channels"]), frequence=text,
                        mode=MODES.get(station["mode"], station["mode"]), bande=station.get("label") or station.get("bande"))
            habits = stations.habits(station)
            if habits:
                text += ", " + dire("manuel.habitude", jour=stations.JOURS[habits[0][0]], heure=habits[0][1])
            return text
        return f"{self.plage['label']}, {text}" if with_plage else text

    def target(self):
        """Description compatible avec une "derniere capture" (verrouillage).
        Station reperee : son propre mode, sa bande et son libelle."""
        plage = self.plage
        station = self.station()
        if station:
            return {"source": "rtl_fm", "freq_hz": self.freq, "mode": station["mode"],
                    "label": station.get("label") or dire("manuel.station_reperee"), "target": station.get("bande"),
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
        return dire("manuel.valide", total=nombre(total, "note")) + notation._station_text(station) + auto


KEYS = {
    "\x1b[A": ("plage", -1), "\x1b[B": ("plage", 1),
    "\x1b[D": ("step", -1), "\x1b[C": ("step", 1),
    "\x1b[5~": ("step", BIG_STEP), "\x1b[6~": ("step", -BIG_STEP),
}


EVENTS_PATH = "/tmp/manuel_evenements.jsonl"  # ecrit par lib/manual_tuner.py (scanner)


def _relay_events(say, out, stop):
    """Annonces des decodeurs de l'ecoute manuelle ("Reception DMR", messages
    decodes) : le scanner ne peut pas parler, il tient la sortie son."""
    try:
        pos = os.path.getsize(EVENTS_PATH)
    except OSError:
        pos = 0
    while not stop.is_set():
        try:
            with open(EVENTS_PATH, encoding="utf-8") as f:
                f.seek(pos)
                lines = f.readlines()
                pos = f.tell()
        except OSError:
            lines = []
        for line in lines:
            try:
                text = json.loads(line)["texte"]
            except (ValueError, KeyError):
                continue
            out(f"[{text}]\n")
            say(text)
        stop.wait(0.3)


def run(key_source, say, out):
    """Boucle du dialogue ; rend la phrase de sortie a dire par la console."""
    plages = ns.read_plages()
    if not plages:
        return dire("manuel.plages_absentes")
    probable = stations_plage()
    if probable:
        plages = [probable] + plages
    tuner = Tuner(plages, ns.last_capture())
    stop_events = threading.Event()
    threading.Thread(target=_relay_events, args=(say, out, stop_events), daemon=True).start()
    ns.request_manual(tuner.target())
    consigne = dire("manuel.consigne")
    intro = conseils.intro("manuel", dire("manuel.titre"), consigne)
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
            text = tuner.describe() + (", " + dire("manuel.filtre_actif") if tuner.filtered else "")
        elif conseils.is_help(key):
            text = consigne
        else:
            text = dire("manuel.aide_courte")
        ns.request_manual(tuner.target())
        out(f"[{text}]\n")
        say(text)
    stop_events.set()
    ns.request_lock(tuner.target())
    return dire("manuel.ferme", position=tuner.describe())
