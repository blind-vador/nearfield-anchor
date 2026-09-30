"""Choix au clavier dans une longue liste : arborescence alphabetique
(gares, aeroports) ou simple liste (communes homonymes, choix court).

Arborescence :
  fleches haut / bas     lettre precedente / suivante, ou entree
                         precedente / suivante dans la lettre depliee
  fleche droite          deplie la lettre
  fleche gauche          replie la lettre et remonte aux lettres
  page haut / page bas   dix entrees
  lettres tapees         recherche : le curseur saute a la premiere entree qui
                         commence par le texte tape, sinon qui le contient
  retour arriere         efface la derniere lettre de la recherche
  Entree                 choisit l'entree (sur une lettre : la deplie)
  Echap                  annule, rien n'est enregistre

Une fleche termine la recherche : les lettres tapees ensuite en commencent
une nouvelle.
"""
import matcher

UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
PAGE_UP, PAGE_DOWN = "\x1b[5~", "\x1b[6~"
ENTER = ("\r", "\n")
BACKSPACE = ("\x7f", "\x08")
ESCAPE = "\x1b"
PAGE = 10


def _letter(norm_name):
    first = norm_name[:1].upper()
    return first if first.isalpha() else "#"


def _letter_name(letter):
    return "chiffres et symboles" if letter == "#" else letter


class Tree:
    def __init__(self, items, spoken, singular, plural):
        """items : dicts avec "nom" ; spoken(item) : phrase dite pour une
        entree ; singular / plural : "gare" / "gares"."""
        self.spoken, self.singular, self.plural = spoken, singular, plural
        keyed = sorted(((matcher.normalize(i["nom"]), i) for i in items), key=lambda p: p[0])
        self.flat = keyed
        self.groups = {}
        for norm, item in keyed:
            self.groups.setdefault(_letter(norm), []).append(item)
        self.letters = sorted(self.groups, key=lambda l: (l == "#", l))
        self.li = 0
        self.ii = None  # None : au niveau des lettres
        self.query = ""

    def _count(self, n):
        return f"{n} {self.singular if n == 1 else self.plural}"

    def current(self):
        if self.ii is None:
            letter = self.letters[self.li]
            return f"{_letter_name(letter)}, {self._count(len(self.groups[letter]))}"
        return self.spoken(self.groups[self.letters[self.li]][self.ii])

    def item(self):
        return None if self.ii is None else self.groups[self.letters[self.li]][self.ii]

    def move(self, delta):
        if self.ii is None:
            wanted = self.li + delta
            self.li = min(max(wanted, 0), len(self.letters) - 1)
            edge = "" if wanted == self.li else ("Première lettre, " if delta < 0 else "Dernière lettre, ")
            return edge + self.current()
        group = self.groups[self.letters[self.li]]
        wanted = self.ii + delta
        self.ii = min(max(wanted, 0), len(group) - 1)
        edge = ""
        if wanted != self.ii:
            edge = f"{'Début' if delta < 0 else 'Fin'} de la lettre {_letter_name(self.letters[self.li])}, "
        return edge + self.current()

    def expand(self):
        if self.ii is not None:
            return self.current()
        self.ii = 0
        return f"{_letter_name(self.letters[self.li])} déplié. {self.current()}"

    def collapse(self):
        if self.ii is None:
            return f"Déjà au niveau des lettres. {self.current()}"
        self.ii = None
        return f"Replié. {self.current()}"

    def _search(self):
        q = matcher.normalize(self.query)
        if self.query[-1:] in (" ", "-", "'") and q:
            q += " "  # "saint " ne doit trouver que "saint ..." pas "saintes"
        hit = next((i for n, i in self.flat if n.startswith(q)), None)
        if hit is None:
            hit = next((i for n, i in self.flat if q in n), None)
        if hit is None:
            return False
        for li, letter in enumerate(self.letters):
            group = self.groups[letter]
            for ii, item in enumerate(group):
                if item is hit:
                    self.li, self.ii = li, ii
                    return True
        return False

    def type_char(self, ch):
        if ch == " " and not self.query:
            return self.current()
        self.query += ch
        if self._search():
            return self.current()
        typed = self.query
        self.query = self.query[:-1]
        return f"Rien pour {typed}."

    def erase(self):
        if not self.query:
            return "Pas de recherche en cours."
        self.query = self.query[:-1]
        if not self.query:
            return "Recherche effacée."
        self._search()
        return f"{self.query} : {self.current()}"


def run_tree(tree, key_source, say, out, intro):
    """Rend l'entree choisie, ou None si Echap."""
    out(intro + "\n")
    say(f"{intro} {tree.current()}")
    for key in key_source:
        if key == ESCAPE:
            return None
        if key in ENTER:
            if tree.ii is not None:
                return tree.item()
            text = tree.expand()
        elif key in (UP, DOWN, PAGE_UP, PAGE_DOWN, RIGHT, LEFT):
            tree.query = ""
            if key == RIGHT:
                text = tree.expand()
            elif key == LEFT:
                text = tree.collapse()
            else:
                step = PAGE if key in (PAGE_UP, PAGE_DOWN) else 1
                if tree.ii is None and step == PAGE:
                    step = 1
                text = tree.move(-step if key in (UP, PAGE_UP) else step)
        elif key in BACKSPACE:
            text = tree.erase()
        elif len(key) == 1 and key.isprintable():
            text = tree.type_char(key)
        else:
            text = "Flèches, lettres pour chercher, entrée pour choisir, échap pour annuler."
        out(f"[{text}]\n")
        say(text)
    return None


def run_list(labels, key_source, say, out, intro):
    """Liste simple : haut / bas, Entree choisit, Echap annule. Rend
    l'indice choisi ou None."""
    index = 0
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
            text = ("Début de liste, " if wanted < 0 else "Fin de liste, " if wanted != index else "") + labels[index]
        else:
            text = "Flèches haut et bas, entrée pour choisir, échap pour annuler."
        out(f"[{text}]\n")
        say(text)
    return None
