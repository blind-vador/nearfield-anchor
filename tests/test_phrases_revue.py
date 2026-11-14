"""Relecture des phrases (2026-11-08) : tout passe par le module phrases, F1
est annonce dans les dialogues, le rejet du journal dit ce qui change pour le
decodeur, et les questions a une IA portent le contexte du scanneur."""
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(RACINE, d) for d in ("lib", "config", "bridge", "console", "watchers")]
import phrases  # noqa: E402

phrases.BASE_DIR = os.path.join(RACINE, "phrases")
# Jamais la personnalite du jour : le texte neutre du depot (2026-11-14, "30 pour cent
# de floraison" un matin de personnalite fleurie).
phrases.PERSONA_DIR = tempfile.mkdtemp()

# --- l'outil de verification passe sur tout le depot
r = subprocess.run([sys.executable, os.path.join(RACINE, "tools", "phrases_verifier.py")],
                   capture_output=True, text=True)
assert r.returncode == 0, r.stdout[-2000:]

# --- contexte du scanneur devant les questions sur une reception
import connecteurs_ia  # noqa: E402
import gemini  # noqa: E402
vues = []
connecteurs_ia.demander = lambda prompt, *a, **k: vues.append(prompt) or "ok"
assert gemini.ask("Explique ce message.", scanneur=True) == ("ok", None)
assert gemini.ask("Une plante ?") == ("ok", None)
assert vues[0].endswith("Explique ce message.") and "en clair" in vues[0] and "aveugles" in vues[0], vues[0]
assert "{langue}" not in vues[0] and "français" in vues[0]
assert vues[1] == "Une plante ?"

# --- rejet au journal : la phrase dit toujours ce qui a change
import journal  # noqa: E402
import propositions  # noqa: E402
TMP = tempfile.mkdtemp()
PRISE = os.path.join(TMP, "canal_x.mp3")


def nouvelle_prise():
    open(PRISE, "wb").close()


journal.ns.forget_capture = lambda p: None
journal.ns.last_capture = lambda: None
journal.ns.add_signature = lambda info: None
journal.ns.juge_de = lambda info: "dsd_dmr"
journal._message_card = lambda p: None
nouvelle_prise()
journal.ns.capture_info = lambda p: {"freq_hz": 446_006_250, "target": "x"}
t = journal._reject(PRISE, juge_note="dsd_dmr")
assert t == phrases.dire("journal.rejete_baisse", juge=propositions.nom("dsd_dmr")), t
assert "perd en confiance" in t and not os.path.exists(PRISE)
nouvelle_prise()
t = journal._reject(PRISE)
assert t == phrases.dire("journal.rejete_supprime"), t
nouvelle_prise()
journal.ns.capture_info = lambda p: {"freq_hz": 446_006_250, "fingerprint": [1, 2, 3]}
t = journal._reject(PRISE, juge_note="dsd_dmr")
assert t == phrases.dire("journal.rejete_appris", juge=propositions.nom("dsd_dmr")) and "empreinte" in t, t
nouvelle_prise()
t = journal._reject(PRISE, rendu={"juge": "nfm", "empreinte": None})
assert t == phrases.dire("journal.rejete_baisse", juge=propositions.nom("nfm")), t

# noter_rejet_simple rend le juge baisse, ou None s'il n'y a rien a baisser
notes = []
propositions._noter = lambda freq, juge, bon, label=None: notes.append((freq, juge, bon))
propositions._juge_courant = lambda j: ("nfm", 145_500_000, "2 m")
assert propositions.noter_rejet_simple(PRISE) == "nfm" and notes[-1] == (145_500_000, "nfm", False)
propositions._juge_courant = lambda j: (None, None, "")
assert propositions.noter_rejet_simple(PRISE) is None

# --- F1 dans un dialogue en arbre rouvert sans consigne : l'aide reste
import arbre  # noqa: E402
import voix  # noqa: E402
voix.prechauffer = lambda *a, **k: None
arbre.conseils.intro = lambda feature, title, consigne: title
dit = []
nav = arbre.Navigator(lambda: [arbre.Node("un", item=1)], [])
arbre.run(nav, iter(["\x1bOP", "\x1b"]), dit.append, lambda t: None, "Titre", "", aide="Les touches.")
assert dit[-1] == "Les touches.", dit

# --- libelles de la notation et des volumes : par la langue
import notation  # noqa: E402
assert notation._value_text("fin", -2) == phrases.dire("notation.echelle.fin.0") == "coupée trop tôt"
assert notation._value_text("pct", 30) == "30 pour cent"
assert notation._nom_onglet(0) == "Contenu"
import voix_cmd  # noqa: E402
assert voix_cmd.PITCHES["-10Hz"] != voix_cmd.PITCHES["+10Hz"]

print("ok")
