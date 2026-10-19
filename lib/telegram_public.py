"""Canaux Telegram PUBLICS, lus sans compte par leur page web d'apercu
(https://t.me/s/<nom>) : titre, description, abonnes, derniers messages.
Sert la veille terrain (watchers/veille_terrain.py) et le dialogue console
"telegram" (abonnements, suggestions). Lecture seule, rien n'est envoye.

Un canal prive, un groupe ou un compte n'a pas d'apercu public : il est
alors "introuvable" ici, et c'est normal.
"""
import html
import re
import time

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux armv7l) veille-terrain"}
TIMEOUT = 20
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def clean_name(text):
    """"@nom", "t.me/nom", "https://t.me/s/nom/123" -> "nom" (ou None)."""
    text = (text or "").strip()
    m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]+)", text)
    name = m.group(1) if m else text.lstrip("@").split("/")[0]
    name = name.replace(" ", "")
    return name if NAME_RE.match(name) else None


def _clean(text):
    text = re.sub(r"<br\s*/?>", " ", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"https?://\S+", "", text)
    return " ".join(text.split())


def _ts(value):
    try:
        import datetime
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def _meta(page, prop):
    m = re.search(rf'<meta property="{prop}" content="([^"]*)"', page)
    return html.unescape(m.group(1)).strip() if m else ""


def fetch(name):
    """{nom, titre, description, abonnes, messages: [{id, texte, t, lien}]}
    ou None si le canal n'a pas d'apercu public."""
    name = clean_name(name)
    if not name:
        return None
    resp = requests.get(f"https://t.me/s/{name}", headers=HEADERS, timeout=TIMEOUT)
    if not resp.ok or "tgme_channel_info" not in resp.text:
        return None  # pas un canal public (redirige vers la page simple)
    page = resp.text
    subs = re.search(r'<span class="counter_value">([^<]+)</span>\s*<span class="counter_type">(?:subscribers|abonn)',
                     page)
    messages = []
    for block in re.split(r'<div class="tgme_widget_message_wrap', page)[1:]:
        post = re.search(r'data-post="([^"]+)"', block)
        text = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not post or not text:
            continue
        messages.append({"id": post.group(1), "texte": _clean(text.group(1))[:600],
                         "t": _ts(when.group(1) if when else ""), "lien": f"https://t.me/{post.group(1)}"})
    return {"nom": name, "titre": _meta(page, "og:title") or name, "description": _meta(page, "og:description"),
            "abonnes": subs.group(1) if subs else None, "messages": messages}
