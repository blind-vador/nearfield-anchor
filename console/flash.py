"""Commande "flash" : flash d'info locale (lib/flash_info.py).

  "flash", "info locale", "quoi de neuf"...   le flash, lu comme une reponse
                                               (toute touche coupe, "repete" le relit)
  "flash details"                              les sujets du jour par rubrique,
                                               dans l'arborescence habituelle :
                                               rubrique > sujet > articles
"""
import sys


import arbre  # noqa: E402
import flash_info  # noqa: E402
import matcher  # noqa: E402
from phrases import dire, nombre  # noqa: E402

DETAIL_WORDS = {"detail", "details", "detaille", "rubrique", "rubriques", "sujets"}
RUBRIQUE_ORDER = ["À la une", "Faits divers et justice", "Politique et société", "Économie",
                  "Transports", "Sport", "Culture et sorties", "Autres", "Autres infos"]


def _media_count(n):
    return nombre(n, "média")


def _details(built, key_source, say, out):
    stories = built["sujets"]
    if not stories:
        return dire("flash.aucun_sujet_local_depuis")
    groups = {}
    index = []
    for story in stories:
        articles = [arbre.Node(
            dire("flash.article", source=a['source'], depuis=flash_info._hours_ago(a['date']), titre=a['titre'])
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
    arbre.run(nav, key_source, say, out, dire("flash.sujets_jour_rubrique"),
              dire("flash.droite_deplie_gauche_replie"), "flash_details")
    return dire("flash.details_fermes")


def texte_complet(scan=None, scan_failed=None):
    """Flash complet, une fois tout rassemble (console/main.py : flash_cmd)."""
    built = flash_info.build(complet=True, scan=scan, scan_failed=scan_failed)
    text = built["texte"]
    if built["remarque"]:
        text += " " + dire("flash.flash_lu_sans_redaction", remarque=built['remarque'])
    return text  # avec ses reperes de jingles : lu par lecture_longue


def run(line, key_source, say, out):
    words = set(matcher.normalize(line).split())
    say(dire("flash.prepare_flash_info"))
    try:
        built = flash_info.build()
    except Exception as exc:  # une source au format inattendu ne doit pas tuer la console
        print(f"[console] flash : {exc!r}", file=sys.stderr)
        return dire("flash.flash_info_echoue_erreur")
    if words & DETAIL_WORDS:
        return _details(built, key_source, say, out)
    text = built["texte"]
    if built["remarque"]:
        text += " " + dire("flash.flash_lu_sans_redaction", remarque=built['remarque'])
    return text
