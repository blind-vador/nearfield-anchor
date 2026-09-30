"""Commandes de reglage de la console : reglages, ville, gare, aeroport.

Les choix sont ecrits dans nearfield-anchor-settings sur le NAS (voir
lib/reglages.py), puis les veilles concernees sont relancees pour les
prendre en compte.
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import arbre  # noqa: E402
import matcher  # noqa: E402
import reglages  # noqa: E402
import sources  # noqa: E402
import tableaux  # noqa: E402
import veilles  # noqa: E402

VILLE_WORDS = {"ville", "commune"}
AIRPORT_ROLES = {"proche": "Aéroport proche", "grand": "Grand aéroport"}
# Les frequences radio tour et approche restent celles de ces deux-la.
RADIO_AIRPORTS = {"LFMH", "LFLL"}
NO_NAS = "Le disque du NAS est inaccessible : rien n'a été enregistré."


def _restart(units):
    text = veilles.handle("restart", units)
    return f" {text}" if text else ""


def status():
    migrated = reglages.ensure()
    lieu = reglages.get("lieu")
    gare = reglages.get("trains")
    avions = reglages.get("avions")
    keys = reglages.keys_status()
    missing = [reglages.KEYS[k][1] for k, ok in keys.items() if not ok]
    parts = [
        f"Commune : {lieu['commune']}, {lieu['departement_nom']}.",
        f"Gare : {gare['gare']}.",
        f"Aéroport proche : {avions['proche']['nom']}. Grand aéroport : {avions['grand']['nom']}.",
        ("Toutes les clés sont présentes." if not missing
         else f"Clés présentes : {len(keys) - len(missing)} sur {len(keys)}. "
              f"Manquantes : {', '.join(missing)}."),
    ]
    if migrated:
        parts.append(f"{len(migrated)} clé{'s' if len(migrated) > 1 else ''} "
                     "recopiée" + ("s" if len(migrated) > 1 else "") + " depuis l'ancien fichier credentials.")
    if migrated is None:
        parts.append("Attention, le dossier de réglages du NAS est inaccessible, "
                     "valeurs lues depuis la copie du Pi.")
    else:
        parts.append("Les fichiers sont dans le partage server, dossier nearfield-anchor-settings.")
    return " ".join(parts)


def _commune_label(c):
    habitants = f", {c['population']} habitants" if c["population"] else ""
    return f"{c['nom']}, {c['departement_nom'] or c['departement']}{habitants}"


def ville(line, key_source, say, out, read_line):
    query = " ".join(w for w in line.split() if matcher.normalize(w) not in VILLE_WORDS).strip()
    if not query:
        prompt = "Tape le nom de ta commune, puis entrée. Entrée seule pour annuler."
        out(prompt + "\n? ")
        say(prompt, lock=True)
        query = read_line(key_source).strip()
        if not query:
            return "Annulé."
    say(f"Recherche de {query}.")
    found = sources.communes(query)
    if found is None:
        return "Le service des communes ne répond pas. Vérifie le réseau."
    if not found:
        return f"Aucune commune trouvée pour {query}."
    if len(found) == 1:
        intro = "Une commune trouvée, entrée pour la choisir, échap pour annuler."
    else:
        intro = (f"{len(found)} communes trouvées, les plus peuplées d'abord. "
                 "Flèches haut et bas, entrée pour choisir, échap pour annuler.")
    index = arbre.run_list([_commune_label(c) for c in found], key_source, say, out, intro)
    if index is None:
        return "Annulé, commune inchangée."
    c = found[index]
    ok = reglages.save("lieu", {"commune": c["nom"], "code_insee": c["code_insee"],
                                "departement": c["departement"], "departement_nom": c["departement_nom"],
                                "lat": c["lat"], "lon": c["lon"]})
    if not ok:
        return NO_NAS
    return (f"Commune enregistrée : {c['nom']}. La vigilance météo suit maintenant "
            f"le département {c['departement_nom'] or c['departement']}."
            + _restart(["meteo-watch", "adsb-watch"]))


def gare(key_source, say, out):
    say("Chargement de la liste des gares.")
    nav = tableaux.station_navigator(board=False, select=lambda node: node.item)
    if nav is None:
        return "Liste des gares indisponible : pas de réseau et pas de copie sur le NAS."
    current = reglages.get("trains")["gare"]
    intro = (f"Gares, favoris en tête puis par ordre alphabétique. Gare actuelle : {current}. "
             "Droite déplie une lettre, gauche la replie, tape des lettres pour chercher, "
             "étoile pour les favoris, entrée choisit, échap annule.")
    chosen = arbre.run(nav, key_source, say, out, intro)
    if chosen is None:
        return "Annulé, gare inchangée."
    if not reglages.save("trains", {"gare": chosen["nom"], "uic": chosen["uic"]}):
        return NO_NAS
    return f"Gare enregistrée : {chosen['nom']}." + _restart(["sncf-watch"])


def aeroport(line, key_source, say, out):
    words = set(matcher.normalize(line).split())
    avions = reglages.get("avions")
    if words & {"grand", "gros"}:
        role = "grand"
    elif words & {"proche", "petit", "local"}:
        role = "proche"
    else:
        roles = list(AIRPORT_ROLES)
        labels = [f"{AIRPORT_ROLES[r]}, actuellement {avions[r]['nom']}" for r in roles]
        index = arbre.run_list(labels, key_source, say, out,
                               "Quel aéroport changer ? Flèches haut et bas, entrée, échap pour annuler.")
        if index is None:
            return "Annulé, aéroports inchangés."
        role = roles[index]
    say("Chargement de la liste des aérodromes.")
    nav = tableaux.airport_navigator(board=False, select=lambda node: node.item)
    if nav is None:
        return "Liste des aérodromes indisponible : pas de réseau et pas de copie sur le NAS."
    intro = (f"{AIRPORT_ROLES[role]}, actuellement {avions[role]['nom']}. Aérodromes français, "
             "favoris en tête puis par ordre alphabétique. Droite déplie une lettre, gauche la replie, "
             "tape des lettres pour chercher, étoile pour les favoris, entrée choisit, échap annule.")
    chosen = arbre.run(nav, key_source, say, out, intro)
    if chosen is None:
        return "Annulé, aéroport inchangé."
    avions[role] = {"nom": chosen["nom"], "oaci": chosen["oaci"],
                    "lat": chosen["lat"], "lon": chosen["lon"]}
    if not reglages.save("avions", avions):
        return NO_NAS
    text = f"{AIRPORT_ROLES[role]} enregistré : {chosen['nom']}."
    if chosen["oaci"] not in RADIO_AIRPORTS:
        text += (" Le contexte aérien le suit, mais le scanner écoute encore "
                 "les fréquences de Saint-Étienne et Lyon.")
    return text + _restart(["adsb-watch"])
