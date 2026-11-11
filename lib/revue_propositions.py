"""Revue unique des propositions (2026-11-11, demande utilisateur).

Le journal proposition reunit trois sortes d'entrees, chacune acceptee (C) ou
refusee (R) pour elle-meme :
  nettoyage   un nettoyage audio propose par l'examen du Mac (propositions_reglages) ;
  nommage     un nom de station, avec son pourquoi (nommage) ;
  frequence   une frequence citee dans un texte, trouvee par l'IA et mise en
              surveillance 14 jours (frequences_ia) : C la garde pour de bon,
              R l'oublie et elle ne sera plus mise en surveillance.
Un examen qui propose un nettoyage ET un nom donne deux entrees distinctes : on
peut accepter l'un et refuser l'autre.

Fiche vocale : chaque entree a un petit fichier audio (radio_log/propositions_audio/
fiche_*.mp3), dit avec la voix de la veille radio, qui resume ce qui est propose et
pourquoi. Il est joue quand on arrive sur l'entree, et retire quand elle est
acceptee ou refusee (ou disparait d'elle-meme).

Nouveautes : une entree dont la fiche n'a pas encore ete ecoutee compte dans la
commande "notifications" ; rien n'est range dans le journal radio.
"""
import json
import os
import re
import threading
import time

import chemins
from sound_paths import RADIO_LOG_DIR

AUDIO_DIR = os.path.join(RADIO_LOG_DIR, "propositions_audio")
VUES = chemins.base("revue_vues.json")
_verrou = threading.Lock()


# --- les entrees -------------------------------------------------------------------------

def _epoch(le):
    if isinstance(le, (int, float)):
        return float(le)
    try:
        return time.mktime(time.strptime(str(le)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0.0


def _mhz(freq_hz):
    return f"{float(freq_hz or 0) / 1e6:.4f}".rstrip("0").rstrip(".").replace(".", ",")


def _phrase_finie(t):
    t = " ".join(str(t or "").split()).strip()
    return t if not t or t[-1] in ".!?…" else t + "."


def texte(entree):
    """Ce que dit la fiche vocale : clair, court, la proposition et son pourquoi."""
    from phrases import dire
    g, p = entree["genre"], entree["item"]
    if g == "nettoyage":
        resume = re.sub(r"^Examen demandé\s*[—-]\s*", "", str(p.get("resume") or "")).strip()
        traits = p.get("traitements") or []
        if traits:
            return dire("revue.fiche_nettoyage", resume=_phrase_finie(resume),
                        niveau=traits[0].get("niveau") or "")
        return dire("revue.fiche_reglage", resume=_phrase_finie(resume))
    if g == "nommage":
        bande = p.get("bande") or ""
        if p.get("ancien_nom"):
            return dire("revue.fiche_renommage", ancien=p["ancien_nom"], nom=p.get("nom") or "",
                        pourquoi=p.get("pourquoi") or "", mhz=_mhz(p.get("freq_hz")),
                        explication=_phrase_finie(p.get("explication")))
        return dire("revue.fiche_nom", nom=p.get("nom") or "", pourquoi=p.get("pourquoi") or "",
                    mhz=_mhz(p.get("freq_hz")), bande=bande, explication=_phrase_finie(p.get("explication")))
    # frequence trouvee
    source = str(p.get("source") or "")
    source = ("un échange transcrit par Whisper" if source.startswith("analyse")
              else "un message décodé" if source.startswith("message") else source or "un texte")
    if p.get("freq_source"):
        source += dire("revue.entendu_sur", mhz=_mhz(p["freq_source"]))
    morceaux = [dire("revue.fiche_frequence", mhz=_mhz(p.get("freq_hz")),
                     modulation=(", " + p["modulation"]) if p.get("modulation") else "", source=source)]
    if p.get("phrase"):
        morceaux.append(dire("revue.extrait", citee=_phrase_finie(p["phrase"])))
    morceaux.append(dire("revue.surveillance"))
    return " ".join(morceaux)


def entrees():
    """Toutes les entrees en attente, dans l'ordre du temps (comme un journal)."""
    out = []
    try:
        import propositions_reglages as pr
        out += [{"genre": "nettoyage", "id": f"n{p.get('id')}", "le": _epoch(p.get("le")), "item": p}
                for p in pr.entries()]
    except Exception as exc:
        print(f"[revue] nettoyages : {exc!r}")
    try:
        import nommage
        out += [{"genre": "nommage", "id": f"m{a.get('id')}", "le": _epoch(a.get("le")), "item": a}
                for a in nommage.en_attente()]
    except Exception as exc:
        print(f"[revue] noms : {exc!r}")
    try:
        import frequences_ia
        out += [{"genre": "frequence", "id": f"f{f.get('id')}", "le": _epoch(f.get("le")), "item": f}
                for f in frequences_ia.a_revoir()]
    except Exception as exc:
        print(f"[revue] frequences : {exc!r}")
    out.sort(key=lambda e: e["le"])
    return out


# --- fiches vocales ----------------------------------------------------------------------

def _nom_fiche(entree):
    sur = re.sub(r"[^A-Za-z0-9_-]", "_", entree["id"])[:60]
    return f"fiche_{entree['genre']}_{sur}.mp3"


def chemin_fiche(entree):
    return os.path.join(AUDIO_DIR, _nom_fiche(entree))


def fiche(entree, creer=True):
    """Chemin de la fiche vocale (fabriquee si besoin), ou None."""
    path = chemin_fiche(entree)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    if not creer:
        return None
    try:
        import prise_vocale
        os.makedirs(AUDIO_DIR, exist_ok=True)
        tmp = path[:-4] + f".{os.getpid()}.tmp.mp3"
        if prise_vocale.synthesize(texte(entree), tmp, source="radio"):
            os.replace(tmp, path)
            return path
    except Exception as exc:
        print(f"[revue] fiche vocale : {exc!r}")
    return None


def preparer(genre, item):
    """Fabrique la fiche d'une entree qui vient d'arriver, sans bloquer."""
    prefixe = {"nettoyage": "n", "nommage": "m", "frequence": "f"}[genre]
    entree = {"genre": genre, "id": f"{prefixe}{item.get('id')}", "le": _epoch(item.get("le")), "item": item}
    threading.Thread(target=fiche, args=(entree,), daemon=True).start()


def retirer_fiche(entree):
    try:
        os.remove(chemin_fiche(entree))
    except OSError:
        pass


def menage(liste=None):
    """Retire les fiches des entrees acceptees ou refusees ailleurs (journal des
    noms, frequence devenue permanente ou expiree)."""
    liste = entrees() if liste is None else liste
    gardees = {_nom_fiche(e) for e in liste}
    try:
        for nom in os.listdir(AUDIO_DIR):
            if nom.startswith("fiche_") and nom not in gardees:
                try:
                    os.remove(os.path.join(AUDIO_DIR, nom))
                except OSError:
                    pass
    except OSError:
        pass
    with _verrou:
        vues = _lire_vues()
        ids = {e["id"] for e in liste}
        if set(vues) - ids:
            _ecrire_vues({k: v for k, v in vues.items() if k in ids})


# --- nouveautes (commande "notifications") ------------------------------------------------

def _lire_vues():
    try:
        with open(VUES, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ecrire_vues(data):
    try:
        tmp = VUES + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, VUES)
    except OSError as exc:
        print(f"[revue] vues non ecrites : {exc}")


def marquer_entendue(entree):
    with _verrou:
        vues = _lire_vues()
        vues[entree["id"]] = time.time()
        _ecrire_vues(vues)


def nouvelles():
    """Nombre d'entrees en attente dont la fiche n'a pas encore ete ecoutee."""
    vues = _lire_vues()
    return sum(1 for e in entrees() if e["id"] not in vues)


# --- accepter / refuser --------------------------------------------------------------------

def accepter(entree, niveau=None):
    """Rend la phrase a dire."""
    from phrases import dire
    g, p = entree["genre"], entree["item"]
    retirer_fiche(entree)
    if g == "nettoyage":
        import propositions_reglages as pr
        traits = p.get("traitements") or []
        action = traits[niveau]["action"] if (traits and niveau is not None and niveau < len(traits)) else p.get("action")
        ok, detail = pr.apply(action)
        pr.set_etat(p["id"], "confirme")
        brut = p.get("brut") or ""
        if brut.startswith("rejets" + os.sep):   # l'IA a entendu une voix dans un rejet : repeche
            try:
                import rejets
                rejets.repecher(os.path.basename(brut))
            except Exception as exc:
                print(f"[revue] repechage : {exc!r}")
        return dire("proposition.confirmee", detail=detail) if ok else dire("proposition.confirmee_manuelle",
                                                                            detail=detail)
    if g == "nommage":
        import nommage
        if nommage.confirmer(p):
            return dire("nommage.confirme", nom=p.get("nom") or "")
        return dire("nommage.deja")
    import frequences_ia
    frequences_ia.garder(p)
    return dire("revue.frequence_gardee", mhz=_mhz(p.get("freq_hz")))


def refuser(entree):
    from phrases import dire
    g, p = entree["genre"], entree["item"]
    retirer_fiche(entree)
    if g == "nettoyage":
        import propositions_reglages as pr
        pr.set_etat(p["id"], "refuse")
        return dire("proposition.refusee")
    if g == "nommage":
        import nommage
        nommage.refuser(p)
        return dire("nommage.refuse", nom=p.get("nom") or "")
    import frequences_ia
    frequences_ia.oublier(p)
    return dire("revue.frequence_oubliee", mhz=_mhz(p.get("freq_hz")))
