"""Sonde de sol et arrosage (2026-10-23, demande de l'utilisateur) : apres un
arrosage, l'eau qui passe fausse la mesure ; on attend qu'elle se stabilise
(son en boucle "sonde_stabilisation" pendant l'attente), puis on dit si la
terre a assez d'eau, trop, ou pas encore assez.

Arrosage reconnu de deux facons :
  - dit a la console ("plante X arrosee") : demander(plant_id), qui ne bloque
    rien (la console continue) ;
  - automatiquement, des qu'une sonde de sol est rattachee a la plante : un
    saut d'humidite de SAUT_POINTS entre deux mesures = un arrosage, note
    comme tel (date d'arrosage de la plante).

verifier() tourne dans veille_systeme.py, toutes les 30 s. Les mesures des
sondes arrivent avec les fenetres de balayage 433 (environ toutes les 10 min) :
stable = deux mesures de suite a moins de STABLE_POINTS, apres au moins
ATTENTE_MIN_SEC, au plus ATTENTE_MAX_SEC."""
import json
import os
import subprocess
import time

ETAT = "/home/vador/autopilot/plantes_sonde.json"
DEMANDES = "/tmp/plantes_arrosage_demandes.json"
SAUT_POINTS = 10
STABLE_POINTS = 3
ATTENTE_MIN_SEC = 10 * 60
ATTENTE_MAX_SEC = 45 * 60
TROP_POURCENT = 80               # terre detrempee
ASSEZ_MARGE = 10                 # au-dessus du seuil de soif de la plante : assez d'eau
VOLUME_BOUCLE = 0.5


def _lire(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def _ecrire(chemin, data):
    tmp = f"{chemin}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, chemin)


def demander(plant_id):
    """La console a note un arrosage : la veille systeme prendra la mesure."""
    demandes = _lire(DEMANDES, [])
    demandes.append({"plante_id": plant_id, "t": time.time()})
    _ecrire(DEMANDES, demandes)


def a_une_sonde(plant_id):
    import capteurs_miens
    return any(e.get("genre") == "sol" and e.get("plante_id") == plant_id for e in capteurs_miens.load())


class _Boucle:
    """Le son d'attente en boucle, tant qu'au moins une plante attend."""

    def __init__(self):
        self._proc = None

    def regler(self, active):
        if active and (self._proc is None or self._proc.poll() is not None):
            import sons
            chemin = sons.chemin("sonde_stabilisation")
            if chemin:
                self._proc = subprocess.Popen(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-stream_loop", "-1", "-i", chemin,
                     "-af", f"volume={VOLUME_BOUCLE}", "-f", "alsa", "default"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif not active and self._proc is not None:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None


_boucle = _Boucle()


def _seuil_soif(plant):
    import plantes
    import plantes_alertes
    try:
        fiche = plantes.card(plant) or {}
    except Exception:
        fiche = {}
    return plantes_alertes.SOIL_THIRSTY.get(fiche.get("eau"), 25)


def _verdict(plant, pct, dire_fn):
    """(evenement du son, phrase)."""
    nom, valeur = plant["nom"], round(pct)
    if pct >= TROP_POURCENT:
        return "plante_trop_eau", dire_fn("plantes.sonde_trop_eau", nom=nom, pct=valeur)
    if pct >= _seuil_soif(plant) + ASSEZ_MARGE:
        return "plante_assez_eau", dire_fn("plantes.sonde_assez_eau", nom=nom, pct=valeur)
    return "plante_besoin_eau", dire_fn("plantes.sonde_pas_assez", nom=nom, pct=valeur, mot=nom.lower())


def _noter_arrosage(plant_id):
    import plantes
    plants = plantes.load()
    for p in plants:
        if p["id"] == plant_id:
            p["arrosee_le"] = plantes._today()
    plantes.save(plants)


def verifier(jouer):
    """Un tour : jouer(evenement, phrase) dit un resultat (veille_systeme._jouer)."""
    import capteurs_miens
    import plantes
    from phrases import dire
    etat = _lire(ETAT, {})
    now = time.time()
    demandes = _lire(DEMANDES, [])
    if demandes:
        _ecrire(DEMANDES, [])
    plants = {p["id"]: p for p in plantes.load()}
    for d in demandes:
        if d.get("plante_id") in plants and a_une_sonde(d["plante_id"]):
            etat.setdefault(d["plante_id"], {})["attente"] = {"debut": d["t"], "mesures": [], "auto": False}
    for pid, plant in plants.items():
        mesure = capteurs_miens.soil_for_plant(pid)
        if not mesure:
            a = etat.get(pid, {}).get("attente")
            if a and now - a["debut"] >= ATTENTE_MAX_SEC:  # sonde muette : on n'attend pas sans fin
                del etat[pid]["attente"]
            continue
        sonde, pct = mesure
        e = etat.setdefault(pid, {})
        t_mesure = (capteurs_miens.latest(sonde) or {}).get("t", now)
        nouvelle = t_mesure != e.get("t")
        if nouvelle and e.get("pct") is not None and pct - e["pct"] >= SAUT_POINTS and "attente" not in e:
            e["attente"] = {"debut": now, "mesures": [], "auto": True}   # arrosage reconnu par la sonde
            _noter_arrosage(pid)
            print(f"[plantes_sonde] arrosage reconnu : {plant['nom']} ({e['pct']:.0f} -> {pct:.0f} %)", flush=True)
        if nouvelle:
            e["pct"], e["t"] = pct, t_mesure
        a = e.get("attente")
        if not a:
            continue
        if nouvelle and t_mesure >= a["debut"]:
            a["mesures"].append(pct)
        m = a["mesures"]
        stable = len(m) >= 2 and abs(m[-1] - m[-2]) < STABLE_POINTS and now - a["debut"] >= ATTENTE_MIN_SEC
        if stable or now - a["debut"] >= ATTENTE_MAX_SEC:
            del e["attente"]
            _ecrire(ETAT, etat)
            _boucle.regler(False)
            evenement, phrase = _verdict(plant, m[-1] if m else pct, dire)
            jouer(evenement, phrase)
    _ecrire(ETAT, etat)
    _boucle.regler(any("attente" in e for e in etat.values()))
