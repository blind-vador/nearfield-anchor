"""Commandes "bus", "tram", "metro", "transports" (lib/transports.py) :
comment roulent les transports en commun de la commune choisie.

  bus / tram / metro    ponctualite en temps reel, lignes les plus en
                        retard, courses annulees, perturbations
  transports            toutes les familles du reseau ; "transports
                        funiculaire", "transports telecabine"...
  transports reseaux    les reseaux trouves pour la commune :
      haut / bas        parcourir
      Entree ou Espace  activer / desactiver
      I                 details : familles, temps reel, sources non lues
      R                 chercher de nouveau (apres un changement de ville)
      Echap             fermer
"""
import sys


import conseils  # noqa: E402
import matcher  # noqa: E402
import reglages  # noqa: E402
import transports  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN, ESC = "\x1b[A", "\x1b[B", "\x1b"
WORDS = {"bus": "bus", "autobus": "bus", "trolleybus": "bus", "trolley": "bus", "car": "bus", "cars": "bus",
         "tram": "tram", "trams": "tram", "tramway": "tram", "tramways": "tram",
         "metro": "metro", "metros": "metro", "funiculaire": "funiculaire",
         "telecabine": "cable", "telepherique": "cable", "navette": "bateau", "bateau": "bateau", "bateaux": "bateau"}
NETWORK_WORDS = {"reseau", "reseaux", "choisir", "chercher", "cherche", "sources"}


def group_in(line):
    return next((WORDS[w] for w in matcher.normalize(line).split() if w in WORDS), None)


def _ensure(say):
    """Reseaux de la commune choisie, cherches s'ils ne le sont pas encore."""
    lieu = reglages.get("lieu")
    if transports.settings(lieu) is None:
        say(dire("transport.recherche", commune=lieu.get("commune")))
        transports.discover(lieu)
        return lieu, True
    return lieu, False


def mode_status(group, say):
    lieu, fresh = _ensure(say)
    say(dire("transport.je_regarde", famille=transports.GROUPS[group]))
    text = transports.status(group, lieu)
    if fresh:
        text = discovery_summary(lieu) + " " + text
    return text


def discovery_summary(lieu=None):
    data = transports.settings(lieu) or {}
    nets = data.get("reseaux") or []
    if not nets:
        return dire("transport.aucun_reseau_trouve", commune=data.get("commune") or "")
    on = [transports._short_name(n["nom"]) for n in nets if n.get("actif")]
    return dire("transport.trouves", nombre=nombre(len(nets), "réseau"), commune=data.get("commune") or "",
                actifs=", ".join(on) or dire("transport.aucun"))


def _line(n):
    scope = dire(f"transport.portee_{n['portee']}") if n["portee"] in ("locale", "departement", "region") else n["portee"]
    return dire("transport.ligne_reseau", nom=n["nom"], portee=scope,
                etat=dire("transport.actif") if n.get("actif") else dire("transport.inactif"),
                temps_reel=dire("transport.avec_temps_reel") if n.get("temps_reel") else dire("transport.sans_temps_reel"))


def _details(n):
    parts = [_line(n) + "."]
    try:
        fams = [transports.GROUPS[g] for g in transports.GROUPS if g in transports.families(n)]
        if fams:
            parts.append(dire("transport.familles", liste=", ".join(fams)))
    except Exception as exc:
        print(f"[transports] details : {exc!r}")
    names = {"trip_updates": dire("transport.contenu_retards"), "service_alerts": dire("transport.contenu_alertes"),
             "vehicle_positions": dire("transport.contenu_positions")}
    contents = sorted({names.get(c, c) for rt in n.get("temps_reel") or [] for c in rt.get("contenu") or []})
    if contents:
        parts.append(dire("transport.contenu_temps_reel", liste=", ".join(contents)))
    if n.get("page"):
        parts.append(dire("transport.page_exploitant"))
    if n.get("siri_lite"):
        services = sorted({s["service"] for s in n["siri_lite"]})
        parts.append(dire("transport.siri_lite_lu", services=", ".join(services)))
        for s in n["siri_lite"]:
            if s.get("cle") and not reglages.cle(s["cle"]):
                parts.append(dire("transport.cle_manquante", fichier=f"{s['cle']}.txt"))
                break
    if n.get("siri"):
        parts.append(dire("transport.siri_non_lu"))
    return " ".join(parts)


def networks(key_source, say, out):
    lieu = reglages.get("lieu")
    if transports.settings(lieu) is None:
        say(dire("transport.recherche", commune=lieu.get("commune")))
        transports.discover(lieu)
    data = transports.settings(lieu)
    nets = data.get("reseaux") or []
    i = 0
    intro = conseils.intro("transports_reseaux", discovery_summary(lieu), dire("transport.reseaux_consigne"))
    out(intro + "\n")
    say(intro + (" " + _line(nets[0]) if nets else ""))
    for key in key_source:
        voix.stop()
        if key == ESC:
            return dire("transport.reseaux_fermes")
        if conseils.is_help(key):
            text = dire("transport.reseaux_consigne")
        elif key in ("r", "R"):
            say(dire("transport.recherche", commune=lieu.get("commune")))
            data = transports.discover(lieu)
            nets, i = data.get("reseaux") or [], 0
            text = discovery_summary(lieu) + (" " + _line(nets[0]) if nets else "")
        elif not nets:
            text = dire("transport.aucun_reseau_trouve", commune=lieu.get("commune"))
        elif key in (UP, DOWN):
            j = i + (-1 if key == UP else 1)
            if 0 <= j < len(nets):
                i = j
                text = _line(nets[i])
            else:
                text = (dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")) + " " + _line(nets[i])
        elif key in ("\r", "\n", " "):
            nets[i]["actif"] = not nets[i].get("actif")
            data["reseaux"] = nets
            text = _line(nets[i]) if reglages.save("transports", data) else dire("plantes.nas_inaccessible")
        elif key in ("i", "I"):
            text = _details(nets[i])
        else:
            text = dire("plantes.touches")
        out(f"[{text}]\n")
        say(text)
    return dire("transport.reseaux_fermes")


def run(name, line, key_source, say, out):
    words = set(matcher.normalize(line).split())
    if name == "transports" and words & NETWORK_WORDS:
        return networks(key_source, say, out)
    group = {"bus": "bus", "tram": "tram", "métro": "metro"}.get(name) or group_in(line)
    if group:
        return mode_status(group, say)
    lieu, fresh = _ensure(say)
    groups = transports.available_groups(lieu)
    if not groups:
        return (discovery_summary(lieu) + " " if fresh else "") + dire("transport.aucune_famille", commune=lieu.get("commune"))
    say(dire("transport.je_regarde_tout"))
    text = " ".join(transports.status(g, lieu) for g in groups)
    return ((discovery_summary(lieu) + " ") if fresh else "") + text + " " + dire("transport.dis_reseaux")
