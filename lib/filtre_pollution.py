"""Filtre d'entree des messages (2026-10-03, demande utilisateur : "notre
entree devrait avoir un watchdog pour croquer ce genre de pollution") :
avant d'etre dit, un message recu par radio passe ici. On ecarte ce qui
n'est pas un message mais de l'habillage commercial : publicite, slogans,
promotion d'emissions, adresses web et reseaux sociaux, titres de chansons.

Trois etages, du moins cher au plus cher :
1. regles : mots et formes typiques de la promotion (PROMO), titre musical
   "ARTISTE - TITRE", nom de la station repete dans son propre texte ;
2. apprentissage : un meme texte deja vu de la meme source (SLOGAN_SEEN fois
   en SLOGAN_WINDOW_SEC) est un slogan d'habillage ;
3. images : Gemini classe d'abord l'image (consigne dab.consigne_image), la
   publicite n'est pas decrite ; une source qui n'envoie que de la
   publicite (IMAGE_SPAM_STREAK d'affilee) est ignoree IMAGE_MUTE_SEC.
Tout ce qui est ecarte est archive (donnees/filtre_pollution_*.jsonl) pour
verifier le filtre.
"""
import re
import time
import unicodedata

import veille_donnees as vd

SLOGAN_SEEN = 1              # deja vu une fois de la meme source : slogan
SLOGAN_WINDOW_SEC = 7 * 86400
IMAGE_SPAM_STREAK = 3
IMAGE_MUTE_SEC = 86400
PROMO = re.compile(
    r"https?://|www\.|\.(fr|com|net|org|be|ch)\b|@|#|\b0[1-9](\s?\d\d){4}\b"
    r"|\b(facebook|instagram|tiktok|snapchat|twitter|youtube|whatsapp|appli\w*|application|podcasts?)\b"
    r"|\b(ecoute[rz]?|retrouve[rz]?|telecharge[rz]?|abonne[rz]?|rejoigne[zs]?|suive[zs]?|gagne[rz]?|jouez|participe[rz]?)\b"
    r"|\b(concours|jeu|cadeaux?|publicite|pub|partenaire|sponsor|offre|promo\w*|reduction|soldes)\b"
    r"|\b(sur (le )?dab|en dab|dab\+?|radio n ?°? ?1|n ?°? ?1|100 ?%|hits?|tubes?|la radio (du|des|de la)|playlist)\b"
    r"|\b(en ce moment|actuellement|titre en cours|a suivre|prochainement|ce soir sur|tous les (jours|soirs|matins))\b",
    re.IGNORECASE)
GENERIC_WORDS = {"radio", "france", "info", "infos", "dab+", "plus", "music", "musique", "hits", "loire", "lyon",
                 "saint", "etienne", "local", "locale", "bleu", "inter", "culture"}
MUSIC_TITLE = re.compile(r"^[^a-z]{2,}\s[-–/]\s[^a-z]{2,}$")   # "EVOLUTION - NOTHING BUT THIEVES"
_memory = vd.Memory("/tmp/filtre_pollution.json")


def _plain(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9%#@.+/: -]", " ", text).split())


def _reject(source, text, reason):
    vd.archive("filtre_pollution", {"source": source, "texte": text, "raison": reason})
    print(f"[filtre_pollution] ecarte ({reason}) : {source} : {text[:80]}")
    return False


def text_ok(source, text, station=None):
    """Garder ce texte comme message ? source : cle stable (station...)."""
    plain = _plain(text)
    if MUSIC_TITLE.match(text.strip()):
        return _reject(source, text, "titre musical")
    if PROMO.search(plain):
        return _reject(source, text, "promotion")
    if station:  # la station qui parle d'elle-meme : habillage
        words = set(re.findall(r"[a-z0-9]{4,}", plain))
        own = {w for w in re.findall(r"[a-z0-9]{4,}", _plain(station)) if w not in GENERIC_WORDS}
        if own & words:
            return _reject(source, text, "nom de la station")
    seen = _memory.data.setdefault("vus", {})
    key = f"{source}|{plain[:120]}"
    now = time.time()
    stamps = [t for t in seen.get(key, []) if now - t < SLOGAN_WINDOW_SEC] + [now]
    seen[key] = stamps[-10:]
    for k in [k for k, v in seen.items() if now - v[-1] > SLOGAN_WINDOW_SEC]:
        del seen[k]
    _memory.save()
    if len(stamps) > SLOGAN_SEEN:
        return _reject(source, text, "slogan repete")
    return True


def images_muted(source):
    return time.time() < _memory.data.get("images_muettes", {}).get(source, 0)


def image_verdict(source, is_ad):
    """Apres classement d'une image : compte les publicites d'affilee d'une
    source, et la fait taire IMAGE_MUTE_SEC si elle n'envoie que ca."""
    streaks = _memory.data.setdefault("series_pub", {})
    streaks[source] = streaks.get(source, 0) + 1 if is_ad else 0
    if streaks[source] >= IMAGE_SPAM_STREAK:
        _memory.data.setdefault("images_muettes", {})[source] = time.time() + IMAGE_MUTE_SEC
        streaks[source] = 0
        print(f"[filtre_pollution] images de {source} ignorees 24 h (publicite seulement)")
    _memory.save()
