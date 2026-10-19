"""Decouverte de l'imprimante au toucher (2026-10-16), premiere etape du
calibrage guide.

Gemini decrit la machine reellement branchee (modele lu dans le firmware,
plus ce que le Pi a mesure lui-meme : sonde, butees, prise Z debranchee) :
une visite guidee, element par element, du point de vue d'une personne
aveugle placee devant la machine. La reponse est gardee dans
reglages_cache (une seule question par machine) ; sans Gemini, une
description de secours ecrite a la main prend le relais.

Chaque element est confirme au toucher (trouve / pas trouve) : les
confirmations sont gardees avec la description.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")
import gemini  # noqa: E402

CACHE = "/home/vador/autopilot/reglages_cache/imprimante_decouverte.json"
CHAMPS = ("nom", "ou", "au_toucher", "role", "prudence")

# Ce que le Pi sait de CETTE machine (mesures du 15-16/10, M43 et M119) :
# donne a Gemini pour qu'il decrive la vraie machine, pas une Ender generique.
FAITS = """- Modele annonce par le firmware : {modele} (Creality Ender-3, carte mere Creality 4.2.7).
- Firmware Marlin {version}.
- Plateau de {x} x {y} mm, hauteur maximale {z} mm, plateau en verre.
- Sonde de nivellement BLTouch (ou compatible) fixee sur la tete : sa tige est {sonde_x} mm a gauche
  et {sonde_y} mm devant la buse.
- Butee X : interrupteur a gauche, touche par la tete. Butee Y : interrupteur au bout de la course du plateau.
- L'interrupteur de fin de course Z, en bas de l'axe Z, est toujours en place mais son cable est
  debranche de la carte : c'est la sonde qui fait l'origine Z.
- Quatre molettes de reglage sous les coins du plateau.
- Reliee en USB a un Raspberry Pi (OctoPrint), avec une webcam."""

QUESTION = """Tu guides une personne aveugle qui decouvre au toucher son imprimante 3D,
avant son tout premier calibrage. Voici la machine exacte :
{faits}

Ecris une visite guidee au toucher, en francais, en tutoyant la personne, debout face a la machine
(l'avant de la machine = le cote ou se trouve l'ecran sur une Ender-3 ; sinon dis-le).
Ordre : d'abord la securite (interrupteur d'alimentation, ce qui chauffe, ce qui bouge),
puis le cadre, la tete et la buse, la sonde et sa tige, les butees X, Y et l'interrupteur Z,
le plateau et ses quatre molettes, l'ecran et son bouton, le lecteur de carte microSD,
le cable USB vers le Pi. Pour chaque element : ou il est par rapport a la personne,
comment le reconnaitre au toucher (forme, matiere, taille compare a un objet courant),
a quoi il sert pendant le calibrage, et la prudence a avoir (chaleur, pieces mobiles,
pincement, ne pas forcer). Phrases courtes, sans couleur ni reference visuelle.
Si un detail varie selon les versions de la machine, dis-le et invite a verifier.

Reponds en JSON strict :
{{"presentation": "deux phrases pour situer la machine dans l'espace",
  "elements": [{{"nom": "...", "ou": "...", "au_toucher": "...", "role": "...", "prudence": "..."}}],
  "variantes": "ce qui peut differer sur cette machine, en une ou deux phrases"}}
Entre 10 et 16 elements."""

# Repli sans Gemini : description generale d'une Ender-3, a confirmer.
SECOURS = {
    "presentation": "L'imprimante est un cadre en métal d'environ 45 centimètres de côté, posé sur la table. "
                    "Face à toi, l'écran et son bouton sont en bas à droite de la base.",
    "elements": [
        {"nom": "interrupteur d'alimentation", "ou": "sur le bloc d'alimentation, à l'arrière droit de la machine",
         "au_toucher": "un petit interrupteur à bascule, à côté de la prise du câble secteur",
         "role": "couper tout en cas de problème", "prudence": "repère-le avant de commencer, sans l'actionner"},
        {"nom": "buse", "ou": "sous la tête d'impression, au centre du cadre",
         "au_toucher": "une petite pointe en laiton, de la taille d'un bout de crayon",
         "role": "elle dépose le plastique ; on règle sa hauteur au-dessus du plateau",
         "prudence": "elle peut chauffer à plus de 200 degrés : ne la touche que si je viens de te dire "
                     "qu'elle est froide"},
        {"nom": "sonde BLTouch", "ou": "fixée sur la tête, à gauche de la buse",
         "au_toucher": "un petit boîtier en plastique, avec une fine tige en dessous",
         "role": "elle touche le plateau pour mesurer sa hauteur", "prudence": "ne tords jamais la tige"},
        {"nom": "plateau", "ou": "au centre, sous la tête", "au_toucher": "une plaque de verre lisse",
         "role": "c'est la surface à régler", "prudence": "il peut chauffer ; il avance et recule"},
        {"nom": "molettes du plateau", "ou": "sous les quatre coins du plateau",
         "au_toucher": "des boutons ronds et crantés, un peu plus larges qu'une pièce de deux euros",
         "role": "les tourner monte ou descend le coin", "prudence": "tourne doucement, sans forcer"},
    ],
    "variantes": "Description générale d'une Ender-3, faite sans Gemini : vérifie chaque élément au toucher.",
}


def _faits(machine, volume, version):
    off = (machine or {}).get("probe_offset") or {}
    return FAITS.format(modele=(machine or {}).get("modele") or "Ender-3 4.2.7", version=version or "2.1.2.5",
                        x=round(volume["x"]), y=round(volume["y"]), z=round(volume["z"]),
                        sonde_x=round(-off.get("x", -50)), sonde_y=round(-off.get("y", -10)))


def _valide(data):
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        return None
    elements = []
    for e in data["elements"]:
        if isinstance(e, dict) and e.get("nom") and e.get("ou"):
            elements.append({c: " ".join(str(e.get(c) or "").split()) for c in CHAMPS})
    if len(elements) < 5:
        return None
    return {"presentation": " ".join(str(data.get("presentation") or "").split()),
            "elements": elements, "variantes": " ".join(str(data.get("variantes") or "").split())}


def charger():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def enregistrer(data):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE)


def visite(machine, volume, version, force=False):
    """Rend (visite, origine) : origine "cache", "gemini" ou "secours"."""
    faits = _faits(machine, volume, version)
    deja = charger()
    if deja and deja.get("faits") == faits and not force:
        return deja, "cache"
    texte, _raison = gemini.ask(QUESTION.format(faits=faits), temperature=0.3, json_mode=True, timeout=60)
    data = None
    if texte:
        try:
            data = _valide(json.loads(texte))
        except ValueError:
            data = None
    if data is None:
        return dict(SECOURS, faits=faits, confirmations={}), "secours"
    data.update(faits=faits, date=time.strftime("%Y-%m-%d %H:%M"), confirmations={})
    enregistrer(data)
    return data, "gemini"


def confirmer(data, nom, trouve):
    """Note au toucher : trouve (True), pas trouve (False)."""
    data.setdefault("confirmations", {})[nom] = {"trouve": trouve, "date": time.strftime("%Y-%m-%d %H:%M")}
    if data.get("date"):  # seulement une visite venue de Gemini (le secours n'est pas garde)
        try:
            enregistrer(data)
        except OSError:
            pass
