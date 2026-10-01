"""Commande "flash" : flash d'info locale (lib/flash_info.py).

  "flash", "info locale", "quoi de neuf"...   le flash, lu comme une reponse
                                               (toute touche coupe, "repete" le relit)
  "flash details"                              les sujets du jour par rubrique,
                                               dans l'arborescence habituelle :
                                               rubrique > sujet > articles
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import arbre  # noqa: E402
import flash_info  # noqa: E402
import matcher  # noqa: E402

DETAIL_WORDS = {"detail", "details", "detaille", "rubrique", "rubriques", "sujets"}
RUBRIQUE_ORDER = ["À la une", "Faits divers et justice", "Politique et société", "Économie",
                  "Transports", "Sport", "Culture et sorties", "Autres", "Autres infos"]


def _media_count(n):
    return f"{n} média{'s' if n > 1 else ''}"


def _details(built, key_source, say, out):
    stories = built["sujets"]
    if not stories:
        return "Aucun sujet local depuis hier."
    groups = {}
    index = []
    for story in stories:
        articles = [arbre.Node(
            f"{a['source']}, il y a {flash_info._hours_ago(a['date'])} : {a['titre']}."
            + (f" {a['resume']}" if a["resume"] else ""))
            for a in sorted(story["articles"], key=lambda a: -a["date"])]
        node = arbre.Node(f"{story['titre']}, {_media_count(story['medias'])}",
                          children=articles, unit=("article", "articles"))
        groups.setdefault(story["rubrique"], []).append(node)
        index.append((matcher.normalize(story["titre"]), node))
    order = sorted(groups, key=lambda r: RUBRIQUE_ORDER.index(r) if r in RUBRIQUE_ORDER else len(RUBRIQUE_ORDER))
    roots = [arbre.Node(f"{r}, {len(groups[r])} sujet{'s' if len(groups[r]) > 1 else ''}",
                        children=groups[r], unit=("sujet", "sujets")) for r in order]
    index.sort(key=lambda p: p[0])
    nav = arbre.Navigator(lambda: roots, index)
    arbre.run(nav, key_source, say, out, "Sujets du jour par rubrique.",
              "Droite déplie, gauche replie, haut et bas pour parcourir, tape des mots pour chercher "
              "un sujet, échap pour fermer. F1 redit ces touches.", "flash_details")
    return "Détails fermés."


def run(line, key_source, say, out):
    words = set(matcher.normalize(line).split())
    say("Je prépare le flash info.")
    try:
        built = flash_info.build()
    except Exception as exc:  # une source au format inattendu ne doit pas tuer la console
        print(f"[console] flash : {exc!r}", file=sys.stderr)
        return "Le flash info a échoué sur une erreur inattendue."
    if words & DETAIL_WORDS:
        return _details(built, key_source, say, out)
    text = built["texte"]
    if built["remarque"]:
        text += f" Flash lu sans rédaction : {built['remarque']}."
    return text
