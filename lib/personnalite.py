"""Personnalite du Pi (2026-10-02) : chaque nuit a minuit, Gemini reecrit les
phrases de l'interface (lib/phrases.py) selon la personnalite choisie par
l'utilisateur (commande "personnalite"), ou une personnalite surprise
inventee chaque nuit. Le fichier de la langue (version neutre) n'est jamais
modifie ; la version du jour est ecrite a part, celle de la veille gardee.

Methode (bonnes pratiques pour faire travailler un grand modele) :
1. fiche de style : une premiere demande fixe la voix du jour (resume d'une
   phrase et quelques regles de ton) ; elle accompagne chaque bloc, pour que
   toutes les phrases parlent de la meme facon ;
2. blocs de BLOCK_SIZE phrases, en JSON, avec leur contexte, leurs variables
   et leurs mots proteges ; reponse demandee en JSON strict (mode JSON de
   Gemini), cles identiques ; chaque bloc reste tres en dessous de la
   fenetre de contexte et un bloc rate ne perd que ses phrases ;
3. verification phrase par phrase (phrases.valid : memes variables, mots
   proteges, longueur pour la synthese vocale, pas de symboles) ; un bloc
   illisible est retente une fois ; une phrase non conforme reste neutre.
"""
import configparser
import datetime
import json
import os
import random
import time

import phrases
from phrases import dire

BLOCK_SIZE = 40         # ~760 phrases reecrivables (2026-10-03) : une vingtaine d'appels par nuit
PAUSE_SEC = 4           # entre deux blocs (quotas de Gemini)
TIMEOUT_SEC = 180
RUN_AT = (0, 0)         # minuit

STYLE_PROMPT = """Tu prépares la voix d'un petit ordinateur domestique (un Raspberry Pi) qui parle à une
personne aveugle par synthèse vocale, en {langue}. Il annonce des réceptions radio, la météo, et répond
aux commandes de sa console.

{demande}

Donne-lui une PERSONA, un personnage avec un caractère : sa façon de parler, ses expressions, son
humeur, et son univers d'où viennent des clins d'œil et des références (personnage populaire,
figure littéraire, métier, archétype...). Ce personnage colore seulement les petites phrases
d'interface, qui doivent rester claires et garder leurs mots utiles.
Date : {date}.

Réponds uniquement par un objet JSON :
{{"resume": "une phrase courte, à lire à voix haute, qui présente le personnage du jour",
  "fiche": "4 à 6 règles concrètes : façon de parler, expressions typiques (plusieurs, à varier),
            univers des clins d'œil, humeur"}}
Contraintes : rester bienveillant envers l'utilisateur, clair à l'oral ; pas d'emoji, pas de markdown."""

USER_DEMAND = "La personne a décrit la personnalité voulue ainsi : « {prompt} ». Respecte-la."
SURPRISE_DEMAND = ("La personne veut un personnage SURPRISE, différent chaque jour : invente-le toi-même, "
                   "original et attachant. Pour t'inspirer seulement (tu peux t'en écarter) : {archetype}.")

BLOCK_PROMPT = """Tu colores les phrases d'interface d'un petit ordinateur domestique qui parle à une
personne aveugle par synthèse vocale, en {langue}, avec le personnage du jour.

Personnage du jour : {resume}
Fiche du personnage :
{fiche}

Garde les mots centraux qui font comprendre la phrase (journal, archives, station, note, favori,
noms de touches, commandes...) : on ne remplace jamais "journal" par "partition". Colore le tour de
la phrase avec la façon de parler du personnage, une expression à lui, un clin d'œil à son univers.
Exemple, personnage "petit robot espiègle et amusé" :
  neutre : "L'enregistrement a été validé comme station probable."
  bon : "Bip ! L'enregistrement est validé comme station probable, il est parfait, waouh !"
  mauvais : "La vibration cosmique de ce moment a été suivie par l'apprentissage musical, que
  l'harmonie demeure." (mots centraux perdus, sens flou, formule de vœux)

Règles impératives, pour chaque phrase :
1. Même sens, toutes les informations et toutes les consignes ; mots centraux gardés.
2. Garde exactement les variables entre accolades, écrites à l'identique ({{nom}} reste {{nom}}),
   et n'en ajoute aucune.
3. Garde tels quels les mots de la liste "garder" (noms de touches, commandes à taper).
4. Au plus {croissance} fois la longueur d'origine ; une seule ligne ; pas d'emoji, pas de markdown.
5. Varie : ne répète pas la même expression ou le même clin d'œil d'une phrase à l'autre ; pas de
   formule de vœux ou de conclusion plaquée en fin de phrase.
6. Écris en {langue}.

Phrases à colorer (JSON) :
{bloc}

Réponds uniquement par un objet JSON dont les clés sont exactement les clés reçues et les valeurs
les phrases colorées : {{"cle": "phrase colorée", ...}}"""

# Archetypes pour le personnage surprise (inspiration seulement, Gemini invente).
ARCHETYPES = ["un petit robot espiègle et amusé", "un militant marxiste dépité", "un patron de banque pressé",
              "un détective de roman noir", "un majordome anglais imperturbable", "un pirate bougon",
              "une vieille institutrice exigeante mais tendre", "un capitaine de vaisseau spatial",
              "un commentateur sportif survolté", "un professeur distrait", "un chevalier de la Table ronde",
              "un chef cuisinier colérique", "un poète romantique tourmenté", "un agent secret blasé",
              "une diva capricieuse", "un vendeur de grand magasin trop enthousiaste", "un chat qui parle",
              "un fonctionnaire tatillon", "un explorateur du dix-neuvième siècle", "un philosophe grec"]
LANGUES = {"fr": "français", "en": "anglais"}


# --- reglages (console.json, cle "personnalite") ---------------------------------------

def settings():
    import reglages
    return reglages.get("console").get("personnalite") or {}


def save_settings(**fields):
    import reglages
    data = reglages.get("console")
    current = data.get("personnalite") or {}
    current.update(fields)
    data["personnalite"] = current
    return reglages.save("console", data)


def description():
    cfg = settings()
    if not cfg.get("active"):
        return phrases.dire("personnalite.neutre")
    if cfg.get("resume"):
        return cfg["resume"]
    return dire("personnalite.surprise_inventee_chaque_nuit") if cfg.get("surprise") else cfg.get("prompt", "")


# --- generation ---------------------------------------------------------------------------

def _ask_json(prompt, temperature):
    import gemini
    for _ in range(2):  # un essai de plus si la reponse n'est pas du JSON
        text, problem = gemini.ask(prompt, temperature=temperature, json_mode=True, timeout=TIMEOUT_SEC)
        if problem:
            return None, problem
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data, None
        except ValueError:
            pass
    return None, dire("personnalite.reponse_gemini_illisible")


def _write_ini(path, entries, header):
    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str
    for key, text in entries.items():
        parser[key] = {"texte": text}
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        for line in header:
            f.write(f"# {line}\n")
        f.write("\n")
        parser.write(f)
    os.replace(tmp, path)


CENTRAL_KEEP = 0.6   # part des mots porteurs de sens (5 lettres et plus) a garder

_STOP = {"votre", "notre", "leurs", "cette", "comme", "aussi", "avant", "apres", "depuis", "encore", "toutes",
         "toute", "alors", "entre", "pendant", "selon", "quand", "rien", "plus"}


def _central_words_lost(neutral, text):
    """Mots centraux de la phrase neutre absents de la phrase coloree ? (raison ou None)"""
    import re
    import unicodedata

    def words(t):
        t = unicodedata.normalize("NFKD", re.sub(r"\{[^}]*\}", " ", t)).encode("ascii", "ignore").decode().lower()
        return {w for w in re.findall(r"[a-z]{5,}", t) if w not in _STOP}
    central = words(neutral)
    if not central:
        return None
    kept = len(central & words(text)) / len(central)
    return None if kept >= CENTRAL_KEEP else f"mots centraux perdus ({kept:.0%})"


LEITMOTIF_MAX = 3   # une tournure inventee (3 mots) dans plus de 3 phrases : tic de langage


def _drop_leitmotifs(new, base, log):
    """Tics de langage (2026-10-04 : "que l'harmonie demeure" dans 39 phrases) :
    une suite de trois mots absente des phrases neutres et presente dans plus
    de LEITMOTIF_MAX phrases du jour fait revenir ces phrases au neutre."""
    import collections
    import re

    def grams(text):
        words = re.findall(r"\w+", text.lower())
        return {" ".join(words[i:i + 3]) for i in range(len(words) - 2)}
    neutral = set()
    for entry in base.values():
        neutral |= grams(entry["texte"])
    counts = collections.Counter(g for text in new.values() for g in grams(text) - neutral)
    tics = {g for g, n in counts.items() if n > LEITMOTIF_MAX}
    dropped = [k for k, text in new.items() if grams(text) & tics]
    for key in dropped:
        del new[key]
    if tics:
        log(f"[personnalite] tics ecartes : {sorted(tics)[:6]} ({len(dropped)} phrases au neutre)")
    return len(dropped)


def generate(log=print):
    """Reecrit les phrases du jour ; rend (resultat, None) ou (None, raison)."""
    cfg = settings()
    if not cfg.get("active"):
        return None, dire("personnalite.aucune_personnalite_choisie")
    lang = phrases.langue()
    langue = LANGUES.get(lang, lang)
    base = phrases.base(lang)
    if not base:
        return None, dire("personnalite.fichier_phrases_introuvable")
    demand = (SURPRISE_DEMAND.format(archetype=random.choice(ARCHETYPES)) if cfg.get("surprise")
              else USER_DEMAND.format(prompt=cfg.get("prompt", "")))
    style, problem = _ask_json(STYLE_PROMPT.format(langue=langue, demande=demand, date=datetime.date.today().isoformat()),
                                 temperature=1.0)
    if problem or not style.get("resume"):
        return None, problem or dire("personnalite.fiche_style_vide")
    resume, fiche = " ".join(str(style["resume"]).split()), str(style.get("fiche", ""))
    log(f"[personnalite] voix du jour : {resume}")
    todo = [(k, v) for k, v in base.items() if phrases.rewritable(k, v)]
    new, kept = {}, 0
    for start in range(0, len(todo), BLOCK_SIZE):
        block = todo[start:start + BLOCK_SIZE]
        payload = json.dumps({k: {"texte": v["texte"], "contexte": v["contexte"],
                                  "variables": sorted(phrases.variables(v["texte"])), "garder": v["garder"]}
                              for k, v in block}, ensure_ascii=False, indent=1)
        answer, problem = _ask_json(BLOCK_PROMPT.format(langue=langue, resume=resume, fiche=fiche, bloc=payload,
                                                        croissance=phrases.MAX_GROWTH), temperature=0.9)
        if problem:
            log(f"[personnalite] bloc {start // BLOCK_SIZE + 1} perdu : {problem}")
            kept += len(block)
        else:
            for key, entry in block:
                text = " ".join(str(answer.get(key, "")).split())
                reason = phrases.valid(entry, text) or _central_words_lost(entry["texte"], text)
                if reason:
                    kept += 1
                    log(f"[personnalite] {key} garde neutre : {reason}")
                else:
                    new[key] = text
        time.sleep(PAUSE_SEC)
    kept += _drop_leitmotifs(new, base, log)
    if not new:
        return None, dire("personnalite.aucune_phrase_reecrite_correctement")
    path = phrases.persona_path(lang)
    if os.path.exists(path):
        os.replace(path, path.replace(".ini", "_hier.ini"))
    _write_ini(path, new, [f"Personnalite du {datetime.date.today():%d/%m/%Y} : {resume}",
                           "Fiche de style : " + " ".join(fiche.split()),
                           "Ecrit par lib/personnalite.py (Gemini) ; la version neutre est phrases/" + lang + ".ini"])
    save_settings(resume=resume, genere_le=time.time())
    return {"resume": resume, "reussies": len(new), "gardees": kept + len(base) - len(todo)}, None


def watch():
    """Thread de meteo_watch : chaque nuit a minuit, nouvelle facon de parler."""
    while True:
        now = datetime.datetime.now()
        run = now.replace(hour=RUN_AT[0], minute=RUN_AT[1], second=30, microsecond=0)
        if run <= now:
            run += datetime.timedelta(days=1)
        time.sleep((run - now).total_seconds())
        try:
            if settings().get("active"):
                result, problem = generate()
                print(f"[personnalite] {result or problem}")
        except Exception as exc:
            print(f"[personnalite] {exc!r}")
