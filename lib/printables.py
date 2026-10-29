"""Client GraphQL Printables.com (recherche + fiche + commentaires).

Pas d'API cle : requetes publiques. Les commentaires et la description
viennent du meme endpoint ; s'ils manquent, on rend des listes vides.
"""
import json
import re
import urllib.error
import urllib.request

API = "https://api.printables.com/graphql/"
MEDIA = "https://media.printables.com/"
UA = "nearfield-anchor/1.0 (+https://printables.com)"
TIMEOUT = 20

SEARCH_Q = """
query SearchModels($query: String!, $limit: Int) {
  result: searchPrints2(query: $query, printType: print, limit: $limit, ordering: best_match) {
    items {
      id name slug ratingAvg likesCount downloadCount datePublished
      user { publicUsername }
      image { filePath }
    }
  }
}
"""

DETAIL_Q = """
query Detail($id: ID!, $commentsLimit: Int, $commentsOffset: Int) {
  print(id: $id) {
    id name slug summary description
    ratingAvg likesCount downloadCount makesCount
    commentCount
    license { name }
    category { name }
    user { publicUsername }
    image { filePath }
    images { filePath }
    comments(limit: $commentsLimit, offset: $commentsOffset) {
      id text author { publicUsername }
    }
  }
}
"""

COMMENTS_Q = """
query Comments($id: ID!, $commentsLimit: Int, $commentsOffset: Int) {
  print(id: $id) {
    commentCount
    comments(limit: $commentsLimit, offset: $commentsOffset) {
      id text author { publicUsername }
    }
  }
}
"""


def _gql(query, variables, operation=None):
    body = {"query": query, "variables": variables}
    if operation:
        body["operationName"] = operation
    req = urllib.request.Request(
        API,
        data=json.dumps(body).encode("utf-8"),
        headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"printables HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"printables injoignable: {exc}") from exc
    if data.get("errors"):
        msg = data["errors"][0].get("message", "erreur GraphQL")
        raise RuntimeError(f"printables: {msg}")
    return data.get("data") or {}


def _media_url(path):
    if not path:
        return None
    if path.startswith("http"):
        return path
    return MEDIA + path.lstrip("/")


def _strip_html(text):
    if not text:
        return ""
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    return " ".join(text.split()).strip()


def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def search(query, limit=8):
    """[{id, source, title, author, likes, downloads, rating, image_url, url, slug}]"""
    data = _gql(SEARCH_Q, {"query": query, "limit": int(limit)}, "SearchModels")
    items = ((data.get("result") or {}).get("items")) or []
    out = []
    for it in items:
        pid = str(it.get("id") or "")
        if not pid:
            continue
        slug = it.get("slug") or ""
        img = (it.get("image") or {}).get("filePath")
        out.append({
            "id": pid,
            "source": "printables",
            "title": (it.get("name") or "").strip() or f"modèle {pid}",
            "author": ((it.get("user") or {}).get("publicUsername") or "").strip(),
            "likes": it.get("likesCount") or 0,
            "downloads": it.get("downloadCount") or 0,
            "rating": _num(it.get("ratingAvg")),
            "image_url": _media_url(img),
            "slug": slug,
            "url": f"https://www.printables.com/model/{pid}-{slug}" if slug else f"https://www.printables.com/model/{pid}",
        })
    return out


def detail(model_id, comments_limit=8, comments_offset=0):
    """Fiche complete : description, images, commentaires (auteur+texte), stats."""
    data = _gql(DETAIL_Q, {"id": str(model_id), "commentsLimit": int(comments_limit), "commentsOffset": int(comments_offset)}, "Detail")
    p = data.get("print")
    if not p:
        raise RuntimeError("modèle introuvable sur Printables")
    pid = str(p.get("id") or model_id)
    slug = p.get("slug") or ""
    images = []
    cover = _media_url((p.get("image") or {}).get("filePath"))
    if cover:
        images.append(cover)
    for im in p.get("images") or []:
        u = _media_url(im.get("filePath"))
        if u and u not in images:
            images.append(u)
    comments, comments_total = _parse_comments(p)
    desc = _strip_html(p.get("description") or "")
    summary = _strip_html(p.get("summary") or "")
    if not desc and summary:
        desc = summary
    return {
        "id": pid,
        "source": "printables",
        "title": (p.get("name") or "").strip() or f"modèle {pid}",
        "author": ((p.get("user") or {}).get("publicUsername") or "").strip(),
        "summary": summary,
        "description": desc,
        "likes": p.get("likesCount") or 0,
        "downloads": p.get("downloadCount") or 0,
        "makes": p.get("makesCount") or 0,
        "rating": _num(p.get("ratingAvg")),
        "license": ((p.get("license") or {}).get("name") or "").strip(),
        "category": ((p.get("category") or {}).get("name") or "").strip(),
        "images": images[:8],
        "comments": comments,
        "comments_total": comments_total,
        "comments_offset": int(comments_offset),
        "slug": slug,
        "url": f"https://www.printables.com/model/{pid}-{slug}" if slug else f"https://www.printables.com/model/{pid}",
        "images_desc": None,  # rempli plus tard (VLM local / Gemini)
        "fiche_fine": None,
    }

def _parse_comments(payload):
    """([{"author","text"}], total)."""
    comments = []
    for c in payload.get("comments") or []:
        text = " ".join((c.get("text") or "").split()).strip()
        if not text:
            continue
        author = ((c.get("author") or {}).get("publicUsername") or "").strip() or "?"
        comments.append({"author": author, "text": text})
    total = payload.get("commentCount")
    if total is None:
        total = len(comments)
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = len(comments)
    return comments, total


def comments(model_id, offset=0, limit=8):
    """Charge un lot de commentaires. {items, total, offset, limit}."""
    data = _gql(
        COMMENTS_Q,
        {"id": str(model_id), "commentsLimit": int(limit), "commentsOffset": int(offset)},
        "Comments",
    )
    p = data.get("print") or {}
    items, total = _parse_comments(p)
    return {"items": items, "total": total, "offset": int(offset), "limit": int(limit)}


def stls(model_id):
    """Fichiers STL d'un modele : [{id, name, file_size, folder, order}]."""
    q = """
    query Stls($id: ID!) {
      print(id: $id) {
        stls { id name fileSize folder order }
      }
    }
    """
    data = _gql(q, {"id": str(model_id)})
    print_obj = (data.get("print") if isinstance(data, dict) else None) or {}
    out = []
    for s in print_obj.get("stls") or []:
        out.append({
            "id": str(s.get("id") or ""),
            "name": (s.get("name") or "modele.stl").strip() or "modele.stl",
            "file_size": int(s.get("fileSize") or 0),
            "folder": (s.get("folder") or "").strip(),
            "order": int(s.get("order") or 0),
        })
    out.sort(key=lambda x: (x["order"], x["name"].lower()))
    return out


def download_stl_url(print_id, stl_id):
    """Lien de telechargement temporaire pour un STL Printables."""
    q = """
    mutation Link($pid: ID!, $files: [DownloadFileInput!]!, $source: DownloadSourceEnum!) {
      getDownloadLink(printId: $pid, files: $files, source: $source) {
        ok
        output { link }
      }
    }
    """
    data = _gql(q, {
        "pid": str(print_id),
        "files": [{"ids": [str(stl_id)], "fileType": "stl"}],
        "source": "model_detail",
    })
    payload = (data.get("getDownloadLink") if isinstance(data, dict) else None) or {}
    if not payload.get("ok"):
        raise RuntimeError("lien de téléchargement refusé")
    link = ((payload.get("output") or {}).get("link") or "").strip()
    if not link:
        raise RuntimeError("lien de téléchargement vide")
    return link


def download_stl(print_id, stl_id, dest_path):
    """Telecharge un STL vers dest_path ; rend la taille ecrite."""
    import os
    link = download_stl_url(print_id, stl_id)
    req = urllib.request.Request(link, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    tmp = dest_path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest_path)
    return len(data)
