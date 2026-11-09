"""SDS TETRA : des octets binaires lus en Latin-1 ne sont pas un message (2026-11-09)."""
import os
import sys

sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", "bridge")]
import tetra_neo as t  # noqa: E402

for faux in ("ÿéÿÛ ÿÿÎÿ ÿÒÿ ÿWóÿî ó çU õûS ú øÿî ÿ ÿÆ Zÿë",
             "ÿûÿöÿúÿÿøÿ ÿòÿïÿÿâÿíÿãÿõÿ ìÿð ÿËÿýê ðÿåÿýÿÿ",
             "øþA fçõ ë úÿÿùðþ Uü þC ù á/ ü ÿ ÿô ùðûY 7 îC"):
    assert not (t.lisible(t.nettoyer(faux)) and t.lisible_brut(faux)), faux
for vrai in ("Circulation perturbée. Retrouvez le détail des perturbations",
             "RDV 14h30 au dépôt, véhicule 12", "Intervention terminée"):
    assert t.lisible(t.nettoyer(vrai)) and t.lisible_brut(vrai), vrai
print("tetra SDS : octets binaires ecartes, phrases gardees")
