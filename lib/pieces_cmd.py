"""Commande "pièces" : chercher un fichier imprimable (Printables).

Dialogue :
  tape la recherche (ou passe-la apres la commande), Entree
  fleches haut / bas     resultat precedent / suivant
  1 a 9                  aller au resultat N
  Entree                 ouvre la fiche
  N                      nouvelle recherche
  Echap ferme. F1 : les touches.

Fiche :
  fleches haut / bas           morceau precedent / suivant
  Ctrl + fleches haut / bas    section precedente / suivante
                               (titre, description, images, stats, commentaires…)
  En fin de lot de commentaires : flèche bas propose le lot suivant,
  flèche bas encore le charge et se place au début du nouveau lot.
  Ctrl + haut sort toujours des commentaires vers la section précédente.
  Retour arriere               revient a la liste
  V                            valider (passe Gemini)
  Echap ferme. F1 : les touches.
"""
import sys
import threading

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import keys  # noqa: E402
import matcher  # noqa: E402
import pieces  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN = "\x1b[A", "\x1b[B"
ESC, ENTER = "\x1b", ("\r", "\n")
BACKSPACE = ("\x7f", "\x08")

STOP = {
    "pieces", "piece", "cherche", "chercher", "rechercher", "recherche",
    "modele", "modeles", "fichier", "fichiers", "imprimable", "imprimables",
    "printables", "impression", "imprimer", "3d", "trois", "dimensions",
}


def _query_from_line(line):
    words = matcher.normalize(line or "").split()
    kept = [w for w in words if w not in STOP]
    return " ".join(kept).strip()


def _ask_query(key_source, say, out, read_line, prompt):
    out(prompt + "\n? ")
    say(prompt)
    typed = read_line(key_source)
    return " ".join((typed or "").split()).strip()


def _liste(key_source, say, out, hits, query):
    i = 0
    title = dire("pieces.titre", n=nombre(len(hits), "résultat"), q=query)
    intro = conseils.intro("pieces", title, dire("pieces.consigne_liste"))
    out(intro + "\n")
    say(f"{intro} {pieces.ligne_liste(1, hits[0])}")
    for key in key_source:
        voix.stop()
        text = None
        if key == ESC:
            return ("ferme",)
        if conseils.is_help(key):
            text = dire("pieces.consigne_liste")
        elif key in (UP, DOWN):
            j = i + (-1 if key == UP else 1)
            if 0 <= j < len(hits):
                i = j
                text = pieces.ligne_liste(i + 1, hits[i])
            else:
                edge = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
                text = edge + " " + pieces.ligne_liste(i + 1, hits[i])
        elif key in ENTER:
            return ("fiche", i)
        elif key in ("n", "N"):
            return ("nouvelle",)
        elif len(key) == 1 and key.isdigit():
            n = int(key)
            if 1 <= n <= len(hits):
                i = n - 1
                text = pieces.ligne_liste(i + 1, hits[i])
            else:
                text = dire("pieces.hors_liste", n=n)
        else:
            text = dire("pieces.touches_liste")
        if text:
            out(f"[{text}]\n")
            say(text)
    return ("ferme",)


def _ctrl_held():
    return keys.modifier_held("ctrl")



def _fiche(key_source, say, out, fiche):
    """Fiche navigable. Rend 'retour' | 'validee' | 'ferme'."""
    pieces.mark_deja_vu(fiche["source"], fiche["id"])
    pending_more = False
    locked = False
    last_img_announce = None
    stop_ticker = threading.Event()

    def _images_ticker():
        while not stop_ticker.wait(12):
            if fiche.get("images_desc_status") != "en_cours":
                continue
            try:
                keys.wake()
            except OSError:
                break

    if (fiche.get("images_desc_status") or "") == "en_cours":
        threading.Thread(target=_images_ticker, daemon=True).start()

    def rebuild():
        secs = pieces.sections_fiche(fiche, include_fine=True)
        return secs, pieces.flatten_sections(secs)

    def section_index(sec_id):
        for i, sec in enumerate(sections):
            if sec["id"] == sec_id:
                return i
        return None

    def first_of(sec_id):
        si = section_index(sec_id)
        if si is None:
            return 0
        for i, (s, _, _) in enumerate(flat):
            if s == si:
                return i
        return 0

    def last_of(sec_id):
        si = section_index(sec_id)
        if si is None:
            return 0
        last = 0
        for i, (s, _, _) in enumerate(flat):
            if s == si:
                last = i
        return last

    def at_comments_end():
        si = section_index("commentaires")
        return si is not None and flat and flat[pos][0] == si and pos == last_of("commentaires")

    def speak_at(i, announce_section=False):
        si, _, text = flat[i]
        if announce_section:
            text = f"{sections[si]['titre']}. {text}"
        return text

    def refresh_if_images(si):
        """Recharge le cache si la section Images a pu recevoir le VLM."""
        nonlocal fiche, sections, flat
        if si is None or sections[si]["id"] != "images":
            return False
        fresh = pieces.refresh_fiche(fiche)
        changed = (
            fresh.get("images_desc") != fiche.get("images_desc")
            or fresh.get("images_desc_status") != fiche.get("images_desc_status")
        )
        if changed:
            fiche = fresh
            sections, flat = rebuild()
        return changed

    def poll_pending_images():
        """Pendant en_cours : relit le cache ; progres verbeux ; si pret, saute a Images."""
        nonlocal fiche, sections, flat, pos, last_img_announce
        if fiche.get("images_desc_status") != "en_cours":
            return None
        fresh = pieces.refresh_fiche(fiche)
        if fresh.get("images_desc_status") == "en_cours":
            was = fresh.get("images_job")
            fresh, _ = pieces.request_images_desc(fresh)
            fiche = fresh
            if fiche.get("images_job") != was:
                sections, flat = rebuild()
            msg = pieces.status_images(fiche)
            if msg and msg != last_img_announce:
                last_img_announce = msg
                return msg
            return None
        if (fresh.get("images_desc") or "").strip() and fresh.get("images_desc_status") == "prete":
            fiche = fresh
            sections, flat = rebuild()
            pos = first_of("images")
            last_img_announce = None
            return dire("pieces.images_pretes") + " " + speak_at(pos, announce_section=True)
        if fresh.get("images_desc_status") == "echec":
            fiche = fresh
            sections, flat = rebuild()
            last_img_announce = None
            return dire("pieces.fiche_images_echec")
        return None


    def go_section(si):
        nonlocal pos
        refresh_if_images(si)
        pos = first_of(sections[si]["id"])
        return speak_at(pos, announce_section=True)

    sections, flat = rebuild()
    pos = 0

    intro = conseils.intro("pieces_fiche", dire("pieces.fiche_ouverte"), dire("pieces.consigne_fiche"))
    first = speak_at(0, announce_section=True) if flat else dire("pieces.fiche_vide")
    opening = f"{intro} {first}".strip()
    img_st = (fiche.get("images_desc_status") or "").strip()
    if img_st == "en_cours":
        opening = f"{opening} {pieces.status_images(fiche)}".strip()
    elif img_st == "prete" and (fiche.get("images_desc") or "").strip():
        opening = f"{opening} {dire('pieces.images_pretes')}".strip()
    out(opening + "\n")
    say(opening)

    def jump_section(delta):
        nonlocal pos, pending_more
        pending_more = False
        if not flat:
            return dire("pieces.fiche_vide")
        cur_si = flat[pos][0]
        target = cur_si + delta
        if target < 0:
            pos = 0
            return dire("pieces.premiere_section") + " " + speak_at(0, announce_section=True)
        if target >= len(sections):
            last_si = len(sections) - 1
            pos = last_of(sections[last_si]["id"])
            return dire("pieces.derniere_section") + " " + speak_at(pos, announce_section=True)
        return go_section(target)

    def load_next_batch():
        nonlocal fiche, sections, flat, pos, pending_more, locked
        remaining = pieces.comments_remaining(fiche)
        if remaining <= 0:
            pending_more = False
            return dire("commun.fin_liste") + " " + flat[pos][2]
        locked = True
        msg = dire("pieces.commentaires_chargement")
        out(f"[{msg}]\n")
        say(msg)
        try:
            next_offset = int(fiche.get("comments_offset") or 0) + len(fiche.get("comments") or [])
            fiche = pieces.load_comments_batch(fiche, next_offset)
        except Exception as exc:
            print(f"[pieces] comments batch : {exc!r}")
            locked = False
            pending_more = False
            return dire("pieces.commentaires_chargement_echec")
        sections, flat = rebuild()
        pos = first_of("commentaires")
        pending_more = False
        locked = False
        return speak_at(pos, announce_section=True)

    def step(delta):
        nonlocal pos, pending_more, fiche, sections, flat
        if not flat:
            return dire("pieces.fiche_vide")
        j = pos + delta
        if not (0 <= j < len(flat)):
            edge = dire("commun.debut_liste") if j < 0 else dire("commun.fin_liste")
            return edge + " " + flat[pos][2]
        crossed = flat[j][0] != flat[pos][0]
        if crossed:
            return go_section(flat[j][0])
        # Dans Images, un pas peut reveler une desc arrivee entre-temps
        if sections[flat[pos][0]]["id"] == "images":
            if refresh_if_images(flat[pos][0]):
                pos = min(j, len(flat) - 1)
                # Si on etait sur "en cours" et que la desc est prete, relire depuis le debut
                if fiche.get("images_desc"):
                    pos = first_of("images")
                    return speak_at(pos, announce_section=True)
        pos = j
        return flat[pos][2]

    try:
      for key in key_source:
        if locked:
            continue
        if key != keys.WAKE:
            voix.stop()
        said = None
        ready = poll_pending_images()
        if ready:
            out(f"[{ready}]\n")
            say(ready)
            # la touche est consommee comme accuse de reception
            continue
        if key == ESC:
            return "ferme"
        if conseils.is_help(key):
            said = dire("pieces.consigne_fiche")
            pending_more = False
        elif key in BACKSPACE or key in ("r", "R"):
            return "retour"
        elif key in (UP, DOWN) and _ctrl_held():
            said = jump_section(-1 if key == UP else 1)
        elif key == UP:
            pending_more = False
            said = step(-1)
        elif key == DOWN:
            if at_comments_end() and pieces.comments_remaining(fiche) > 0:
                if pending_more:
                    said = load_next_batch()
                else:
                    n = pieces.comments_remaining(fiche)
                    pending_more = True
                    said = dire(
                        "pieces.commentaires_plus",
                        n=nombre(n, "commentaire supplémentaire", "commentaires supplémentaires"),
                    )
            else:
                pending_more = False
                said = step(1)
        elif key in ("v", "V"):
            pending_more = False
            say(dire("pieces.validation_en_cours"))
            out(dire("pieces.validation_en_cours") + "\n")
            fine, err = pieces.valider_gemini(fiche)
            if err == "cle":
                said = dire("pieces.validation_sans_cle")
            elif err == "quota":
                said = dire("pieces.validation_quota")
            elif err or not fine:
                said = dire("pieces.validation_echec")
            else:
                comments = fiche.get("comments")
                total = fiche.get("comments_total")
                offset = fiche.get("comments_offset")
                img_job = fiche.get("images_job")
                img_status = fiche.get("images_desc_status")
                img_desc = fiche.get("images_desc")
                fiche.clear()
                fiche.update(fine)
                if comments is not None:
                    fiche["comments"] = comments
                    fiche["comments_total"] = total
                    fiche["comments_offset"] = offset
                if img_desc and not fiche.get("images_desc"):
                    fiche["images_desc"] = img_desc
                if img_job:
                    fiche["images_job"] = img_job
                if img_status:
                    fiche["images_desc_status"] = img_status
                sections, flat = rebuild()
                pos = first_of("fine") if section_index("fine") is not None else pos
                said = dire("pieces.validation_ok") + " " + speak_at(pos, announce_section=True)
        else:
            said = dire("pieces.touches_fiche")
            pending_more = False
        if said:
            out(f"[{said}]\n")
            say(said)
    finally:
        stop_ticker.set()
    return "ferme"



def run(line, key_source, say, out, read_line):
    query = _query_from_line(line)
    while True:
        if not query:
            query = _ask_query(key_source, say, out, read_line, dire("pieces.demande_recherche"))
            if not query:
                return dire("pieces.fermee")
        say(dire("pieces.recherche_en_cours", q=query))
        try:
            hits, used = pieces.search(query, limit=8)
        except Exception as exc:
            print(f"[pieces] search : {exc!r}")
            return dire("pieces.reseau")
        if not hits:
            say(dire("pieces.rien", q=query))
            query = _ask_query(key_source, say, out, read_line, dire("pieces.demande_recherche"))
            if not query:
                return dire("pieces.fermee")
            continue
        pieces.session_save({"query": used, "ids": [h["id"] for h in hits]})
        while True:
            action = _liste(key_source, say, out, hits, used)
            if action[0] == "ferme":
                return dire("pieces.fermee")
            if action[0] == "nouvelle":
                query = ""
                break
            i = action[1]
            say(dire("pieces.ouverture_fiche"))
            try:
                fiche = pieces.open_fiche(hits[i])
            except Exception as exc:
                print(f"[pieces] detail : {exc!r}")
                say(dire("pieces.fiche_echec"))
                continue
            hits[i]["deja_vu"] = True
            result = _fiche(key_source, say, out, fiche)
            if result == "ferme":
                return dire("pieces.fermee")
            continue
