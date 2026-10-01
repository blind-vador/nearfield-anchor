"""Lecture des paquets radio Meshtastic et APRS LoRa (charge utile LoRa deja
demodulee par tools/lora_traitement.py), sans la bibliotheque meshtastic
complete : en-tete, dechiffrement AES-CTR du canal public par defaut (sa
cle est publique, documentee par Meshtastic), et les messages utiles du
protobuf "Data" (texte, position, nom du noeud). Un canal prive (autre cle)
reste illisible : on le dit et on n'insiste pas.
"""
import base64
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Cle du canal public par defaut ("AQ==" dans l'application), documentee.
DEFAULT_KEY = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==")
BROADCAST = 0xFFFFFFFF
PORTS = {1: "texte", 3: "position", 4: "nœud", 67: "télémétrie", 8: "point de repère", 70: "traceroute"}


def _varint(data, i):
    value, shift = 0, 0
    while i < len(data):
        b = data[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7
    raise ValueError("varint tronque")


def _fields(data):
    """Protobuf brut -> {numero: [valeurs]} (varint, 32/64 bits, octets)."""
    out, i = {}, 0
    while i < len(data):
        key, i = _varint(data, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(data, i)
        elif wire == 1:
            value, i = data[i:i + 8], i + 8
        elif wire == 2:
            length, i = _varint(data, i)
            value, i = data[i:i + length], i + length
        elif wire == 5:
            value, i = data[i:i + 4], i + 4
        else:
            raise ValueError("type protobuf inconnu")
        out.setdefault(number, []).append(value)
    return out


def _decrypt(payload, packet_id, sender, key=DEFAULT_KEY):
    nonce = struct.pack("<QI", packet_id, sender) + b"\x00" * 4
    decryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor()
    return decryptor.update(payload) + decryptor.finalize()


def decode_meshtastic(frame):
    """Trame LoRa Meshtastic -> dict lisible, ou None si ce n'en est pas une."""
    if len(frame) < 17:
        return None
    dest, sender, packet_id, flags, channel, _, _ = struct.unpack("<IIIBBBB", frame[:16])
    info = {"de": f"!{sender:08x}", "pour": "tous" if dest == BROADCAST else f"!{dest:08x}",
            "id": packet_id, "sauts_restants": flags & 7, "canal": channel}
    clear = _decrypt(frame[16:], packet_id, sender)
    try:
        fields = _fields(clear)
        port = fields.get(1, [None])[0]
        body = fields.get(2, [b""])[0]
    except (ValueError, IndexError):
        info["lisible"] = False  # canal prive : autre cle
        return info
    if port not in PORTS or not isinstance(body, (bytes, bytearray)):
        info["lisible"] = False
        return info
    info.update(lisible=True, type=PORTS[port])
    if port == 1:  # texte brut, pas un protobuf
        info["texte"] = bytes(body).decode("utf-8", errors="replace")
        return info
    try:
        sub = _fields(body)
        if port == 3:
            lat = struct.unpack("<i", sub[1][0])[0] / 1e7 if 1 in sub else None
            lon = struct.unpack("<i", sub[2][0])[0] / 1e7 if 2 in sub else None
            info.update(lat=lat, lon=lon, altitude=sub.get(3, [None])[0])
        elif port == 4:
            info.update(nom=bytes(sub.get(2, [b""])[0]).decode("utf-8", errors="replace"),
                        nom_court=bytes(sub.get(3, [b""])[0]).decode("utf-8", errors="replace"))
    except (ValueError, IndexError, struct.error):
        pass
    return info


def decode_lora_aprs(frame):
    """Trame APRS LoRa ("<\\xff\\x01" puis texte TNC2) -> texte TNC2 ou None."""
    if frame[:3] == b"<\xff\x01":
        return frame[3:].decode("utf-8", errors="replace").strip()
    return None
