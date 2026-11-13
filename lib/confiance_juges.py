"""Confiance dans la sortie d'un decodeur (2026-11-13, demande utilisateur : "sur
les frequences ou on voit beaucoup de DMR on ouvre quand meme sur d'autres
decodeurs alors qu'ils devraient avoir perdu en confiance").

Constat sur le Pi (7 jours) : sur les relais DMR 438,524 et 467,238 MHz, des prises
s'ouvraient en FreeDV 700E (10 refus de l'utilisateur, aucun oui), en BLU ou en AM.
La memoire des juges (lib/propositions.rang) ne servait qu'a ORDONNER les rendus,
jamais a en refuser un : quand le DMR n'avait pas de voix dans la prise, le FreeDV
restait seul et s'ouvrait. Et le DMR prouve sans voix (synchros propres, donnees,
relais au repos) n'etait regarde qu'APRES la presentation des voix numeriques.

Ce que dit la litterature, et ce qu'on en garde :
  - classification de modulation par vraisemblance (Dobre et al., IET Commun. 2007) :
    on decide par rapport de vraisemblance, prior compris, plutot que par seuils
    independants par decodeur ;
  - test sequentiel de Wald (SPRT) : chaque indice (synchro tenue, trame a CRC
    juste, avis de l'utilisateur) multiplie un rapport de cotes ; on ouvre quand la
    cote depasse un seuil ;
  - fausses alarmes de synchro (mot de synchro court, tolerance d'erreurs, et ici
    une dizaine de decodeurs essayes en parallele sur le meme son : comparaisons
    multiples) : une "synchro" isolee ne prouve presque rien ; un signal deja
    prouve d'un autre protocole dans la meme prise EXPLIQUE les autres sorties
    (le DMR lu par un demodulateur BLU donne des synchros FreeDV : constat du
    2026-11-05 dans lib/juges_sync.py).

Cote d'un rendu = cote de sa famille sur la frequence (historique : Beta "cette
famille contre les autres", alimente par les oui de l'utilisateur, les indices de
relais et les decodages deja vus) x cote du juge (lib/propositions.rang : oui/non
de l'utilisateur, voisines) x force de la preuve de cette prise x explication par
un autre protocole prouve dans la prise. Sous SEUIL_OUVERTURE, le rendu n'ouvre
pas : il reste derriere J ou aux rejets, ou l'utilisateur peut encore le valider
(et la confiance remonte).

Une frequence neuve n'est bridee par rien : famille 0,5, juge 0,5, la preuve seule
decide, comme avant."""
import time

SEUIL_OUVERTURE = 0.35
FORCE_FAMILLE = 2.0          # Beta(1, 1) : une famille contre les autres, sans souvenir
VU_POIDS = 0.5               # un decodage vu (pas un avis) compte pour un demi-oui
VU_MAX = 40
EXPLIQUE = 0.15              # autre protocole prouve dans la meme prise
PLANCHER = 1e-3

ANALOGIQUES = {"am", "nfm", "wfm", "usb", "lsb", "analogique"}


def famille(juge, protocole=None):
    """Famille de signal : DMR, NXDN, FreeDV, analogique... ; None si inconnue (auto)."""
    j = (juge or "").lower()
    if j in ANALOGIQUES:
        return "analogique"
    if j.startswith("freedv"):
        return "FreeDV"
    if j in ("donnees", "multimon"):
        return None                       # des donnees cohabitent avec la voix
    if j == "-fa":
        return _famille_protocole(protocole)
    import propositions
    nom = propositions.NOMS.get(juge)
    if juge in ("-fi", "-fn"):
        return "NXDN"
    if juge in ("-f1", "-f2"):
        return "P25"
    if j == "tetra":
        return "TETRA"
    return nom or juge


def _famille_protocole(protocole):
    if not protocole:
        return None
    import propositions
    juge = propositions.juge_du_protocole(protocole)
    return famille(juge) if juge and juge != "-fa" else str(protocole).split()[0]


def juge_equivalent(juge, protocole=None):
    """Auto-detection : le juge du protocole decode (-fa qui rend du DMR = -fs)."""
    if juge == "-fa" and protocole:
        import propositions
        return propositions.juge_du_protocole(protocole) or juge
    return juge


def _poids_juge(st, maintenant):
    import propositions
    oui = propositions._poids(st.get("faits"), True) if st.get("faits") else float(st.get("oui") or 0)
    return oui + propositions._a_priori(st, maintenant) + VU_POIDS * min(int(st.get("vu") or 0), VU_MAX)


def historique(freq_hz):
    """{famille: poids} des signaux reconnus sur cette frequence."""
    import propositions
    cle = propositions._cle(freq_hz)
    if not cle:
        return {}
    with propositions._verrou:
        juges = dict(propositions._lire_scores()["frequences"].get(cle) or {})
    maintenant = time.time()
    out = {}
    for juge, st in juges.items():
        fam = famille(juge)
        if not fam or not isinstance(st, dict):
            continue
        out[fam] = out.get(fam, 0.0) + _poids_juge(st, maintenant)
    return out


def part_famille(freq_hz, fam, hist=None):
    hist = historique(freq_hz) if hist is None else hist
    n = hist.get(fam, 0.0)
    total = sum(hist.values())
    return (n + FORCE_FAMILLE / 2) / (total + FORCE_FAMILLE)


def force_preuve(rendu):
    """Rapport de vraisemblance de la preuve propre a cette prise."""
    rendu = rendu or {}
    juge = (rendu.get("mode") or rendu.get("juge") or "").lower()
    if juge.startswith("freedv"):
        return 3.0 if rendu.get("confirme") else 1.5
    if juge in ANALOGIQUES:
        part = float((rendu.get("detail") or {}).get("part") or 0)
        return 1.5 + 2.5 * max(0.0, min(part, 1.0))
    return 4.0 if rendu.get("confirme") else 2.0


def _cote(p):
    p = min(max(p, PLANCHER), 1 - PLANCHER)
    return p / (1 - p)


def confiance(freq_hz, juge, rendu=None, prouve=None, hist=None):
    """(probabilite, detail) que ce rendu soit le bon decodeur pour cette prise.
    prouve : protocole prouve dans la prise (synchros propres), ou None."""
    import propositions
    protocole = (rendu or {}).get("protocole")
    fam = famille(juge, protocole)
    hist = historique(freq_hz) if hist is None else hist
    p_fam = part_famille(freq_hz, fam, hist) if fam else 0.5
    r = propositions.rang(freq_hz, juge_equivalent(juge, protocole))
    lr = force_preuve(rendu) if rendu is not None else 1.0
    fam_prouve = _famille_protocole(prouve) if prouve else None
    explique = bool(fam_prouve and fam and fam != fam_prouve)
    cote = _cote(p_fam) * _cote(r) * lr * (EXPLIQUE if explique else 1.0)
    p = cote / (1 + cote)
    total = sum(hist.values())
    dominante = max(hist, key=hist.get) if hist and total > 0 else None
    detail = {"confiance": round(p, 3), "famille": fam, "part_famille": round(p_fam, 3), "juge": round(r, 3),
              "preuve": round(lr, 2), "explique_par": fam_prouve if explique else None,
              "dominante": dominante, "part_dominante": round(hist[dominante] / total, 3) if dominante else None}
    return p, detail


def raison(detail):
    """Ligne de journal lisible."""
    morceaux = [f"confiance {detail['confiance']:.2f}"]
    if detail.get("dominante") and detail["dominante"] != detail.get("famille"):
        morceaux.append(f"frequence {detail['dominante']} a {round(100 * (detail.get('part_dominante') or 0))} %")
    morceaux.append(f"famille {detail.get('famille')} {detail['part_famille']:.2f}")
    morceaux.append(f"tes avis sur ce juge {detail['juge']:.2f}")
    morceaux.append(f"preuve x{detail['preuve']}")
    if detail.get("explique_par"):
        morceaux.append(f"{detail['explique_par']} prouve dans la prise")
    return ", ".join(morceaux)


def trier(freq_hz, rendus, prouve=None, cle_juge="mode"):
    """(ouvrables, ecartes) : rendus classes par confiance ; chaque rendu recoit
    "confiance" (detail). Les ecartes restent ecoutables (J, rejets)."""
    hist = historique(freq_hz)
    notes = []
    for r in rendus:
        p, detail = confiance(freq_hz, r.get(cle_juge), r, prouve, hist)
        r["confiance"] = detail
        notes.append((p, r))
    notes.sort(key=lambda x: -x[0])
    ouvrables = [r for p, r in notes if not _ecarte(p, r["confiance"])]
    ecartes = [r for p, r in notes if _ecarte(p, r["confiance"])]
    for r in ecartes:
        print(f"[confiance] {float(freq_hz) / 1e6:.4f} MHz, {r.get(cle_juge)} n'ouvre pas : "
              f"{raison(r['confiance'])}", flush=True)
    return ouvrables, ecartes


def a_priori(freq_hz, juge, hist=None):
    """Confiance avant toute preuve (ordre et parole des decodeurs en direct)."""
    return confiance(freq_hz, juge, None, None, hist)[0]


def _ecarte(p, detail):
    """Agnostique (2026-11-13, doctrine) : la famille dominante d'une frequence ne
    fait que baisser la cote ; elle n'ecarte jamais seule un decodeur. Il faut une
    preuve contre CE juge : tes refus (ici ou sur les voisines), ou un autre
    protocole prouve dans la meme prise qui explique sa sortie. Sans ca, un NXDN
    ou un dPMR atypique sur un relais DMR connu garde la parole."""
    if p >= SEUIL_OUVERTURE:
        return False
    return detail.get("juge", 0.5) < 0.5 or bool(detail.get("explique_par"))


def bloque(freq_hz, juge, hist=None):
    """Ecoute en direct : ce juge ecoute sans prendre la parole (meme regle que trier)."""
    p, detail = confiance(freq_hz, juge, None, None, hist)
    return _ecarte(p, detail)

