"""Commande "mes capteurs" : les capteurs sans fil de l'utilisateur
(lib/capteurs_miens.py) et leur appairage.

Liste
  fleches haut / bas        parcourir
  I                         details : modele, numero, plante, dernieres valeurs
  N ou Tab                  appairer un nouveau capteur
  Retour arriere            retirer ; Ctrl + Z annule
Appairage ("nouveau capteur", "nouvelle sonde", ou N dans la liste)
  Le Pi reserve la radio des capteurs (433 et 868 MHz) pendant 5 minutes.
  On met la pile dans le capteur, pres du Pi : chaque capteur JAMAIS entendu
  ces 30 derniers jours est annonce (genre, modele, valeurs, force du
  signal), avec le geste qui prouve que c'est le sien (sonde dans un verre
  d'eau, eau dans le pluviometre, coupelles tournees a la main...). Quand
  le geste fait bouger les mesures, le Pi le dit.
  haut / bas    passer d'un capteur entendu a l'autre
  Entree        le garder (si le geste n'a pas ete reconnu : une deuxieme
                fois pour le garder quand meme)
  Puis : la plante (sonde de sol) ou le nom. Echap annule. F1 : les touches.
"""
import sys
import threading
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import capteurs_catalogue as cat  # noqa: E402
import capteurs_miens as cm  # noqa: E402
import conseils  # noqa: E402
import keys  # noqa: E402
import matcher  # noqa: E402
import plantes  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN = "\x1b[A", "\x1b[B"
BACKSPACE = ("\x7f", "\x08")
CTRL_Z, TAB, ESC, ENTER = "\x1a", "\t", "\x1b", ("\r", "\n")
PAIR_WORDS = {"nouveau", "nouvelle", "appairer", "appairage", "ajouter", "ajoute", "brancher"}
START_TIMEOUT_SEC = 90
VALUE_FIELDS = [("moisture", "capteur.valeur_sol"), ("temperature_C", "capteur.valeur_temperature"),
                ("humidity", "capteur.valeur_humidite"), ("rain_mm", "capteur.valeur_pluie"),
                ("wind_avg_km_h", "capteur.valeur_vent"), ("co2_ppm", "capteur.valeur_co2"),
                ("pm2_5_ug_m3", "capteur.valeur_particules")]


def values_text(values):
    parts = []
    for field, phrase in VALUE_FIELDS:
        v = values.get(field)
        if isinstance(v, (int, float)):
            parts.append(dire(phrase, valeur=f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",")))
    if values.get("battery_ok") == 0:
        parts.append(dire("capteurs.pile_faible"))
    return ", ".join(parts)


def _signal(event):
    snr = event.get("snr")
    if not isinstance(snr, (int, float)):
        return ""
    return dire("capteur.signal_fort" if snr >= 20 else "capteur.signal_moyen" if snr >= 10 else "capteur.signal_faible")


def candidate_text(c):
    e = c["dernier"]
    number = (f"{e.get('id')}" + (f", canal {e.get('channel')}" if e.get("channel") not in (None, "") else "")) \
        if e.get("id") is not None else "?"
    text = dire("capteur.entendu", genre=cat.NAMES[c["genre"]], modele=str(e.get("model")).replace("-", " "),
                numero=number)
    vals = values_text(e)
    if vals:
        text += " " + vals + "."
    signal = _signal(e)
    if signal:
        text += " " + signal
    gesture = cat.GESTURES.get(c["genre"])
    text += " " + (dire("capteur.geste_reussi") if c["geste"] else
                   dire(f"capteur.geste.{gesture}") if gesture else dire("capteur.geste_aucun"))
    return text


def _type_name(key_source, say, out, default):
    """Saisie d'un nom avec echo ; Entree sur une ligne vide garde default ; Echap -> None."""
    typed = ""
    say(dire("capteur.nom_question", defaut=default))
    for key in key_source:
        voix.stop()
        if key in ENTER:
            out("\n")
            return typed.strip() or default
        if key == ESC:
            return None
        if key in BACKSPACE:
            if typed:
                removed, typed = typed[-1], typed[:-1]
                out("\b \b")
                say(dire("plantes.efface_caractere", caractere=keys.char_name(removed)))
            continue
        if key == keys.WAKE:
            continue
        if len(key) == 1 and key.isprintable():
            typed += key
            out(key)
            say(keys.char_name(key))
    return None


def pair(key_source, say, out):
    """Appairage guide. Rend la phrase de fin."""
    say(dire("capteur.appairage_prepare"))
    known = cm.known_keys()
    tracker = cm.Pairing(known)
    state = {"courant": 0, "debut": None, "fini": False}
    lock = threading.Lock()
    stop = threading.Event()
    cm.request()

    def tell(text):
        out(f"[{text}]\n")
        say(text)

    def watch():
        offset = 0
        asked = time.time()
        while not stop.is_set():
            events, offset, started = cm.read_events(offset)
            if started and state["debut"] is None:
                state["debut"] = time.time()
                tell(dire("capteur.appairage_ecoute", minutes=cm.PAIRING_SEC // 60))
            for e in events:
                with lock:
                    for what, c in tracker.feed(e):
                        if what == "nouveau":
                            tell(dire("capteur.nouveau_entendu", nombre=len(tracker.candidates)) + " " + candidate_text(c))
                        else:
                            tell(dire("capteur.geste_vu", genre=cat.NAMES[c["genre"]]) + " "
                                 + dire("capteur.entree_pour_garder"))
            if state["debut"] is None and time.time() - asked > START_TIMEOUT_SEC:
                tell(dire("capteur.scanner_absent"))
                state["fini"] = True
                return
            if state["debut"] and not cm.requested():
                if not stop.is_set():
                    tell(dire("capteur.appairage_delai") if not tracker.candidates else dire("capteur.appairage_delai_choix"))
                state["fini"] = True
                return
            stop.wait(1)

    threading.Thread(target=watch, daemon=True).start()
    insist = None
    try:
        for key in key_source:
            if key == keys.WAKE:
                continue
            voix.stop()
            if key == ESC:
                return dire("capteur.appairage_annule")
            with lock:
                cands = list(tracker.candidates)
            if conseils.is_help(key):
                tell(dire("capteur.appairage_consigne"))
                continue
            if not cands:
                tell(dire("capteur.rien_entendu") if not state["fini"] else dire("capteur.appairage_fini_rien"))
                continue
            if key in (UP, DOWN):
                j = state["courant"] + (-1 if key == UP else 1)
                if 0 <= j < len(cands):
                    state["courant"] = j
                    insist = None
                    tell(candidate_text(cands[j]))
                else:
                    tell((dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")) + " "
                         + candidate_text(cands[state["courant"]]))
                continue
            if key in ENTER:
                c = cands[min(state["courant"], len(cands) - 1)]
                testable = cat.GESTURES.get(c["genre"]) is not None
                if testable and not c["geste"] and insist != c["cle"]:
                    insist = c["cle"]
                    tell(dire("capteur.geste_pas_vu"))
                    continue
                stop.set()
                cm.stop()
                return _finish(key_source, say, out, c)
            tell(dire("capteur.appairage_touches"))
        return dire("capteur.appairage_annule")
    finally:
        stop.set()
        cm.stop()


def _finish(key_source, say, out, c):
    """Rattachement a une plante (sonde de sol) ou nom, puis enregistrement."""
    plant_id = None
    label = cat.NAMES[c["genre"]]
    default = label
    if c["genre"] == "sol":
        plants = plantes.load()
        if plants:
            import plantes_cmd
            options = [p["nom"] for p in plants] + [dire("capteur.aucune_plante")]
            try:
                k = plantes_cmd._ask(key_source, say, out, dire("capteur.quelle_plante"), options)
            except plantes_cmd.Closed:
                return dire("capteur.appairage_annule")
            if k < len(plants):
                plant_id = plants[k]["id"]
                default = dire("capteur.nom_sonde_de", plante=plants[k]["nom"])
    name = _type_name(key_source, say, out, default)
    if name is None:
        return dire("capteur.appairage_annule")
    entry = cm.entry_from(c, name, plant_id)
    items = [x for x in cm.load() if x["cle"] != entry["cle"]] + [entry]
    if not cm.save(items):
        return dire("plantes.nas_inaccessible")
    text = dire("capteur.appaire", nom=name, genre=label)
    if plant_id:
        text += " " + dire("capteur.appaire_plante")
    if entry.get("etalonnage"):
        text += " " + dire("capteur.etalonne")
    return text


def line(entry):
    values = cm.latest(entry)
    if values and entry.get("genre") == "sol":
        pct = cat.soil_percent(values, entry.get("etalonnage"))
        state = dire("capteur.valeur_sol", valeur=f"{pct:.0f}") if pct is not None else values_text(values)
    else:
        state = values_text(values) if values else dire("capteur.pas_de_releve")
    return dire("capteur.ligne_mien", nom=entry["nom"], genre=cat.NAMES[entry.get("genre") or "autre"],
                etat=state or dire("capteur.pas_de_releve"))


def details(entry):
    parts = [line(entry) + ".", dire("capteur.details", modele=entry.get("modele") or "?", numero=entry.get("id"),
                                     date=entry.get("ajoute_le") or "?")]
    if entry.get("plante_id"):
        plant = next((p for p in plantes.load() if p["id"] == entry["plante_id"]), None)
        if plant:
            parts.append(dire("capteur.details_plante", plante=plant["nom"]))
    if entry.get("etalonnage"):
        parts.append(dire("capteur.etalonne"))
    return " ".join(parts)


def run(line_typed, key_source, say, out):
    keys_iter = iter(key_source)
    if set(matcher.normalize(line_typed).split()) & PAIR_WORDS:
        return pair(keys_iter, say, out)
    items = cm.load()
    i = 0
    undo = []
    title = dire("capteur.mes_titre", nombre=nombre(len(items), "capteur")) if items else dire("capteur.mes_titre_vide")
    intro = conseils.intro("mes_capteurs", title, dire("capteur.mes_consigne"))
    out(intro + "\n")
    say(intro + (" " + line(items[0]) if items else ""))
    for key in keys_iter:
        if key == keys.WAKE:
            continue
        voix.stop()
        if key == ESC:
            return dire("capteur.mes_fermes")
        if key in (TAB, "n", "N"):
            text = pair(keys_iter, say, out)
            items = cm.load()
            i = min(i, max(len(items) - 1, 0))
        elif conseils.is_help(key):
            text = dire("capteur.mes_consigne")
        elif key == CTRL_Z:
            if not undo:
                text = dire("plantes.rien_a_annuler")
            else:
                at, back = undo.pop()
                items.insert(at, back)
                if cm.save(items):
                    i, text = at, dire("capteur.remis", nom=back["nom"])
                else:
                    items.pop(at)
                    text = dire("plantes.nas_inaccessible")
        elif not items:
            text = dire("capteur.mes_titre_vide")
        elif key in (UP, DOWN):
            j = i + (-1 if key == UP else 1)
            if 0 <= j < len(items):
                i = j
                text = line(items[i])
            else:
                text = (dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")) + " " + line(items[i])
        elif key in ("i", "I"):
            text = details(items[i])
        elif key in BACKSPACE:
            gone = items.pop(i)
            if cm.save(items):
                undo.append((i, gone))
                i = min(i, max(len(items) - 1, 0))
                text = dire("capteur.retire", nom=gone["nom"]) + (" " + line(items[i]) if items else "")
            else:
                items.insert(i, gone)
                text = dire("plantes.nas_inaccessible")
        else:
            text = dire("plantes.touches")
        out(f"[{text}]\n")
        say(text)
    return dire("capteur.mes_fermes")
