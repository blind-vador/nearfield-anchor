"""Commande "personnalite" : choisir la personnalite du Pi (lib/personnalite.py).

  texte libre   la personnalite voulue ("un vieux loup de mer bourru mais gentil")
  neutre        retour au ton d'origine
  surprise      Gemini en invente une nouvelle chaque nuit
  entree seule  rien ne change
Ensuite, "oui" lance la reecriture tout de suite (une a deux minutes), sinon
elle se fait cette nuit a minuit.
"""
import sys


import matcher  # noqa: E402
import personnalite  # noqa: E402
from phrases import dire  # noqa: E402


def run(key_source, say, out, read_line):
    text = dire("personnalite.etat", description=personnalite.description()) + " " + dire("personnalite.demande")
    out(text + "\n? ")
    say(text)
    answer = read_line(key_source).strip()
    word = matcher.normalize(answer)
    if not answer:
        return dire("personnalite.inchangee")
    if word in ("neutre", "aucune", "normal", "normale"):
        personnalite.save_settings(active=False)
        return dire("personnalite.retour_neutre")
    if word == "surprise":
        personnalite.save_settings(active=True, surprise=True, prompt="", resume="")
    else:
        personnalite.save_settings(active=True, surprise=False, prompt=answer, resume="")
    text = dire("personnalite.enregistree")
    out(text + "\n? ")
    say(text)
    if matcher.normalize(read_line(key_source)) not in ("oui", "o", "yes"):
        return dire("personnalite.plus_tard")
    say(dire("personnalite.en_cours"))
    result, problem = personnalite.generate()
    if problem:
        return dire("personnalite.echec", raison=problem)
    return dire("personnalite.faite", resume=result["resume"], reussies=result["reussies"], gardees=result["gardees"])
