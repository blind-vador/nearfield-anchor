"""Commande "decors" (2026-11-13) : les decors de la gare (lib/decors.py).

Liste
  haut / bas         Nouveau decor, puis les decors existants
  Entree             creer, ou modifier le decor choisi
  Retour arriere     supprimer le decor choisi (O pour oui, N pour non)
  Echap ferme. F1 : les touches.

Conditions (elles s'empilent : toutes doivent etre vraies)
  haut / bas         la condition : evenement ponctuel, jour, mois, annee,
                     heure, minute, meteo, puis Suivant
  gauche / droite    sa valeur ; page precedente / suivante : de dix en dix ;
                     debut / fin : premier et dernier choix
  Entree             sur l'annee : la taper (rien = toutes les annees) ;
                     ailleurs : Suivant
  Echap              abandonne le decor

Action
  haut / bas         action, puis texte et jingle (annonce) ou son et
                     Telecharger un son (jouer un son), puis Enregistrer
  gauche / droite    la valeur ; chaque jingle et chaque son est joue
  Entree             texte : le taper ; Telecharger : le dialogue "cherche des
                     sons", qui pose dans le depot des decors et revient ici
                     sur le son telecharge ; Enregistrer : garde le decor
  Echap              revient aux conditions
"""
import os
import subprocess

import conseils
import decors
import keys
import voix
from phrases import dire, etiquette, nombre

UP, DOWN, LEFT, RIGHT = "\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C"
PGUP, PGDN = "\x1b[5~", "\x1b[6~"
HOME, END = ("\x1b[1~", "\x1b[H"), ("\x1b[4~", "\x1b[F")
ENTREE = ("\r", "\n")
BACKSPACE = ("\x7f", "\x08")
ESC = "\x1b"
OUI = ("o", "O", "y", "Y")
GRAND_PAS = 10

CHOIX = {
    "ponctuel": [False, True],
    "jour": list(range(0, 32)),
    "mois": list(range(0, 13)),
    "heure": decors.HEURES,
    "minute": [None] + list(range(0, 60)),
    "meteo": decors.METEOS,
    "action": ["annonce", "son"],
    "jingle": list(decors.JINGLES),
}
CONDITIONS = ["ponctuel", "jour", "mois", "annee", "heure", "minute", "meteo", "suivant"]
_ecoute = [None]


# --- ce qu'on dit -----------------------------------------------------------

def _mois(n):
    noms = [m.strip() for m in dire("decors.mois_noms").split(",")]
    return noms[n - 1] if 1 <= n <= len(noms) else str(n)


def _jour(n):
    return dire("decors.premier") if n == 1 else str(n)


def _heure(h):
    return dire("decors.minuit") if h == 0 else nombre(h, "heure")


def _nom_son(relatif):
    if not relatif:
        return dire("decors.son_aucun")
    nom = os.path.splitext(os.path.basename(relatif))[0].replace("_", " ")
    dossier = os.path.dirname(relatif)
    if dossier == os.path.relpath(decors.DEPOT, decors.TRAIN_SCANER_DIR):
        return dire("decors.son_depot", nom=nom)
    if not dossier:
        return dire("decors.son_gare", nom=etiquette(f"decors.dossier_{nom}", nom))
    return dire("decors.son_gare", nom=f"{etiquette('decors.dossier_' + dossier, dossier.replace('_', ' '))} {nom}")


def valeur(champ, d):
    v = d.get(champ)
    if champ == "ponctuel":
        return dire("decors.ponctuel_oui") if v else dire("decors.ponctuel_non")
    if champ == "jour":
        return _jour(v) if v else dire("decors.tous_les_jours")
    if champ == "mois":
        return _mois(v) if v else dire("decors.tous_les_mois")
    if champ == "annee":
        return str(v) if v else dire("decors.toutes_les_annees")
    if champ == "heure":
        if v in ("ouverture", "fermeture", "toujours"):
            return dire(f"decors.heure_{v}")
        return _heure(v)
    if champ == "minute":
        if v is None:
            return dire("decors.heure_toujours")
        return dire("decors.minute_pile") if v == 0 else str(v)
    if champ == "meteo":
        return dire(f"decors.meteo_{v or 'indifferent'}")
    if champ == "action":
        return dire(f"decors.action_{v}")
    if champ == "texte":
        return v.strip() if (v or "").strip() else dire("decors.texte_vide")
    if champ == "jingle":
        return dire(f"decors.jingle_{v}")
    if champ == "son":
        return _nom_son(v)
    return ""


def champ_texte(champ, d):
    if champ in ("suivant", "telecharger", "enregistrer"):
        return dire(f"decors.bouton_{champ}")
    return dire(f"decors.champ_{champ}", valeur=valeur(champ, d))


def decrire(d):
    """Une phrase : quand, puis quoi."""
    jour, mois, annee = d.get("jour") or 0, d.get("mois") or 0, d.get("annee") or 0
    if jour and mois:
        quand = [dire("decors.d_jour_mois", jour=_jour(jour), mois=_mois(mois))]
    elif mois:
        quand = [dire("decors.d_jours_du_mois", mois=_mois(mois))]
    elif jour:
        quand = [dire("decors.d_jour_chaque_mois", jour=_jour(jour))]
    else:
        quand = [dire("decors.tous_les_jours")]
    if annee:
        quand.append(dire("decors.d_en_annee", annee=annee))
    elif jour or mois:
        quand.append(dire("decors.d_tous_les_ans"))
    heure, minute = d.get("heure"), d.get("minute")
    if isinstance(heure, int):
        if minute is None:
            quand.append(dire("decors.d_a_heure", heure=_heure(heure)))
        else:
            quand.append(dire("decors.d_a_heure_minute", heure=_heure(heure),
                              minute=dire("decors.minute_pile") if minute == 0 else minute))
    else:
        if minute is not None:
            quand.append(dire("decors.d_minute_chaque_heure", minute=minute))
        quand.append(dire(f"decors.d_heure_{heure or 'toujours'}"))
    if d.get("meteo"):
        quand.append(dire(f"decors.d_meteo_{d['meteo']}"))
    if d.get("ponctuel"):
        quand.append(dire("decors.d_ponctuel"))
    if d.get("action") == "son":
        quoi = dire("decors.d_son", nom=_nom_son(d.get("son")))
    else:
        quoi = dire("decors.d_annonce", texte=(d.get("texte") or "").strip(),
                    jingle=dire(f"decors.d_jingle_{d.get('jingle') or 'moderne'}"))
    return dire("decors.description", conditions=", ".join(quand), action=quoi)


# --- ecoute -------------------------------------------------------------------

def _arreter():
    proc, _ecoute[0] = _ecoute[0], None
    if proc is not None and proc.poll() is None:
        proc.terminate()


def _jouer(chemin):
    _arreter()
    if not chemin or not os.path.isfile(chemin):
        return
    _ecoute[0] = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", chemin, "-f", "alsa", "default"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- dialogue -------------------------------------------------------------------

class _Dialogue:
    def __init__(self, key_source, say, out, read_line):
        self.keys, self.say, self.out, self.read_line = key_source, say, out, read_line

    def parler(self, texte):
        self.out(texte + "\n")
        self.say(texte)

    def touches(self):
        for key in self.keys:
            if key == keys.WAKE:
                continue
            voix.stop()
            _arreter()
            yield key

    def touche(self):
        return next(self.touches(), ESC)

    # Un curseur : gauche/droite, pages, debut/fin.
    @staticmethod
    def deplacer(choix, actuel, key):
        try:
            k = choix.index(actuel)
        except ValueError:
            k = 0
        if key == RIGHT:
            n = k + 1
        elif key == LEFT:
            n = k - 1
        elif key == PGDN:
            n = min(k + GRAND_PAS, len(choix) - 1)
        elif key == PGUP:
            n = max(k - GRAND_PAS, 0)
        elif key in HOME:
            n = 0
        else:
            n = len(choix) - 1
        if 0 <= n < len(choix):
            return choix[n], None
        return actuel, dire("commun.debut_liste") if n < 0 else dire("commun.fin_liste")

    def haut_bas(self, champs, i, key, d):
        n = i + (1 if key == DOWN else -1)
        if 0 <= n < len(champs):
            return n, champ_texte(champs[n], d)
        bord = dire("commun.debut_liste") if n < 0 else dire("commun.fin_liste")
        return i, f"{bord} {champ_texte(champs[i], d)}"

    def annee(self, d):
        self.parler(dire("decors.annee_saisie"))
        while True:
            texte = "".join(self.read_line(self.keys).split())
            if not texte:
                d["annee"] = 0
                return champ_texte("annee", d)
            if texte.isdigit() and 2000 <= int(texte) <= 2200:
                d["annee"] = int(texte)
                return champ_texte("annee", d)
            self.parler(dire("decors.annee_invalide", texte=texte))

    def conditions(self, d):
        """Vrai : suivant ; False : abandon."""
        i = 0
        consigne = dire("decors.conditions_consigne")
        intro = conseils.intro("decors_conditions", dire("decors.conditions_titre"), consigne)
        self.parler(f"{intro} {champ_texte(CONDITIONS[0], d)}")
        for key in self.touches():
            champ = CONDITIONS[i]
            texte = None
            if conseils.is_help(key):
                texte = consigne
            elif key == ESC:
                return False
            elif key in (UP, DOWN):
                i, texte = self.haut_bas(CONDITIONS, i, key, d)
            elif key in (LEFT, RIGHT, PGUP, PGDN) + HOME + END:
                if champ in CHOIX:
                    d[champ], bord = self.deplacer(CHOIX[champ], d.get(champ), key)
                    texte = bord or valeur(champ, d)
                elif champ == "annee":
                    texte = dire("decors.annee_entree")
                else:
                    texte = champ_texte(champ, d)
            elif key in ENTREE:
                if champ == "annee":
                    texte = self.annee(d)
                else:
                    cle = decors.erreur(dict(d, action="annonce", texte="x"))
                    if cle:
                        texte = dire(cle)
                    else:
                        return True
            if texte:
                self.parler(texte)
        return False

    def action(self, d):
        """"enregistre", "retour" (aux conditions) ou None (plus de touches)."""
        candidats = decors.candidats()
        i = 0

        def champs():
            milieu = ["texte", "jingle"] if d["action"] == "annonce" else ["son", "telecharger"]
            return ["action"] + milieu + ["enregistrer"]

        consigne = dire("decors.action_consigne")
        intro = conseils.intro("decors_action", dire("decors.action_titre"), consigne)
        self.parler(f"{intro} {champ_texte('action', d)}")
        for key in self.touches():
            liste = champs()
            champ = liste[i]
            texte = None
            if conseils.is_help(key):
                texte = consigne
            elif key == ESC:
                return "retour"
            elif key in (UP, DOWN):
                i, texte = self.haut_bas(liste, i, key, d)
            elif key in (LEFT, RIGHT, PGUP, PGDN) + HOME + END:
                if champ in ("action", "jingle"):
                    d[champ], bord = self.deplacer(CHOIX[champ], d.get(champ), key)
                    texte = bord or valeur(champ, d)
                    if champ == "jingle" and not bord:
                        _jouer(decors.chemin_jingle(d["jingle"]))
                elif champ == "son":
                    if not candidats:
                        texte = dire("decors.son_vide")
                    else:
                        chemins = [c for _, c in candidats]
                        actuel = d.get("son") if d.get("son") in chemins else None
                        if actuel is None:
                            d["son"], bord = (chemins[-1] if key == LEFT else chemins[0]), None
                        else:
                            d["son"], bord = self.deplacer(chemins, actuel, key)
                        texte = bord or valeur("son", d)
                        if not bord:
                            _jouer(decors.chemin_son(d["son"]))
                elif champ == "texte":
                    texte = dire("decors.texte_entree")
                else:
                    texte = champ_texte(champ, d)
            elif key in ENTREE:
                if champ == "texte":
                    self.parler(dire("decors.texte_saisie"))
                    saisi = " ".join(self.read_line(self.keys).split())
                    if saisi:
                        d["texte"] = saisi
                    texte = champ_texte("texte", d)
                elif champ == "telecharger":
                    texte = self.telecharger(d)
                    candidats = decors.candidats()
                    if d.get("son"):
                        i = champs().index("son")
                elif champ == "enregistrer":
                    cle = decors.erreur(d)
                    if cle:
                        texte = dire(cle)
                    else:
                        return "enregistre"
                else:
                    texte = dire("decors.entree_ailleurs")
            if texte:
                self.parler(texte)
        return None

    def telecharger(self, d):
        import sons_en_ligne_cmd
        fin = sons_en_ligne_cmd.run("", self.keys, self.say, self.out, self.read_line, depot=decors.DEPOT)
        poses = getattr(sons_en_ligne_cmd.run, "poses", [])
        if not poses:
            return f"{fin} {dire('decors.telecharge_aucun')} {champ_texte('son', d)}"
        d["son"] = os.path.relpath(poses[0], decors.TRAIN_SCANER_DIR)
        _jouer(poses[0])
        return f"{fin} {champ_texte('son', d)}"

    def assistant(self, base):
        """Le decor enregistre, ou None."""
        d = dict(base)
        while True:
            if not self.conditions(d):
                return None
            r = self.action(d)
            if r == "enregistre":
                if not decors.enregistrer(d):
                    self.parler(dire("decors.nas"))
                    return None
                return d
            if r is None:
                return None


def _entree(liste, i):
    if i == 0:
        n = len(liste)
        quoi = nombre(n, "décor") if n else dire("decors.aucun")
        return f"{dire('decors.nouveau')} {quoi}."
    return decrire(liste[i - 1])


def run(line, key_source, say, out, read_line):
    dlg = _Dialogue(key_source, say, out, read_line)
    liste = decors.lire()
    i = 0
    consigne = dire("decors.consigne")
    intro = conseils.intro("decors", dire("decors.titre"), consigne)
    dlg.parler(f"{intro} {_entree(liste, i)}")
    try:
        for key in dlg.touches():
            texte = None
            if conseils.is_help(key):
                texte = consigne
            elif key == ESC:
                break
            elif key in (UP, DOWN):
                n = i + (1 if key == DOWN else -1)
                if 0 <= n <= len(liste):
                    i = n
                    texte = _entree(liste, i)
                else:
                    bord = dire("commun.debut_liste") if n < 0 else dire("commun.fin_liste")
                    texte = f"{bord} {_entree(liste, i)}"
            elif key in ENTREE:
                base = decors.nouveau() if i == 0 else liste[i - 1]
                fait = dlg.assistant(base)
                liste = decors.lire()
                if fait:
                    i = 1 + next((k for k, d in enumerate(liste) if d["id"] == fait.get("id")), len(liste) - 1)
                    texte = dire("decors.enregistre", decor=decrire(fait))
                else:
                    i = min(i, len(liste))
                    texte = f"{dire('decors.abandon')} {_entree(liste, i)}"
            elif key in BACKSPACE:
                if i == 0:
                    texte = dire("decors.rien_a_supprimer")
                else:
                    d = liste[i - 1]
                    dlg.parler(dire("decors.supprimer_question", decor=decrire(d)))
                    if dlg.touche() in OUI:
                        if decors.supprimer(d["id"]):
                            liste = decors.lire()
                            i = min(i, len(liste))
                            texte = f"{dire('decors.supprime')} {_entree(liste, i)}"
                        else:
                            texte = dire("decors.nas")
                    else:
                        texte = f"{dire('decors.garde')} {_entree(liste, i)}"
            else:
                texte = dire("decors.aide_liste")
            if texte:
                dlg.parler(texte)
    finally:
        _arreter()
    return dire("decors.ferme")
