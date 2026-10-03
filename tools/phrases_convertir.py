#!/usr/bin/env python3
"""Outil de developpement : fait passer des textes d'interface d'un fichier
Python par le module des phrases (lib/phrases.py).

  phrases_convertir.py liste <fichier.py>
      textes candidats, avec leur numero de ligne
  phrases_convertir.py convertir <fichier.py> <prefixe> <lignes> [--fixe]
      remplace les textes de ces lignes (ex. 12,40-45) par dire("prefixe.cle", ...)
      et ajoute les phrases a phrases/fr.ini

Les variables gardent le nom du code ({nom}, {freq}...) ; une expression
complexe devient {v1}, {v2}... Un format (:.1f) est applique avant l'appel.
Contexte : la fonction qui dit la phrase ; a affiner a la main si besoin.
"""
import ast
import configparser
import keyword
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INI = os.path.join(ROOT, "phrases", "fr.ini")
FR = re.compile(r"[éèàùâêîôûç]|\b(le|la|les|des|une|est|pas|pour|sur|dans|du|aucun|vers)\b", re.I)


def _docstrings(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _text_of(node):
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{…}" for v in node.values)
    return node.value


def candidates(src):
    tree = ast.parse(src)
    docs = _docstrings(tree)
    parents = _parents(tree)
    out = []
    for node in ast.walk(tree):
        if id(node) in docs:
            continue
        if isinstance(parents.get(id(node)), ast.JoinedStr):
            continue  # morceau d'une f-string : c'est la f-string entiere qui compte
        if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            text = _text_of(node)
            if len(text) >= 4 and " " in text.strip() and FR.search(text) and not text.lstrip().startswith(("[", "{", "/")):
                out.append((node.lineno, node.end_lineno, text, node))
    return sorted(out, key=lambda c: (c[0], c[3].col_offset))


def _function_of(tree, lineno):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= lineno <= node.end_lineno:
            if best is None or node.lineno > best.lineno:
                best = node
    return best.name if best else "module"


def _var_name(expr, src, used):
    base = None
    if isinstance(expr, ast.Name):
        base = expr.id
    elif isinstance(expr, ast.Attribute):
        base = expr.attr
    elif isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Constant) and isinstance(expr.slice.value, str):
        base = expr.slice.value
    elif isinstance(expr, ast.Call) and isinstance(expr.func, (ast.Name, ast.Attribute)):
        base = expr.func.id if isinstance(expr.func, ast.Name) else expr.func.attr
    base = re.sub(r"\W", "_", base or "") if base else ""
    if not base or not base.isidentifier() or keyword.iskeyword(base) or base.startswith("_"):
        base = "v"
    name, n = base, 1
    while name in used or name == "v":
        n += 1 if name != "v" else 0
        name = f"{base}{n}"
        if name not in used:
            break
        n += 1
    used.add(name)
    return name


def _convert(node, src):
    """(gabarit, [(variable, expression python)])."""
    if isinstance(node, ast.Constant):
        return node.value.replace("{", "{{").replace("}", "}}"), []
    parts, args, used = [], [], set()
    for v in node.values:
        if isinstance(v, ast.Constant):
            parts.append(v.value.replace("{", "{{").replace("}", "}}"))
            continue
        expr = ast.get_source_segment(src, v.value)
        if v.format_spec is not None:
            spec = "".join(x.value if isinstance(x, ast.Constant) else "{" + ast.get_source_segment(src, x.value) + "}"
                           for x in v.format_spec.values)
            expr = f'format({expr}, f"{spec}")' if "{" in spec else f'format({expr}, "{spec}")'
        if v.conversion == ord("r"):
            expr = f"repr({expr})"
        elif v.conversion == ord("s"):
            expr = f"str({expr})"
        name = _var_name(v.value, src, used)
        args.append((name, " ".join(expr.split())))
        parts.append("{" + name + "}")
    return "".join(parts), args


STOP = {"le", "la", "les", "l", "un", "une", "des", "de", "du", "d", "a", "à", "et", "ou", "en", "est", "je", "tu",
        "il", "on", "pour", "par", "sur", "dans", "ce", "c", "qu", "que", "j", "n", "ne", "pas", "se", "s", "y"}


def _slug(text):
    """Cle lisible tiree des premiers mots : "Réglages automatiques activés" -> reglages_automatiques_actives."""
    import unicodedata
    plain = unicodedata.normalize("NFD", text.lower())
    plain = "".join(c for c in plain if unicodedata.category(c) != "Mn")
    words = [w for w in re.findall(r"[a-z0-9]+", re.sub(r"\{[^}]*\}", " ", plain)) if w not in STOP]
    return "_".join(words[:4]) or "phrase"


KEY_WORDS = ["flèches haut et bas", "flèche haut", "flèche bas", "flèche droite", "flèche gauche", "gauche et droite",
             "haut et bas", "entrée", "échap", "f1", "f2", "majuscule tab", "tab", "page haut", "page bas", "contrôle",
             "majuscule", "étoile", "espace", "retour arrière"]


def protected(text):
    """Mots a garder tels quels : touches, lettres de raccourci, commandes a dire."""
    found, low = [], text.lower()
    for word in KEY_WORDS:
        m = re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", low)
        if m and not any(word in f.lower() for f in found):
            found.append(text[m.start():m.end()])
    found += re.findall(r"(?<![\w'])([A-Z]|OK|F\d)(?= :| pour| ou|,| et|\.)", text)
    for m in re.finditer(r"\b(?:[Dd]is|[Tt]ape)\s+([a-zéèêàçù]+(?: (?:activés|désactivés|zéro|précédent|automatiques|audio|"
                         r"détails|meteo|météo|radio|capteurs|satellites|données))?)", text):
        found.append(m.group(1))
    out = []
    for w in found:
        if w and w.lower() not in [o.lower() for o in out]:
            out.append(w)
    return out


def _lines(spec):
    out = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out


def convert(path, prefix, spec, fixe=False):
    src = open(path, encoding="utf-8", newline="").read()
    crlf = "\r\n" in src
    src = src.replace("\r\n", "\n")
    tree = ast.parse(src)
    wanted = _lines(spec)
    chosen = [c for c in candidates(src) if c[0] in wanted]
    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(INI, encoding="utf-8")
    existing = set(parser.sections())
    # Les positions de l'arbre syntaxique sont en OCTETS (UTF-8) : on decoupe
    # le source en octets, sinon chaque lettre accentuee decale tout.
    raw = src.encode("utf-8")
    lines = raw.split(b"\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    edits, entries, skipped = [], [], []
    counters = {}
    for lineno, end, text, node in chosen:
        func = _function_of(tree, lineno)
        if func == "module":
            skipped.append((lineno, text))  # constante figee au chargement : a la main
            continue
        key = f"{prefix}.{_slug(text)}"
        n = 2
        while key in existing:
            key = f"{prefix}.{_slug(text)}_{n}"
            n += 1
        existing.add(key)
        template, args = _convert(node, src)
        call = f'dire("{key}"' + "".join(f", {n}={e}" for n, e in args) + ")"
        start = offsets[node.lineno - 1] + node.col_offset
        stop = offsets[node.end_lineno - 1] + node.end_col_offset
        edits.append((start, stop, call))
        entries.append((key, " ".join(template.split()), f"{os.path.basename(path)}, fonction {func}"))
    for start, stop, call in sorted(edits, reverse=True):
        raw = raw[:start] + call.encode("utf-8") + raw[stop:]
    src = raw.decode("utf-8")
    if edits and "from phrases import dire" not in src:
        m = list(re.finditer(r"^(import \w+|from \w+ import [^\n]+)(  # noqa: E402)?\n", src, re.M))
        anchor = m[-1].end() if m else 0
        noqa = "  # noqa: E402" if m and m[-1].group(2) else ""
        src = src[:anchor] + f"from phrases import dire{noqa}\n" + src[anchor:]
    open(path, "w", encoding="utf-8", newline="").write(src.replace("\n", "\r\n") if crlf else src)
    with open(INI, "a", encoding="utf-8") as f:
        f.write(f"\n# --- {os.path.relpath(path, ROOT)} ---\n")
        for key, template, ctx in entries:
            f.write(f"[{key}]\ntexte = {template}\ncontexte = {ctx}\n")
            keep = protected(template)
            if keep:
                f.write("garder = " + ", ".join(keep) + "\n")
            if fixe:
                f.write("fixe = oui\n")
            f.write("\n")
    print(f"{len(edits)} textes convertis dans {path}")
    for lineno, text in skipped:
        print(f"  a faire a la main (constante de module) : ligne {lineno} : {text[:80]}")


def constants(path, prefix, names):
    """Constantes de module NOM = "texte" : la constante disparait, chaque
    usage devient dire(cle) (evalue au moment de parler, personnalite du jour)."""
    src = open(path, encoding="utf-8", newline="").read()
    crlf = "\r\n" in src
    src = src.replace("\r\n", "\n")
    tree = ast.parse(src)
    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(INI, encoding="utf-8")
    existing = set(parser.sections())
    entries, raw = [], src.encode("utf-8")
    targets = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in names and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            targets[node.targets[0].id] = node
    for name in names:
        node = targets.get(name)
        if node is None:
            print(f"  {name} : pas une constante texte simple, a la main")
            continue
        key = f"{prefix}.{name.lower()}"
        if key in existing:
            key = f"{prefix}.{_slug(node.value.value)}"
        existing.add(key)
        entries.append((key, node.value.value.replace("{", "{{").replace("}", "}}"), name))
    # suppression des affectations (de la fin vers le debut), puis remplacement des usages
    lines = raw.split(b"\n")
    for name, node in sorted(((n, targets[n]) for n in names if n in targets), key=lambda t: -t[1].lineno):
        del lines[node.lineno - 1:node.end_lineno]
    src = b"\n".join(lines).decode("utf-8")
    for key, _, name in entries:
        src = re.sub(r"(?<![\w.])" + name + r"(?!\w)", f'dire("{key}")', src)
    if entries and "from phrases import dire" not in src:
        m = list(re.finditer(r"^(import \w+|from \w+ import [^\n]+)(  # noqa: E402)?\n", src, re.M))
        anchor = m[-1].end() if m else 0
        noqa = "  # noqa: E402" if m and m[-1].group(2) else ""
        src = src[:anchor] + f"from phrases import dire{noqa}\n" + src[anchor:]
    open(path, "w", encoding="utf-8", newline="").write(src.replace("\n", "\r\n") if crlf else src)
    with open(INI, "a", encoding="utf-8") as f:
        f.write(f"\n# --- {os.path.relpath(path, ROOT)} (constantes) ---\n")
        for key, template, name in entries:
            f.write(f"[{key}]\ntexte = {' '.join(template.split())}\ncontexte = {os.path.basename(path)}, {name}\n")
            keep = protected(template)
            if keep:
                f.write("garder = " + ", ".join(keep) + "\n")
            f.write("\n")
    stem = os.path.splitext(os.path.basename(path))[0]
    for other in __import__("glob").glob(os.path.join(ROOT, "*", "*.py")):
        text = open(other, encoding="utf-8", errors="replace").read()
        for _, _, name in entries:
            if re.search(r"\b" + stem + r"\." + name + r"\b", text):
                print(f"  ATTENTION : {os.path.relpath(other, ROOT)} utilise {stem}.{name}")
    print(f"{len(entries)} constantes converties dans {path}")


if __name__ == "__main__":
    if sys.argv[1] == "constantes":
        constants(sys.argv[2], sys.argv[3], sys.argv[4].split(","))
        sys.exit(0)
    if sys.argv[1] == "liste":
        src = open(sys.argv[2], encoding="utf-8").read().replace("\r\n", "\n")
        for lineno, end, text, node in candidates(src):
            print(f"{lineno:5d}  {text[:120]}")
    else:
        convert(sys.argv[2], sys.argv[3], sys.argv[4], "--fixe" in sys.argv)
