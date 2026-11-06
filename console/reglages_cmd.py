"""Commandes de reglage de la console : reglages, ville, gare, aeroport.

Les choix sont ecrits dans nearfield-anchor-settings sur le NAS (voir
lib/reglages.py), puis les veilles concernees sont relancees pour les
prendre en compte.
"""
import sys


import arbre  # noqa: E402
import conseils  # noqa: E402
import matcher  # noqa: E402
import reglages  # noqa: E402
import sources  # noqa: E402
import tableaux  # noqa: E402
import veilles  # noqa: E402
from phrases import dire, nombre  # noqa: E402

VILLE_WORDS = {"ville", "commune"}
AIRPORT_ROLES = ("proche", "grand")
# Les frequences radio tour et approche restent celles de ces deux-la.
RADIO_AIRPORTS = {"LFMH", "LFLL"}


def _role(role):
    return dire(f"reglages.role_{role}")


def _restart(units):
    text = veilles.handle("restart", units)
    return f" {text}" if text else ""


def status():
    migrated = reglages.ensure()
    lieu = reglages.get("lieu")
    gare = reglages.get("trains")
    avions = reglages.get("avions")
    keys = reglages.keys_status()
    missing = [reglages.KEY_LABELS[k] for k, ok in keys.items() if not ok]
    parts = [
        dire("reglages.commune", commune=lieu['commune'], departement=lieu['departement_nom']),
        dire("reglages.gare", gare=gare['gare']),
        dire("reglages.aeroport_proche_grand_aeroport", nom=avions['proche']['nom'], nom2=avions['grand']['nom']),
        (dire("reglages.toutes_cles_sont_presentes") if not missing
         else dire("reglages.cles_presentes_manquantes", v1=len(keys) - len(missing), len=len(keys), join=', '.join(missing))),
    ]
    import connecteurs_ia
    autres = connecteurs_ia.actifs()
    if autres:
        parts.append(dire("reglages.connecteurs_ia", liste=", ".join(autres)))
    if migrated:
        parts.append(dire("reglages.cles_recopiees", cles=nombre(len(migrated), "clé recopiée", "clés recopiées")))
    if migrated is None:
        parts.append(dire("reglages.attention_dossier_reglages_nas"))
    else:
        parts.append(dire("reglages.fichiers_sont_partage_server"))
    return " ".join(parts)


def _commune_label(c):
    habitants = f", {c['population']} habitants" if c["population"] else ""
    return f"{c['nom']}, {c['departement_nom'] or c['departement']}{habitants}"


def ville(line, key_source, say, out, read_line):
    query = " ".join(w for w in line.split() if matcher.normalize(w) not in VILLE_WORDS).strip()
    if not query:
        prompt = conseils.intro("ville", dire("reglages.nom_ta_commune"), dire("reglages.tape_puis_entree_entree"))
        out(prompt + "\n? ")
        say(prompt, lock=True)
        query = read_line(key_source).strip()
        if not query:
            return dire("commun.annule")
    say(dire("reglages.recherche", query=query))
    found = sources.communes(query)
    if found is None:
        return dire("reglages.service_communes_repond_verifie")
    if not found:
        return dire("reglages.aucune_commune_trouvee", query=query)
    if len(found) == 1:
        title = dire("reglages.commune_trouvee")
    else:
        title = dire("reglages.communes_trouvees", communes=nombre(len(found), "commune trouvée", "communes trouvées"))
    index = arbre.run_list([_commune_label(c) for c in found], key_source, say, out, title)
    if index is None:
        return dire("reglages.annule_commune_inchangee")
    c = found[index]
    ok = reglages.save("lieu", {"commune": c["nom"], "code_insee": c["code_insee"],
                                "departement": c["departement"], "departement_nom": c["departement_nom"],
                                "lat": c["lat"], "lon": c["lon"]})
    if not ok:
        return dire("reglages.no_nas")
    _find_transports(say)
    return (dire("reglages.commune_enregistree_vigilance_meteo", nom=c['nom'], v1=c['departement_nom'] or c['departement'])
            + _restart(["meteo-watch", "adsb-watch"]) + " " + dire("reglages.transports_en_recherche"))


def _find_transports(say):
    """Nouvelle commune : ses reseaux de transport (lib/transports.py) sont
    cherches en arriere-plan, le resultat est dit quand il arrive."""
    import threading

    def job():
        try:
            import transports
            import transports_cmd
            transports.discover(reglages.get("lieu"))
            say(transports_cmd.discovery_summary())
        except Exception as exc:
            print(f"[reglages] transports : {exc!r}")
            say(dire("reglages.transports_echec"))
    threading.Thread(target=job, daemon=True).start()


def gare(key_source, say, out):
    say(dire("reglages.chargement_liste_gares"))
    nav = tableaux.station_navigator(board=False, select=lambda node: node.item)
    if nav is None:
        return dire("reglages.liste_gares_indisponible_reseau")
    current = reglages.get("trains")["gare"]
    title = dire("reglages.gares_favoris_tete_puis", current=current)
    chosen = arbre.run(nav, key_source, say, out, title, dire("reglages.choose_keys"), "choix_gare")
    if chosen is None:
        return dire("reglages.annule_gare_inchangee")
    if not reglages.save("trains", {"gare": chosen["nom"], "uic": chosen["uic"]}):
        return dire("reglages.no_nas")
    return dire("reglages.gare_enregistree", nom=chosen['nom']) + _restart(["sncf-watch"])


def aeroport(line, key_source, say, out):
    words = set(matcher.normalize(line).split())
    avions = reglages.get("avions")
    if words & {"grand", "gros"}:
        role = "grand"
    elif words & {"proche", "petit", "local"}:
        role = "proche"
    else:
        roles = list(AIRPORT_ROLES)
        labels = [dire("reglages.role_actuellement", role=_role(r), nom=avions[r]['nom']) for r in roles]
        index = arbre.run_list(labels, key_source, say, out, dire("reglages.quel_aeroport_changer"))
        if index is None:
            return dire("reglages.annule_aeroports_inchanges")
        role = roles[index]
    say(dire("reglages.chargement_liste_aerodromes"))
    nav = tableaux.airport_navigator(board=False, select=lambda node: node.item)
    if nav is None:
        return dire("reglages.liste_aerodromes_indisponible_reseau")
    title = dire("reglages.choix_aeroport_titre", role=_role(role), nom=avions[role]['nom'])
    chosen = arbre.run(nav, key_source, say, out, title, dire("reglages.choose_keys"), "choix_aeroport")
    if chosen is None:
        return dire("reglages.annule_aeroport_inchange")
    avions[role] = {"nom": chosen["nom"], "oaci": chosen["oaci"],
                    "lat": chosen["lat"], "lon": chosen["lon"]}
    if not reglages.save("avions", avions):
        return dire("reglages.no_nas")
    text = dire("reglages.aeroport_enregistre", role=_role(role), nom=chosen['nom'])
    if chosen["oaci"] not in RADIO_AIRPORTS:
        text += (" " + dire("reglages.contexte_aerien_suit_mais"))
    return text + _restart(["adsb-watch"])
