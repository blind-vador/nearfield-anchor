"""Recherche de pieces imprimables : session, deja-vu, cache fiches, Gemini.

Stockage NAS (survive au reboot) :
  nearfield-anchor-pieces/deja_vu.json
  nearfield-anchor-pieces/fiches/<source>_<id>.json

La passe images locale (VLM Mac) viendra via le pont compagnon ; en attendant
images_desc reste vide et la fiche le dit clairement. Valider appelle Gemini
(meme cle que vision.py) pour une fiche fine mise en cache.
"""
import chemins
import base64
import json
import re
import os
import time
import urllib.error
import urllib.request

import printables
import reglages

STORE = "/media/nas/nd1/server/nearfield-anchor-pieces"
PRETS = os.path.join(STORE, "prets")
CACHE_SD = chemins.base("pieces_cache")
SESSION = "/tmp/pieces_session.json"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]
REQUEST_TIMEOUT_SEC = 60
COMMENTS_BATCH = 8
IMAGES_DESC_VIA = "vlm-en+argos-fr"  # invalide le cache si pipeline change
IMAGES_JOB_STALE_SEC = 120  # re-soumettre si en_cours trop longtemps

# Expansion FR -> mots EN utiles pour les catalogues (sans LLM).
SYNONYMES = {
    "iss": ["ISS", "International Space Station", "space station"],
    "station spatiale": ["space station", "ISS", "orbital station"],
    "maquette": ["model", "scale model", "miniature"],
    "tiroir": ["drawer", "organizer"],
    "crochet": ["hook", "wall hook", "hanger"],
    "support": ["mount", "holder", "bracket", "stand"],
    "boite": ["box", "case", "container"],
    "boîte": ["box", "case", "container"],
    "cable": ["cable", "wire", "cable management", "cable chain"],
    "câble": ["cable", "wire", "cable management", "cable chain"],
    "ender": ["ender 3", "ender3", "creality"],
    "rangement": ["organizer", "storage", "holder"],
    "pot": ["pot", "planter", "vase"],
    "lampe": ["lamp", "light"],
}


def _ensure(path):
    os.makedirs(path, exist_ok=True)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    _ensure(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _key(source, model_id):
    return f"{source}:{model_id}"


def _fiche_path(source, model_id):
    safe = f"{source}_{model_id}.json"
    nas = os.path.join(STORE, "fiches", safe)
    sd = os.path.join(CACHE_SD, "fiches", safe)
    return nas, sd


def deja_vu_set():
    data = _read_json(os.path.join(STORE, "deja_vu.json"), None)
    if data is None:
        data = _read_json(os.path.join(CACHE_SD, "deja_vu.json"), {"ids": []})
    return set(data.get("ids") or [])


def mark_deja_vu(source, model_id):
    ids = deja_vu_set()
    ids.add(_key(source, model_id))
    payload = {"ids": sorted(ids), "maj": time.time()}
    try:
        _write_json(os.path.join(STORE, "deja_vu.json"), payload)
    except OSError:
        pass
    try:
        _write_json(os.path.join(CACHE_SD, "deja_vu.json"), payload)
    except OSError:
        pass


def is_deja_vu(source, model_id):
    return _key(source, model_id) in deja_vu_set()


def load_fiche(source, model_id):
    nas, sd = _fiche_path(source, model_id)
    data = _read_json(nas, None)
    if data is None:
        data = _read_json(sd, None)
    return data


def save_fiche(fiche):
    nas, sd = _fiche_path(fiche["source"], fiche["id"])
    fiche = dict(fiche)
    fiche["maj"] = time.time()
    try:
        _write_json(nas, fiche)
    except OSError:
        pass
    try:
        _write_json(sd, fiche)
    except OSError:
        pass


def _note_fr(rating):
    if rating is None:
        return None
    if float(rating) <= 0:
        return None
    # 4.965 -> "5" : une decimale suffit a l'oral
    v = round(float(rating), 1)
    text = f"{v:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def expand_query(text):
    """Version anglaise courte pour Printables (AND strict : pas de melange FR+EN)."""
    raw = " ".join((text or "").split()).strip()
    if not raw:
        return ""
    low = raw.lower()
    remaining = low
    parts = []
    for fr, en_list in sorted(SYNONYMES.items(), key=lambda kv: -len(kv[0])):
        if fr in remaining:
            parts.append(en_list[0])  # un seul synonyme par locution
            remaining = remaining.replace(fr, " ")
    for w in remaining.split():
        if len(w) > 1:
            parts.append(w)
    # Printables traite la requete en AND : 3-4 mots max
    out = []
    seen = set()
    for w in parts:
        k = w.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(w)
        if len(out) >= 4:
            break
    joined = " ".join(out).strip()
    return joined if joined and joined.lower() != low else ""


def search(query, limit=8):
    """Essaie la requete telle quelle, puis une version etendue si vide."""
    raw = " ".join((query or "").split()).strip()
    if not raw:
        return [], ""
    tried = []
    for q in (raw, expand_query(raw)):
        if not q or q in tried:
            continue
        tried.append(q)
        try:
            items = printables.search(q, limit=limit)
        except Exception as exc:
            print(f"[pieces] search {q!r} : {exc!r}")
            items = []
        if items:
            for it in items:
                it["deja_vu"] = is_deja_vu(it["source"], it["id"])
            return items, q
    return [], raw


def open_fiche(hit):
    """Charge detail Printables, fusionne cache, rend la fiche."""
    cached = load_fiche(hit["source"], hit["id"]) or {}
    detail = printables.detail(hit["id"], comments_limit=COMMENTS_BATCH, comments_offset=0)
    # Preserve enrichissements deja calcules
    if cached.get("images_desc"):
        detail["images_desc"] = cached["images_desc"]
    if cached.get("fiche_fine"):
        detail["fiche_fine"] = cached["fiche_fine"]
    save_fiche(detail)
    mark_deja_vu(detail["source"], detail["id"])
    detail, _ = request_images_desc(detail)
    return detail


def load_comments_batch(fiche, offset):
    """Charge le lot de commentaires a offset ; met a jour la fiche et le cache."""
    batch = printables.comments(fiche["id"], offset=offset, limit=COMMENTS_BATCH)
    fiche = dict(fiche)
    fiche["comments"] = batch["items"]
    fiche["comments_total"] = batch["total"]
    fiche["comments_offset"] = batch["offset"]
    save_fiche(fiche)
    return fiche


def comments_remaining(fiche):
    total = int(fiche.get("comments_total") or 0)
    offset = int(fiche.get("comments_offset") or 0)
    n = len(fiche.get("comments") or [])
    return max(0, total - (offset + n))


def job_folder(job_id):
    if not job_id:
        return None
    import compagnon
    path = os.path.join(compagnon.BRIDGE_DIR, "travaux", job_id)
    return path if os.path.isdir(path) else None


def job_progress(job_id):
    """{percent, etape} depuis progression.json, ou None."""
    folder = job_folder(job_id)
    if not folder:
        return None
    data = _read_json(os.path.join(folder, "progression.json"), None)
    if not data:
        # travail encore la = en attente prise en charge
        if os.path.isfile(os.path.join(folder, "travail.json")):
            return {"percent": 0, "etape": "en_attente"}
        if os.path.isfile(os.path.join(folder, "travail.pris.json")):
            return {"percent": 3, "etape": "pris"}
        return None
    return {"percent": int(data.get("percent") or 0), "etape": data.get("etape") or ""}


def status_images(fiche):
    """Phrase d'etat pour la section Images / annonces."""
    from phrases import dire
    status = fiche.get("images_desc_status") or ""
    desc = (fiche.get("images_desc") or "").strip()
    if desc and status == "prete":
        return dire("pieces.images_disponibles")
    if status == "echec":
        return dire("pieces.fiche_images_echec")
    if status == "en_cours":
        prog = job_progress(fiche.get("images_job"))
        if not prog:
            return dire("pieces.images_soumises")
        etape = prog.get("etape") or ""
        pct = max(0, int(prog.get("percent") or 0))
        if etape == "en_attente":
            return dire("pieces.images_soumises_attente")
        if etape == "pris" or etape == "prise_en_charge":
            return dire("pieces.images_prise_en_charge")
        if etape.startswith("image_"):
            # image_2_sur_4
            parts = etape.split("_")
            try:
                i, n = parts[1], parts[3]
                return dire("pieces.images_progression", percent=pct, i=i, n=n)
            except (IndexError, ValueError):
                return dire("pieces.images_progression_simple", percent=pct)
        if etape == "traduction":
            return dire("pieces.images_traduction", percent=pct)
        if etape == "termine":
            return dire("pieces.images_bientot")
        if pct >= 0:
            return dire("pieces.images_progression_simple", percent=pct)
        return dire("pieces.images_soumises")
    return dire("pieces.fiche_images_absentes")


def request_images_desc(fiche):
    """Depose un travail decrire_images si utile. Met a jour la fiche (job id).

    Sans compagnon capable, ne fait rien. Re-demande si la desc en cache
    vient d'une ancienne pipeline, ou si un job « en cours » est trop vieux.
    """
    import compagnon
    desc = (fiche.get("images_desc") or "").strip()
    via = fiche.get("images_desc_via") or ""
    if desc and via == IMAGES_DESC_VIA:
        return fiche, None
    if fiche.get("images_job") and fiche.get("images_desc_status") == "en_cours":
        age = time.time() - float(fiche.get("maj") or fiche.get("images_job_t") or 0)
        # maj est touche a chaque save ; images_job_t marque le depot
        started = float(fiche.get("images_job_t") or 0)
        age = time.time() - started if started else IMAGES_JOB_STALE_SEC + 1
        if age < IMAGES_JOB_STALE_SEC:
            return fiche, fiche.get("images_job")
        print(f"[pieces] job images stale ({int(age)} s), nouveau depot")
    if not (fiche.get("images") or []):
        return fiche, None
    if not compagnon.available("decrire_images"):
        return fiche, None
    urls = (fiche.get("images") or [])[:4]
    params = {
        "source": fiche.get("source"),
        "piece_id": fiche.get("id"),
        "titre": fiche.get("title"),
        "description": (fiche.get("description") or "")[:500],
        "images": [],
    }
    job_id, folder, job = compagnon.new_job("decrire_images", params)
    saved = []
    for i, url in enumerate(urls):
        ext = ".jpg"
        low = url.lower().split("?", 1)[0]
        if low.endswith(".png"):
            ext = ".png"
        elif low.endswith(".webp"):
            ext = ".webp"
        name = f"image_{i}{ext}"
        dest = os.path.join(folder, name)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nearfield-anchor/1.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
            with open(dest, "wb") as f:
                f.write(data)
            saved.append(name)
        except Exception as exc:
            print(f"[pieces] image job dl {url} : {exc!r}")
    if not saved:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        return fiche, None
    job["params"]["images"] = saved
    compagnon.submit(job)
    try:
        import compagnon_serveur
        compagnon_serveur.force_notify(job_id)
    except Exception as exc:
        print(f"[pieces] force_notify : {exc!r}")
    fiche = dict(fiche)
    fiche["images_job"] = job_id
    fiche["images_job_t"] = time.time()
    fiche["images_desc_status"] = "en_cours"
    # Ne garde pas une vieille desc pendant le recalcul
    if (fiche.get("images_desc_via") or "") != IMAGES_DESC_VIA:
        fiche["images_desc"] = None
    save_fiche(fiche)
    print(f"[pieces] decrire_images confie : {job_id} ({len(saved)} images)")
    return fiche, job_id


def apply_images_desc(source, piece_id, texte, job_id=None):
    """Ecrit la description d'images dans le cache fiche."""
    fiche = load_fiche(source, piece_id) or {"id": piece_id, "source": source}
    fiche["images_desc"] = _phrases_completes(texte)
    fiche["images_desc_status"] = "prete"
    fiche["images_desc_via"] = IMAGES_DESC_VIA
    if job_id:
        fiche["images_job"] = job_id
    save_fiche(fiche)
    return fiche


def refresh_fiche(fiche):
    """Recharge depuis le cache (resultat compagnon arrive pendant la nav)."""
    fresh = load_fiche(fiche.get("source"), fiche.get("id"))
    if not fresh:
        return fiche
    # Garde les champs de pagination commentaires de la session courante
    for k in ("comments", "comments_offset", "comments_total"):
        if k in fiche:
            fresh[k] = fiche[k]
    return fresh


def session_save(state):
    _write_json(SESSION, state)


def session_load():
    return _read_json(SESSION, None)


def ligne_liste(i, hit):
    """Texte d'une ligne de resultat pour la voix."""
    from phrases import dire, nombre  # import local : phrases charge apres path
    prefix = dire("pieces.deja_vu_prefixe") + " " if hit.get("deja_vu") else ""
    nfr = _note_fr(hit.get("rating"))
    note = (" " + dire("pieces.note", note=nfr)) if nfr else ""
    likes = nombre(hit.get("likes") or 0, "J'aime", "J'aime")
    dl = nombre(hit.get("downloads") or 0, "téléchargement")
    return dire("pieces.ligne", n=i, prefixe=prefix, titre=hit["title"], note=note, likes=likes, dl=dl)




def _download(url, timeout=25):
    """Telecharge une image Printables (octets bruts)."""
    req = urllib.request.Request(url, headers={"User-Agent": "nearfield-anchor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _est_parlable(text):
    """True si espeak a quelque chose a lire (au moins 2 lettres)."""
    return len(re.findall(r"[A-Za-zÀ-ÿ]", text or "")) >= 2


def _nettoyer_pour_voix(text):
    """Retire puces / markdown / debris qui donnent des lignes silencieuses."""
    text = text or ""
    lines = []
    for raw in text.replace("\r", "\n").splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^[\-\u2022\u2013\u2014\*•=─—]+\s*", "", s)
        s = s.replace("*", "").replace("#", "").replace("`", "")
        s = re.sub(r"[|/_\\]{2,}", " ", s)
        if re.fullmatch(r"[\W\d_]+", s or ""):
            continue
        s = " ".join(s.split())
        if _est_parlable(s):
            lines.append(s)
    text = " ".join(lines)
    text = re.sub(r"\s+[\-\u2022•]\s+", ". ", text)
    text = re.sub(r"\.{2,}", ". ", text)
    text = " ".join(text.split()).strip()
    text = re.sub(r"(?i)\b(photo\s+\d+)\.\s+", r"\1 : ", text)
    return text


def _phrases_completes(text):
    """Ne garde que des phrases terminees par . ! ou ?."""
    text = _nettoyer_pour_voix(text)
    if not text:
        return ""
    if text[-1] not in ".!?":
        last = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
        if last >= 0:
            text = text[: last + 1].strip()
        else:
            return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    kept = []
    for part in parts:
        if not _est_parlable(part):
            continue
        if part[-1] not in ".!?":
            continue
        body = part.rstrip(".!?…").strip()
        letters = len(re.findall(r"[A-Za-zÀ-ÿ]", part))
        if letters < 8:
            continue
        if re.search(r"(?i)\b(que|de|des|les|la|le|un|une|et|ou|a|à|pour|avec|c'est|the|a|an)$", body):
            continue
        kept.append(part)
    return " ".join(kept).strip()


def _chunk_text(text, max_len=220, exiger_point=False):
    """Decoupe un long texte en morceaux navigables (phrases, puis longueur).

    Ne coupe pas sur un point sans espace (URLs, decimales).
    Ignore les debris non parlables (symboles seuls = silence espeak).
    Si exiger_point : tronque aux phrases completes terminees par .!?
    """
    if exiger_point:
        text = _phrases_completes(text)
    else:
        text = _nettoyer_pour_voix(text)
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    out = []
    for part in parts or [text]:
        if not _est_parlable(part):
            continue
        if len(part) <= max_len:
            out.append(part)
            continue
        words = part.split()
        cur, n = [], 0
        for w in words:
            add = len(w) + (1 if n else 0)
            if cur and n + add > max_len:
                piece = " ".join(cur)
                if _est_parlable(piece):
                    out.append(piece)
                cur, n = [w], len(w)
            else:
                cur.append(w)
                n += add
        if cur:
            piece = " ".join(cur)
            if _est_parlable(piece):
                out.append(piece)
    return out


def sections_fiche(fiche, include_fine=True):
    """Sections navigables : [{id, titre, items:[str]}].

    Flèches = item suivant/précédent (toutes sections confondues).
    Ctrl+flèches = première entrée de la section voisine.
    """
    from phrases import dire, nombre
    sections = []

    titre_items = [dire("pieces.fiche_titre", titre=fiche["title"])]
    if fiche.get("author"):
        titre_items.append(dire("pieces.fiche_auteur", auteur=fiche["author"]))
    sections.append({"id": "titre", "titre": dire("pieces.section_titre"), "items": titre_items})

    desc = (fiche.get("description") or "").strip()
    if desc:
        desc_items = [dire("pieces.fiche_description_intro")] + _chunk_text(desc, 220)
    else:
        desc_items = [dire("pieces.fiche_sans_description")]
    sections.append({"id": "description", "titre": dire("pieces.section_description"), "items": desc_items})

    img = (fiche.get("images_desc") or "").strip()
    status = fiche.get("images_desc_status") or ""
    if img and status == "prete":
        img_items = [dire("pieces.fiche_images_intro")] + _chunk_text(img, 220, exiger_point=True)
    elif status == "en_cours":
        img_items = [status_images(fiche)]
    elif status == "echec":
        img_items = [dire("pieces.fiche_images_echec")]
    else:
        img_items = [dire("pieces.fiche_images_absentes")]
    sections.append({"id": "images", "titre": dire("pieces.section_images"), "items": img_items})

    nfr = _note_fr(fiche.get("rating"))
    note = dire("pieces.note", note=nfr) if nfr else dire("pieces.sans_note")
    stats_items = [dire(
        "pieces.fiche_stats",
        note=note,
        likes=nombre(fiche.get("likes") or 0, "J'aime", "J'aime"),
        dl=nombre(fiche.get("downloads") or 0, "téléchargement"),
        makes=nombre(fiche.get("makes") or 0, "impression réussie", "impressions réussies"),
        licence=fiche.get("license") or dire("pieces.licence_inconnue"),
        categorie=fiche.get("category") or dire("pieces.categorie_inconnue"),
    )]
    prep = status_preparation(fiche)
    if (fiche.get("impression_preparee") or {}).get("fichiers"):
        stats_items.append(prep)
    sections.append({"id": "stats", "titre": dire("pieces.section_stats"), "items": stats_items})

    comments = fiche.get("comments") or []
    total = int(fiche.get("comments_total") or len(comments) or 0)
    offset = int(fiche.get("comments_offset") or 0)
    if total <= 0 and not comments:
        com_items = [dire("pieces.fiche_sans_commentaires")]
    else:
        # Intro du lot courant (texte integral, sans plafond de longueur).
        lot = (offset // COMMENTS_BATCH) + 1 if COMMENTS_BATCH else 1
        com_items = [dire(
            "pieces.fiche_commentaires_lot",
            lot=lot,
            n=nombre(len(comments), "commentaire"),
            total=nombre(total, "commentaire"),
        )]
        for c in comments:
            chunks = _chunk_text(c["text"], 220) or [""]
            com_items.append(dire("pieces.commentaire", auteur=c["author"], texte=chunks[0]))
            for suite in chunks[1:]:
                com_items.append(dire("pieces.commentaire_suite", texte=suite))
    sections.append({
        "id": "commentaires",
        "titre": dire("pieces.section_commentaires"),
        "items": com_items,
        "more": max(0, total - (offset + len(comments))),
        "offset": offset,
        "total": total,
    })

    if include_fine and (fiche.get("fiche_fine") or "").strip():
        fine_items = [dire("pieces.fiche_fine_intro")] + _chunk_text(fiche["fiche_fine"], 220, exiger_point=True)
        sections.append({"id": "fine", "titre": dire("pieces.section_fine"), "items": fine_items})

    return sections


def flatten_sections(sections):
    """Liste plate [(si, ii, texte)] pour la nav item par item."""
    flat = []
    for si, sec in enumerate(sections):
        for ii, text in enumerate(sec["items"]):
            flat.append((si, ii, text))
    return flat


def texte_fiche(fiche, include_fine=True):
    """Tout le texte d'un coup (repli / debug) ; la console navigue via sections_fiche."""
    parts = []
    for sec in sections_fiche(fiche, include_fine=include_fine):
        parts.extend(sec["items"])
    return " ".join(parts)



def _taille_vocale(n):
    """Octets -> phrase courte."""
    from phrases import dire, nombre
    if n < 1000:
        return dire("pieces.taille_octets", n=nombre(n, "octet"))
    if n < 1_000_000:
        return dire("pieces.taille_ko", n=max(1, round(n / 1000)))
    if n < 1_000_000_000:
        # une decimale utile sous 10 Mo
        mo = n / 1_000_000
        if mo < 10:
            return dire("pieces.taille_mo_fin", n=f"{mo:.1f}".replace(".", ","))
        return dire("pieces.taille_mo", n=max(1, round(mo)))
    return dire("pieces.taille_go", n=f"{n / 1_000_000_000:.1f}".replace(".", ","))


def preparer_impression(fiche, vers_octoprint=True):
    """Telecharge les STL sur le NAS et, si possible, vers OctoPrint sans imprimer.

    Ne demarre jamais une impression. Safe imprimante eteinte.
    Rend (fiche, compte_rendu_dict) ou (fiche, None) si echec total avant depot.
    compte_rendu : {ok, fichiers:[{nom, octets, chemin, octoprint}], erreurs:[str], dossier}
    """
    import printables
    import shutil

    model_id = fiche.get("id")
    source = fiche.get("source") or "printables"
    if not model_id:
        return fiche, {"ok": False, "erreurs": ["sans_id"], "fichiers": []}

    try:
        stl_list = printables.stls(model_id)
    except Exception as exc:
        print(f"[pieces] stls : {exc!r}")
        return fiche, {"ok": False, "erreurs": ["liste"], "fichiers": []}
    if not stl_list:
        return fiche, {"ok": False, "erreurs": ["aucun_stl"], "fichiers": []}

    dossier = os.path.join(PRETS, f"{source}_{model_id}")
    _ensure(dossier)
    fichiers = []
    erreurs = []
    for i, stl in enumerate(stl_list, 1):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stl["name"]).strip("._") or f"modele_{i}.stl"
        if not safe.lower().endswith(".stl"):
            safe += ".stl"
        dest = os.path.join(dossier, safe)
        try:
            # Reutilise si deja present et taille coherente
            if os.path.isfile(dest) and stl["file_size"] and abs(os.path.getsize(dest) - stl["file_size"]) < 64:
                nbytes = os.path.getsize(dest)
            else:
                nbytes = printables.download_stl(model_id, stl["id"], dest)
                # miroir SD
                mirror = os.path.join(CACHE_SD, "prets", f"{source}_{model_id}")
                _ensure(mirror)
                try:
                    shutil.copy2(dest, os.path.join(mirror, safe))
                except OSError:
                    pass
            entry = {
                "nom": safe,
                "octets": nbytes,
                "chemin": dest,
                "stl_id": stl["id"],
                "octoprint": None,
            }
            if vers_octoprint:
                try:
                    import octoprint as op
                    entry["octoprint"] = op.televerser(
                        dest,
                        dossier=f"pieces/{source}_{model_id}",
                        selectionner=(i == 1),
                    )
                except Exception as exc:
                    code = getattr(exc, "code", None) or type(exc).__name__
                    erreurs.append(f"octoprint:{code}")
                    print(f"[pieces] televerser {safe} : {exc!r}")
            fichiers.append(entry)
        except Exception as exc:
            erreurs.append(safe)
            print(f"[pieces] download {safe} : {exc!r}")

    ok = bool(fichiers)
    fiche = dict(fiche)
    fiche["impression_preparee"] = {
        "quand": time.time(),
        "dossier": dossier,
        "fichiers": [{"nom": f["nom"], "octets": f["octets"], "octoprint": f.get("octoprint")} for f in fichiers],
        "erreurs": erreurs,
    }
    save_fiche(fiche)
    return fiche, {
        "ok": ok,
        "dossier": dossier,
        "fichiers": fichiers,
        "erreurs": erreurs,
    }


def status_preparation(fiche):
    """Phrase d'etat pour une preparation deja faite."""
    from phrases import dire, nombre
    prep = fiche.get("impression_preparee") or {}
    files = prep.get("fichiers") or []
    if not files:
        return dire("pieces.pas_prepare")
    n = len(files)
    noms = ", ".join(f["nom"] for f in files[:3])
    if n > 3:
        noms += ", " + dire("pieces.et_autres", n=nombre(n - 3, "autre fichier", "autres fichiers"))
    sur_octo = sum(1 for f in files if f.get("octoprint"))
    if sur_octo:
        return dire("pieces.deja_prepare_octo", n=nombre(n, "fichier"), noms=noms)
    return dire("pieces.deja_prepare", n=nombre(n, "fichier"), noms=noms)


def annonce_preparation(compte):
    """Texte vocal apres preparer_impression."""
    from phrases import dire, nombre
    if not compte or not compte.get("ok"):
        errs = (compte or {}).get("erreurs") or []
        if "aucun_stl" in errs:
            return dire("pieces.prep_aucun_stl")
        return dire("pieces.prep_echec")
    files = compte["fichiers"]
    parts = [dire("pieces.prep_ok", n=nombre(len(files), "fichier STL", "fichiers STL"))]
    for i, f in enumerate(files, 1):
        parts.append(dire(
            "pieces.prep_fichier",
            i=i,
            n=len(files),
            nom=f["nom"],
            taille=_taille_vocale(f["octets"]),
        ))
    sur_octo = [f for f in files if f.get("octoprint")]
    if sur_octo:
        parts.append(dire("pieces.prep_octo_ok", n=nombre(len(sur_octo), "fichier", "fichiers")))
    elif any(str(e).startswith("octoprint:") for e in (compte.get("erreurs") or [])):
        parts.append(dire("pieces.prep_octo_echec"))
    parts.append(dire("pieces.prep_pas_lancee"))
    parts.append(dire("pieces.prep_reprise"))
    return " ".join(parts)


def list_prets():
    """Preparations telechargees : dossiers PRETS + fiche cache si connue.

    Tri : plus recent d'abord. Rend [{source, id, title, dossier, fichiers,
    quand, fiche_fine, images_desc, fiche}].
    """
    _ensure(PRETS)
    mirror = os.path.join(CACHE_SD, "prets")
    roots = []
    for root in (PRETS, mirror):
        if os.path.isdir(root):
            roots.append(root)
    seen = set()
    items = []
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in sorted(names):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder) or name in seen:
                continue
            # printables_1279051
            if "_" not in name:
                continue
            source, _, mid = name.partition("_")
            if not source or not mid:
                continue
            seen.add(name)
            files = []
            try:
                for fn in sorted(os.listdir(folder)):
                    path = os.path.join(folder, fn)
                    if os.path.isfile(path) and not fn.endswith(".part"):
                        files.append({"nom": fn, "octets": os.path.getsize(path), "chemin": path})
            except OSError:
                continue
            if not files:
                continue
            fiche = load_fiche(source, mid) or {
                "source": source, "id": mid, "title": name,
            }
            prep = fiche.get("impression_preparee") or {}
            quand = float(prep.get("quand") or 0)
            if not quand:
                try:
                    quand = os.path.getmtime(folder)
                except OSError:
                    quand = 0
            # Prefer NAS path as dossier canonique
            dossier = os.path.join(PRETS, name)
            if not os.path.isdir(dossier):
                dossier = folder
            items.append({
                "source": source,
                "id": mid,
                "title": fiche.get("title") or name,
                "dossier": dossier,
                "fichiers": files,
                "quand": quand,
                "fiche_fine": (fiche.get("fiche_fine") or "").strip(),
                "images_desc": (fiche.get("images_desc") or "").strip(),
                "fiche": fiche,
            })
    items.sort(key=lambda it: it["quand"], reverse=True)
    return items


def ligne_pret(item, index=None, total=None):
    """Annonce courte d'un pret : titre + desc Gemini (ou repli)."""
    from phrases import dire, nombre
    titre = item.get("title") or item.get("id") or "?"
    nfiles = len(item.get("fichiers") or [])
    prefix = ""
    if index is not None and total is not None:
        prefix = dire("pieces.pret_rang", i=index + 1, n=total) + " "
    fine = (item.get("fiche_fine") or "").strip()
    if fine:
        # Premiere phrase complete pour le passage de liste
        chunks = _chunk_text(fine, 320, exiger_point=True)
        apercu = chunks[0] if chunks else fine[:240]
        return prefix + dire("pieces.pret_ligne_fine", titre=titre, n=nombre(nfiles, "fichier"), texte=apercu)
    img = (item.get("images_desc") or "").strip()
    if img:
        chunks = _chunk_text(img, 280, exiger_point=True)
        apercu = chunks[0] if chunks else img[:240]
        return prefix + dire("pieces.pret_ligne_images", titre=titre, n=nombre(nfiles, "fichier"), texte=apercu)
    return prefix + dire("pieces.pret_ligne_simple", titre=titre, n=nombre(nfiles, "fichier"))


def delete_pret(item):
    """Supprime le dossier pret (NAS + miroir SD) et le marqueur sur la fiche."""
    import shutil
    name = f"{item['source']}_{item['id']}"
    removed = False
    for root in (PRETS, os.path.join(CACHE_SD, "prets")):
        folder = os.path.join(root, name)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
            removed = True
    fiche = load_fiche(item["source"], item["id"])
    if fiche and fiche.get("impression_preparee"):
        fiche = dict(fiche)
        fiche.pop("impression_preparee", None)
        save_fiche(fiche)
    return removed


def gcodes_pret(item):
    """Fichiers .gcode/.gco dans le dossier pret."""
    out = []
    for f in item.get("fichiers") or []:
        low = f["nom"].lower()
        if low.endswith(".gcode") or low.endswith(".gco"):
            out.append(f)
    return out

def valider_gemini(fiche):
    """Passe fine Gemini a partir des images + textes ; met en cache.

    S'appuie sur la description locale (VLM compagnon) si deja disponible.
    """
    key = reglages.cle("gemini")
    if not key:
        return None, "cle"
    images_locales = (fiche.get("images_desc") or "").strip()
    prompt = (
        "Tu aides une personne aveugle à choisir un fichier d'imprimante 3D. "
        "Réponds en français, texte brut uniquement. "
        "Interdit : listes à puces, markdown, symboles décoratifs, tirets en début de ligne. "
        "Chaque phrase doit être complète et se terminer par un point. "
        "Structure en cinq blocs, chacun en une ou deux phrases : "
        "Quoi. Taille et forme. Impression (supports, multipièces, difficulté). "
        "Confiance (titre, tags, commentaires, description locale). "
        "Mots-clés français utiles pour retrouver l'objet. "
        "N'invente pas de dimensions précises si tu ne les vois pas. "
        "Si une description locale des images est fournie, appuie-toi dessus et complète, "
        "sans la contredire sans raison.\n\n"
        f"Titre : {fiche.get('title')}\n"
        f"Auteur : {fiche.get('author')}\n"
        f"Catégorie : {fiche.get('category')}\n"
        f"Licence : {fiche.get('license')}\n"
        f"Description auteur : {(fiche.get('description') or '(vide)')[:800]}\n"
        f"Description locale des images : {images_locales[:1500] or '(pas encore disponible)'}\n"
        f"Commentaires : " + (
            " | ".join(
                f"{c['author']}: {c['text']}" for c in (fiche.get("comments") or [])[:8]
            )
            or "(aucun)"
        )
    )
    parts = [{"text": prompt}]
    lim = 1 if images_locales else 3
    for url in (fiche.get("images") or [])[:lim]:
        try:
            jpeg = _download(url)
            mime = "image/jpeg"
            low = url.lower().split("?", 1)[0]
            if low.endswith(".png"):
                mime = "image/png"
            elif low.endswith(".webp"):
                mime = "image/webp"
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(jpeg).decode()}})
        except Exception as exc:
            print(f"[pieces] image {url} : {exc}")
    body = json.dumps({"contents": [{"parts": parts}]}).encode()
    last = "service"
    for model in GEMINI_MODELS:
        req = urllib.request.Request(
            GEMINI_URL.format(model=model),
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        try:
            out = json.load(urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC))
            text = out["candidates"][0]["content"]["parts"][0]["text"].strip()
            text = _phrases_completes(text) or _nettoyer_pour_voix(text)
            if not text:
                last = "service"
                continue
            fiche = dict(fiche)
            fiche["fiche_fine"] = text
            fiche["fiche_fine_via"] = f"gemini:{model}"
            if not (fiche.get("images_desc") or "").strip():
                fallback = _phrases_completes(text)
                if fallback:
                    first = _chunk_text(fallback, 240, exiger_point=True)
                    fiche["images_desc"] = first[0] if first else fallback
                    fiche["images_desc_status"] = "prete"
                    fiche["images_desc_via"] = "gemini-repli"
            save_fiche(fiche)
            mark_deja_vu(fiche["source"], fiche["id"])
            return fiche, None
        except urllib.error.HTTPError as exc:
            print(f"[pieces] gemini {model} : HTTP {exc.code}")
            if exc.code in (401, 403):
                return None, "cle"
            if exc.code == 429:
                last = "quota"
            continue
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError, ValueError) as exc:
            print(f"[pieces] gemini {model} : {exc!r}")
            last = "service"
            continue
    return None, last
