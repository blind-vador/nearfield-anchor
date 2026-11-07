"""Plusieurs rendus pour une même prise (2026-11-02).

Quand plusieurs juges ont trouvé du clair, on garde chaque rendu à côté de
l'enregistrement du journal. Flèche gauche rejette le rendu en cours et lance le
suivant. Deux choses distinctes en découlent (2026-11-05) : l'avis sur le juge,
qui descend ici, et son empreinte sonore, retenue pour ce juge sur cette seule
fréquence : ce qu'il sortira de ressemblant ne rouvrira plus, sans rien dire des
autres juges. J passe au rendu suivant sans le rejeter. V ou C sur le rendu entendu retient ce juge. Trois
confirmations recentes, sans rival serieux, suffisent : il passe devant, et s'il
confirme on presente lui seul, sauf si la modulation mesuree sur l'IQ dit autre
chose (un nouveau locuteur, une autre modulation). Tous les decodeurs tournent
toujours. En dessous, ou si il ne confirme pas cette prise, toutes les
demodulations restent au journal. Une confirmation vieille
d'une semaine ne pese plus que la moitie, pour qu'un contexte change
ne reste pas colle a d'anciennes prises.

Aucune fréquence n'est devinée. Le souvenir est celui de l'antenne, ici,
comme les empreintes de parasites déjà en place.
"""
import hashlib
import math
import json
import os
import shutil
import subprocess
import threading
import time
import wave

import chemins
import noise_signatures as ns
from phrases import dire, nombre

_verrou = threading.Lock()
SCORES = chemins.base("juges_freq.json")
# Même pas que les parasites : une fréquence, pas une bande entière.
PAS_HZ = 12_500
# Un seul oui, sans non : (1+1)/(1+0+2). En dessous, le juge n'a pas
# encore été retenu : la preuve du signal départage, pas le souvenir.
SEUIL_RETENU = 0.66
# Trois confirmations fraiches. A une semaine, chacune ne pese plus que moitie.
DEMI_VIE_S = 7 * 24 * 3600
TAU_S = DEMI_VIE_S / math.log(2)
POIDS_SUR = 2.4
MARGE_SUR = 2.0
FAITS_MAX = 30

NOMS = {
    "am": "AM", "nfm": "FM étroite", "wfm": "FM large",
    "usb": "BLU haute", "lsb": "BLU basse",
    "-fa": "détection automatique", "-fs": "DMR",
    "-fi": "NXDN 48", "-fn": "NXDN 96", "-fm": "dPMR", "-fz": "M17",
    "-f1": "P25", "-f2": "P25 phase 2", "-fd": "D-Star", "-fy": "YSF",
    "-fx": "X2 TDMA", "-fp": "ProVoice", "-fh": "EDACS", "-fe": "EDACS EA",
    "analogique": "FM analogique",
    "multimon": "décodeur de données",
    "freedv-700D": "FreeDV 700D",
    "freedv-700E": "FreeDV 700E",
    "freedv-1600": "FreeDV 1600",
}


def nom(juge):
    return NOMS.get(juge or "", juge or "juge")


def _cle(freq_hz):
    try:
        return str(round(float(freq_hz) / PAS_HZ))
    except (TypeError, ValueError):
        return None


def _lire_scores():
    try:
        with open(SCORES, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("frequences", {})
    return data


def _ecrire_scores(data):
    os.makedirs(os.path.dirname(SCORES), exist_ok=True)
    tmp = SCORES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SCORES)


def _poids(faits, bon):
    """Poids des confirmations (ou des refus) encore frais."""
    maintenant = time.time()
    total = 0.0
    for ev in faits or []:
        if bool(ev.get("bon")) != bool(bon):
            continue
        age = max(0.0, maintenant - float(ev.get("t") or maintenant))
        total += math.exp(-age / TAU_S)
    return total


def _faits_anciens(st):
    """Les oui/non notes avant l'horloge : le decompte part d'aujourd'hui."""
    maintenant = time.time()
    faits = [{"t": maintenant, "bon": True} for _ in range(int(st.get("oui") or 0))]
    faits += [{"t": maintenant, "bon": False} for _ in range(int(st.get("non") or 0))]
    return faits[-FAITS_MAX:]


def _migrer(data):
    change = False
    for juges in (data.get("frequences") or {}).values():
        if not isinstance(juges, dict):
            continue
        for st in juges.values():
            if not isinstance(st, dict) or "faits" in st:
                continue
            st["faits"] = _faits_anciens(st)
            change = True
    return change


def _rang_de(st):
    faits = st.get("faits")
    if faits:
        oui, non = _poids(faits, True), _poids(faits, False)
    else:
        oui, non = st.get("oui", 0), st.get("non", 0)
    return (oui + 1) / (oui + non + 2)


def suffisant(freq_hz):
    """Juge assez confirme recemment pour qu'on s'en tienne a lui s'il
    confirme cette prise. None tant que les prises recentes ne tranchent pas."""
    cle = _cle(freq_hz)
    if not cle:
        return None
    with _verrou:
        data = _lire_scores()
        if _migrer(data):
            _ecrire_scores(data)
        juges = data["frequences"].get(cle) or {}
    poids = []
    for juge, st in juges.items():
        if not isinstance(st, dict):
            continue
        oui = _poids(st.get("faits"), True)
        non = _poids(st.get("faits"), False)
        if oui <= non:
            continue
        poids.append((oui, juge))
    if not poids:
        return None
    poids.sort(reverse=True)
    meilleur, juge = poids[0]
    second = poids[1][0] if len(poids) > 1 else 0.0
    if meilleur < POIDS_SUR:
        return None
    if second and meilleur < MARGE_SUR * second:
        return None
    return juge


def rang(freq_hz, juge):
    """0,5 sans souvenir. Monte quand l'utilisateur valide ce juge ici,
    descend quand il le rejette. Sert à le tester plus ou moins tôt."""
    cle = _cle(freq_hz)
    if not cle or not juge:
        return 0.5
    with _verrou:
        st = _lire_scores()["frequences"].get(cle, {}).get(juge) or {}
    return _rang_de(st)


def en_tete(freq_hz):
    """Juge retenu sur cette fréquence (au moins un oui, score au-dessus
    du seuil), ou None. Il passe devant. Les autres restent essayés."""
    cle = _cle(freq_hz)
    if not cle:
        return None
    with _verrou:
        juges = dict(_lire_scores()["frequences"].get(cle) or {})
    choix = None
    for juge, st in juges.items():
        if not juge or st.get("oui", 0) <= 0:
            continue
        score = _rang_de(st)
        if score < SEUIL_RETENU:
            continue
        marque = (score, st.get("oui", 0))
        if choix is None or marque > choix[0]:
            choix = (marque, juge)
    return choix[1] if choix else None


def vu(freq_hz, juge):
    """Combien de fois ce juge a deja sorti du clair sur cette frequence."""
    cle = _cle(freq_hz)
    if not cle or not juge:
        return 0
    with _verrou:
        st = (_lire_scores()["frequences"].get(cle) or {}).get(juge) or {}
    return int(st.get("vu") or 0)


def noter_vu(freq_hz, juge):
    """Ce juge a sorti de la voix ou des donnees en clair. La prochaine fois
    il passe devant. Ce n'est pas un oui de l'utilisateur : les autres juges,
    et l'analogique, continuent d'etre essayes."""
    cle = _cle(freq_hz)
    if not cle or not juge:
        return
    with _verrou:
        data = _lire_scores()
        st = data["frequences"].setdefault(cle, {}).setdefault(juge, {"oui": 0, "non": 0})
        st["vu"] = int(st.get("vu") or 0) + 1
        _ecrire_scores(data)


def _noter_bande(label, juge, bon):
    """La meme note, pour la confiance du juge sur la bande (juges_scores.json) :
    sans elle, seule la commande "juge" nourrissait ce score."""
    if not label or not str(juge).startswith("-"):
        return
    chemin = chemins.base("juges_scores.json")
    try:
        with open(chemin, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    b = s.setdefault("bandes", {}).setdefault(label, {"_protocoles": {}})
    js = b.setdefault(juge, {"oui": 0, "non": 0, "propositions": 0})
    js["oui" if bon else "non"] = js.get("oui" if bon else "non", 0) + 1
    tmp = chemin + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)


def _noter(freq_hz, juge, bon, label=None):
    cle = _cle(freq_hz)
    if not cle or not juge:
        return
    try:
        _noter_bande(label, juge, bon)
    except OSError as exc:
        print(f"[propositions] confiance de bande non notee : {exc}")
    with _verrou:
        data = _lire_scores()
        st = data["frequences"].setdefault(cle, {}).setdefault(juge, {"oui": 0, "non": 0})
        if "faits" not in st:
            st["faits"] = _faits_anciens(st)
        st["oui" if bon else "non"] = st.get("oui" if bon else "non", 0) + 1
        st["faits"].append({"t": time.time(), "bon": bool(bon)})
        st["faits"] = st["faits"][-FAITS_MAX:]
        _ecrire_scores(data)


def _chemin(journal):
    return journal[:-4] + ".propositions.json"


def _charger(journal):
    if not journal:
        return None
    try:
        with open(_chemin(journal), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not doc.get("items"):
        return None
    return doc


def _sauver(journal, doc):
    path = _chemin(journal)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _empreinte_wav(chemin):
    try:
        with wave.open(chemin) as w:
            if w.getsampwidth() != 2:
                return None
            pcm = w.readframes(w.getnframes())
            if w.getnchannels() == 2:
                import audioop
                pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
            return ns.fingerprint(pcm, w.getframerate(), "s16le")
    except (OSError, wave.Error, EOFError):
        return None


def _empreinte_pcm(pcm, taux, canaux):
    if canaux == 2:
        import audioop
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    return ns.fingerprint(pcm, taux, "s16le")


def _vers_mp3(pcm, taux, canaux, dest):
    if canaux == 2:
        import audioop
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        canaux = 1
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "s16le", "-ar", str(taux), "-ac", str(canaux), "-i", "-",
         "-codec:a", "libmp3lame", "-q:a", "5", dest],
        input=pcm, capture_output=True, timeout=60)
    return proc.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 200


def _wav_vers_mp3(src, dest):
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-ac", "1", "-codec:a", "libmp3lame", "-q:a", "5", dest],
        capture_output=True, timeout=60)
    return proc.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 200


def poser(journal, freq_hz, label, juge, empreinte, autres):
    """autres : rendus en plus de celui déjà dans le journal.
    Chaque autre est {juge, pcm, taux, canaux} ou {juge, wav}."""
    if not journal or not autres:
        return
    items = [{"juge": juge, "fichier": None, "empreinte": empreinte}]
    base = os.path.basename(journal)[:-4]
    dossier = os.path.dirname(journal)
    for i, autre in enumerate(autres, start=1):
        dest = os.path.join(dossier, f"{base}.p{i}.mp3")
        if autre.get("wav"):
            ok = _wav_vers_mp3(autre["wav"], dest)
            emp = _empreinte_wav(autre["wav"])
        else:
            ok = _vers_mp3(autre.get("pcm") or b"", autre.get("taux") or 22050,
                           autre.get("canaux") or 1, dest)
            emp = _empreinte_pcm(autre.get("pcm") or b"", autre.get("taux") or 22050,
                                 autre.get("canaux") or 1)
        if not ok:
            try:
                os.remove(dest)
            except OSError:
                pass
            continue
        items.append({"juge": autre.get("juge"), "fichier": os.path.basename(dest), "empreinte": emp})
    if len(items) < 2:
        return
    _sauver(journal, {"courante": 0, "freq_hz": freq_hz, "label": label or "", "items": items})
    print(f"[propositions] {os.path.basename(journal)} : {len(items)} rendus")


def fichier_courant(journal):
    """Fichier à jouer : le journal, ou le rendu d'un autre juge."""
    doc = _charger(journal)
    if not doc:
        return journal
    item = doc["items"][min(doc.get("courante", 0), len(doc["items"]) - 1)]
    if not item.get("fichier"):
        return journal
    chemin = os.path.join(os.path.dirname(journal), item["fichier"])
    return chemin if os.path.exists(chemin) else journal


def rendu_courant(journal):
    """Rendu en cours d'une prise qui a une fiche : {juge, fichier, empreinte}, ou None."""
    doc = _charger(journal)
    if not doc:
        return None
    return doc["items"][min(doc.get("courante", 0), len(doc["items"]) - 1)]


def habillage_courant(journal):
    """(frequence, bande, juge) si le rendu en cours est un fichier annexe, sans
    ceremonie enregistree ; None pour la prise elle-meme, qui a deja la sienne."""
    doc = _charger(journal)
    if not doc:
        return None
    item = doc["items"][min(doc.get("courante", 0), len(doc["items"]) - 1)]
    if not item.get("fichier"):
        return None
    return doc.get("freq_hz"), doc.get("label") or "", item.get("juge")


def annonce_courante(journal):
    """La position seulement : la ceremonie du rendu dit la bande et la modulation."""
    doc = _charger(journal)
    if not doc or len(doc["items"]) < 2:
        return None
    i = min(doc.get("courante", 0), len(doc["items"]) - 1)
    return dire("journal.proposition", indice=i + 1, total=len(doc["items"]))


def avancer(journal):
    """J : rendu suivant, sans rejeter. None s'il n'y en a qu'un."""
    doc = _charger(journal)
    if not doc or len(doc["items"]) < 2:
        return None
    doc["courante"] = (doc.get("courante", 0) + 1) % len(doc["items"])
    _sauver(journal, doc)
    return annonce_courante(journal)


def _apprendre(doc, item):
    emp = item.get("empreinte")
    freq = doc.get("freq_hz")
    if not emp or not freq:
        return False
    ns.add_signature({"freq_hz": freq, "label": doc.get("label") or "", "fingerprint": emp, "juge": item.get("juge")})
    return True


def _oter_fichier(journal, item):
    nom_f = item.get("fichier")
    if not nom_f:
        return
    try:
        os.remove(os.path.join(os.path.dirname(journal), nom_f))
    except OSError:
        pass


def rejeter(journal):
    """Flèche gauche sur le rendu en cours.

    Rend une phrase si d'autres rendus restent (la prise demeure au journal).
    Rend None si la prise entière doit suivre le rejet habituel.
    """
    doc = _charger(journal)
    if not doc:
        return None
    i = min(doc.get("courante", 0), len(doc["items"]) - 1)
    item = doc["items"][i]
    _noter(doc.get("freq_hz"), item.get("juge"), False, doc.get("label"))
    appris = _apprendre(doc, item)
    if len(doc["items"]) == 1:
        if item.get("fichier"):
            _oter_fichier(journal, item)
        try:
            os.remove(_chemin(journal))
        except OSError:
            pass
        return None
    _oter_fichier(journal, item)
    del doc["items"][i]
    if not doc["items"]:
        try:
            os.remove(_chemin(journal))
        except OSError:
            pass
        return None
    doc["courante"] = min(i, len(doc["items"]) - 1)
    _sauver(journal, doc)
    reste = nombre(len(doc["items"]), "autre rendu", "autres rendus")
    if appris:
        return dire("journal.rejet_suivant", rejete=nom(item.get("juge")), reste=reste)
    return dire("journal.rejet_suivant_sans_empreinte", rejete=nom(item.get("juge")), reste=reste)


def a_fiche(journal):
    return _charger(journal) is not None


def confirmer(journal):
    """V, C ou N : le rendu entendu est le bon pour cette fréquence."""
    doc = _charger(journal)
    if doc:
        if doc.get("confirmee"):
            return
        i = min(doc.get("courante", 0), len(doc["items"]) - 1)
        item = doc["items"][i]
        _noter(doc.get("freq_hz"), item.get("juge"), True, doc.get("label"))
        doc["confirmee"] = item.get("juge")
        _sauver(journal, doc)
        return
    juge, freq, label = _juge_courant(journal)
    _noter(freq, juge, True, label)


def noter_rejet_simple(journal):
    """Rejet d'une prise qui n'a qu'un rendu : ce juge descend sur cette fréquence.
    Rend le juge noté (None : pas de juge ou pas de fréquence, rien n'a baissé)."""
    juge, freq, label = _juge_courant(journal)
    _noter(freq, juge, False, label)
    return juge if juge and _cle(freq) else None


def _juge_courant(journal):
    """Juge du rendu en cours, fréquence et bande."""
    doc = _charger(journal)
    if doc and doc.get("items"):
        i = min(doc.get("courante", 0), len(doc["items"]) - 1)
        return doc["items"][i].get("juge"), doc.get("freq_hz"), doc.get("label") or ""
    info = ns.capture_info(journal) or {}
    metrics = info.get("metrics") or {}
    juge = metrics.get("juge") or metrics.get("modulation_banc") or info.get("mode")
    if not info.get("freq_hz") and journal:   # prise vocale d'un message : sa fiche dit le type
        try:
            with open(journal[:-4] + ".json", encoding="utf-8") as f:
                carte = json.load(f)
            return carte.get("type_message"), carte.get("frequence_hz"), carte.get("protocole") or ""
        except (OSError, ValueError):
            pass
    return juge, info.get("freq_hz"), info.get("label") or ""


def renvoyer_flux(journal):
    """Touche R : le flux non décodé repart aux juges, sauf celui en cours.

    Le wav reste sur le NAS, à côté du journal. On en dépose une copie dans
    la file des juges. Rend la phrase à dire."""
    flux = journal[:-4] + ".flux.wav" if journal and journal.endswith(".mp3") else ""
    if not flux or not os.path.isfile(flux) or os.path.getsize(flux) < 48_000:
        return dire("journal.pas_flux")
    juge, freq, label = _juge_courant(journal)
    if not freq:
        return dire("journal.pas_flux")
    dossier = chemins.base("brut_dsd")
    os.makedirs(dossier, exist_ok=True)
    cle = hashlib.md5(os.path.basename(journal).encode()).hexdigest()[:10]
    dest = os.path.join(dossier, f"renvoi_{cle}.wav")
    fiche_path = dest[:-4] + ".json"
    if os.path.isfile(dest) and os.path.isfile(fiche_path):
        return dire("journal.flux_deja")
    shutil.copyfile(flux, dest)
    fiche = {
        "freq_hz": int(freq),
        "mode": "renvoi",
        "band": label,
        "t": time.time(),
        "etiquette": "renvoi",
        "direct": {"valide": False, "protocole": None, "propres": 0, "total": 0,
                   "prive": False, "chiffre": False},
        "juges": {},
    }
    fiche["journal"] = journal
    tmp = fiche_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fiche, f, ensure_ascii=False)
    os.replace(tmp, fiche_path)
    return dire("journal.flux_renvoye")


def renvoyer_decodeur(journal, juge):
    """Contrôle+R : le flux part à un seul décodeur. Son fichier est gardé,
    même court, même marqué chiffré. Ce n'est pas un déchiffrement."""
    flux = journal[:-4] + ".flux.wav" if journal and journal.endswith(".mp3") else ""
    if not flux or not os.path.isfile(flux) or os.path.getsize(flux) < 48_000:
        return dire("journal.pas_flux")
    if juge not in NOMS:
        return dire("journal.decodeur_rien", nom=juge or "décodeur")
    _juge, freq, label = _juge_courant(journal)
    if not freq:
        return dire("journal.pas_flux")
    dossier = chemins.base("brut_dsd")
    os.makedirs(dossier, exist_ok=True)
    cle = hashlib.md5(os.path.basename(journal).encode()).hexdigest()[:10]
    dest = os.path.join(dossier, f"decodeur_{cle}_{str(juge).lstrip('-')}.wav")
    fiche_path = dest[:-4] + ".json"
    if os.path.isfile(dest) and os.path.isfile(fiche_path):
        return dire("journal.flux_deja")
    shutil.copyfile(flux, dest)
    fiche = {
        "freq_hz": int(freq),
        "mode": "decodeur",
        "band": label,
        "t": time.time(),
        "etiquette": "decodeur",
        "decodeur": juge,
        "journal": journal,
        "direct": {"valide": False, "protocole": None, "propres": 0, "total": 0,
                   "prive": False, "chiffre": False},
        "juges": {},
    }
    tmp = fiche_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fiche, f, ensure_ascii=False)
    os.replace(tmp, fiche_path)
    return dire("journal.decodeur_envoye", nom=nom(juge))


def rendre_decodeur(wav, fiche, juge, verdict, dossier):
    """Dépose la sortie de ce seul décodeur sur la prise, et la phrase."""
    journal = _journal_du_renvoi(wav, fiche)
    verdict = verdict or {}
    secondes = str(verdict.get("voix_s") or 0).replace(".", ",")
    nom_j = nom(juge)
    if not journal:
        return dire("journal.decodeur_rien", nom=nom_j)
    chemin = None
    if dossier and juge:
        candidat = os.path.join(dossier, "voix_%s.wav" % str(juge).lstrip("-"))
        if os.path.isfile(candidat) and os.path.getsize(candidat) > 44:
            chemin = candidat
    ajoute = False
    if chemin:
        try:
            info = ns.capture_info(journal) or {}
        except Exception:
            info = {}
        origine = (info.get("metrics") or {}).get("juge") or info.get("mode") or "prise"
        ajoute = ajouter_rendu(journal, (fiche or {}).get("freq_hz"),
                                (fiche or {}).get("band") or "", origine, juge, chemin)
    if ajoute and verdict.get("chiffre"):
        return dire("journal.decodeur_chiffre", nom=nom_j, secondes=secondes)
    if ajoute:
        return dire("journal.decodeur_ajoute", nom=nom_j, secondes=secondes)
    if verdict.get("protocole"):
        return dire("journal.decodeur_sans_voix", nom=nom_j, protocole=verdict.get("protocole"))
    return dire("journal.decodeur_rien", nom=nom_j)


def _journal_du_renvoi(wav, fiche):
    """La prise d'origine. Le nom est dans la fiche, sinon on le retrouve
    par l'empreinte posee au moment du renvoi."""
    chemin = (fiche or {}).get("journal")
    if chemin and os.path.isfile(chemin):
        return chemin
    nom = os.path.basename(wav or "")
    if not (nom.startswith("renvoi_") and nom.endswith(".wav")):
        return None
    cle = nom[len("renvoi_"):-4]
    try:
        from sound_paths import RADIO_LOG_DIR
    except Exception:
        RADIO_LOG_DIR = "/media/nas/nd1/server/radio_log"
    if not os.path.isdir(RADIO_LOG_DIR):
        return None
    for dossier, _sous, fichiers in os.walk(RADIO_LOG_DIR):
        for fichier in fichiers:
            if not fichier.endswith(".mp3") or ".p" in fichier[:-4]:
                continue
            if hashlib.md5(fichier.encode()).hexdigest()[:10] == cle:
                return os.path.join(dossier, fichier)
    return None


def _capital(texte):
    texte = (texte or "").strip()
    if not texte:
        return ""
    return texte[0].upper() + texte[1:]


def _phrase_renvoi(verdicts, ajoutes):
    """Ce que chaque juge a repondu, y compris ceux qui n'ont rien sorti."""
    morceaux = []
    poses = set(ajoutes)
    if len(ajoutes) == 1:
        morceaux.append(dire("journal.rendu_ajoute", nom=nom(ajoutes[0])))
    elif ajoutes:
        morceaux.append(dire("journal.rendus_ajoutes",
                             nombre=nombre(len(ajoutes), "rendu", "rendus"),
                             liste=", ".join(nom(j) for j in ajoutes)))
    else:
        morceaux.append(dire("journal.rendu_aucun"))
    vus = []
    rien = 0
    for juge, v in (verdicts or {}).items():
        v = v or {}
        if juge in poses:
            if v.get("chiffre"):
                vus.append(_capital(dire("journal.juge_chiffre", juge=nom(juge))))
            continue
        if v.get("chiffre"):
            vus.append(_capital(dire("journal.juge_chiffre", juge=nom(juge))))
        elif v.get("protocole") and v.get("voix_s", 0) < 0.4:
            vus.append(_capital(dire("journal.juge_sans_voix", juge=nom(juge), protocole=v["protocole"])))
        elif v.get("voix_s", 0) >= 0.4:
            vus.append(_capital(dire("journal.juge_voix", juge=nom(juge),
                                     secondes=str(v.get("voix_s")).replace(".", ","))))
        else:
            rien += 1
    morceaux.extend(vus)
    if rien:
        morceaux.append(_capital(dire("journal.juges_rien",
                                     nombre=nombre(rien, "juge", "juges"))))
    return dire("journal.juges_repondu", detail=" ".join(m for m in morceaux if m))


def ajouter_rendu(journal, freq_hz, label, origine, juge, wav):
    """Ajoute un rendu à la prise, sans effacer ceux déjà là. Le curseur
    se place dessus : entrée le joue."""
    if not journal or not wav or not os.path.isfile(wav):
        return False
    doc = _charger(journal)
    if doc:
        items = list(doc.get("items") or [])
        freq_hz = doc.get("freq_hz", freq_hz)
        label = doc.get("label") or label
    else:
        items = [{"juge": origine or "prise", "fichier": None, "empreinte": None}]
        doc = None
    base = os.path.basename(journal)[:-4]
    dossier = os.path.dirname(journal)
    n = 1
    while os.path.exists(os.path.join(dossier, f"{base}.p{n}.mp3")):
        n += 1
    dest = os.path.join(dossier, f"{base}.p{n}.mp3")
    if not _wav_vers_mp3(wav, dest):
        try:
            os.remove(dest)
        except OSError:
            pass
        return False
    items.append({"juge": juge, "fichier": os.path.basename(dest),
                  "empreinte": _empreinte_wav(wav)})
    if doc:
        doc["items"] = items
        doc["courante"] = len(items) - 1
        if freq_hz is not None:
            doc["freq_hz"] = freq_hz
        _sauver(journal, doc)
    else:
        _sauver(journal, {"courante": len(items) - 1, "freq_hz": freq_hz,
                          "label": label or "", "items": items})
    print(f"[propositions] {os.path.basename(journal)} : rendu {nom(juge)} ajouté")
    return True


def rendre_renvoi(wav, fiche, verdicts, dossier, flux_fm=True):
    """Depose sur la prise d'origine chaque son ecoutable, et la phrase a dire.

    Le flux est le son FM du recepteur : il devient l'ecoute FM etroite.
    Les voix numeriques presentes (meme en dessous du seuil automatique)
    deviennent les autres rendus. Touche J les parcours. None si deja dit.
    """
    journal = _journal_du_renvoi(wav, fiche)
    if not journal:
        print(f"[propositions] renvoi sans prise : {wav}")
        return _phrase_renvoi(verdicts, [])
    doc = _charger(journal)
    if doc and doc.get("renvoi_wav") == os.path.basename(wav or ""):
        return None
    voix = {}
    if flux_fm and wav and os.path.isfile(wav) and os.path.getsize(wav) > 48_000:
        voix["nfm"] = wav
    if dossier and os.path.isdir(dossier):
        for juge in verdicts or {}:
            chemin = os.path.join(dossier, "voix_%s.wav" % str(juge).lstrip("-"))
            if os.path.isfile(chemin) and os.path.getsize(chemin) > 44:
                voix[juge] = chemin
    ajoutes = list(voix)
    if ajoutes:
        try:
            info = ns.capture_info(journal) or {}
        except Exception:
            info = {}
        origine = (info.get("metrics") or {}).get("juge") or info.get("mode") or "prise"
        poser(journal, (fiche or {}).get("freq_hz"), (fiche or {}).get("band") or "",
              origine, None, [{"juge": j, "wav": voix[j]} for j in ajoutes])
        doc = _charger(journal)
        if doc:
            doc["renvoi_wav"] = os.path.basename(wav or "")
            _sauver(journal, doc)
    phrase = _phrase_renvoi(verdicts, ajoutes)
    print(f"[propositions] renvoi {os.path.basename(journal)} : {phrase}")
    return phrase
