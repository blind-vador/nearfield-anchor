"""Sonneries : un nom, trois dossiers.

Le nom est en morceaux séparés par _ :
    {bande}_{incoming|freq_open|freq_over}
    {bande}_{private_call|public_call}_{incoming|freq_open|freq_over}
private_call et public_call ne sont ajoutés que si le décodeur sait
que l'appel est privé ou public.

depot : on y dépose un son, quel que soit son nom.
default_ringtone : les sonneries d'origine.
radio_scaner : les sonneries actives, sous le nom ci-dessus.
Sans sonnerie propre, on reprend la générale (incoming, freq_open, freq_over).
"""
import os
import re
import shutil

from sound_paths import NAS_BASE, RADIO_SCANER_DIR

DEPOT = os.path.join(NAS_BASE, "sonneries", "depot")
DEFAUT = os.path.join(NAS_BASE, "sonneries", "default_ringtone")
ACTIVES = RADIO_SCANER_DIR
EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus")
APPELS = ("public_call", "private_call")
EVENEMENTS = ("incoming", "freq_open", "freq_over")
_RADIO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "watchers", "radio_watch.py")
_bandes = None


def _audios(dossier):
    try:
        noms = os.listdir(dossier)
    except OSError:
        return []
    out = []
    for nom in noms:
        if os.path.splitext(nom)[1].lower() in EXTENSIONS:
            chemin = os.path.join(dossier, nom)
            if os.path.isfile(chemin):
                out.append(chemin)
    return sorted(out, key=lambda p: os.path.basename(p).lower())


def assurer():
    """Crée le dépôt et les sonneries d'origine. La première fois, on y copie
    les sons déjà en place, sans les enlever des sonneries actives."""
    os.makedirs(DEPOT, exist_ok=True)
    os.makedirs(DEFAUT, exist_ok=True)
    if _audios(DEFAUT):
        return
    for src in _audios(ACTIVES):
        dest = os.path.join(DEFAUT, os.path.basename(src))
        if not os.path.exists(dest):
            try:
                shutil.copyfile(src, dest)
            except OSError as exc:
                print(f"[sonneries] copie d'origine impossible ({src}) : {exc}")


def bandes():
    """(id, nom parlé, bas, haut) lus dans les bandes du scanner, plus le PMR 446."""
    global _bandes
    if _bandes is not None:
        return _bandes
    try:
        texte = open(os.path.abspath(_RADIO), encoding="utf-8").read()
    except OSError:
        texte = ""
    debut = texte.find("BANDS = [")
    fin = texte.find("\n]\n", debut) if debut >= 0 else -1
    bloc = texte[debut:fin] if fin > debut else ""
    marques = [m.start() for m in re.finditer(r'"name": "', bloc)]
    out = []
    for i, start in enumerate(marques):
        morceau = bloc[start:marques[i + 1] if i + 1 < len(marques) else len(bloc)]
        nom = re.search(r'"name": "([^"]+)"', morceau)
        parle = re.search(r'"name_spoken": "([^"]+)"', morceau)
        bas = re.search(r'"low_hz": ([0-9_]+)', morceau)
        haut = re.search(r'"high_hz": ([0-9_]+)', morceau)
        if not (nom and parle and bas and haut):
            continue
        out.append((nom.group(1), parle.group(1),
                    int(bas.group(1).replace("_", "")), int(haut.group(1).replace("_", ""))))
    if not any(b[0] == "pmr446" for b in out):
        out.append(("pmr446", "PMR 446", 446_000_000, 446_200_000))
    _bandes = out
    return out


def bande_pour(freq_hz):
    """La bande la plus étroite qui contient la fréquence."""
    try:
        freq = float(freq_hz)
    except (TypeError, ValueError):
        return None
    dedans = [b for b in bandes() if b[2] <= freq <= b[3]]
    if not dedans:
        return None
    dedans.sort(key=lambda b: b[3] - b[2])
    return dedans[0][0]


def _dans(dossier, tige):
    for ext in EXTENSIONS:
        chemin = os.path.join(dossier, tige + ext)
        if os.path.isfile(chemin):
            return chemin
    return None


def tige(bande, evenement, appel=None):
    morceaux = [m for m in (bande, appel if appel in APPELS else None, evenement) if m]
    return "_".join(morceaux)


# Ces événements radio ont leur groupe (DMR, TETRA, ou la fin PMR).
# On ne les remet pas dans la liste des messages : ce serait le même son deux fois.
_GROUPES_NOMMES = {
    "dmr_vocal": "dmr", "dmr_nouveau": "dmr",
    "tetra_ouverture": "tetra", "tetra_appel_prive": "tetra",
    "tetra_message": "tetra", "tetra_nouvelle_porteuse": "tetra",
    "pmr446_fin": None,
}
_ORDRE_VEILLES = ("radio_scaner", "flight_scaner", "meteo_scaner", "terrain",
                  "plantes", "camera", "systeme", "jingles")


def resoudre(bande, evenement, appel=None, groupe=None):
    """Son à jouer, du plus précis au plus général.

    Un appel public ou privé sans son d'arrivée propre reprend le son du
    groupe (voix DMR, ouverture ou appel privé TETRA), puis celui de la
    bande, puis la sonnerie radio générale. L'ouverture et la fin ne
    prennent pas ce son de groupe : elles restent celles de la bande.
    """
    essais = []
    if bande and appel in APPELS:
        essais.append(tige(bande, evenement, appel))
    # Le son du groupe (voix DMR, ouverture TETRA) ne remplace que l'arrivée.
    # L'ouverture reste celle de la bande, puis la générale : sinon le même
    # sting est joué deux fois, et freq_open n'est plus jamais entendu.
    if appel in APPELS and evenement == "incoming":
        if groupe == "dmr":
            essais.append("dmr_vocal")
        elif groupe == "tetra":
            essais.append("tetra_appel_prive" if appel == "private_call" else "tetra_ouverture")
    if bande:
        essais.append(tige(bande, evenement))
    if bande == "pmr446" and evenement == "freq_over":
        essais.append("pmr446_fin")
    essais.append(evenement)
    for nom in essais:
        trouve = _dans(ACTIVES, nom)
        if trouve:
            return trouve
    return None


def _slot(veille, groupe, evenement, bande=None, parle=None, appel=None, catalogue=False):
    return {"veille": veille, "groupe": groupe, "bande": bande, "parle": parle,
            "appel": appel, "evenement": evenement, "catalogue": catalogue}


def slots():
    """Toutes les veilles, sauf les sons SNCF. La radio : une bande, puis
    DMR, TETRA, puis les messages. Le reste : une veille, ses événements."""
    import sons
    out = []
    for bande, parle, _bas, _haut in bandes():
        for appel in (None,) + APPELS:
            for evenement in EVENEMENTS:
                out.append(_slot("radio_scaner", bande, evenement, bande, parle, appel))
    for evenement, groupe in (("dmr_vocal", "dmr"), ("dmr_nouveau", "dmr"),
                              ("tetra_ouverture", "tetra"), ("tetra_appel_prive", "tetra"),
                              ("tetra_message", "tetra"), ("tetra_nouvelle_porteuse", "tetra")):
        out.append(_slot("radio_scaner", groupe, evenement, catalogue=True))
    rang = {nom: i for i, nom in enumerate(_ORDRE_VEILLES)}
    autres = []
    for evenement, rel in sons.SONS.items():
        if evenement in _GROUPES_NOMMES:
            continue
        veille = str(rel).split("/", 1)[0]
        if veille not in rang:
            continue
        groupe = "messages" if veille == "radio_scaner" else veille
        autres.append(_slot(veille, groupe, evenement, catalogue=True))
    autres.sort(key=lambda s: (rang.get(s["veille"], 99), s["evenement"]))
    out.extend(autres)
    return out


def saut(items, i, pas, quoi):
    """Groupe suivant dans la veille, ou veille suivante. None au bord d'un groupe."""
    if not items:
        return i
    if quoi == "veille":
        ordre = list(dict.fromkeys(s["veille"] for s in items))
        k = (ordre.index(items[i]["veille"]) + pas) % len(ordre)
        cible = ordre[k]
        return next(n for n, s in enumerate(items) if s["veille"] == cible)
    veille = items[i]["veille"]
    ordre = list(dict.fromkeys(s["groupe"] for s in items if s["veille"] == veille))
    k = ordre.index(items[i]["groupe"]) + pas
    if k < 0 or k >= len(ordre):
        return None
    cible = ordre[k]
    return next(n for n, s in enumerate(items) if s["veille"] == veille and s["groupe"] == cible)


def tige_slot(slot):
    if slot.get("catalogue"):
        import sons
        rel = sons.SONS.get(slot["evenement"]) or slot["evenement"]
        return os.path.splitext(os.path.basename(rel))[0]
    return tige(slot.get("bande"), slot.get("evenement"), slot.get("appel"))


def dossier_slot(slot):
    """Où vit le fichier : la veille pour un événement du catalogue, sinon la radio."""
    if slot.get("catalogue"):
        import sons
        rel = sons.SONS.get(slot["evenement"]) or ""
        sous = os.path.dirname(rel)
        if sous:
            return os.path.join(NAS_BASE, sous)
    return ACTIVES


def active(slot):
    return _dans(dossier_slot(slot), tige_slot(slot))


def entendre(slot):
    """Le son que cette entrée joue vraiment : le sien, ou le repli."""
    propre = active(slot)
    if propre:
        return propre
    if slot.get("catalogue"):
        import sons
        return sons.chemin(slot["evenement"])
    groupe = slot.get("groupe") if slot.get("groupe") in ("dmr", "tetra") else None
    return resoudre(slot.get("bande"), slot.get("evenement"), slot.get("appel"), groupe)


def candidats():
    """D'abord le dépôt, puis les sonneries d'origine. (origine, chemin)."""
    assurer()
    return [("depot", p) for p in _audios(DEPOT)] + [("defaut", p) for p in _audios(DEFAUT)]


def _dedans(chemin, dossier):
    try:
        return os.path.commonpath([os.path.realpath(chemin), os.path.realpath(dossier)]) == os.path.realpath(dossier)
    except ValueError:
        return False


def poser(slot, origine, source):
    """Dépôt : le fichier quitte le dépôt et prend le nom de l'événement.
    Défaut : on en copie une, sous ce même nom. Rend le chemin, ou None."""
    if origine not in ("depot", "defaut") or not source or not os.path.isfile(source):
        return None
    dossier = DEPOT if origine == "depot" else DEFAUT
    if not _dedans(source, dossier):
        return None
    ext = os.path.splitext(source)[1].lower()
    if ext not in EXTENSIONS:
        return None
    dossier = dossier_slot(slot)
    os.makedirs(dossier, exist_ok=True)
    dest = os.path.join(dossier, tige_slot(slot) + ext)
    for autre in EXTENSIONS:
        chemin = os.path.join(dossier, tige_slot(slot) + autre)
        if os.path.isfile(chemin) and os.path.realpath(chemin) != os.path.realpath(source):
            try:
                os.remove(chemin)
            except OSError:
                pass
    try:
        if origine == "depot":
            if os.path.realpath(source) != os.path.realpath(dest):
                os.replace(source, dest)
        else:
            shutil.copyfile(source, dest)
    except OSError as exc:
        print(f"[sonneries] rangement impossible : {exc}")
        return None
    _miroir(dest)
    return dest


def _miroir(dest):
    """La veille lit souvent la copie locale : on la met à jour avec le NAS."""
    import sons
    try:
        rel = os.path.relpath(dest, NAS_BASE)
    except ValueError:
        return
    if rel.startswith(".."):
        return
    local = os.path.join(sons.LOCAL_BASE, rel)
    try:
        os.makedirs(os.path.dirname(local), exist_ok=True)
        shutil.copyfile(dest, local)
    except OSError as exc:
        print(f"[sonneries] copie locale : {exc}")
