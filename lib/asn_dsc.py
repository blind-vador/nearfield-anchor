"""Decodeur ASN / DSC VHF (appel selectif numerique, UIT-R M.493), 2026-11-01.

Aucun outil empaquete ne le decode (multimon-ng non plus) : alertes de detresse,
appels de securite, appels individuels des navires, en clair sur la VHF marine
(canal 70, 156,525 MHz) -- mais on ne pose pas de mur : il ecoute tout flux FM
que le scanner ouvre, comme multimon-ng.

Couche physique (VHF) : FSK 1200 bauds, 2100 Hz = B = bit 0, 1300 Hz = Y = bit 1.
Un symbole = 10 bits : 7 bits d'information (poids faible d'abord) puis 3 bits
de controle = nombre de bits 0 parmi les 7 (poids fort d'abord). Diversite
temporelle : chaque caractere passe en position DX puis en position RX deux
caracteres plus tard (DX et RX alternent). Mise en phase : DX = 125, RX = 111,
110, ..., 104 ; le message commence au 7e DX.

Utilisable seul : "python3 asn_dsc.py [taux]" lit du S16LE mono sur l'entree
standard et ecrit une ligne "ASN: ..." par appel decode (meme usage que
multimon-ng dans radio_watch)."""
import sys

import numpy as np

BAUDS = 1200
TAUX_INTERNE = 24000                 # 20 echantillons par bit, tout rond
ECH_BIT = TAUX_INTERNE // BAUDS
TON_Y, TON_B = 1300, 2100
FENETRE_SEC = 4.0                    # audio analyse a chaque passe (un appel dure < 1 s)
PAS_SEC = 1.0
EOS = (117, 122, 127)
DX_PHASE = 125
RX_PHASES = list(range(111, 103, -1))   # 111 ... 104
MESSAGE_MAX = 40

FORMATS = {102: "zone géographique", 112: "détresse", 114: "groupe", 116: "tous navires",
           120: "individuel", 123: "individuel automatique", 121: "ATIS"}
# ATIS (2026-11-01) : identification automatique de l'emetteur sur les voies
# navigables (accord RAINWAT), meme modulation que l'ASN, envoyee a la fin de
# chaque emission vocale. Specificateur 121 puis le code ATIS (10 chiffres,
# "9" + pays + indicatif) -- a confirmer sur une vraie salve.
PAYS = {"226": "France", "227": "France", "228": "France", "211": "Allemagne", "218": "Allemagne",
        "244": "Pays-Bas", "245": "Pays-Bas", "246": "Pays-Bas", "205": "Belgique", "253": "Luxembourg",
        "269": "Suisse", "203": "Autriche", "270": "Tchéquie", "243": "Hongrie", "238": "Croatie",
        "264": "Roumanie", "207": "Bulgarie", "261": "Pologne", "267": "Slovaquie", "279": "Serbie"}
CATEGORIES = {100: "routine", 108: "sécurité", 110: "urgence", 112: "détresse"}
NATURES = {100: "incendie ou explosion", 101: "voie d'eau", 102: "abordage", 103: "échouement",
           104: "gîte, risque de chavirer", 105: "naufrage", 106: "désemparé, à la dérive",
           107: "détresse non précisée", 108: "abandon du navire", 109: "piraterie ou attaque armée",
           110: "homme à la mer", 112: "balise de détresse"}
TELECOMMANDES = {100: "téléphonie", 101: "téléphonie duplex", 103: "interrogation", 104: "impossible d'accepter",
                 105: "fin d'appel", 106: "données", 109: "téléphonie BLU", 110: "accusé de réception de détresse",
                 112: "relais de détresse", 113: "télex FEC", 115: "télex ARQ", 118: "essai",
                 121: "mise à jour de position", 126: None}


def bits_symbole(valeur):
    info = [(valeur >> i) & 1 for i in range(7)]
    zeros = info.count(0)
    return info + [(zeros >> 2) & 1, (zeros >> 1) & 1, zeros & 1]


def symbole(bits):
    """Valeur 0-127 si les bits de controle sont justes, sinon None."""
    valeur = sum(b << i for i, b in enumerate(bits[:7]))
    return valeur if list(bits) == bits_symbole(valeur) else None


def _motif(valeurs):
    return np.array([b for v in valeurs for b in bits_symbole(v)], dtype=np.int8)


# Deux paires DX/RX de mise en phase consecutives, pour chaque position possible.
_MOTIFS = [(k, _motif([DX_PHASE, RX_PHASES[k], DX_PHASE, RX_PHASES[k + 1]])) for k in range(len(RX_PHASES) - 1)]
_POIDS = 1 << np.arange(20, dtype=np.int64)      # convolve retourne le noyau : bit 0 = poids fort
_ENTIERS = [int(sum(int(b) << (19 - i) for i, b in enumerate(m[:20]))) for _, m in _MOTIFS]


def demoduler(pcm, taux):
    """Audio S16 -> score par echantillon a 24 kHz (> 0 : ton Y / bit 1)."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if taux != TAUX_INTERNE and len(x):
        t = np.arange(0, len(x) * TAUX_INTERNE / taux) * taux / TAUX_INTERNE
        x = np.interp(t, np.arange(len(x)), x).astype(np.float32)
    n = np.arange(len(x))
    noyau = np.ones(ECH_BIT, dtype=np.float32) / ECH_BIT
    energie = []
    for ton in (TON_Y, TON_B):
        osc = np.exp(-2j * np.pi * ton * n / TAUX_INTERNE)
        bande = np.convolve(x * osc, noyau, "same")
        energie.append(np.abs(bande))
    return energie[0] - energie[1]


def _bits(score, phase):
    return (score[phase::ECH_BIT] > 0).astype(np.int8)


def _caracteres(bits, debut, k):
    """Caracteres du message a partir d'une mise en phase trouvee : debut =
    indice de bit du DX de la paire k. Chaque caractere : DX, ou sa copie RX
    deux paires plus loin si le DX est abime."""
    def lire(i):
        b = bits[i:i + 10]
        return symbole(b) if len(b) == 10 else None
    paire0 = debut - 20 * k              # paire 0 de la mise en phase
    sortie = []
    for p in range(6, 6 + MESSAGE_MAX):
        dx = lire(paire0 + 20 * p)
        rx = lire(paire0 + 20 * (p + 2) + 10)
        c = dx if dx is not None else rx
        sortie.append(c)
        if c in EOS and len(sortie) >= 3:
            break
        if dx is None and rx is None and len([s for s in sortie if s is None]) > 3:
            return None
    return sortie


def chercher(score):
    """Appels decodes dans ce score : listes de caracteres (None = illisible)."""
    trouves = []
    for phase in range(ECH_BIT):
        for polarite in (1, 0):
            bits = _bits(score, phase)
            if not polarite:
                bits = 1 - bits
            if len(bits) < 200:
                continue
            # Chaque fenetre de 20 bits en un entier : une paire DX/RX de mise
            # en phase se reconnait d'un coup (le Pi ne chauffe pas pour rien).
            mots = np.convolve(bits.astype(np.int64), _POIDS, "valid")
            for k, motif in _MOTIFS:
                (candidats,) = np.nonzero(mots == _ENTIERS[k])
                for debut in candidats:
                    if np.array_equal(bits[debut:debut + len(motif)], motif):
                        chars = _caracteres(bits, int(debut), k)
                        if chars:
                            trouves.append(chars)
            if trouves:
                return trouves
    return trouves


def _mmsi(chars):
    if len(chars) < 5 or any(c is None or c > 99 for c in chars):
        return None
    return "".join(f"{c:02d}" for c in chars)[:9]


def _position(chars):
    if len(chars) < 5 or any(c is None or c > 99 for c in chars):
        return None
    d = "".join(f"{c:02d}" for c in chars)
    if d == "9999999999":
        return None
    q, lat_d, lat_m, lon_d, lon_m = int(d[0]), d[1:3], d[3:5], d[5:8], d[8:10]
    ns = "sud" if q in (2, 3) else "nord"
    eo = "ouest" if q in (1, 3) else "est"
    return f"{int(lat_d)} degrés {int(lat_m)} {ns}, {int(lon_d)} degrés {int(lon_m)} {eo}"


def interpreter(chars):
    """Texte lisible d'un appel, ou None si trop abime."""
    if not chars or chars[0] is None:
        return None
    fmt = chars[0]
    if fmt not in FORMATS:
        return None
    reste = chars[2:] if len(chars) > 1 and chars[1] == fmt else chars[1:]   # specificateur double ou simple
    morceaux = []
    if fmt == 121:
        if len(reste) < 5 or any(c is None or c > 99 for c in reste[:5]):
            return None
        code = "".join(f"{c:02d}" for c in reste[:5])
        pays = PAYS.get(code[1:4]) if code.startswith("9") else None
        morceaux.append(f"identification ATIS du bateau {code}" + (f", {pays}" if pays else ""))
    elif fmt == 112:
        moi = _mmsi(reste[0:5])
        nature = NATURES.get(reste[5]) if len(reste) > 5 else None
        pos = _position(reste[6:11])
        morceaux.append(f"ALERTE DE DÉTRESSE du navire {moi or 'inconnu'}")
        if nature:
            morceaux.append(f"nature : {nature}")
        if pos:
            morceaux.append(f"position {pos}")
    else:
        if fmt == 116:
            cat, moi, tc = (reste[0] if reste else None), _mmsi(reste[1:6]), reste[6:8]
            dest = "tous les navires"
        else:
            dest_mmsi = _mmsi(reste[0:5])
            dest = (f"zone" if fmt == 102 else f"station {dest_mmsi}") if dest_mmsi or fmt == 102 else "inconnu"
            cat, moi, tc = (reste[5] if len(reste) > 5 else None), _mmsi(reste[6:11]), reste[11:13]
        morceaux.append(f"appel {FORMATS[fmt]}, catégorie {CATEGORIES.get(cat, 'inconnue')}")
        morceaux.append(f"de {moi or 'inconnu'} vers {dest}")
        tcs = [TELECOMMANDES.get(t) for t in tc if t in TELECOMMANDES and TELECOMMANDES.get(t)]
        if tcs:
            morceaux.append(", ".join(tcs))
    if not any(c in EOS for c in chars):
        morceaux.append("fin de message illisible")
    return ", ".join(morceaux)


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
        for chars in chercher(demoduler(tampon, taux)):
            texte = interpreter(chars)
            if texte and texte not in deja:
                deja = (deja + [texte])[-20:]   # un appel est vu dans plusieurs fenetres
                print(f"ASN: {texte}", flush=True)


if __name__ == "__main__":
    main()
