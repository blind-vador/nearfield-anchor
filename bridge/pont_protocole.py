"""Protocole du pont Pi <-> machine compagne, commun aux deux cotes
(Python 3.9 et plus ; bibliotheque cryptography).

Confiance : chaque machine a sa paire de cles Ed25519 ; a l'appairage elles
echangent leurs cles publiques par le disque du NAS, et chacune dit un code
a 4 chiffres tire des DEUX cles : si quelqu'un avait remplace une cle en
route, les codes differeraient. Ensuite chaque message (connexion, travail,
fin, resultat, oubli) est signe et verifie avec la cle publique de l'autre.

Liaison : c'est la machine compagne qui se connecte au Pi (port PORT), comme
ca son pare-feu n'a rien a ouvrir ; le Pi y pousse les travaux. Une ligne
JSON par message. Si la machine dort, le Pi envoie un paquet de reveil
(Wake-on-LAN) et elle se reconnecte.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PORT = 47811
MAX_SKEW = 300  # secondes d'ecart d'horloge toleres
MAX_LINE = 65536


def load_or_create_key(path):
    try:
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except FileNotFoundError:
        key = Ed25519PrivateKey.generate()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        return key


def public_b64(key):
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def pairing_code(pub_pi, pub_compagnon):
    """Code a 4 chiffres tire des deux cles publiques, dit par les deux machines."""
    digest = hashlib.sha256(base64.b64decode(pub_pi) + base64.b64decode(pub_compagnon)).digest()
    return "%04d" % (int.from_bytes(digest[:4], "big") % 10000)


def _canonical(msg):
    body = {k: v for k, v in msg.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign(key, msg):
    msg = dict(msg)
    msg.setdefault("t", time.time())
    msg.setdefault("nonce", secrets.token_hex(8))
    msg["sig"] = base64.b64encode(key.sign(_canonical(msg))).decode()
    return msg


def verify(pub_b64, msg, max_age=MAX_SKEW):
    """Signature valide par cette cle publique, et message recent (max_age
    None : pas de limite, pour les fichiers comme les resultats)."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        pub.verify(base64.b64decode(msg["sig"]), _canonical(msg))
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False
    return max_age is None or abs(time.time() - float(msg.get("t", 0))) <= max_age


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Channel:
    """Connexion TCP, une ligne JSON signee par message."""

    def __init__(self, sock, key):
        self.sock = sock
        self.key = key
        self.reader = sock.makefile("rb")

    def send(self, msg):
        data = json.dumps(sign(self.key, msg), ensure_ascii=False).encode() + b"\n"
        self.sock.sendall(data)

    def receive(self, pub_b64):
        """Message suivant verifie, None si la connexion est fermee ;
        ValueError si un message n'est pas signe par cette cle."""
        line = self.reader.readline(MAX_LINE)
        if not line:
            return None
        msg = json.loads(line)
        if not verify(pub_b64, msg):
            raise ValueError("message non signé par la machine attendue")
        return msg

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # debloque une lecture en cours dans un autre fil
        except OSError:
            pass
        for thing in (self.reader, self.sock):
            try:
                thing.close()
            except OSError:
                pass


def wake_on_lan(mac_address, ips=()):
    """Paquet magique vers la machine endormie (adresse materielle)."""
    try:
        raw = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
    except (AttributeError, ValueError):
        return False
    if len(raw) != 6:
        return False
    packet = b"\xff" * 6 + raw * 16
    targets = {"255.255.255.255"}
    for ip in ips:
        parts = ip.split(".")
        if len(parts) == 4:
            targets.add(".".join(parts[:3] + ["255"]))  # reseau local /24
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            for port in (9, 7):
                try:
                    s.sendto(packet, (target, port))
                except OSError:
                    pass
    return True
