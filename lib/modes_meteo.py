"""Mode S etendu (2026-10-03) : les avions mesurent l'air ou ils volent et
le transmettent aux radars sur 1090 MHz, en clair -- vent, temperature,
pression, humidite (registre BDS 4,4, "meteo de routine") et dangers
(BDS 4,5 : turbulence, cisaillement, microrafale, givrage, turbulence de
sillage). Decode par pyModeS sur les trames brutes de dump1090 (fenetre
ADS-B locale, watchers/adsb_local_watch.py) ; chaque compte rendu est dit
au fil de l'eau comme un message du journal "messages", et les vents en
altitude sont publies (VENTS_PATH) pour la meteo locale.
"""
import json
import os
import time

import veille_donnees as vd
from phrases import Etiquettes, dire

SOUND = "/media/nas/nd1/server/radio_scaner/decoding_numeric.mp3"
VENTS_PATH = "/tmp/vents_altitude.json"
VENTS_KEEP_SEC = 3 * 3600
REPEAT_SEC = 300           # un compte rendu par avion toutes les 5 min au plus (il en envoie a la chaine)
FEET_TO_M = 0.3048
KT_TO_KMH = 1.852
HAZARD_LEVELS = Etiquettes("modes.danger_niveau", {1: "léger", 2: "modéré", 3: "fort"}, "intensité d'un danger signalé par un avion")
HAZARDS = Etiquettes("modes.danger", {"turbulence": "turbulence", "cisaillement": "cisaillement du vent",
                                      "microrafale": "microrafale", "givrage": "givrage",
                                      "sillage": "turbulence de sillage"}, "danger signalé par un avion")
_memory = vd.Memory("/tmp/modes_meteo.json")


def _direction(degrees):
    import meteo_locale
    return meteo_locale._wind_from(degrees)


def _publish_wind(icao, alt_m, speed_kmh, direction, temp):
    try:
        with open(VENTS_PATH, encoding="utf-8") as f:
            winds = json.load(f)
    except (OSError, ValueError):
        winds = []
    now = time.time()
    winds = [w for w in winds if now - w["t"] < VENTS_KEEP_SEC] + [
        {"t": now, "avion": icao, "altitude_m": alt_m, "vent_kmh": speed_kmh, "direction": direction, "temp": temp}]
    try:
        tmp = VENTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(winds[-200:], f)
        os.replace(tmp, VENTS_PATH)
    except OSError:
        pass


def _decode(msg):
    """{icao, altitude_m, genre: meteo|dangers, ...} ou None."""
    import pyModeS as pms
    if len(msg) != 28 or pms.df(msg) not in (20, 21):
        return None
    try:
        bds = pms.bds.infer(msg, mrar=True)
    except Exception:
        return None
    alt_ft = pms.common.altcode(msg) if pms.df(msg) == 20 else None
    out = {"icao": pms.icao(msg), "altitude_m": round(alt_ft * FEET_TO_M, -2) if alt_ft else None}
    if bds == "BDS44":
        speed, direction = pms.commb.wind44(msg)
        temp = pms.commb.temp44(msg)
        temp = temp[0] if isinstance(temp, tuple) else temp
        if speed is None or temp is None or not -80 <= temp <= 50:
            return None
        out.update(genre="meteo", vent_kmh=round(speed * KT_TO_KMH), direction=direction, temp=round(temp),
                   pression=pms.commb.p44(msg), humidite=pms.commb.hum44(msg))
        return out
    if bds == "BDS45":
        levels = {"turbulence": pms.commb.turb45(msg), "cisaillement": pms.commb.ws45(msg),
                  "microrafale": pms.commb.mb45(msg), "givrage": pms.commb.ic45(msg), "sillage": pms.commb.wv45(msg)}
        found = {k: v for k, v in levels.items() if v}
        if not found:
            return None
        out.update(genre="dangers", dangers=found)
        return out
    return None


def _sentence(d):
    if d["genre"] == "meteo":
        text = dire("modes.meteo", altitude=int(d["altitude_m"]) if d.get("altitude_m") else "?",
                    direction=_direction(d["direction"]), vent=d["vent_kmh"],
                    temperature=vd.fr_number(d["temp"], 0).replace("-", dire("meteo.moins") + " "))
        if d.get("humidite") is not None:
            text += " " + dire("modes.humidite", humidite=round(d["humidite"]))
        return text
    dangers = ", ".join(f"{HAZARDS[k]} {HAZARD_LEVELS.get(v) or ''}".strip() for k, v in d["dangers"].items())
    return dire("modes.dangers", dangers=dangers)


def process(msg):
    """Une trame brute Mode S (hexadecimal) : annonce si c'est de la meteo ou un danger."""
    try:
        d = _decode(msg.strip().strip("*;").upper())
    except Exception:
        return False
    if not d:
        return False
    if d["genre"] == "meteo":
        _publish_wind(d["icao"], d.get("altitude_m"), d["vent_kmh"], d["direction"], d["temp"])
    if not _memory.due(f"{d['genre']}|{d['icao']}", REPEAT_SEC):
        return False
    _memory.save()
    fiche = {"protocole": "Mode S", "frequence_hz": 1_090_000_000, "source": d["icao"], "mesures": d}

    def text():  # dans la file d'annonces : identite de l'avion sans bloquer l'ecoute
        import acars_traduction as tr
        import avions_info
        card = avions_info.card(hex_addr=d["icao"])
        fiche["source"] = " ".join(x for x in (card.get("modele"), card.get("immatriculation")) if x) or d["icao"]
        return dire("acars.provenance", qui=tr._who(card), protocole="Mode S") + " " + _sentence(d)

    vd.announce(text, SOUND, f"message_avion_modes_{d['icao']}", "modes_meteo", fiche=fiche)
    return True
