"""Veille APRS (144,800 MHz) : positions, stations meteo, messages et
bulletins des radioamateurs, reseau public tres actif en France.

rtl_fm (FM etroite) -> multimon-ng -A (AFSK1200, sortie texte TNC2) ->
aprslib pour decoder. Fenetre d'ecoute placee dans le cycle du scanner par
le systeme d'attention, comme l'ACARS.

Annonces (le reste est seulement archive, voir veille_donnees.archive) :
- messages adresses et bulletins : toujours (anti-repetition 1 h) ;
- station jamais annoncee depuis STATION_EVERY_SEC : indicatif, distance,
  direction, en mouvement ou non ;
- station qui passe a moins de NEAR_KM : re-annoncee (NEAR_EVERY_SEC) ;
- station meteo : releve toutes les WX_EVERY_SEC par station.
Les derniers releves meteo sont publies pour le flash et la console
(WX_PATH).
"""
import json
import os
import re
import subprocess
import threading
import time

import aprslib

import aprs_texte
import relais
import veille_donnees as vd
from sound_paths import RADIO_SCANER_DIR
from phrases import dire

APRS_FREQ_HZ = 144_800_000
SAMPLE_RATE = 22050
SOUND = os.path.join(RADIO_SCANER_DIR, "decoding_numeric.mp3")
STATE_PATH = "/tmp/aprs_watch_state.json"
WX_PATH = "/tmp/meteo_stations_aprs.json"
STATION_EVERY_SEC = 6 * 3600
NEAR_KM = 3.0
NEAR_EVERY_SEC = 1800
WX_EVERY_SEC = 3 * 3600
MESSAGE_EVERY_SEC = 3600
MAX_DISTANCE_KM = 150      # au-dela : trajet anormal (digipeteur lointain), archive seulement
WX_KEEP_SEC = 3 * 3600
LINE = re.compile(r"^APRS: (\S+>.*)$")

_memory = vd.Memory(STATE_PATH)
# Ecoute d'un relais en orbite (passage de l'ISS, satellites_watch) : les
# stations sont alors lointaines (toute l'Europe), annoncees "via" le relais,
# dans la limite de VIA_MAX_ANNOUNCES par passage.
_via = {"nom": None, "annonces": 0}
VIA_MAX_ANNOUNCES = 6


def _weather_text(wx):
    parts = []
    if wx.get("temperature") is not None:
        parts.append(dire("aprs.degres", fr_number=vd.fr_number(wx['temperature'])))
    if wx.get("humidity") is not None:
        parts.append(dire("aprs.humidite_cent", int=int(wx['humidity'])))
    if wx.get("wind_speed") is not None:
        parts.append(dire("aprs.vent_kilometres_heure", round=round(wx['wind_speed'] * 3.6)))
    if wx.get("rain_1h"):
        parts.append(dire("aprs.millimetres_pluie_heure", fr_number=vd.fr_number(wx['rain_1h'], 1)))
    if wx.get("pressure") is not None:
        parts.append(f"pression {round(wx['pressure'])} hectopascals")
    return ", ".join(parts)


def _publish_weather(call, lat, lon, wx):
    try:
        with open(WX_PATH, encoding="utf-8") as f:
            stations = json.load(f)
    except (OSError, ValueError):
        stations = {}
    now = time.time()
    stations[call] = {"lat": lat, "lon": lon, "meteo": wx, "t": now}
    stations = {k: v for k, v in stations.items() if now - v.get("t", 0) < WX_KEEP_SEC}
    try:
        tmp = WX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stations, f)
        os.replace(tmp, WX_PATH)
    except OSError:
        pass


def _process(packet):
    """Annonce ce qui le merite. Rend True si une annonce a eu lieu."""
    vd.archive("aprs", packet)
    if packet.get("format") == "thirdparty" and isinstance(packet.get("subpacket"), dict):
        # Relaye par une passerelle (internet, LoRa) : le contenu reste local.
        packet = packet["subpacket"]
    call = packet.get("from", "")
    fmt = packet.get("format")
    if fmt == "message" and packet.get("message_text") and not packet.get("response"):
        to = packet.get("addresse", "")
        text = packet["message_text"].strip()
        if _memory.due(f"msg|{call}|{text[:60]}", MESSAGE_EVERY_SEC):
            vd.announce(dire("aprs.message_aprs", spell=vd.spell(call), spell2=vd.spell(to), text=text), SOUND,
                        f"APRS_message_{call}", "aprs_watch")
            return True
        return False
    if fmt == "bulletin" or packet.get("bid") is not None:
        text = (packet.get("message_text") or "").strip()
        if text and _memory.due(f"bln|{call}|{text[:60]}", MESSAGE_EVERY_SEC):
            vd.announce(dire("aprs.bulletin_aprs", spell=vd.spell(call), text=text), SOUND, f"APRS_bulletin_{call}", "aprs_watch")
            return True
        return False
    lat, lon = packet.get("latitude"), packet.get("longitude")
    if lat is None or lon is None:
        return False
    km = vd.distance_km(lat, lon)
    if _via["nom"]:
        if _via["annonces"] >= VIA_MAX_ANNOUNCES:
            return False
        name = (packet.get("object_name") or call).strip()
        _via["annonces"] += 1
        vd.announce(dire("aprs.via_station", nom=_via['nom'], spell=vd.spell(name), where=vd.where(lat, lon)), SOUND,
                    f"APRS_via_{name}", "aprs_watch")
        return True
    if km > MAX_DISTANCE_KM:
        return False
    name = (packet.get("object_name") or packet.get("item_name") or call).strip()
    wx = packet.get("weather")
    if wx:
        _publish_weather(name, lat, lon, wx)
        details = _weather_text(wx)
        if details and _memory.due(f"wx|{name}", WX_EVERY_SEC):
            vd.announce(dire("aprs.station_meteo_aprs", spell=vd.spell(name), where=vd.where(lat, lon), details=details), SOUND,
                        f"APRS_meteo_{name}", "aprs_watch")
            return True
        return False
    speed = packet.get("speed") or 0
    moving = speed >= 5
    kind = aprs_texte.station_type(packet)
    text = f"{kind.capitalize() if kind else 'Station APRS'} {vd.spell(name)}, {vd.where(lat, lon)}"
    if moving:
        text += dire("aprs.mouvement_kilometres_heure", round=round(speed))
    # Relais qui annonce sa frequence : appris et ajoute a l'ecoute du scanner (lib/relais.py).
    learned = relais.learn(name, packet.get("comment") or "", packet.get("symbol"), km)
    if learned:
        print(f"[aprs_watch] relais appris : {name} sur {learned / 1e6:.4f} MHz")
    # Commentaire traduit (reglages de relais, tension, sigles...) : voir lib/aprs_texte.py.
    comment = aprs_texte.comment(packet.get("comment") or packet.get("status") or "", name)
    header = text
    if comment:
        text += f". {comment[0].upper()}{comment[1:]}"
    first = _memory.due(f"station|{name}", STATION_EVERY_SEC)
    near = km <= NEAR_KM and _memory.due(f"near|{name}", NEAR_EVERY_SEC)
    if first or near:
        prefix = (dire("aprs.tout_pres") + " ") if near and not first else ""
        raw_comment = (packet.get("comment") or packet.get("status") or "").strip()
        explained = _explain(name, packet, raw_comment) if aprs_texte.is_weird(raw_comment, comment) else None
        if explained:  # l'explication remplace la traduction
            text = f"{header}. {explained.rstrip('.')}"
        if learned:
            text += dire("aprs.ajoute_mon_ecoute_relais")
        vd.announce(prefix + text + ".", SOUND, f"APRS_{name}", "aprs_watch")
        return True
    return False


# Commentaire APRS reste obscur apres traduction (demande utilisateur
# 2026-10-01) : Gemini l'explique simplement, avec une touche d'humour.
# Explication gardee par (station, commentaire) : pas de nouvel appel tant
# que la station repete le meme texte.
EXPLAIN_PROMPT = """Tu es la voix d'un récepteur radio domestique, au ton amusé et bienveillant.
Une station radioamateur APRS vient de diffuser ce paquet. Son indicatif et sa position ont déjà
été annoncés : ne les répète pas. Explique en français, à une personne aveugle, en 1 à 3 phrases
courtes à lire à voix haute, ce que son commentaire veut sans doute dire, avec une petite blague.
Si c'est de la télémétrie codée ou incompréhensible, dis-le simplement, avec humour. N'invente
aucun fait, aucun nom. Pas de markdown, pas d'emoji, pas de liste.

Paquet brut : {raw}
Format : {fmt} ; symbole APRS : {table}{symbol}
Commentaire : {comment}
"""
EXPLAIN_CACHE_MAX = 300


def _explain(name, packet, raw_comment):
    cache = _memory.data.setdefault("explications", {})
    key = f"{name}|{raw_comment}"[:300]
    if key in cache:
        return cache[key]
    try:
        import gemini
        text, problem = gemini.ask(EXPLAIN_PROMPT.format(
            raw=packet.get("raw", ""), fmt=packet.get("format", ""), table=packet.get("symbol_table", ""),
            symbol=packet.get("symbol", ""), comment=raw_comment))
    except Exception as exc:
        text, problem = None, repr(exc)
    if problem:
        print(f"[aprs_watch] explication Gemini indisponible : {problem}")
        return None
    cache[key] = text
    while len(cache) > EXPLAIN_CACHE_MAX:
        cache.pop(next(iter(cache)))
    vd.archive("aprs_gemini", {"station": name, "commentaire": raw_comment, "texte": text})
    return text


def _reader(proc, packets):
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        m = LINE.match(line)
        if not m:
            continue
        try:
            packets.append(aprslib.parse(m.group(1)))
        except (aprslib.ParseError, aprslib.UnknownFormat, ValueError) as exc:
            vd.archive("aprs", {"raw": m.group(1), "erreur": str(exc)[:100]})


def scan_window(dwell_sec=30, freq_hz=APRS_FREQ_HZ, via=None):
    """Une passe d'ecoute APRS. Rend True si au moins une annonce a eu lieu.
    via : nom d'un relais en orbite ecoute sur freq_hz (passage de l'ISS)."""
    _via.update(nom=via, annonces=0)
    try:
        rtl = subprocess.Popen(["rtl_fm", "-f", str(freq_hz), "-M", "fm", "-s", str(SAMPLE_RATE),
                                "-l", "0", "-E", "dc", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("[aprs_watch] rtl_fm introuvable")
        return False
    try:
        mm = subprocess.Popen(["multimon-ng", "-A", "-a", "AFSK1200", "-t", "raw", "-"],
                              stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("[aprs_watch] multimon-ng introuvable")
        rtl.kill()
        rtl.wait()
        return False
    rtl.stdout.close()
    packets = []
    reader = threading.Thread(target=_reader, args=(mm, packets), daemon=True)
    reader.start()
    announced = False
    count = 0
    try:
        deadline = time.time() + dwell_sec
        while time.time() < deadline:
            time.sleep(0.2)
            while packets:
                count += 1
                if _process(packets.pop(0)):
                    announced = True
    finally:
        _via.update(nom=None)
        for p in (rtl, mm):
            if p.poll() is None:
                p.terminate()
        for p in (rtl, mm):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        while packets:  # recus pendant l'arret
            count += 1
            if _process(packets.pop(0)):
                announced = True
        _memory.save()
    if count:
        print(f"[aprs_watch] {count} paquet(s) APRS decodes en {dwell_sec:.0f} s")
    return announced
