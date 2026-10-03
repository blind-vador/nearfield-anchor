"""Verification des phrases : chaque dire("cle", ...) du code existe dans
phrases/fr.ini et recoit exactement les variables de son gabarit ; les cles
construites (f-strings) sont signalees pour un controle a l'oeil."""
import ast
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import phrases  # noqa: E402

phrases.BASE_DIR = os.path.join(ROOT, "phrases")
lang = sys.argv[1] if len(sys.argv) > 1 else "fr"
ini = phrases.base(lang)
errors = 0
for path in sorted(glob.glob(os.path.join(ROOT, "*", "*.py"))):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dire" and node.args):
            continue
        where = f"{os.path.relpath(path, ROOT)}:{node.lineno}"
        arg = node.args[0]
        if isinstance(arg, ast.JoinedStr):
            first = arg.values[0]
            prefix = first.value if isinstance(first, ast.Constant) else ""
            if not any(k.startswith(prefix) for k in ini):
                print(f"{where} : aucune cle ne commence par {prefix}")
                errors += 1
            continue
        if not isinstance(arg, ast.Constant):
            continue
        key = arg.value
        if key not in ini:
            print(f"{where} : cle absente {key}")
            errors += 1
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue  # **variables : non verifiable
        given = {kw.arg for kw in node.keywords}
        wanted = phrases.variables(ini[key]["texte"])
        if given != wanted:
            print(f"{where} : {key} attend {sorted(wanted)}, recoit {sorted(given)}")
            errors += 1
print(f"{len(ini)} phrases, {errors} probleme(s)")
sys.exit(1 if errors else 0)
