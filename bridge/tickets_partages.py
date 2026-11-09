"""Tickets partages entre le Pi et les machines compagnes (2026-11-04).

Un ticket, c'est un son pose sur le disque du NAS avec la liste des EPREUVES a
lui faire passer. Toutes les machines, le Pi compris, y travaillent ensemble, et
chacune rend son retour SUR LE TICKET :

  tickets/<id>/ticket.json         le ticket, signe par le Pi (empreintes des sons)
  tickets/<id>/son.wav             le discriminateur 48 kHz (ou usb.wav, lsb.wav : BLU du banc)
  tickets/<id>/avis-<machine>.json le retour d'UNE machine, signe par elle :
                                   ses prises en cours et ses verdicts
  tickets/<id>/rendu-<machine>-<epreuve>.<ext>  voix ou image decodee
  tickets/<id>/clos.json           conclusion du Pi : plus personne n'y touche

Un fichier de retour par machine plutot qu'un seul fichier commun : sur un
partage SMB, deux machines qui reecrivent le meme fichier perdent l'ecriture
de l'autre (pas de verrou fiable). Le ticket, c'est le dossier entier ; chacun
lit les retours de tous.

Double passe evitee : avant une epreuve, la machine la PREND (prise datee,
limitee a PRISE_SEC) dans son retour, attend PRISE_ATTENTE_SEC, relit les
autres ; si une autre l'a prise avant elle, elle la lui laisse. Une prise
abandonnee (machine eteinte) expire et l'epreuve redevient libre. Une machine
ne prend que ce qu'elle sait faire (savoir) : FT8 reste au Pi si le Mac n'a pas
jt9.

Controle des machines : une epreuve qui PROPOSE quelque chose est repassee par une
AUTRE machine (contre-epreuve). Les decodeurs sont deterministes : cela prouve que la
machine est saine, PAS que le signal est vrai, c'est l'humain qui le dit au journal.
Un desaccord n'annule donc rien : le rendu reste, avec son doute, et le Pi note la
machine. Les negatifs ne sont jamais repasses (c'est l'essentiel du travail). Les renvois de
l'utilisateur (touche R, Controle+R) n'ont pas de contre-epreuve : c'est lui
qui ecoute et qui tranche ("contre": false dans les infos du ticket).

Le choix entre plusieurs propositions n'est PAS fait ici : juges_watch garde sa
regle (rendus multiples au journal, confiance par bande, empreintes de rejet).

Python 3.9 et plus ; numpy seulement pour les epreuves de donnees."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave

import juges_radio
import pont_protocole as proto

VERSION = 2
DOSSIER = "tickets"
PRISE_SEC = juges_radio.TIMEOUT_SEC + 100   # une epreuve dure au plus TIMEOUT_SEC
PRISE_ATTENTE_SEC = 2.0
PROPRES_TOLERANCE = 0.1
TAUX_DONNEES = 22050                        # multimon-ng, FFSK pro et ASN lisent du 22,05 kHz
TAUX_DIREWOLF = 48000                       # lib/prise_unique.TAUX
MODES_FREEDV = ("700D", "700E", "1600")     # lib/juges_sync.FREEDV

# Toutes les epreuves qui tournent sur un SON (les veilles qui tiennent le dongle
# en IQ -- ACARS, VDL2, ADS-B, rtl_433, TETRA, DAB... -- n'en laissent aucun).
DONNEES_SON = ("multimon", "ffsk", "asn", "direwolf-AIS", "direwolf-EAS", "sstv")
_TRAME_DIREWOLF = re.compile(r"(!AIVD[MO],|^AIS \d|ZCZC-|NNNN|^EAS)")   # lib/donnees_prise._TRAME
_TRAME_MULTIMON = re.compile(r"[A-Z][A-Z0-9_]{1,14}: ")
_JT9_SEUL = threading.Lock()                # jt9 ecrit ses fichiers de travail dans le dossier courant


def epreuve_dsd(juge):
    return "dsd" + juge          # "-fs" -> "dsd-fs"


def juge_de(epreuve):
    return epreuve[3:] if epreuve.startswith("dsd-") else None


def epreuves_blu(modulations):
    """FreeDV et FT8/FT4 sur la BLU demodulee par le banc (lib/juges_sync)."""
    return ["%s@%s" % (e, m) for m in modulations
            for e in ["freedv-" + f for f in MODES_FREEDV] + ["ft8", "ft4"]]


def _decoupe(epreuve):
    """"ft8@usb" -> ("ft8", "usb.wav") ; le son brut s'appelle son.wav."""
    nom, _, source = epreuve.partition("@")
    return nom, (source or "son") + ".wav"


def savoir(epreuve, outils):
    """Cette machine a-t-elle de quoi passer cette epreuve ?"""
    nom = _decoupe(epreuve)[0]
    if juge_de(nom):
        cle = "dsd"
    elif nom.startswith("freedv-"):
        cle = "freedv"
    elif nom in ("ft8", "ft4"):
        cle = "jt9"
    elif nom.startswith("direwolf-"):
        return bool(outils.get("direwolf")) and os.path.exists(outils.get("direwolf_conf") or "")
    elif nom == "sstv":
        return bool(outils.get("python_sstv")) and os.path.exists(outils.get("sstv_script") or "")
    elif nom == "multimon":
        cle = "multimon"
    else:
        return nom in ("ffsk", "asn")
    return bool(outils.get(cle))


def impossibles(epreuves, outils):
    return [e for e in epreuves if not savoir(e, outils)]


# --- fichiers ----------------------------------------------------------------------------

def _lire(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _ecrire(chemin, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(chemin), prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, chemin)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _dossier(racine, tid):
    return os.path.join(racine, DOSSIER, tid)


def ouverts(racine):
    """Identifiants des tickets pas encore clos, plus vieux d'abord."""
    try:
        noms = sorted(os.listdir(os.path.join(racine, DOSSIER)))
    except OSError:
        return []
    return [n for n in noms if not n.startswith(".")
            and not os.path.exists(os.path.join(racine, DOSSIER, n, "clos.json"))]


def existe(racine, tid):
    return os.path.exists(os.path.join(_dossier(racine, tid), "ticket.json"))


def ouvrir(racine, cle, pi, sons, infos, epreuves):
    """Le Pi pose un ticket. sons : {nom publie: chemin local}. ticket.json est
    ecrit en dernier : sans lui, le ticket n'existe pas encore pour les autres."""
    tid = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    d = _dossier(racine, tid)
    os.makedirs(d)
    empreintes = {}
    for nom, chemin in sons.items():
        shutil.copyfile(chemin, os.path.join(d, nom))
        empreintes[nom] = proto.file_digest(os.path.join(d, nom))
    _ecrire(os.path.join(d, "ticket.json"), proto.sign(cle, {
        "type": "ticket", "version": VERSION, "id": tid, "pi": pi, "cree": time.time(),
        "sons": empreintes, "epreuves": list(epreuves), "infos": infos}))
    return tid


def lire(racine, tid, cles, strict=True):
    """(ticket, {machine: avis}). cles : {machine: cle publique}. strict : un
    retour qu'on ne peut pas verifier est ignore (le Pi) ; sinon il sert
    seulement a ne pas refaire ce qu'un autre a pris (machine compagne qui ne
    connait pas les cles des autres compagnes)."""
    d = _dossier(racine, tid)
    ticket = _lire(os.path.join(d, "ticket.json"))
    if not ticket or ticket.get("id") != tid or ticket.get("pi") not in cles \
            or not proto.verify(cles[ticket["pi"]], ticket, max_age=None):
        return None, {}
    avis = {}
    try:
        noms = os.listdir(d)
    except OSError:
        return None, {}
    for nom in noms:
        if not (nom.startswith("avis-") and nom.endswith(".json")):
            continue
        a = _lire(os.path.join(d, nom))
        m = (a or {}).get("machine")
        if not a or a.get("ticket") != tid or nom != "avis-%s.json" % m:
            continue
        verifie = m in cles and proto.verify(cles[m], a, max_age=None)
        if strict and not verifie:
            continue
        a["verifie"] = verifie
        avis[m] = a
    return ticket, avis


def clos(racine, tid):
    return _lire(os.path.join(_dossier(racine, tid), "clos.json"))


def clore(racine, tid, cle, pi, conclusion):
    _ecrire(os.path.join(_dossier(racine, tid), "clos.json"),
            proto.sign(cle, dict(conclusion, type="clos", ticket=tid, pi=pi)))


def nettoyer(racine, garde_sec, abandon_sec=24 * 3600):
    """Retire les tickets clos depuis garde_sec (le temps que tous l'aient vu),
    et ceux abandonnes depuis bien plus longtemps."""
    try:
        noms = os.listdir(os.path.join(racine, DOSSIER))
    except OSError:
        return
    for nom in noms:
        d = os.path.join(racine, DOSSIER, nom)
        c = _lire(os.path.join(d, "clos.json"))
        t = (c or {}).get("t") or (_lire(os.path.join(d, "ticket.json")) or {}).get("cree")
        if t is None:
            try:
                t = os.path.getmtime(d)
            except OSError:
                continue
        if time.time() - t > (garde_sec if c else abandon_sec):
            shutil.rmtree(d, ignore_errors=True)


# --- qui fait quoi -----------------------------------------------------------------------

def _prise_active(a, ep, maintenant):
    p = (a.get("prises") or {}).get(ep)
    return bool(p and p.get("jusqu_a", 0) > maintenant and ep not in (a.get("verdicts") or {}))


def _verdicts(avis, ep):
    return {m: a["verdicts"][ep] for m, a in avis.items() if ep in (a.get("verdicts") or {})}


def a_prendre(ticket, avis, machine, n, outils, pour_moi=None):
    """Ce que machine peut prendre, au plus n : d'abord les contre-epreuves
    (une proposition d'une AUTRE machine, pas encore repassee), puis les
    epreuves libres (personne ne l'a rendue ni prise), dans l'ordre du ticket.
    pour_moi(epreuve) : le Pi laisse aux compagnes ce qu'elles savent faire."""
    maintenant = time.time()
    contre = (ticket.get("infos") or {}).get("contre", True)
    a_repasser, libres = [], []
    for ep in ticket.get("epreuves", []):
        if not savoir(ep, outils):
            continue
        v = _verdicts(avis, ep)
        if machine in v or any(_prise_active(a, ep, maintenant) for m, a in avis.items() if m != machine):
            continue
        if not v:
            if pour_moi is None or pour_moi(ep):
                libres.append(ep)
        elif contre and len(v) == 1 and next(iter(v.values())).get("propose"):
            a_repasser.append(ep)
    return (a_repasser + libres)[:n]


def _avis_propre(racine, tid, machine):
    a = _lire(os.path.join(_dossier(racine, tid), "avis-%s.json" % machine)) or {}
    return {"prises": a.get("prises") or {}, "verdicts": a.get("verdicts") or {}, "rendus": a.get("rendus") or {}}


def _publier(racine, tid, machine, nom, cle, a):
    _ecrire(os.path.join(_dossier(racine, tid), "avis-%s.json" % machine), proto.sign(cle, {
        "type": "avis", "version": VERSION, "ticket": tid, "machine": machine, "nom": nom,
        "prises": a["prises"], "verdicts": a["verdicts"], "rendus": a["rendus"]}))


def prendre(racine, tid, machine, nom, cle, cles, epreuves):
    """Prend ces epreuves ; rend celles qui sont bien a nous apres verification
    (une autre machine qui les a prises plus tot les garde)."""
    a = _avis_propre(racine, tid, machine)
    t = time.time()
    for ep in epreuves:
        a["prises"][ep] = {"t": t, "jusqu_a": t + PRISE_SEC}
    _publier(racine, tid, machine, nom, cle, a)
    time.sleep(PRISE_ATTENTE_SEC)
    _, avis = lire(racine, tid, cles, strict=False)
    gardees = []
    for ep in epreuves:
        rivales = [(o["prises"][ep]["t"], m) for m, o in avis.items()
                   if m != machine and _prise_active(o, ep, time.time())]
        if any(r < (t, machine) for r in rivales):
            del a["prises"][ep]
        else:
            gardees.append(ep)
    if len(gardees) < len(epreuves):
        _publier(racine, tid, machine, nom, cle, a)
    return gardees


def rendre(racine, tid, machine, nom, cle, resultats):
    """resultats : {epreuve: (verdict, chemin du rendu ou None)}."""
    d = _dossier(racine, tid)
    a = _avis_propre(racine, tid, machine)
    for ep, (verdict, rendu) in resultats.items():
        a["prises"].pop(ep, None)
        a["verdicts"][ep] = verdict
        if rendu:
            nom_rendu = "rendu-%s-%s%s" % (machine, ep.replace("@", "_"), os.path.splitext(rendu)[1])
            shutil.copyfile(rendu, os.path.join(d, nom_rendu))
            a["rendus"][ep] = {"fichier": nom_rendu, "empreinte": proto.file_digest(os.path.join(d, nom_rendu))}
    _publier(racine, tid, machine, nom, cle, a)


# --- les epreuves ------------------------------------------------------------------------

def _pcm_wav(wav):
    with wave.open(wav) as w:
        return w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())


def _audio_donnees(wav):
    """Le son ramene a TAUX_DONNEES (passe-bas avant decimation) : PCM s16."""
    import numpy as np
    taux, ch, brut = _pcm_wav(wav)
    x = np.frombuffer(brut, dtype="<i2")[::ch].astype(np.float32)
    if taux != TAUX_DONNEES:
        k = np.arange(127) - 63
        h = np.sinc(2 * 0.45 * TAUX_DONNEES / taux * k) * np.hamming(127)
        x = np.convolve(x, h / h.sum(), "same")
        t = np.arange(int(len(x) * TAUX_DONNEES / taux)) * (taux / TAUX_DONNEES)
        x = np.interp(t, np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype("<i2").tobytes()


def _multimon(wav, outils, priorite):
    import decodeurs
    r = subprocess.run(list(priorite) + [outils["multimon"]] + decodeurs.MULTIMON_ARGS,
                       input=_audio_donnees(wav), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=juges_radio.TIMEOUT_SEC)
    lignes = [l.strip() for l in r.stdout.decode(errors="replace").splitlines()]
    lignes = [l for l in lignes if l and not l.startswith(("multimon-ng", "Enabled demodulators", "(C)"))]
    # Une trame porte le nom de son demodulateur ("POCSAG1200: ..."). Le reste, ce sont
    # des octets du FSK dans le bruit ("xrMN") : gardes a l'archive, pas lus a voix haute.
    bons = [l for l in lignes if _TRAME_MULTIMON.match(l) and decodeurs.meaningful(l)]
    bons, _ = decodeurs.fms_tenus(bons)
    return bons, [l for l in lignes if l not in bons]


def _direwolf(mode, wav, outils, priorite):
    taux, ch, brut = _pcm_wav(wav)
    if taux != TAUX_DIREWOLF or ch != 1:
        return []
    r = subprocess.run(list(priorite) + [outils["direwolf"], "-c", outils["direwolf_conf"], "-B", mode,
                                         "-r", str(taux), "-n", "1", "-b", "16", "-t", "0", "-a", "0",
                                         "-q", "hd", "-"],
                       input=brut, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=juges_radio.TIMEOUT_SEC)
    lignes = (l.strip() for l in r.stdout.decode(errors="replace").splitlines())
    return ["%s: %s" % (mode, l) for l in lignes if l and _TRAME_DIREWOLF.search(l)]


def _sstv(wav, outils, tmp, priorite):
    """(messages, image) : le guetteur du scanner, ses memes modes et son code."""
    taux, ch, brut = _pcm_wav(wav)
    if ch != 1:
        brut = b"".join(brut[i:i + 2] for i in range(0, len(brut), 2 * ch))
    dossier = tempfile.mkdtemp(prefix="sstv_", dir=tmp)
    r = subprocess.run(list(priorite) + [outils["python_sstv"], outils["sstv_script"], str(taux), dossier],
                       input=brut, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=juges_radio.TIMEOUT_SEC)
    messages, image = [], None
    for ligne in r.stdout.decode(errors="replace").splitlines():
        morceaux = ligne.strip().split(" ", 4)
        if len(morceaux) == 5 and morceaux[:2] == ["SSTV:", "image"]:
            messages.append("SSTV: %s %s" % (morceaux[3], morceaux[4]))
            image = morceaux[2]
    return messages, image


def _freedv(mode, wav, outils):
    import juges_sync
    pcm = juges_sync._mono_8_ou_12(wav, 8000)
    if not pcm or len(pcm) < 8000:
        return None
    return juges_sync.freedv_mesure(outils["freedv"], mode, pcm)


def _ft(nom, wav, outils):
    import juges_sync
    pcm = juges_sync._mono_8_ou_12(wav, 12000)
    if not pcm or len(pcm) < 12000 * 6:
        return []
    with _JT9_SEUL:
        textes = juges_sync._jt9(outils["jt9"], "-8" if nom == "ft8" else "--ft4", pcm, 12000)
    return ["%s: %s" % (nom.upper(), t) for t in textes]


def passer(ep, wav, outils, tmp, priorite=("nice", "-n", "19"), garder=False):
    """Fait passer une epreuve dans le dossier temporaire tmp ; rend (verdict,
    rendu ou None). Le verdict porte toujours "propose" (quelque chose a confirmer)."""
    nom = _decoupe(ep)[0]
    juge = juge_de(nom)
    if juge:
        return juges_radio.juger(juge, wav, dsd=outils["dsd"], priorite=priorite, garder=garder,
                                 voix=os.path.join(tmp, "voix_%s.wav" % juge.lstrip("-")))
    rendu = None
    douteux = []
    if nom.startswith("freedv-"):
        mode = nom[len("freedv-"):]
        m = _freedv(mode, wav, outils)
        rendu = m["wav"] if m else None
        verdict = {"propose": bool(rendu), "protocole": "FreeDV " + mode if rendu else None,
                   "voix_s": round(juges_radio.duree_wav(rendu), 1) if rendu else 0.0, "t": time.time()}
        if m:
            verdict.update(synchro=m["synchro"], trames=m["trames"], synchro_part=round(m["synchro"] / max(1, m["trames"]), 2),
                           suite_s=m.get("suite_s"))
        return verdict, rendu
    if nom in ("ft8", "ft4"):
        messages = _ft(nom, wav, outils)
    elif nom == "sstv":
        messages, rendu = _sstv(wav, outils, tmp, priorite)
    elif nom.startswith("direwolf-"):
        messages = _direwolf(nom[len("direwolf-"):], wav, outils, priorite)
    elif nom == "multimon":
        messages, douteux = _multimon(wav, outils, priorite)
    elif nom == "ffsk":
        import ffsk_pro
        messages = ["%s: %s" % (p, t) for p, t in ffsk_pro.decoder(_audio_donnees(wav), TAUX_DONNEES)]
    elif nom == "asn":
        import asn_dsc
        pcm = _audio_donnees(wav)
        messages = ["ASN: %s" % t for t in (asn_dsc.interpreter(c) for c in
                                             asn_dsc.chercher(asn_dsc.demoduler(pcm, TAUX_DONNEES))) if t]
    else:
        return {"propose": False, "inconnue": True, "t": time.time()}, None
    messages = sorted(set(messages))[:20]
    return {"propose": bool(messages), "messages": messages, "douteux": sorted(set(douteux))[:20],
            "t": time.time()}, rendu


def _passer_sur(ep, sons, ticket, outils, tmp, priorite):
    infos = ticket.get("infos") or {}
    try:
        return passer(ep, sons[_decoupe(ep)[1]], outils, tmp, priorite, garder=bool(infos.get("garder")))
    except Exception as exc:     # une epreuve qui casse ne bloque pas le ticket
        return {"propose": False, "erreur": repr(exc)[:200], "t": time.time()}, None


def travailler(racine, tid, machine, nom, cle, cles, outils, n, priorite=("nice", "-n", "19"), pour_moi=None):
    """Un tour de travail sur un ticket : prend jusqu'a n epreuves, les passe en
    parallele, rend les verdicts. Rend le nombre d'epreuves passees."""
    from concurrent.futures import ThreadPoolExecutor
    ticket, avis = lire(racine, tid, cles, strict=False)
    if not ticket or clos(racine, tid):
        return 0
    voulues = a_prendre(ticket, avis, machine, n, outils, pour_moi)
    if not voulues:
        return 0
    d = _dossier(racine, tid)
    eps = prendre(racine, tid, machine, nom, cle, cles, voulues)
    if not eps:
        return 0
    with tempfile.TemporaryDirectory(prefix="ticket_") as tmp:
        sons = {}
        for fichier in {_decoupe(ep)[1] for ep in eps}:   # lu une fois sur le NAS, pas par chaque decodeur
            sons[fichier] = os.path.join(tmp, fichier)
            shutil.copyfile(os.path.join(d, fichier), sons[fichier])
            if proto.file_digest(sons[fichier]) != (ticket.get("sons") or {}).get(fichier):
                return 0
        with ThreadPoolExecutor(max_workers=max(1, len(eps))) as pool:
            sortis = list(pool.map(lambda ep: _passer_sur(ep, sons, ticket, outils, tmp, priorite), eps))
        rendre(racine, tid, machine, nom, cle, dict(zip(eps, sortis)))
        for ep, (_, rendu) in zip(eps, sortis):
            if rendu and ep.startswith("freedv-"):     # juges_sync le laisse dans son dossier
                shutil.rmtree(os.path.dirname(rendu), ignore_errors=True)
    return len(eps)


# --- conclusion (le Pi) ------------------------------------------------------------------

def accord(a, b):
    """Deux verdicts de la meme epreuve disent-ils la meme chose ?"""
    if bool(a.get("propose")) != bool(b.get("propose")):
        return False
    if "messages" in a or "messages" in b:
        return sorted(a.get("messages") or []) == sorted(b.get("messages") or [])
    pa, pb = a.get("propres") or 0, b.get("propres") or 0
    return a.get("protocole") == b.get("protocole") and abs(pa - pb) <= max(1, PROPRES_TOLERANCE * max(pa, pb))


def conclure(ticket, avis, contre_possible=None, forcer=False):
    """Ou en est le ticket. contre_possible(epreuve, auteur) : une autre machine
    peut-elle encore la repasser ? forcer : on conclut avec ce qu'on a (ticket
    trop vieux). Rend {"fini", "verdicts", "etablies", "seules", "desaccords",
    "manque"} :
      verdicts   : {epreuve: verdict} a appliquer ; un desaccord y porte doute=True
      etablies   : propositions rendues identiques par deux machines
      seules     : propositions qu'aucune autre machine n'a pu repasser
      desaccords : {epreuve: {machine: verdict}} la contre-epreuve dit autre chose (aussi dans seules)
      manque     : epreuves encore attendues."""
    exige = (ticket.get("infos") or {}).get("contre", True)
    verdicts, etablies, seules, desaccords, manque = {}, [], [], {}, []
    for ep in ticket.get("epreuves", []):
        v = _verdicts(avis, ep)
        if not v:
            manque.append(ep)
            continue
        liste = list(v.values())
        verdicts[ep] = liste[0]
        if not any(x.get("propose") for x in liste):
            continue
        if len(liste) == 1:
            auteur = next(iter(v))
            if exige and not forcer and (contre_possible is None or contre_possible(ep, auteur)):
                manque.append(ep)
                del verdicts[ep]
            else:
                seules.append(ep)
        elif all(accord(liste[0], x) for x in liste[1:]):
            etablies.append(ep)
        else:
            # Une machine a trouve, l'autre autrement : on le presente avec ce doute,
            # c'est l'humain qui dit (et le Pi note la machine).
            desaccords[ep] = v
            seules.append(ep)
            verdicts[ep] = dict(next(x for x in liste if x.get("propose")), doute=True)
    return {"fini": forcer or not manque, "verdicts": verdicts, "etablies": etablies, "seules": seules,
            "desaccords": desaccords, "manque": manque}


def freedv_retenues(epreuves):
    """Dans l'ordre de MODES_FREEDV. Toutes restent (2026-11-05) : chacune a prouve sa
    synchro (juges_sync.freedv_mesure), l'humain departage derriere J."""
    return sorted(epreuves, key=lambda e: tuple(MODES_FREEDV).index(e.split("@")[0][7:])
                  if e.startswith("freedv-") else -1)


def poser_marque(racine, tid, nom):
    with open(os.path.join(_dossier(racine, tid), nom), "w"):
        pass


def a_marque(racine, tid, nom):
    return os.path.exists(os.path.join(_dossier(racine, tid), nom))


def rendu_de(racine, tid, avis, ep):
    """Chemin du rendu (voix, image) de l'epreuve ep, empreinte verifiee, ou None."""
    for a in avis.values():
        v = (a.get("rendus") or {}).get(ep)
        if v and a.get("verifie", True):
            chemin = os.path.join(_dossier(racine, tid), v["fichier"])
            if os.path.exists(chemin) and proto.file_digest(chemin) == v.get("empreinte"):
                return chemin
    return None
