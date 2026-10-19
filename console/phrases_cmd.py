"""Commande "phrases" (2026-10-19) : les phrases gardees en cache par les
voix (console et annonces, lib/cache_voix.py), pour reprendre celles que
Piper a bafouillees -- sinon elles seraient rejouees telles quelles a chaque
fois. Arborescence au clavier :

  voix en cache        nombre de phrases, dont validees
    veille             gare, avions, console... (nombre de phrases)
      type de phrases  quand la veille en a plusieurs (imprimante, touches...)
        phrase         Entree : l'ecouter ; r : la recalculer et l'ecouter
                       (autant de fois que voulu) ; v : la valider (ou
                       retirer la validation) ; retour arriere : l'effacer

Retour arriere sur une voix, une veille ou un type efface toutes leurs
phrases apres confirmation (deuxieme appui) ; les phrases validees restent.
Une phrase effacee est recalculee la prochaine fois qu'elle est dite, ou par
la preparation des voix.
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import arbre  # noqa: E402
import cache_voix  # noqa: E402
import conseils  # noqa: E402
import voix_sources  # noqa: E402
from phrases import dire, nombre  # noqa: E402

FEATURE = "phrases_cache"
QUALITES = {"x_low": "très basse", "low": "basse", "medium": "moyenne", "high": "haute"}


def _n(k):
    return nombre(k, "phrase")


def _compte(entrees, valides):
    v = sum(1 for e in entrees if e.chemin in valides)
    return _n(len(entrees)) + (", " + dire("phrases_cache.dont_validees", nombre=v) if v else "")


def _nom_source(source):
    if source in voix_sources.SOURCES:
        return voix_sources.SOURCES[source]
    return dire(f"phrases_cache.groupe_{source}")


def _nom_famille(fam):
    if fam in ("autres", "morceaux"):
        return dire(f"phrases_cache.famille_{fam}")
    return fam.replace("_", " ")


def _label_phrase(e, valides, rang):
    texte = e.texte if e.texte is not None else dire("phrases_cache.sans_texte", numero=rang)
    extras = []
    if e.debit != cache_voix.NEUTRE:
        extras.append(dire("phrases_cache.debit", debit=e.debit.replace("%", " %")))
    if e.chemin in valides:
        extras.append(dire("phrases_cache.validee"))
    return ", ".join([texte] + extras)


def _feuilles(entrees, valides):
    entrees = sorted(entrees, key=lambda e: (e.texte is None, (e.texte or "").lower(), e.debit, e.cache))
    return [arbre.Node(_label_phrase(e, valides, i + 1), item=e) for i, e in enumerate(entrees)]


def _groupe(label, entrees, children):
    node = arbre.Node(label, children=children)
    node.entrees = entrees
    return node


def construire(entrees, valides):
    par_voix = {}
    for e in entrees:
        par_voix.setdefault(e.voix, []).append(e)
    racines = []
    noms = {v: voix_sources.nom_voix(v) for v in par_voix}
    doubles = [n for n in noms.values() if list(noms.values()).count(n) > 1]
    for v, nom in list(noms.items()):  # deux voix du meme nom (Siwis moyenne et basse) : la qualite les distingue
        qualite = v.split("#")[0].rsplit("-", 1)[-1] if v.startswith("piper:") else ""
        if nom in doubles and qualite in QUALITES:
            noms[v] = f"{nom} {dire('phrases_cache.qualite', qualite=QUALITES[qualite])}"
    for voix in sorted(par_voix, key=lambda v: (-len(par_voix[v]), v)):
        ev = par_voix[voix]
        par_source = {}
        for e in ev:
            par_source.setdefault(e.source, []).append(e)
        sources = []
        for source in sorted(par_source, key=lambda s: (s in ("inconnues", "annonces", "console"), s)):
            es = par_source[source]
            par_fam = {}
            for e in es:
                par_fam.setdefault(e.famille or "autres", []).append(e)
            if len(par_fam) > 1:
                children = [_groupe(f"{_nom_famille(f)}, {_compte(par_fam[f], valides)}", par_fam[f],
                                    _feuilles(par_fam[f], valides))
                            for f in sorted(par_fam, key=lambda f: (f in ("autres", "morceaux"), f))]
            else:
                children = _feuilles(es, valides)
            sources.append(_groupe(f"{_nom_source(source)}, {_compte(es, valides)}", es, children))
        nom = noms[voix]
        genre = dire("phrases_cache.locale") if voix.startswith("piper:") else dire("phrases_cache.microsoft")
        racines.append(_groupe(f"{nom}, {genre}, {_compte(ev, valides)}", ev, sources))
    return racines


def _chemin(nav):
    """Position du curseur : indices depuis la racine (pour s'y replacer)."""
    path, node = [], nav.node
    while node is not None:
        siblings = node.parent.loaded() if node.parent else nav.roots
        path.insert(0, next(i for i, n in enumerate(siblings) if n is node))
        node = node.parent
    return path


def _replacer(nav, path):
    siblings, node = nav.roots, None
    for i in path:
        if not siblings:
            break
        node = siblings[min(i, len(siblings) - 1)]
        siblings = node.loaded()
    nav.node = node or nav.roots[0]


def _jouer(path):
    import audio_lock
    import sound_player
    import voix
    voix.stop()
    lock = audio_lock.acquire("console", timeout=10)
    try:
        sound_player.play_sound_effect(path, timeout_sec=120, label="phrases")
    finally:
        audio_lock.release(lock)


def run(key_source, say, out):
    import voix_cmd
    say(dire("phrases_cache.chargement"))
    entrees = cache_voix.inventaire(voix_cmd.echo_texts())
    if not entrees:
        return dire("phrases_cache.vide")
    valides = cache_voix.validees()
    roots = construire(entrees, valides)
    nav = arbre.Navigator(lambda: roots, [])
    consigne = dire("phrases_cache.consigne")
    intro = conseils.intro(FEATURE, dire("phrases_cache.titre", voix=len(roots)), consigne)
    out(intro + "\n")
    say(f"{intro} {nav.current()}")

    def rebatir(position):
        nonlocal roots, valides
        valides = cache_voix.validees()
        roots = construire([e for e in entrees if _existe(e)], valides)
        nav.roots, nav.build_roots = roots, (lambda: roots)
        if roots:
            _replacer(nav, position)

    a_confirmer = None
    for key in key_source:
        node = nav.node
        e = node.item
        confirmer, a_confirmer = a_confirmer, None
        if key == arbre.ESCAPE:
            return dire("phrases_cache.ferme")
        if key in arbre.ENTER:
            if e is not None:
                _jouer(e.chemin)
                continue
            text = nav.expand()
        elif key in (arbre.UP, arbre.DOWN, arbre.PAGE_UP, arbre.PAGE_DOWN):
            step = arbre.PAGE if key in (arbre.PAGE_UP, arbre.PAGE_DOWN) else 1
            text = nav.move(-step if key in (arbre.UP, arbre.PAGE_UP) else step)
        elif key == arbre.RIGHT:
            text = nav.expand() if e is None else dire("phrases_cache.consigne_phrase")
        elif key == arbre.LEFT:
            text = nav.collapse()
        elif conseils.is_help(key):
            text = dire("phrases_cache.consigne_phrase") if e is not None else consigne
        elif key in ("v", "V"):
            if e is None:
                text = dire("phrases_cache.v_sur_phrase")
            else:
                oui = e.chemin not in valides
                cache_voix.valider(e, oui)
                rebatir(_chemin(nav))
                text = dire("phrases_cache.validee_ok" if oui else "phrases_cache.validation_retiree")
        elif key in ("r", "R"):
            if e is None:
                text = dire("phrases_cache.r_sur_phrase")
            else:
                say(dire("phrases_cache.recalcul"))
                ok, raison = cache_voix.recalculer(e)
                if ok:
                    rebatir(_chemin(nav))
                    _jouer(e.chemin)
                    text = dire("phrases_cache.recalculee")
                else:
                    text = dire(f"phrases_cache.echec_{raison}")
        elif key in arbre.BACKSPACE:
            position = _chemin(nav)
            if e is not None:
                if e.chemin in valides:
                    text = dire("phrases_cache.validee_protegee")
                else:
                    cache_voix.effacer([e])
                    rebatir(position)
                    text = dire("phrases_cache.phrase_effacee") + (" " + nav.current() if roots else "")
            elif confirmer is node:
                faits, gardes = cache_voix.effacer(node.entrees)
                rebatir(position)
                text = dire("phrases_cache.effacees", nombre=_n(faits))
                if gardes:
                    text += " " + dire("phrases_cache.gardees", nombre=_n(gardes))
                if roots:
                    text += " " + nav.current()
            else:
                a_confirmer = node
                v = sum(1 for x in node.entrees if x.chemin in valides)
                text = dire("phrases_cache.confirmer", nombre=_n(len(node.entrees) - v), nom=node.label.split(",")[0])
            if not roots:
                say(dire("phrases_cache.vide"))
                return dire("phrases_cache.ferme")
        else:
            text = dire("phrases_cache.touches")
        out(f"[{text}]\n")
        say(text)
    return dire("phrases_cache.ferme")


def _existe(e):
    import os
    return os.path.exists(e.chemin)
