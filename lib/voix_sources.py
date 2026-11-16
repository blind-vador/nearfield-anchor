"""Voix par source (2026-10-19) : chaque veille (gare, avions, imprimante...),
la console et les touches du clavier ont leur propre voix, reglee par la
commande "voix" :

  voix          voix principale : une voix Microsoft, une voix Piper, espeak,
                ou "aleatoire" (une voix differente a chaque fois)
  debit         debit de la voix principale ("+20%")
  hauteur       hauteur (voix Microsoft seulement, "+10Hz")
  secours       voix Piper quand Microsoft ne repond pas, ou "aleatoire"
  secours_debit debit de la voix de secours
  effet         effet sur la voix des annonces (lib/effets_voix.py) : "aucun",
                "aleatoire" ou un effet (echo, reverbe...) ; pas pour la console

Etat de Microsoft partage par tous les services (MS_ETAT, tenu par la sonde
de la console et par chaque echec ou succes) : on ne fait plus attendre une
annonce 8 s sur un service qui ne repond pas.

Modes aleatoires (2026-10-22) : "aleatoire" (voix Microsoft), "aleatoire_tout"
(toutes les voix : Microsoft, Mac, Google, Piper, MBROLA), "aleatoire_distant"
(voix calculees ailleurs que sur le Pi : Microsoft, Mac, Google) et
"aleatoire_piper" (voix locales seulement : Piper et MBROLA). Microsoft
absent : ses voix sortent simplement du tirage des modes melanges.

Refus Microsoft (2026-10-22) : pas de test regulier ; erreur_microsoft(exc),
appelee la ou une synthese echoue, reconnait un refus 403, le note dans le
journal et lance la reparation (edge-maj.service) ; noter_microsoft(True)
note le retour.

choisir(source) rend {"moteur": "edge"|"piper"|"espeak", "voix", "debit",
"hauteur", "aleatoire"} ; fichier_cache() nomme un echantillon en cache pour
ce choix (le debit et la hauteur entrent dans le nom s'ils ne sont pas
neutres).
"""
import chemins
import json
import os
import random
import sys
import time

from phrases import Etiquettes

ALEATOIRE = "aleatoire"
ALEATOIRE_PIPER = "aleatoire_piper"   # une voix locale (Piper, MBROLA) differente a chaque fois
ALEATOIRE_TOUT = "aleatoire_tout"     # toutes les voix (2026-10-22)
ALEATOIRE_DISTANT = "aleatoire_distant"  # voix calculees ailleurs : Microsoft, Mac, Google
MODES_MELANGES = (ALEATOIRE_TOUT, ALEATOIRE_DISTANT)
JOURNAL_MS = chemins.base("journal_voix_microsoft.jsonl")
JOURNAL_MS_NAS = "/media/nas/nd1/server/logs/journal_voix_microsoft.jsonl"
REPARATION = "/tmp/microsoft_reparation.json"
REPARATION_ECART_SEC = 1800
ESPEAK = "espeak"
MS_ETAT = "/tmp/microsoft_etat.json"
MS_VALIDITE_SEC = 20 * 60
CACHE_SEC = 10
NEUTRE_DEBIT, NEUTRE_HAUTEUR = "+0%", "+0Hz"

SOURCES = Etiquettes("voix.source", {
    "console": "console : réponses et dialogues",
    "touches": "console : lettres et touches du clavier",
    "trains": "veille gare",
    "avions": "veille avions",
    "helicos": "veille hélicoptères",
    "radio": "veille radio : scanner, réceptions",
    "donnees": "données radio : APRS, capteurs, pagers, LoRa",
    "terrain": "veille terrain",
    "alertes": "alertes météo et crues",
    "capteurs": "bulletin horaire des capteurs météo",
    "bus": "bus et tram",
    "disques": "veille disques",
    "imprimante": "veille imprimante",
    "plantes": "plantes",
    "systeme": "système : USB, machine compagne",
    "tickets": "tickets et journal des extensions",
}, "source de voix, dans la commande voix")
ANNONCES = [s for s in SOURCES if s not in ("console", "touches")]

# Processus -> source ; etiquette d'annonce (prise_vocale) -> source.
SCRIPTS = {"radio_watch": "radio", "sncf_watch": "trains", "meteo_watch": "alertes", "crue_watch": "alertes",
           "adsb_watch": "avions", "stas_watch": "bus", "nas_watch": "disques", "main": "console",
           "imprimante_watch": "imprimante", "veille_terrain": "terrain", "usb_watch": "systeme",
           "compagnon_watch": "systeme", "veille_systeme": "systeme"}
TAGS = {"meteo_capteurs": "capteurs", "plantes": "plantes", "veille_terrain": "terrain", "terrain": "terrain", "helicos": "helicos",
        "compagnon": "systeme", "usb": "systeme", "tickets": "tickets"}

_cache = {"t": 0.0, "data": None}


def source_pour(tag=None):
    """Source d'une annonce : son etiquette si on la connait, sinon le service."""
    if tag in TAGS:
        return TAGS[tag]
    script = os.path.splitext(os.path.basename(sys.argv[0] or ""))[0]
    return SCRIPTS.get(script, "donnees" if tag else "console")


def _tout():
    if _cache["data"] is None or time.time() - _cache["t"] > CACHE_SEC:
        import reglages
        try:
            data = reglages.get("voix_sources") or {}
        except Exception:
            data = {}
        _cache.update(t=time.time(), data=data)
    return _cache["data"]


def _defaut(source):
    base = {"voix": ALEATOIRE, "debit": NEUTRE_DEBIT, "hauteur": NEUTRE_HAUTEUR,
            "secours": ALEATOIRE, "secours_debit": NEUTRE_DEBIT, "effet": "aucun"}
    if source in ("console", "touches"):
        import reglages
        console = reglages.get("console")
        base.update(voix=console.get("voix") or ESPEAK, secours=console.get("voix_secours") or "piper:fr_FR-siwis-medium",
                    debit=console.get("debit_echo" if source == "touches" else "debit", NEUTRE_DEBIT))
        base["secours_debit"] = base["debit"]
    return base


def reglage(source):
    r = _defaut(source)
    r.update(_tout().get(source) or {})
    return r


def enregistrer(source, valeurs):
    import reglages
    data = dict(reglages.get("voix_sources") or {})
    data[source] = valeurs
    ok = reglages.save("voix_sources", data)
    _cache["data"] = None
    return ok


# --- Microsoft -------------------------------------------------------------------------

def microsoft_ok():
    """Microsoft est-il utilisable ? Un echec ne l'ecarte que MS_VALIDITE_SEC,
    puis il est retente (2026-10-23 : sans sonde reguliere, un seul rate le 23 a
    13 h 12 l'avait sorti des tirages pour de bon, sans jamais de retour).
    Pas encore d'etat ou dernier essai reussi : utilisable."""
    try:
        with open(MS_ETAT, encoding="utf-8") as f:
            etat = json.load(f)
    except (OSError, ValueError):
        return True
    return bool(etat.get("ok")) or time.time() - etat.get("t", 0) >= MS_VALIDITE_SEC


def journal_microsoft(evenement, **details):
    """Une ligne JSON dans le journal des voix Microsoft (et sa copie sur le NAS)."""
    ligne = json.dumps(dict(t=time.time(), date=time.strftime("%Y-%m-%d %H:%M:%S"), evenement=evenement,
                            service=os.path.basename(sys.argv[0] or "?"), **details), ensure_ascii=False)
    print(f"[voix] microsoft : {ligne}", flush=True)
    for chemin in (JOURNAL_MS, JOURNAL_MS_NAS):
        if chemin == JOURNAL_MS_NAS and not os.path.ismount("/media/nas/nd1"):
            continue
        try:
            with open(chemin, "a", encoding="utf-8") as f:
                f.write(ligne + "\n")
        except OSError:
            pass


def _reparation():
    try:
        with open(REPARATION, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _ecrire_reparation(data):
    try:
        tmp = REPARATION + f".{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, REPARATION)
    except OSError:
        pass


def erreur_microsoft(exc):
    """Une synthese Microsoft a echoue. Refus 403 (Microsoft a change son acces) :
    note dans le journal (une fois par panne) et reparation lancee, au plus
    toutes les REPARATION_ECART_SEC. Les autres erreurs (reseau) : rien."""
    texte = repr(exc)
    if "403" not in texte:
        return
    etat = _reparation()
    if etat.get("ouverte") and time.time() - etat.get("t", 0) < REPARATION_ECART_SEC:
        return
    if not etat.get("ouverte"):
        journal_microsoft("refus_microsoft", erreur=texte[:300])
    _ecrire_reparation({"ouverte": True, "t": time.time(), "depuis": etat.get("depuis") or time.time()})
    try:
        import subprocess
        subprocess.Popen(["sudo", "-n", "systemctl", "start", "edge-maj"],  # regle sudo d'install.sh
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def noter_microsoft(ok):
    if ok:
        etat = _reparation()
        if etat.get("ouverte"):
            _ecrire_reparation({"ouverte": False, "t": time.time()})
            journal_microsoft("retabli", panne_minutes=round((time.time() - etat.get("depuis", time.time())) / 60))
    try:
        tmp = MS_ETAT + f".{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ok": bool(ok), "t": time.time()}, f)
        os.replace(tmp, MS_ETAT)
    except OSError:
        pass


# --- choix ------------------------------------------------------------------------------

def _microsoft_voix():
    import speak
    return speak.EDGE_TTS_VOICES


def secours(source):
    import piper_out
    r = reglage(source)
    voix = piper_out.canonical(r["secours"])
    # Un fournisseur desactive ne revient pas par le secours, meme choisi avant.
    alea = voix == ALEATOIRE or not piper_out.resolve(voix) or not permise(voix)
    if alea:
        ids = repli_local()
        if not ids:
            return {"moteur": ESPEAK, "voix": ESPEAK, "debit": r["secours_debit"], "hauteur": NEUTRE_HAUTEUR,
                    "aleatoire": False}
        voix = random.choice(ids)
    return {"moteur": "piper", "voix": voix, "debit": r["secours_debit"], "hauteur": NEUTRE_HAUTEUR,
            "aleatoire": alea}


def _pool():
    import piper_out
    return piper_out.voix_locales()


def tirage(mode, ms_ok=None):
    """Voix du tirage d'un mode aleatoire (identifiants "piper:..." ou voix Microsoft)."""
    import piper_out
    ms_ok = microsoft_ok() if ms_ok is None else ms_ok
    if mode == ALEATOIRE_PIPER:
        return _pool()
    if mode == ALEATOIRE:
        return list(_microsoft_voix()) if ms_ok else []
    distantes = (list(_microsoft_voix()) if ms_ok else []) + piper_out.voix_distantes()
    if mode == ALEATOIRE_DISTANT:
        return distantes
    if mode == ALEATOIRE_TOUT:
        return _pool() + distantes
    return []


# Le Mac compte plusieurs familles de voix, chacune avec son score (2026-11-06).
MAC_FAMILLES = ("mac_eloquence", "mac_nuance", "mac_premium")
FOURNISSEURS = ("microsoft",) + MAC_FAMILLES + ("google", "piper", "mbrola")
# Le Mac ne donne que des noms (say -v ?). Liste verifiee sur les identifiants
# com.apple.eloquence.* ; le reste vient de com.apple.voice.compact ou .enhanced.
_MAC_ELOQUENCE = {"Eddy", "Flo", "Grandma", "Grandpa", "Jacques", "Reed", "Rocko", "Sandy", "Shelley"}
NIVEAUX = ("desactive", "faible", "normale", "appreciees")
# Désactivé : ignoré. Faible, normale, appréciées : parts du tirage.
POIDS = {"desactive": 0, "faible": 1, "normale": 3, "appreciees": 8}
_PREFS = "_fournisseurs"
_PREFS_MAC = "_voix_mac"   # niveau de chaque voix du Mac, a l'interieur du poids du fournisseur (2026-11-06)


def famille_mac(nom):
    """Famille d'une voix du Mac, d'apres son nom."""
    if nom.split(" (", 1)[0] in _MAC_ELOQUENCE:
        return "mac_eloquence"
    return "mac_premium" if "(Premium)" in nom else "mac_nuance"


def fournisseur(voix):
    """Famille d'une voix tirée : microsoft, une famille du Mac, google, piper ou mbrola."""
    if (voix or "").startswith("piper:mbrola#"):
        return "mbrola"
    if (voix or "").startswith("piper:google#"):
        return "google"
    if (voix or "").startswith("piper:mac#"):
        return famille_mac(voix.split("#", 1)[1])
    if (voix or "").startswith("piper:"):
        return "piper"
    if voix == ESPEAK:
        return "espeak"
    return "microsoft"


def en_direct(choix):
    """Espeak et MBROLA se pilotent en direct. Rien n'entre dans le cache."""
    if not choix:
        return False
    if choix.get("moteur") in (ESPEAK, "mbrola"):
        return True
    return (choix.get("voix") or "").startswith("piper:mbrola#")


def phrase_entiere(choix):
    """Google et le Mac : une seule requête pour toute la phrase, puis le cache.
    Découpée en mots, Google échoue sur les mots courts et le secours Piper
    prend toute l'annonce."""
    voix = (choix or {}).get("voix") or ""
    return voix.startswith("piper:google#") or voix.startswith("piper:mac#")


def preferences():
    """Niveau de chaque fournisseur. Défaut : normale. Une famille du Mac sans
    réglage reprend l'ancien niveau unique du Mac."""
    data = _tout().get(_PREFS) or {}
    ancien_mac = data.get("mac") if data.get("mac") in NIVEAUX else "normale"
    return {nom: data[nom] if data.get(nom) in NIVEAUX else (ancien_mac if nom in MAC_FAMILLES else "normale")
            for nom in FOURNISSEURS}


def enregistrer_preferences(valeurs):
    import reglages
    data = dict(reglages.get("voix_sources") or {})
    data[_PREFS] = {nom: valeurs.get(nom, "normale") if valeurs.get(nom) in NIVEAUX else "normale"
                    for nom in FOURNISSEURS}
    ok = reglages.save("voix_sources", data)
    _cache["data"] = None
    return ok


def niveaux_mac(noms=None):
    """Niveau de chaque voix du Mac. Defaut : normale."""
    data = _tout().get(_PREFS_MAC) or {}
    if noms is None:
        noms = list(data)
    return {nom: data[nom] if data.get(nom) in NIVEAUX else "normale" for nom in noms}


def enregistrer_niveaux_mac(valeurs):
    import reglages
    data = dict(reglages.get("voix_sources") or {})
    # Le menu ne montre qu'une famille a la fois : les autres voix gardent leur niveau.
    data[_PREFS_MAC] = {**(data.get(_PREFS_MAC) or {}),
                        **{nom: niveau for nom, niveau in valeurs.items() if niveau in NIVEAUX}}
    ok = reglages.save("voix_sources", data)
    _cache["data"] = None
    return ok


def _poids_voix(voix):
    """Part d'une voix dans son fournisseur : seul le Mac se regle voix par voix."""
    if fournisseur(voix) not in MAC_FAMILLES:
        return POIDS["normale"]
    nom = voix.split("#", 1)[1]
    return POIDS.get(niveaux_mac([nom])[nom], POIDS["normale"])


def permise(voix):
    """Faux si le fournisseur, ou cette voix du Mac, est desactive."""
    if POIDS.get(preferences().get(fournisseur(voix), "normale"), POIDS["normale"]) <= 0:
        return False
    return _poids_voix(voix) > 0


def repli_local():
    """Voix locales permises pour un secours : Piper s'il est permis, sinon MBROLA."""
    import piper_out
    voix = [v for v in piper_out.voix_locales() if permise(v)]
    return [v for v in voix if fournisseur(v) == "piper"] or voix


def echantillon_permis(nom_fichier, choix, suffixe_nom=""):
    """Un echantillon en cache peut-il remplacer ce choix aleatoire ? Seulement du
    meme fournisseur et s'il est permis (2026-11-06 : tirer une voix du Mac pouvait
    rejouer n'importe quel echantillon Piper, meme Piper desactive)."""
    if choix.get("moteur") != "piper":
        return True
    voix = "piper:" + nom_fichier[len("piper-"):-len(suffixe_nom + ".wav")]
    return fournisseur(voix) == fournisseur(choix.get("voix")) and permise(voix)


def aleatoire_tout_actif():
    """Vrai si au moins une veille est sur « aléatoire, toutes les voix »."""
    return any(reglage(s).get("voix") == ALEATOIRE_TOUT for s in ANNONCES)


def peser(pool, prefs):
    """Une voix. Chaque fournisseur pèse son niveau, puis une voix au hasard
    dedans. None si tout ce qui reste est désactivé."""
    groupes = {}
    for voix in pool or ():
        if _poids_voix(voix) > 0:
            groupes.setdefault(fournisseur(voix), []).append(voix)
    poids = []
    for nom, voix in groupes.items():
        w = POIDS.get((prefs or {}).get(nom, "normale"), 3)
        if w > 0 and voix:
            poids.append((w, voix))
    if not poids:
        return None
    total = sum(w for w, _ in poids)
    marque = random.uniform(0, total)
    cumul = 0.0
    for w, voix in poids:
        cumul += w
        if marque < cumul:
            return random.choices(voix, weights=[_poids_voix(v) for v in voix])[0]
    return random.choice(poids[-1][1])


def _rendu(v, r):
    if v.startswith("piper:mbrola#"):
        return {"moteur": "mbrola", "voix": v, "debit": r["debit"], "hauteur": NEUTRE_HAUTEUR,
                "aleatoire": True}
    if v.startswith("piper:"):
        return {"moteur": "piper", "voix": v, "debit": r["debit"], "hauteur": NEUTRE_HAUTEUR,
                "aleatoire": True}
    return {"moteur": "edge", "voix": v, "debit": r["debit"], "hauteur": r["hauteur"], "aleatoire": True}


def a_preparer(mode):
    """Voix d'un tirage dont les phrases fixes se préparent d'avance.
    Pas Microsoft (son cache à lui), pas Google (une phrase entière, au moment
    où elle est dite), pas MBROLA (en direct, sans cache). Un fournisseur
    désactivé n'est pas préparé."""
    prefs = preferences()
    out = []
    for v in tirage(mode, ms_ok=False):
        if not v.startswith("piper:") or v.startswith(("piper:google#", "piper:mbrola#")):
            continue
        if POIDS.get(prefs.get(fournisseur(v), "normale"), 3) <= 0 or _poids_voix(v) <= 0:
            continue
        out.append(v)
    return out


def choisir(source, ms_ok=None):
    """Voix a utiliser maintenant pour cette source. Si Microsoft est absent
    alors qu'il est demande, le choix rendu est la voix de secours, avec la
    voix demandee dans "demande" : ce qu'elle a deja en cache passe avant."""
    import piper_out
    r = reglage(source)
    ms_ok = microsoft_ok() if ms_ok is None else ms_ok
    voix = r["voix"]
    if voix in MODES_MELANGES:
        pool = tirage(voix, ms_ok)
        v = peser(pool, preferences()) if pool else None
        if not v:
            return secours(source)
        return _rendu(v, r)
    if voix == ALEATOIRE_PIPER:
        pool = _pool()
        v = peser(pool, preferences()) if pool else None
        if v:
            return _rendu(v, r)
        voix = ESPEAK
    if voix == ESPEAK:
        return {"moteur": ESPEAK, "voix": ESPEAK, "debit": r["debit"], "hauteur": NEUTRE_HAUTEUR, "aleatoire": False}
    if voix.startswith("piper:"):
        voix = piper_out.canonical(voix)
        if piper_out.resolve(voix):
            return {"moteur": "piper", "voix": voix, "debit": r["debit"], "hauteur": NEUTRE_HAUTEUR,
                    "aleatoire": False}
        return secours(source)
    alea = voix == ALEATOIRE
    if not ms_ok:
        repli = secours(source)
        repli["demande"] = {"moteur": "edge", "voix": random.choice(_microsoft_voix()) if alea else voix,
                            "debit": r["debit"], "hauteur": r["hauteur"], "aleatoire": alea}
        return repli
    return {"moteur": "edge", "voix": random.choice(_microsoft_voix()) if alea else voix,
            "debit": r["debit"], "hauteur": r["hauteur"], "aleatoire": alea}


def suffixe(choix):
    """"" ou "@+20%" / "@+20%-10Hz" : le debit et la hauteur font partie du nom."""
    debit = choix.get("debit") or NEUTRE_DEBIT
    hauteur = choix.get("hauteur") or NEUTRE_HAUTEUR if choix.get("moteur") == "edge" else NEUTRE_HAUTEUR
    if debit == NEUTRE_DEBIT and hauteur == NEUTRE_HAUTEUR:
        return ""
    return "@" + debit + ("" if hauteur == NEUTRE_HAUTEUR else hauteur)


def fichier_cache(dossier, choix):
    """Nom de l'echantillon de ce choix dans le dossier d'une phrase repetitive."""
    if choix["moteur"] == "edge":
        return os.path.join(dossier, f"{choix['voix']}{suffixe(choix)}.mp3")
    return os.path.join(dossier, f"piper-{choix['voix'].split(':', 1)[1]}{suffixe(choix)}.wav")


def voix_piper_source(source):
    """{(voix piper, debit)} que cette source peut utiliser maintenant
    (preparation a l'avance) : sa voix locale choisie, toutes celles du tirage
    si aleatoire, ou sa voix de secours tant que Microsoft ne repond pas."""
    import piper_out
    r = reglage(source)
    voix = r["voix"]
    if voix in MODES_MELANGES or voix == ALEATOIRE_PIPER:
        return {(v, r["debit"]) for v in a_preparer(voix)}
    if voix.startswith("piper:"):
        voix = piper_out.canonical(voix)
        return {(voix, r["debit"])} if piper_out.resolve(voix) else set()
    if voix == ESPEAK or microsoft_ok():
        return set()
    sec = piper_out.canonical(r["secours"])
    if sec == ALEATOIRE or not piper_out.resolve(sec) or not permise(sec):
        return {(v, r["secours_debit"]) for v in _pool() if permise(v)}
    return {(sec, r["secours_debit"])}


def voix_piper_en_service():
    found = set()
    for source in ANNONCES:
        found |= voix_piper_source(source)
    return found


def locale(voix):
    """Voix principale qui ne passe pas par Microsoft (pas d'etape secours)."""
    return voix in (ALEATOIRE_PIPER,) + MODES_MELANGES or voix == ESPEAK or voix.startswith("piper:")


def effet(source):
    """Id de l'effet a poser sur cette annonce (lib/effets_voix.py), ou None.
    Jamais pour la console ni les touches : leurs reponses restent seches."""
    if source not in ANNONCES:
        return None
    import effets_voix
    return effets_voix.tirer(reglage(source).get("effet"))


def resume(source):
    """Phrase courte : "voix aleatoire, secours Siwis"."""
    r = reglage(source)
    texte = nom_voix(r["voix"]) if locale(r["voix"]) else f"{nom_voix(r['voix'])}, secours {nom_voix(r['secours'])}"
    if source in ANNONCES and r.get("effet") not in (None, "aucun"):
        from phrases import dire
        texte += ", " + dire("voix.resume_effet", effet=dire(f"voix.effet.{r['effet']}"))
    return texte


def nom_voix(voix):
    import piper_out
    if voix == ALEATOIRE:
        return "aléatoire"
    if voix == ALEATOIRE_PIPER:
        return "aléatoire, voix locales"
    if voix == ALEATOIRE_TOUT:
        return "aléatoire, toutes les voix"
    if voix == ALEATOIRE_DISTANT:
        return "aléatoire, voix distantes"
    if voix == ESPEAK:
        return "espeak"
    if voix.startswith("piper:"):
        return piper_out.label(piper_out.canonical(voix)).split(",")[0]
    parts = voix.split("-")
    return parts[2].replace("MultilingualNeural", "").replace("Neural", "") if len(parts) > 2 else voix
