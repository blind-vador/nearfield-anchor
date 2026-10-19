"""Reglages audio par bande, ajustes d'apres les notes de l'utilisateur
(console/bilan.py), avec historique des versions (2026-10-02) :

  appliquer   ajoute une version (les reglages precedents sont gardes)
  precedent   revient a la version d'avant (reglage contre-productif)
  remise_a_zero  reglages d'usine ; l'historique est archive

Lu par le scanner (watchers/radio_watch.py, watchers/airband_bridge.py) a
chaque ecoute : un changement s'applique a la capture suivante, sans
redemarrage. Sur la carte SD du Pi.
"""
import fcntl
import json
import os
import time

from phrases import Etiquettes

STORE_PATH = "/home/vador/autopilot/reglages_audio.json"
LOCK_PATH = "/tmp/reglages_audio.lock"

# Valeurs d'usine. attente_fin_s None : la valeur du scanner (3 s, ou celle
# de la fenetre RTLSDR-Airband).
DEFAULTS = {
    "attente_fin_s": None,        # silence avant de clore une transmission
    "marge_detection_db": 0.0,    # + : plus exigeant pour ouvrir ; - : ouvre plus tot
    "passe_bas_hz": 3000.0,       # aigus gardes par le filtre vocal
    "reduction_bruit_db": 10.0,   # retrait maximal du souffle appris
    "anti_crachotements": 8.0,    # seuil des claquements (plus bas = plus fort)
    "anti_sifflement": False,     # retire les tonalites fixes
    "anti_bourdonnement": False,  # retire les harmoniques du secteur (50 Hz)
    "gain_db": 0.0,               # volume apres nettoyage
    "filtre_voix": True,          # bande vocale (300 Hz a "aigus gardes") sur ce qu'on entend
    "pilote_fm": True,            # radio FM : retire le pilote stereo a 19 kHz
    # Voix numerique (DMR... decodee par dsd-fme, 2026-10-14) : ni filtres ni
    # nettoyage, mais sa propre attente de fin (None : 10 s du scanner,
    # DSD_FME_IDLE_SEC) et son propre volume -- une bande balayee peut melanger
    # analogique et numerique, chacun garde ses valeurs.
    "attente_fin_numerique_s": None,
    "gain_numerique_db": 0.0,
}
NAMES = Etiquettes("audio.reglage", {
    "attente_fin_s": "attente avant de clore", "marge_detection_db": "marge de détection",
    "passe_bas_hz": "aigus gardés", "reduction_bruit_db": "retrait du souffle",
    "anti_crachotements": "seuil anti-crachotements", "anti_sifflement": "filtre anti-sifflement",
    "anti_bourdonnement": "filtre anti-bourdonnement", "gain_db": "volume",
    "filtre_voix": "filtre voix", "pilote_fm": "filtre du pilote stéréo FM",
    "attente_fin_numerique_s": "attente avant de clore, numérique", "gain_numerique_db": "volume numérique",
}, "nom d'un réglage audio, suivi de sa valeur")

# Reglages a la main (2026-10-14, demande utilisateur : choisir soi-meme les
# couches de filtres d'une bande, et un type de contenu qui pose un jeu de
# couches adapte). Ordre de priorite dans get() : usine < appris des notes
# (bandes) < type de contenu < couches reglees a la main. L'apprentissage
# automatique continue d'ecrire "bandes" mais ne passe jamais au-dessus d'une
# couche reglee a la main. Stocke dans "manuel" : {bande: {"contenu", "couches"}}.
# Valeur 0 de anti_crachotements ou reduction_bruit_db : couche coupee.
CONTENTS = {
    # Musique : rien qui ronge le son -- l'anti-sifflement ecrase les notes
    # tenues (il cherche les sons fixes), le retrait de souffle les gargouille.
    "musique": {"filtre_voix": False, "reduction_bruit_db": 0.0, "anti_crachotements": 0.0,
                "anti_sifflement": False, "anti_bourdonnement": False, "pilote_fm": True},
    "voix": {"filtre_voix": True, "passe_bas_hz": 3000.0, "reduction_bruit_db": 10.0, "anti_crachotements": 8.0,
             "anti_sifflement": False, "anti_bourdonnement": False},
    # Donnees / numerique : aucun filtre, pour ne rien abimer de ce qu'un decodeur doit lire.
    "donnees": {"filtre_voix": False, "reduction_bruit_db": 0.0, "anti_crachotements": 0.0,
                "anti_sifflement": False, "anti_bourdonnement": False, "pilote_fm": False, "gain_db": 0.0},
}
CONTENT_ORDER = ["auto", "musique", "voix", "donnees"]
CONTENT_NAMES = Etiquettes("audio.contenu", {"auto": "automatique, appris de tes notes", "musique": "musique",
                                             "voix": "voix", "donnees": "données ou numérique"},
                           "type de contenu d'une bande radio")


def _empty():
    return {"bandes": {}, "historique": [], "auto": False, "manuel": {}}


def load():
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            return {**_empty(), **json.load(f)}
    except (OSError, ValueError):
        return _empty()


class _Locked:
    def __enter__(self):
        self.f = open(LOCK_PATH, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.data = load()
        return self.data

    def __exit__(self, *exc):
        if exc[0] is None:
            os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
            tmp = STORE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STORE_PATH)
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


# Contenu appris d'une station (lib/contenus.py) : seuls les contenus qui
# COUPENT le traitement de la voix s'appliquent ; une station de voix garde
# les reglages appris de sa bande.
LEARNED_CONTENTS = ("musique", "donnees")


def get(band, freq_hz=None):
    """Reglages en vigueur pour une bande : usine, appris des notes, contenu
    appris de la station (freq_hz, si la bande est en contenu automatique),
    type de contenu choisi, puis couches reglees a la main (toujours en dernier)."""
    data = load()
    m = data["manuel"].get(band or "", {})
    learned = {}
    if freq_hz and m.get("contenu", "auto") == "auto":
        try:
            import contenus
            code = contenus.dominant(freq_hz)
            if code in LEARNED_CONTENTS:
                learned = CONTENTS[code]
        except Exception:
            pass
    return {**DEFAULTS, **data["bandes"].get(band or "", {}), **learned, **CONTENTS.get(m.get("contenu"), {}),
            **m.get("couches", {})}


def manual(band):
    """{"contenu": ..., "couches": {...}} regles a la main pour cette bande."""
    m = load()["manuel"].get(band or "", {})
    return {"contenu": m.get("contenu", "auto"), "couches": dict(m.get("couches", {}))}


def set_manual(band, contenu=None, couches=None):
    """Remplace le reglage a la main d'une bande (contenu "auto" et aucune
    couche : la bande revient entierement a l'apprentissage)."""
    with _Locked() as data:
        manual_all = data.setdefault("manuel", {})
        if (contenu in (None, "auto")) and not couches:
            manual_all.pop(band, None)
        else:
            manual_all[band] = {"contenu": contenu or "auto", "couches": dict(couches or {})}


def last_change(band):
    """Heure du dernier changement de cette bande (0 si jamais)."""
    times = [h["t"] for h in load()["historique"] if band in h["changements"]]
    return max(times) if times else 0.0


def apply(changes, description):
    """changes : {bande: {parametre: valeur}} ; une version de plus."""
    with _Locked() as data:
        before = {band: dict(data["bandes"].get(band, {})) for band in changes}
        for band, params in changes.items():
            data["bandes"].setdefault(band, {}).update(params)
        data["historique"].append({"t": time.time(), "description": description,
                                   "changements": changes, "avant": before})
    return len(load()["historique"])


def previous():
    """Annule la derniere version ; rend sa description, ou None."""
    with _Locked() as data:
        if not data["historique"]:
            return None
        last = data["historique"].pop()
        for band, params in last["avant"].items():
            if params:
                data["bandes"][band] = params
            else:
                data["bandes"].pop(band, None)
        return last["description"]


def revert_band(band):
    """Annule le dernier changement de CETTE bande seulement (mode
    automatique : un reglage qui a fait baisser les notes) ; rend sa
    description, ou None."""
    with _Locked() as data:
        for entry in reversed(data["historique"]):
            if band in entry["changements"]:
                before = entry["avant"].get(band, {})
                if before:
                    data["bandes"][band] = before
                else:
                    data["bandes"].pop(band, None)
                del entry["changements"][band]
                entry["avant"].pop(band, None)
                if not entry["changements"]:
                    data["historique"].remove(entry)
                return entry["description"]
    return None


def set_auto(on):
    with _Locked() as data:
        data["auto"] = bool(on)


def reset():
    """Reglages d'usine ; l'ancien etat est archive a cote (rien de perdu).
    Les reglages faits a la main sont des choix, pas un apprentissage : ils
    restent en place."""
    kept = load().get("manuel") or {}
    if os.path.exists(STORE_PATH):
        os.replace(STORE_PATH, STORE_PATH.replace(".json", time.strftime("_archive_%Y-%m-%d_%H%M%S.json")))
    if kept:
        with _Locked() as data:
            data["manuel"] = kept


def spoken_value(param, value):
    if isinstance(value, bool):
        return "activé" if value else "désactivé"
    if value is None:
        return {"attente_fin_s": "3 secondes", "attente_fin_numerique_s": "10 secondes"}.get(param, "valeur d'origine")
    unit = {"attente_fin_s": " secondes", "marge_detection_db": " décibels", "passe_bas_hz": " hertz",
            "reduction_bruit_db": " décibels", "gain_db": " décibels", "attente_fin_numerique_s": " secondes",
            "gain_numerique_db": " décibels"}.get(param, "")
    text = f"{value:+g}" if param in ("marge_detection_db", "gain_db", "gain_numerique_db") else f"{value:g}"
    return text.replace(".", ",") + unit
