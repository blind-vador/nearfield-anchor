"""Frequences actives, apprises d'ici (2026-11-05) : tout ce qui emet, voix ou non.

Les relais appris par l'APRS (lib/relais.py), le registre DMR et les stations
notees ne couvrent pas tout : une frequence qui porte de l'IDLE DMR, du chiffre,
des donnees ou un signal encore inconnu n'etait retenue nulle part. Ici chaque
frequence entendue est notee avec sa nature, ses protocoles, ses modulations et
ses heures ; le scanner y revient d'autant plus qu'elle est active (bands). On ne
presume de rien : c'est de l'attention en plus, jamais un filtre, et tous les
decodeurs restent ouverts sur la frequence revisitee.

Relais possible : une frequence ou l'on observe ce qui fait un relais -- synchro
de relais DMR, tonalite d'acces entendue plusieurs fois, indicatif de relais lu ou
entendu, frequence annoncee dans un texte, ou une autre frequence active au
decalage d'usage de la bande. Il rejoint les relais ecoutes s'il est dans une
bande radioamateur (relais.detecte) et l'utilisateur en est prevenu une fois."""
import datetime
import fcntl
import json
import os
import time

import chemins

PATH = chemins.base("frequences_actives.json")
LOCK = "/tmp/frequences_actives.lock"
PAS_HZ = 12_500
OUBLI_SEC = 60 * 86400
PAIRE_RECENTE_SEC = 30 * 86400
VUS_MIN = {"signal": 3}             # un pic inconnu doit revenir ; le reste compte des la 2e fois
VUS_MIN_DEFAUT = 2
VISITE_MIN_SEC = 10 * 60
VISITE_MAX_SEC = 6 * 3600
PAR_PASSE = 8                       # un rtl_power par passe : la liste reste courte
TONALITE_MIN = 2
FM_DIFFUSION = (87_500_000, 108_000_000)    # couverte par sa bande et ses interets
# (bas, haut, decalages) : l'ecart entrée/sortie d'usage. Il sert seulement a
# remarquer que deux frequences actives vont peut-etre ensemble.
DECALAGES = [(28_000_000, 30_000_000, [100_000]),
             (50_000_000, 54_000_000, [500_000, 600_000, 1_000_000]),
             (144_000_000, 146_000_000, [600_000]),
             (146_000_000, 174_000_000, [4_600_000, 5_000_000]),
             (400_000_000, 430_000_000, [5_000_000, 10_000_000]),
             (430_000_000, 440_000_000, [1_600_000, 5_000_000, 7_600_000, 9_400_000]),
             (440_000_000, 470_000_000, [5_000_000, 10_000_000])]


class _Verrou:
    def __enter__(self):
        self.f = open(LOCK, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.data = load()
        return self.data

    def __exit__(self, *exc):
        if exc[0] is None:
            tmp = PATH + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, PATH)
        self.f.close()


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cle(freq_hz):
    return str(int(round(float(freq_hz) / PAS_HZ)) * PAS_HZ)


def _plus(d, cle, n=1):
    d[cle] = d.get(cle, 0) + n


def _paire(data, e, maintenant):
    """Une autre frequence active au decalage d'usage de cette bande."""
    f = e["freq_hz"]
    if e.get("vus", 0) < VUS_MIN_DEFAUT:
        return None
    for bas, haut, decalages in DECALAGES:
        if not bas <= f <= haut:
            continue
        for ecart in decalages:
            for autre in (f - ecart, f + ecart):
                o = data.get(_cle(autre))
                if o and o.get("vus", 0) >= VUS_MIN_DEFAUT and maintenant - o.get("dernier", 0) < PAIRE_RECENTE_SEC:
                    return o["freq_hz"]
    return None


def raisons_relais(data, e, maintenant=None):
    """Ce qu'on a observe sur cette frequence qui fait penser a un relais."""
    maintenant = maintenant or time.time()
    out = []
    if e.get("relais_dmr"):
        out.append("synchro de relais DMR")
    tons = [t for t, n in (e.get("tonalites") or {}).items() if n >= TONALITE_MIN]
    if tons:
        out.append(f"tonalité {tons[0]} hertz")
    if e.get("indicatif_relais") and not any(abs(e["freq_hz"] - f) <= t for f, t in ((144_800_000, 6_250),)):
        out.append(f"indicatif {e['indicatif_relais']}")   # pas un digipeater sur la frequence APRS
    if e.get("annoncee"):
        out.append("annoncée dans un texte")
    autre = _paire(data, e, maintenant)
    if autre:
        out.append(f"va peut-être avec {autre / 1e6:.4f} mégahertz".replace(".", ","))
    return out


def noter(freq_hz, nature, protocole=None, modulation=None, target=None, label=None, tonalite=None,
          relais_dmr=False, indicatif=None, indicatif_relais=None, annoncee=False, texte=None,
          surveillance_sec=None):
    """nature : voix, numerique, chiffre, donnees, signal (inconnu), texte, annoncee.
    surveillance_sec (2026-11-11, lib/frequences_ia) : frequence seulement annoncee
    (par une IA) ; elle s'efface apres ce delai si rien d'autre n'y a ete entendu."""
    if not freq_hz:
        return
    maintenant = time.time()
    nouveau = None
    try:
        with _Verrou() as data:
            e = data.setdefault(_cle(freq_hz), {"freq_hz": int(_cle(freq_hz)), "vus": 0, "premier": maintenant,
                                                "natures": {}, "protocoles": {}, "modulations": {},
                                                "heures": [0] * 24})
            e["vus"] += 1
            e["dernier"] = maintenant
            if nature == "annoncee" and surveillance_sec and set(e.get("natures") or {}) <= {"annoncee"}:
                e["expire"] = max(e.get("expire", 0), maintenant + surveillance_sec)
            elif nature in ("voix", "numerique", "chiffre", "donnees"):
                e.pop("expire", None)    # elle a donne quelque chose : elle reste
            _plus(e["natures"], nature)
            e["heures"][datetime.datetime.fromtimestamp(maintenant).hour] += 1
            if protocole:
                _plus(e["protocoles"], str(protocole))
            if modulation:
                _plus(e["modulations"], str(modulation))
            if target:
                e["target"] = target
            if label:
                e["label"] = label
            if tonalite:
                _plus(e.setdefault("tonalites", {}), str(tonalite))
            if relais_dmr:
                e["relais_dmr"] = True
            if indicatif:
                _plus(e.setdefault("indicatifs", {}), indicatif)
            if indicatif_relais:
                e["indicatif_relais"] = indicatif_relais
            if annoncee:
                e["annoncee"] = True
            if texte:
                e["textes"] = (e.get("textes") or [])[-4:] + [str(texte)[:160]]
            raisons = raisons_relais(data, e, maintenant)
            if raisons and not e.get("relais_possible"):
                e["relais_possible"] = raisons
                nouveau = dict(e)
            for cle in [c for c, x in data.items() if (maintenant - x.get("dernier", 0) > OUBLI_SEC
                                                       and not x.get("gardee"))
                        or (x.get("expire") and x["expire"] < maintenant)]:
                del data[cle]
    except OSError as exc:
        print(f"[frequences] non notee : {exc}")
        return
    if nouveau:
        _relais_trouve(nouveau, raisons)


def garder(freq_hz):
    """Frequence en surveillance acceptee par l'utilisateur : elle reste."""
    try:
        with _Verrou() as data:
            e = data.get(_cle(freq_hz))
            if e:
                e.pop("expire", None)
                e["gardee"] = True
    except OSError as exc:
        print(f"[frequences] non gardee : {exc}")


def oublier_surveillance(freq_hz):
    """Frequence en surveillance refusee : retiree si rien d'autre n'y a ete capte."""
    try:
        with _Verrou() as data:
            e = data.get(_cle(freq_hz))
            if e and e.get("expire"):
                del data[_cle(freq_hz)]
    except OSError as exc:
        print(f"[frequences] non oubliee : {exc}")


def _relais_trouve(e, raisons):
    print(f"[frequences] {e['freq_hz'] / 1e6:.4f} MHz : relais possible ({', '.join(raisons)})")
    try:
        import relais
        relais.detecte(e["freq_hz"], raisons, e.get("indicatif_relais"), protocole=_majeur(e.get("protocoles")))
        import sons
        import veille_donnees as vd
        from phrases import dire
        vd.announce(dire("relais.detecte", mhz=f"{e['freq_hz'] / 1e6:.4f}".rstrip("0").replace(".", ","),
                         raisons=", ".join(raisons)),
                    sons.chemin("nouvel_emetteur"), "donnees_relais", "frequences")
    except Exception as exc:
        print(f"[frequences] relais possible non signale : {exc!r}")


def depuis_capture(freq_hz, mode, label, target, metrics):
    """Une ecoute enregistree (noise_signatures.record_capture : tous les chemins)."""
    metrics = metrics or {}
    dmr = metrics.get("dmr") or {}
    nature = "chiffre" if metrics.get("chiffre") else "numerique" if metrics.get("numerique") else "voix"
    noter(freq_hz, nature, protocole=metrics.get("protocole") or metrics.get("mode_numerique"),
          modulation=metrics.get("modulation_banc") or mode, target=target, label=label,
          tonalite=metrics.get("ctcss_hz"), relais_dmr=bool(dmr.get("relais")))


def _majeur(compteur):
    return max(compteur, key=compteur.get) if compteur else None


def _utile(e):
    natures = e.get("natures") or {}
    if e.get("annoncee") or e.get("relais_possible") or e.get("expire") or e.get("gardee"):
        return True      # expire : en surveillance (frequence citee, lib/frequences_ia)
    seul_signal = set(natures) <= {"signal"}
    return e.get("vus", 0) >= (VUS_MIN["signal"] if seul_signal else VUS_MIN_DEFAUT)


def _attente(e, maintenant):
    """Plus elle emet, plus on y revient ; une frequence qui n'a donne qu'un signal
    inconnu reste au rythme le plus lent."""
    if set(e.get("natures") or {}) <= {"signal"}:
        return VISITE_MAX_SEC
    if (e.get("heures") or [0] * 24)[datetime.datetime.fromtimestamp(maintenant).hour] >= 2:
        return VISITE_MIN_SEC        # son heure habituelle
    return max(VISITE_MIN_SEC, VISITE_MAX_SEC / (1 + e.get("vus", 0)))


def _deja_suivie(f):
    try:
        import dmr_registre
        import relais
        import stations
        if relais.relais_proche(f, PAS_HZ // 2)[0] is not None:
            return True
        if dmr_registre.key(f) in dmr_registre.load():
            return True
        return any(abs(s.get("freq_hz", 0) - f) <= PAS_HZ for s in stations.load().get("stations", []))
    except Exception:
        return False


def bands(now=None):
    """Bandes "fm_channels" du scanner : les frequences actives a revisiter. La
    modulation n'est qu'une premiere hypothese, le banc les essaie toutes."""
    maintenant = now or time.time()
    dues = {}
    try:
        import ignores
        import interets
        with _Verrou() as data:
            candidates = sorted(data.values(), key=lambda e: -e.get("vus", 0))
            n = 0
            for e in candidates:
                f = e["freq_hz"]
                if n >= PAR_PASSE:
                    break
                if (FM_DIFFUSION[0] <= f <= FM_DIFFUSION[1] or not _utile(e)
                        or maintenant - e.get("visite", 0) < _attente(e, maintenant)
                        or (e.get("target") and interets.disabled(e["target"]))
                        or ignores.frequence_ignoree(f) or _deja_suivie(f)):
                    continue
                e["visite"] = maintenant
                mode = _majeur({m: k for m, k in (e.get("modulations") or {}).items()
                                if m in ("am", "nfm", "usb", "lsb")}) or "nfm"
                dues.setdefault(mode, []).append(f)
                n += 1
    except Exception as exc:
        print(f"[frequences] bandes non calculees : {exc!r}")
        return []
    return [{"name": f"actives_{mode}", "name_spoken": "fréquence active", "type": "fm_channels",
             "channels": sorted(freqs), "mode": mode, "margin_db": 6.0, "span_hz": 12_500}
            for mode, freqs in dues.items()]


def resume():
    """(actives, relais possibles, sans voix) pour la commande relais."""
    data = load()
    utiles = [e for e in data.values() if _utile(e)]
    return (len(utiles), sum(1 for e in utiles if e.get("relais_possible")),
            sum(1 for e in utiles if (e.get("natures") or {}) and "voix" not in e["natures"]))
