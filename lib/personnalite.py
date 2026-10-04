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

Date : {date}. Mot d'inspiration du jour (pour l'humeur seulement) : {graine}.

La personnalité ne change que le TON des petites phrases d'interface : chaleur, familiarité,
enthousiasme, humour léger. Elle ne change jamais le vocabulaire utile, la clarté ni la longueur.
Pas de thème filé (musique, mer, cosmos...), pas de métaphore, pas de formule de vœux.
Écris la fiche de style de cette voix pour aujourd'hui. Réponds uniquement par un objet JSON :
{{"resume": "une phrase courte qui décrit la personnalité du jour, à lire à voix haute",
  "fiche": "3 à 5 règles de ton concrètes et sobres (exclamations courtes possibles, familiarité...)"}}
Contraintes : rester bienveillant, clair et compréhensible à l'oral ; pas d'emoji, pas de markdown."""

USER_DEMAND = "La personne a décrit la personnalité voulue ainsi : « {prompt} ». Respecte-la."
SURPRISE_DEMAND = ("La personne veut une personnalité SURPRISE, différente chaque jour : invente-la toi-même, "
                   "originale et attachante (un personnage, une humeur, une époque, un métier...).")

BLOCK_PROMPT = """Tu réécris les phrases d'interface d'un petit ordinateur domestique qui parle à une
personne aveugle par synthèse vocale, en {langue}, avec la personnalité du jour.

Personnalité du jour : {resume}
Fiche de style :
{fiche}

Il s'agit d'une PETITE TOUCHE DE TON, pas d'une réécriture. Exemple :
  neutre : "L'enregistrement a été validé comme station probable."
  bon (informel joyeux) : "L'enregistrement est validé comme station probable, il est parfait, waouh !"
  mauvais : "La vibration cosmique de ce moment a été suivie par l'apprentissage musical, que
  l'harmonie demeure." (sens perdu, métaphore, formule de vœux, trop long)

Règles impératives, pour chaque phrase :
1. Garde la phrase d'origine presque mot pour mot : change au plus quelques mots, ou ajoute une
   courte exclamation (deux à quatre mots). Laisse certaines phrases telles quelles.
2. Garde exactement le même sens, toutes les informations et toutes les consignes.
3. Garde exactement les variables entre accolades, écrites à l'identique ({{nom}} reste {{nom}}),
   et n'en ajoute aucune.
4. Garde tels quels les mots de la liste "garder" (noms de touches, commandes à taper).
5. Au plus {croissance} fois la longueur d'origine ; une seule ligne ; pas d'emoji, pas de markdown.
6. Interdit : formule de vœux ou de conclusion ("que votre journée...", "que l'harmonie..."),
   métaphore, référence à un thème (musique, cosmos, mer...), répétition d'une même tournure ou
   d'une même exclamation dans plusieurs phrases.
7. Écris en {langue}.

Phrases à réécrire (JSON) :
{bloc}

Réponds uniquement par un objet JSON dont les clés sont exactement les clés reçues et les valeurs
les phrases réécrites : {{"cle": "phrase réécrite", ...}}"""

GRAINES = ["marée", "lanterne", "boussole", "jazz", "brocante", "astronome", "facteur", "volcan", "théière",
           "phare", "chaton", "explorateur", "orage", "piano", "bibliothèque", "cosmonaute", "jardinier",
           "corsaire", "horloger", "funambule", "météorite", "accordéon", "renard", "sorcier", "détective"]
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
    demand = SURPRISE_DEMAND if cfg.get("surprise") else USER_DEMAND.format(prompt=cfg.get("prompt", ""))
    style, problem = _ask_json(STYLE_PROMPT.format(langue=langue, demande=demand, date=datetime.date.today().isoformat(),
                                                    graine=random.choice(GRAINES)), temperature=1.0)
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
                reason = phrases.valid(entry, text)
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
