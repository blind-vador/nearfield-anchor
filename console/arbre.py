"""Arborescence au clavier (gares, aeroports, tableaux des departs et
arrivees) et liste simple (communes homonymes, choix court).

Arborescence :
  fleches haut / bas     entree precedente / suivante du meme niveau
  page haut / page bas   dix entrees
  fleche droite          deplie (lettre, gare, departs...)
  fleche gauche          replie et remonte d'un niveau
  lettres tapees         recherche : le curseur saute a la premiere entree qui
                         commence par le texte tape, sinon qui le contient
  retour arriere         efface la derniere lettre de la recherche
  etoile                 ajoute ou retire des favoris (en haut de la liste)
  Entree                 deplie, ou choisit l'entree (selon la commande)
  Echap                  ferme, rien n'est enregistre

Une fleche termine la recherche : les lettres tapees ensuite en commencent
une nouvelle.
"""
import conseils
import matcher
from phrases import dire

UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
PAGE_UP, PAGE_DOWN = "\x1b[5~", "\x1b[6~"
ENTER = ("\r", "\n")
BACKSPACE = ("\x7f", "\x08")
ESCAPE = "\x1b"
FAVORITE = "*"
PAGE = 10


class Node:
    """label : texte dit. children : liste, ou loader appele a chaque
    ouverture (tableaux toujours a jour) -- il peut lever LoadError avec
    la phrase a dire. item : donnee rendue si l'entree est choisie. fav :
    identifiant de favori, None si l'entree ne peut pas en etre un. unit :
    ("train", "trains") pour annoncer le nombre d'entrees a l'ouverture."""

    def __init__(self, label, children=None, loader=None, item=None, fav=None,
                 unit=None, empty="Rien ici."):
        self.label, self.loader, self.item, self.fav = label, loader, item, fav
        self.unit, self.empty = unit, empty
        self.parent = None
        self._children = None
        if children is not None:
            self._set_children(children)

    def _set_children(self, children):
        self._children = children
        for child in children:
            child.parent = self

    @property
    def expandable(self):
        return self._children is not None or self.loader is not None

    def open(self):
        if self.loader is not None:
            self._set_children(self.loader())
        return self._children or []

    def loaded(self):
        return self._children or []


class LoadError(Exception):
    """Echec de chargement d'un niveau : le message est dit tel quel."""


def _count(n, unit):
    singular, plural = unit
    return f"{n} {singular if n == 1 else plural}"


def alphabet(items, make_node, unit, aliases=None):
    """Regroupe par premiere lettre. Rend (noeuds lettres, index de
    recherche [(nom normalise, noeud)] trie). aliases(item) : autres
    textes qui trouvent l'entree a la recherche (codes d'aeroport...)."""
    named = sorted(((matcher.normalize(i["nom"]), make_node(i), i) for i in items), key=lambda p: p[0])
    index = [(norm, node) for norm, node, _ in named]
    if aliases:
        for _, node, item in named:
            index.extend((matcher.normalize(a), node) for a in aliases(item) if a)
    groups = {}
    for norm, node, _ in named:
        first = norm[:1].upper()
        groups.setdefault(first if first.isalpha() else "#", []).append(node)
    letters = []
    for letter in sorted(groups, key=lambda l: (l == "#", l)):
        name = "chiffres et symboles" if letter == "#" else letter
        nodes = groups[letter]
        letters.append(Node(f"{name}, {_count(len(nodes), unit)}", children=nodes))
    return letters, index


class Navigator:
    def __init__(self, build_roots, search_index, select=None, toggle_favorite=None):
        """build_roots() : liste du premier niveau, reconstruite apres un
        changement de favori. select(noeud) : appele par Entree sur une
        entree non depliable avec item ; ce qu'il rend termine le dialogue.
        toggle_favorite(noeud) : rend la phrase a dire."""
        self.build_roots = build_roots
        self.roots = build_roots()
        self.index = search_index
        self.select = select
        self.toggle_favorite = toggle_favorite
        self.node = self.roots[0]
        self.query = ""

    def _siblings(self):
        return self.node.parent.loaded() if self.node.parent else self.roots

    def current(self):
        return self.node.label

    def move(self, delta):
        siblings = self._siblings()
        position = next(i for i, n in enumerate(siblings) if n is self.node)
        wanted = position + delta
        position = min(max(wanted, 0), len(siblings) - 1)
        self.node = siblings[position]
        edge = "" if wanted == position else ("Début, " if delta < 0 else "Fin, ")
        return edge + self.current()

    def expand(self):
        node = self.node
        if not node.expandable:
            return f"{node.label}. Rien à déplier."
        try:
            children = node.open()
        except LoadError as exc:
            return str(exc)
        if not children:
            return node.empty
        self.node = children[0]
        head = _count(len(children), node.unit) if node.unit else "déplié"
        return f"{head}. {self.current()}"

    def collapse(self):
        if self.node.parent is None:
            return dire("arbre.deja_au_premier_niveau", current=self.current())
        self.node = self.node.parent
        return dire("arbre.replie", current=self.current())

    def _search(self):
        q = matcher.normalize(self.query)
        if self.query[-1:] in (" ", "-", "'") and q:
            q += " "  # "saint " ne doit trouver que "saint ..." pas "saintes"
        hit = next((n for norm, n in self.index if norm.startswith(q)), None)
        if hit is None:
            hit = next((n for norm, n in self.index if q in norm), None)
        if hit is not None:
            self.node = hit
        return hit is not None

    def type_char(self, ch):
        if ch == " " and not self.query:
            return self.current()
        self.query += ch
        if self._search():
            return self.current()
        typed, self.query = self.query, self.query[:-1]
        return dire("arbre.rien", typed=typed)

    def erase(self):
        if not self.query:
            return dire("arbre.recherche_cours")
        self.query = self.query[:-1]
        if not self.query:
            return dire("arbre.recherche_effacee")
        self._search()
        return f"{self.query} : {self.current()}"

    def favorite(self):
        if self.node.fav is None or self.toggle_favorite is None:
            return dire("arbre.cette_entree_peut_etre")
        text = self.toggle_favorite(self.node)
        was_root = self.node.parent is None
        self.roots = self.build_roots()
        if was_root:
            # Le favori retire (ou reconstruit) en tete de liste : on se
            # replace sur le meme identifiant s'il existe encore.
            fav = self.node.fav
            self.node = next((n for n in self.roots if n.fav == fav), self.roots[0])
        return text


def run(nav, key_source, say, out, title, consigne="", feature=None):
    """Boucle du dialogue. Rend ce que select() a rendu, ou None (Echap).
    consigne : touches du dialogue, dite selon le niveau d'aide (conseils.py)
    et a la demande par F1."""
    intro = conseils.intro(feature, title, consigne)
    out(intro + "\n")
    say(f"{intro} {nav.current()}")
    for key in key_source:
        if key == ESCAPE:
            return None
        if key in ENTER:
            node = nav.node
            if node.expandable:
                text = nav.expand()
            elif nav.select is not None and node.item is not None:
                return nav.select(node)
            else:
                text = nav.current()
        elif key in (UP, DOWN, PAGE_UP, PAGE_DOWN, RIGHT, LEFT):
            nav.query = ""
            if key == RIGHT:
                text = nav.expand()
            elif key == LEFT:
                text = nav.collapse()
            else:
                step = PAGE if key in (PAGE_UP, PAGE_DOWN) else 1
                text = nav.move(-step if key in (UP, PAGE_UP) else step)
        elif conseils.is_help(key):
            text = consigne or dire("arbre.consigne_dialogue")
        elif key in BACKSPACE:
            text = nav.erase()
        elif key == FAVORITE:
            text = nav.favorite()
        elif len(key) == 1 and key.isprintable():
            text = nav.type_char(key)
        else:
            text = dire("arbre.fleches_lettres_chercher_etoile")
        out(f"[{text}]\n")
        say(text)
    return None




def run_list(labels, key_source, say, out, title, consigne=None, feature="liste"):
    """Liste simple : haut / bas, Entree choisit, Echap annule. Rend
    l'indice choisi ou None."""
    consigne = consigne or dire("arbre.list_consigne")
    index = 0
    intro = conseils.intro(feature, title, consigne)
    out(intro + "\n")
    say(f"{intro} {labels[0]}")
    for key in key_source:
        if key == ESCAPE:
            return None
        if key in ENTER:
            return index
        if key in (UP, DOWN):
            wanted = index + (-1 if key == UP else 1)
            index = min(max(wanted, 0), len(labels) - 1)
            text = (dire("arbre.debut_liste") if wanted < 0 else "Fin de liste, " if wanted != index else "") + labels[index]
        elif conseils.is_help(key):
            text = consigne
        else:
            text = dire("arbre.fleches_haut_bas_entree")
        out(f"[{text}]\n")
        say(text)
    return None
