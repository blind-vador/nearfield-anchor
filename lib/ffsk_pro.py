"""Signalisation des radios professionnelles analogiques (2026-11-01) : MDC1200
(Motorola), FleetSync II (Kenwood) et MPT1327 (reseaux a ressources partagees,
dont la variante francaise). Ce sont des identifiants et des messages en clair
envoyes en salves FFSK 1200 bauds (1200 / 1800 Hz) au debut ou a la fin d'une
emission vocale, ou en continu sur une voie de controle MPT1327. multimon-ng
ne les decode pas.

Formats repris des implementations de reference, pas du code :
  - MDC1200 : encodeur/decodeur de Matthew Kaufman (mdc-encode-decode) --
    un bit egal au precedent a 1200 Hz, un changement a 1800 Hz ; synchro
    0x07092A446F ; 112 bits entrelaces 16 x 7 ; parite convolutive (prises 0,
    2, 5, 6) corrigeant une erreur ; CRC-16 0x1021 a bits reflechis, inverse.
    Verifie contre l'encodeur de reference (tests/test_ffsk_pro.py).
  - FleetSync II et MPT1327 : SDRTrunk (DSheirer) -- 1200 Hz = 1 ; mots de
    code de 64 bits (48 utiles, CRC-15 0x6815 depart 1, parite paire) ;
    synchros FleetSync 0x23EB, MPT1327 0xC4D7 (controle) / 0x3B28 (trafic),
    variante francaise 0xB433 / 0x4BCC en polarite inversee.

Utilisable seul : "python3 ffsk_pro.py [taux]" lit du S16LE mono sur l'entree
standard et ecrit une ligne "MDC1200: ...", "FLEETSYNC: ..." ou "MPT1327: ..."
par message decode (meme usage que multimon-ng et asn_dsc)."""
import sys

import numpy as np

BAUDS = 1200
TAUX_INTERNE = 24000
ECH_BIT = TAUX_INTERNE // BAUDS
FENETRE_SEC = 4.0
# Une passe coute ~0,18 s sur le Pi 4 (mesure 2026-11-01) : toutes les 2 s plutot
# que chaque seconde. La salve la plus longue (FleetSync GPS) fait 0,45 s : avec
# 4 s de fenetre, chacune est vue entiere au moins une fois.
PAS_SEC = 2.0


# --- demodulation commune ------------------------------------------------------

def demoduler(pcm, taux):
    """Audio S16 -> score par echantillon a 24 kHz (> 0 : 1800 Hz)."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if taux != TAUX_INTERNE and len(x):
        t = np.arange(0, len(x) * TAUX_INTERNE / taux) * taux / TAUX_INTERNE
        x = np.interp(t, np.arange(len(x)), x).astype(np.float32)
    n = np.arange(len(x))
    noyau = np.ones(ECH_BIT, dtype=np.float32) / ECH_BIT
    energie = [np.abs(np.convolve(x * np.exp(-2j * np.pi * f * n / TAUX_INTERNE), noyau, "same"))
               for f in (1800, 1200)]
    return energie[0] - energie[1]


def _tons(score, phase):
    """1 si le bit est a 1800 Hz."""
    return (score[phase::ECH_BIT] > 0).astype(np.int8)


def _chercher(bits, motif, erreurs_max):
    """Indices de debut du motif dans bits (au plus erreurs_max bits faux)."""
    if len(bits) < len(motif):
        return []
    a = 2 * bits.astype(np.int32) - 1
    m = 2 * np.asarray(motif, dtype=np.int32) - 1
    accord = np.correlate(a, m, "valid")          # nombre d'accords - nombre de desaccords
    return list(np.nonzero(accord >= len(motif) - 2 * erreurs_max)[0])


def _entier(bits):
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def _motif(valeur, longueur):
    return [(valeur >> (longueur - 1 - i)) & 1 for i in range(longueur)]


# --- mots de code de 64 bits (FleetSync, MPT1327) -------------------------------

POLY15 = 0x6815


def _crc15_unitaire(position):
    """CRC-15 (depart 0) du message de 48 bits ou seul le bit `position` vaut 1."""
    reg = 0
    for i in range(48):
        retour = ((reg >> 14) & 1) ^ (1 if i == position else 0)
        reg = (reg << 1) & 0x7FFF
        if retour:
            reg ^= POLY15
    return reg


_TABLE15 = [_crc15_unitaire(i) for i in range(48)] + [1 << (14 - i) for i in range(15)]


def _crc15(bits48):
    c = 1
    for i, b in enumerate(bits48):
        if b:
            c ^= _TABLE15[i]
    return c


def mot_de_code(bits64):
    """Bits du mot verifies, corriges d'une erreur au besoin, ou None."""
    bits = [int(b) for b in bits64]
    if len(bits) != 64:
        return None
    recu = _entier(bits[48:63])
    if sum(bits) % 2 == 0:
        return bits if _crc15(bits[:48]) == recu else None
    # parite fausse : une seule erreur, retrouvee par son syndrome
    syndrome = recu ^ _crc15(bits[:48])
    if syndrome == 0:
        bits[63] ^= 1
        return bits
    if syndrome in _TABLE15:
        bits[_TABLE15.index(syndrome)] ^= 1
        return bits
    return None


# --- MDC1200 -------------------------------------------------------------------

MDC_SYNC = _motif(0x07092A446F, 40)
MDC_DOUBLES = (0x35, 0x55)
MDC_OPERATIONS = {0x00: "urgence", 0x01: "identification", 0x22: "contrôle de présence",
                  0x2B: "blocage ou déblocage du poste", 0x35: "appel sélectif", 0x55: "message"}


def _crc_mdc(octets):
    crc = 0
    for o in octets:
        o = int(f"{o:08b}"[::-1], 2)
        for j in range(7, -1, -1):
            retour = ((crc >> 15) & 1) ^ ((o >> j) & 1)
            crc = (crc << 1) & 0xFFFF
            if retour:
                crc ^= 0x1021
    return int(f"{crc:016b}"[::-1], 2) ^ 0xFFFF


def _mdc_corriger(data):
    """Parite convolutive (prises 0, 2, 5, 6) : corrige une erreur isolee."""
    reg = [0] * 7
    syn = 0
    for i in range(7):
        for j in range(8):
            reg = [(data[i] >> j) & 1] + reg[:6]
            b = (reg[0] + reg[2] + reg[5] + reg[6]) & 1
            syn = ((syn << 1) | (b ^ ((data[i + 7] >> j) & 1))) & 0xFF
            if bin(syn & 0xA6).count("1") >= 3:
                syn ^= 0xA6
                fi, fj = i, j - 7
                if fj < 0:
                    fi, fj = fi - 1, fj + 8
                if fi >= 0:
                    data[fi] ^= 1 << fj
    return data


def _mdc_bloc(bits112):
    """112 bits recus -> 4 octets utiles si le CRC est bon, sinon None."""
    lbits = [0] * 112
    c = 0
    for col in range(16):
        for ligne in range(7):
            lbits[c] = int(bits112[ligne * 16 + col])
            c += 1
    data = [sum(lbits[i * 8 + j] << j for j in range(8)) for i in range(14)]
    data = _mdc_corriger(data)
    if _crc_mdc(data[:4]) != (data[5] << 8 | data[4]):
        return None
    return data[:4]


def mdc1200(score):
    """Paquets MDC1200 trouves : liste de textes."""
    sortie = []
    for phase in range(ECH_BIT):
        donnees = np.cumsum(_tons(score, phase)) % 2      # un changement = 1800 Hz
        for pol in (0, 1):
            d = donnees ^ pol
            for debut in _chercher(d, MDC_SYNC, 4):
                bloc = d[debut + 40: debut + 152]
                if len(bloc) < 112:
                    continue
                utiles = _mdc_bloc(bloc)
                if not utiles:
                    continue
                op, arg, unite = utiles[0], utiles[1], (utiles[2] << 8) | utiles[3]
                texte = f"poste {unite:04X}, {MDC_OPERATIONS.get(op, f'opération {op:02X}')}, argument {arg:02X}"
                if op in MDC_DOUBLES:
                    extra = _mdc_bloc(d[debut + 152: debut + 264]) if len(d) >= debut + 264 else None
                    if extra:
                        texte += ", données " + " ".join(f"{o:02X}" for o in extra)
                if texte not in sortie:
                    sortie.append(texte)
        if sortie:
            return sortie
    return sortie


# --- FleetSync II --------------------------------------------------------------

FS_SYNC = [0, 1, 0, 1, 0] + _motif(0x23EB, 16)


def fleetsync(score):
    sortie = []
    for phase in range(ECH_BIT):
        tons = _tons(score, phase)
        for bits in (1 - tons, tons):              # 1200 Hz = 1 ; l'autre polarite par prudence
            for debut in _chercher(bits, FS_SYNC, 1):
                mot = mot_de_code(bits[debut + 21: debut + 85])
                if not mot:
                    continue
                m = [None] * 21 + mot                # indices comme SDRTrunk : message depuis la synchro
                flotte = _entier(m[37:45]) + 99
                de, vers = _entier(m[45:57]) + 999, _entier(m[57:69]) + 999
                if m[31]:
                    nature = "accusé de réception"
                elif m[35]:
                    nature = "position GPS"
                elif m[30]:
                    nature = f"état {_entier(m[21:28])}"
                else:
                    nature = "identification"
                texte = f"flotte {flotte}, poste {de} vers {vers}, {nature}"
                if texte not in sortie:
                    sortie.append(texte)
        if sortie:
            return sortie
    return sortie


# --- MPT1327 -------------------------------------------------------------------

MPT_SYNCS = [
    # (polarite des bits : 0 = 1200 Hz vaut 1, synchro, nom)
    (0, [1, 0, 1, 0] + _motif(0xC4D7, 16), "contrôle"),
    (0, [1, 0, 1, 0] + _motif(0x3B28, 16), "trafic"),
    (1, [1, 0, 1, 0] + _motif(0xB433, 16), "contrôle, variante française"),
    (1, [1, 0, 1, 0] + _motif(0x4BCC, 16), "trafic, variante française"),
]
MPT_ACQUITS = {264: "accusé", 265: "suite à venir", 266: "appel en file", 267: "message refusé",
               268: "poste appelé indisponible", 269: "urgence", 270: "essayer une autre adresse",
               271: "rappel"}
MPT_SILENCIEUX = set(range(256, 264)) | {280, 281}   # invitations d'acces, marqueurs, maintien : en continu


def _adresse(prefixe, ident):
    if ident >= 8100:
        return f"adresse spéciale {prefixe}-{ident}"
    return f"{prefixe}-{ident}"


def _mpt_texte(m):
    """Mot d'adresse (m indexe comme SDRTrunk : 20 bits de synchro devant)."""
    if not m[20]:
        return None                                # mot de donnees seul : pas d'en-tete
    prefixe = _entier(m[21:28])
    ident1 = _entier(m[28:41])
    genre = _entier(m[41:50])
    if genre < 256:                                # GTC : attribution d'un canal de trafic
        canal = _entier(m[43:53])
        ident2 = _entier(m[53:66])
        return f"appel attribué au canal {canal}, {_adresse(prefixe, ident2)} vers {_adresse(prefixe, ident1)}"
    if genre in MPT_SILENCIEUX:
        return None
    ident2 = _entier(m[50:63])
    if genre == 284:
        return f"paramètres du système {_entier(m[26:41])}"
    if genre in MPT_ACQUITS:
        return f"{MPT_ACQUITS[genre]} pour {_adresse(prefixe, ident1)}"
    if genre == 272:
        return f"appel de disponibilité vers {_adresse(prefixe, ident1)}"
    if genre == 278:
        return f"message d'état de {_adresse(prefixe, ident2)} vers {_adresse(prefixe, ident1)}"
    if genre == 279:
        return f"données courtes de {_adresse(prefixe, ident2)} vers {_adresse(prefixe, ident1)}"
    if genre == 282:
        return f"libération du canal {_entier(m[21:31])}"
    if genre == 283:
        return f"changement de canal vers le {_entier(m[35:45])} pour {_adresse(prefixe, ident1)}"
    return None


def mpt1327(score):
    sortie = []
    for phase in range(ECH_BIT):
        tons = _tons(score, phase)
        for pol, motif, voie in MPT_SYNCS:
            bits = tons if pol else 1 - tons
            for debut in _chercher(bits, motif, 1):
                mot = mot_de_code(bits[debut + 20: debut + 84])
                if not mot:
                    continue
                texte = _mpt_texte([None] * 20 + mot)
                if texte:
                    texte = f"voie de {voie}, {texte}"
                    if texte not in sortie:
                        sortie.append(texte)
        if sortie:
            return sortie
    return sortie


DECODEURS = [("MDC1200", mdc1200), ("FLEETSYNC", fleetsync), ("MPT1327", mpt1327)]


def decoder(pcm, taux):
    """[(protocole, texte)] trouves dans ce son."""
    score = demoduler(pcm, taux)
    return [(nom, t) for nom, f in DECODEURS for t in f(score)]


def main():
    taux = int(sys.argv[1]) if len(sys.argv) > 1 else 22050
    entree = sys.stdin.buffer
    fenetre = int(FENETRE_SEC * taux) * 2
    pas = int(PAS_SEC * taux) * 2
    tampon, deja = b"", []
    while True:
        data = entree.read(pas)
        if not data:
            break
        tampon = (tampon + data)[-fenetre:]
        if len(tampon) < fenetre // 2:
            continue
        for nom, texte in decoder(tampon, taux):
            ligne = f"{nom}: {texte}"
            if ligne not in deja:
                deja = (deja + [ligne])[-40:]   # un message est vu dans plusieurs fenetres
                print(ligne, flush=True)


if __name__ == "__main__":
    main()
