"""Verification des phrases (architecture de langue, lib/phrases.py) :

- chaque dire("cle", ...) du code existe dans phrases/<langue>.ini et recoit
  exactement les variables de son gabarit ;
- chaque dire_ou("cle", repli) a aussi sa section : le repli francais du code
  ne doit servir qu'en secours, jamais a la place de la langue (2026-11-08) ;
  seules les descriptions de commandes (commande.*) en sont dispensees ;
- aucune section n'est ecrite deux fois dans le fichier de langue ;
- chaque consigne de dialogue (cle en .consigne...) dit F1, et protege le mot
  F1 de la personnalite (garder) ; les consignes envoyees a une IA sont a part.
Les cles construites (f-strings) sont signalees pour un controle a l'oeil.

  python3 tools/phrases_verifier.py [langue]
"""
import ast
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "lib"), os.path.join(ROOT, "config")]
import phrases  # noqa: E402

phrases.BASE_DIR = os.path.join(ROOT, "phrases")
lang = sys.argv[1] if len(sys.argv) > 1 else "fr"
ini = phrases.base(lang)
INI_PATH = os.path.join(phrases.BASE_DIR, f"{lang}.ini")
# Consignes qui ne sont pas des touches : questions a une IA, saisie d'un nom.
CONSIGNES_HORS_TOUCHES = {"dab.consigne_image", "radio.consigne_image_sstv", "whisper.consigne_nom",
                          "manuel.renommer_consigne", "arbre.consigne_dialogue", "conseils.consignes_remises_zero_chaque"}
errors = 0


def signaler(texte):
    global errors
    print(texte)
    errors += 1


for path in sorted(glob.glob(os.path.join(ROOT, "*", "*.py"))):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in ("dire", "dire_ou"):
            continue
        where = f"{os.path.relpath(path, ROOT)}:{node.lineno}"
        arg = node.args[0]
        if isinstance(arg, ast.JoinedStr):
            first = arg.values[0]
            prefix = first.value if isinstance(first, ast.Constant) else ""
            if name == "dire" and not any(k.startswith(prefix) for k in ini):
                signaler(f"{where} : aucune cle ne commence par {prefix}")
            continue
        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
            continue
        key = arg.value
        if name == "dire_ou":
            if key not in ini and not key.startswith("commande."):
                signaler(f"{where} : dire_ou sans section {key} (le repli du code remplace la langue)")
            continue
        if key not in ini:
            signaler(f"{where} : cle absente {key}")
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue  # **variables : non verifiable
        given = {kw.arg for kw in node.keywords}
        wanted = phrases.variables(ini[key]["texte"])
        if given != wanted:
            signaler(f"{where} : {key} attend {sorted(wanted)}, recoit {sorted(given)}")

# Sections en double : configparser garde la derniere sans rien dire.
vues = {}
with open(INI_PATH, encoding="utf-8") as f:
    for numero, ligne in enumerate(f, 1):
        m = re.match(r"^\[([^\]]+)\]\s*$", ligne)
        if m:
            if m.group(1) in vues:
                signaler(f"{lang}.ini:{numero} : section {m.group(1)} deja ecrite ligne {vues[m.group(1)]}")
            vues.setdefault(m.group(1), numero)

# Consignes de dialogue : F1 annonce et protege.
for key, entry in ini.items():
    if ".consigne" not in key or key in CONSIGNES_HORS_TOUCHES or key.startswith(("imprimante.cal.", "ia.")):
        continue
    if "F1" not in entry["texte"]:
        signaler(f"{lang}.ini [{key}] : consigne de dialogue sans F1")
    elif entry["garder"] and "F1" not in entry["garder"]:
        signaler(f"{lang}.ini [{key}] : F1 dit mais pas garde (la personnalite peut l'oter)")

print(f"{len(ini)} phrases, {errors} probleme(s)")
sys.exit(1 if errors else 0)
