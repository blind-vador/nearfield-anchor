"""Commande "exploration dmr" (2026-10-17) : ecoute en direct des frequences
DMR connues, groupe d'appel par groupe d'appel, et ciblage de la veille.

  fleches haut / bas       frequence DMR precedente / suivante (favorites
                           d'abord) : ecoute en direct, voix numerique seule
  fleches gauche / droite  groupe d'appel : "tous les groupes", puis chacun ;
                           sur un groupe, seul ce groupe est entendu
  Espace                   ignorer / ne plus ignorer (le groupe, ou la
                           frequence sur "tous les groupes") : la veille DMR
                           ne s'y arrete plus
  F                        favori oui / non (groupe ou frequence)
  C                        mode cible : la veille n'ecoute plus que les favoris
  D                        duree max d'une ecoute DMR : 1, 2 ou 5 minutes
  R                        recherche de nouvelles frequences DMR (bandes pro
                           VHF et UHF) ; R a nouveau l'arrete
  Entree                   tout ce qu'on sait de la frequence
  Echap                    ferme ; le scanner reprend son balayage

Pendant l'ecoute, chaque nouvel appel est annonce (groupe, intervalle).
Reglages : lib/dmr_filtres.py ; registre : lib/dmr_registre.py.
"""
import sys
import threading


import conseils  # noqa: E402
import dmr_cmd  # noqa: E402
import dmr_filtres  # noqa: E402
import dmr_registre  # noqa: E402
import manuel  # noqa: E402
import noise_signatures as ns  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"


def _mhz(freq_hz):
    return f"{freq_hz / 1e6:.4f}".rstrip("0").rstrip(".").replace(".", ",")


def frequencies():
    """Frequences DMR du registre, plus les stations reperees en DMR ;
    favorites d'abord, ignorees a la fin."""
    known = list(dmr_registre.targets())
    keys = {dmr_filtres.freq_key(e["freq_hz"]) for e in known}
    try:
        import stations
        for s in stations.load()["stations"]:
            if stations.is_digital(s) and dmr_filtres.freq_key(s["freq_hz"]) not in keys:
                known.append({"freq_hz": s["freq_hz"], "groupes": {}, "sessions": 0})
                keys.add(dmr_filtres.freq_key(s["freq_hz"]))
    except Exception:
        pass
    rank = {"favori": 0, None: 1, "ignore": 2}
    return sorted(known, key=lambda e: rank.get(dmr_filtres.freq_state(e["freq_hz"]), 1))


def _groups(entry):
    groups = entry.get("groupes") or {}
    return sorted(groups, key=lambda tg: -groups[tg].get("n", 0))


class Explorer:
    def __init__(self):
        self.freqs = frequencies()
        self.fi = 0
        self.gi = 0  # 0 = tous les groupes

    @property
    def entry(self):
        return self.freqs[self.fi]

    @property
    def group(self):
        groups = _groups(self.entry)
        return groups[self.gi - 1] if 0 < self.gi <= len(groups) else None

    def listen(self):
        dmr_filtres.set_focus(self.group)
        ns.request_manual({"source": "rtl_fm", "freq_hz": self.entry["freq_hz"], "mode": "nfm",
                           "label": dire("dmr_exploration.libelle"), "target": "dmr", "numerique": True,
                           "filter": False, "log_path": None, "fingerprint": None, "time": 0})

    def _state_text(self, state):
        return (", " + dire(f"dmr_exploration.etat_{state}")) if state else ""

    def describe_freq(self):
        e = self.entry
        nature = dire("dmr.relais") if e.get("relais") else dire("dmr.direct")
        text = dire("dmr_exploration.frequence", numero=self.fi + 1, total=len(self.freqs),
                    frequence=_mhz(e["freq_hz"]), nature=nature,
                    groupes=nombre(len(e.get("groupes") or {}), "groupe"))
        if e.get("derniere"):
            text += ", " + dmr_cmd._when(e["derniere"])
        return text + self._state_text(dmr_filtres.freq_state(e["freq_hz"]))

    def describe_group(self):
        tg = self.group
        if tg is None:
            return dire("dmr_exploration.tous_groupes")
        g = self.entry["groupes"][tg]
        text = dire("dmr_exploration.groupe", groupe=tg, appels=nombre(g.get("n", 0), "appel"),
                    emetteurs=nombre(len(g.get("emetteurs") or []), "émetteur"))
        if g.get("derniere"):
            text += ", " + dmr_cmd._when(g["derniere"])
        return text + self._state_text(dmr_filtres.group_state(tg))

    def move_freq(self, delta):
        self.fi = (self.fi + delta) % len(self.freqs)
        self.gi = 0
        self.listen()
        return self.describe_freq()

    def move_group(self, delta):
        self.gi = (self.gi + delta) % (len(_groups(self.entry)) + 1)
        self.listen()
        return self.describe_group()

    def toggle(self, wanted):
        """Espace (ignore) ou F (favori), sur le groupe ou la frequence."""
        tg = self.group
        if tg is None:
            on = dmr_filtres.freq_state(self.entry["freq_hz"]) != wanted
            dmr_filtres.set_freq(self.entry["freq_hz"], wanted if on else None)
            key = f"dmr_exploration.frequence_{wanted}_{'oui' if on else 'non'}"
            return dire(key, frequence=_mhz(self.entry["freq_hz"]))
        on = dmr_filtres.group_state(tg) != wanted
        dmr_filtres.set_group(tg, wanted if on else None)
        return dire(f"dmr_exploration.groupe_{wanted}_{'oui' if on else 'non'}", groupe=tg)

    def toggle_targeted(self):
        on = not dmr_filtres.targeted()
        dmr_filtres.set_targeted(on)
        return dire("dmr_exploration.cible_oui" if on else "dmr_exploration.cible_non")

    def search(self):
        if dmr_filtres.search_requested():
            dmr_filtres.search_done()
            return dire("dmr_exploration.recherche_arretee")
        dmr_filtres.request_search()
        return dire("dmr_exploration.recherche_demandee")

    def refresh(self):
        """Apres une recherche : nouvelles frequences, sans perdre la place."""
        current = self.entry["freq_hz"]
        self.freqs = frequencies() or self.freqs
        self.fi = next((i for i, e in enumerate(self.freqs) if e["freq_hz"] == current), 0)


def run(key_source, say, out):
    if not frequencies():
        return dire("dmr_exploration.aucune")
    explorer = Explorer()
    dmr_filtres.set_exploration(True)
    stop_events = threading.Event()
    threading.Thread(target=manuel._relay_events, args=(say, out, stop_events), daemon=True).start()
    explorer.listen()
    consigne = dire("dmr_exploration.consigne")
    intro = conseils.intro("exploration_dmr", dire("dmr_exploration.titre"), consigne)
    out(intro + "\n")
    say(intro + " " + explorer.describe_freq())
    try:
        for key in key_source:
            if key == "\x1b":
                break
            explorer.refresh()
            if key in (UP, DOWN):
                text = explorer.move_freq(-1 if key == UP else 1)
            elif key in (LEFT, RIGHT):
                text = explorer.move_group(-1 if key == LEFT else 1)
            elif key == " ":
                text = explorer.toggle("ignore")
            elif key in ("f", "F"):
                text = explorer.toggle("favori")
            elif key in ("c", "C"):
                text = explorer.toggle_targeted()
            elif key in ("d", "D"):
                text = dire("dmr_exploration.duree", duree=nombre(dmr_filtres.next_duration() // 60, "minute"))
            elif key in ("r", "R"):
                text = explorer.search()
            elif key in ("\r", "\n"):
                text = dmr_cmd.describe(explorer.entry) if explorer.entry.get("sessions") else explorer.describe_freq()
            elif conseils.is_help(key):
                text = consigne
            else:
                text = dire("dmr_exploration.aide_courte")
            out(f"[{text}]\n")
            say(text)
    finally:
        stop_events.set()
        dmr_filtres.set_exploration(False)
        dmr_filtres.search_done()
        ns.request_unlock()
    return dire("dmr_exploration.ferme")
