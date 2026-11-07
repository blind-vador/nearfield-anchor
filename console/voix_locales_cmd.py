"""Commande "voix locales" (2026-10-18) : voix neuronales Piper, installees ou
a telecharger depuis le depot officiel (rhasspy/piper-voices sur Hugging
Face, voir lib/piper_voix.py). Arborescence au clavier (console/arbre.py) :

  Voix installees            chacune : l'ecouter, la retirer
  Toutes les langues         langue (francais d'abord ; taper des lettres
                             pour chercher) > voix (qualite, taille,
                             installee ou non) > ecouter l'extrait officiel,
                             installer (telechargement verifie), retirer
  Source                     d'ou viennent les voix

Une voix installee apparait aussitot dans la commande "voix" (choix de la
voix de la console) ; une voix francaise sert aussi aux annonces en secours
et ses phrases sont preparees en tache de fond.
"""
import os
import sys
import tempfile


import arbre  # noqa: E402
import matcher  # noqa: E402
import piper_out  # noqa: E402
import piper_voix  # noqa: E402
from phrases import dire  # noqa: E402

QUALITIES = {"x_low": "très basse", "low": "basse", "medium": "moyenne", "high": "haute"}


def _action(label, **item):
    return arbre.Node(label, item=item)


def _voice_label(key, entry):
    parts = [entry["name"].replace("_", " ").capitalize(),
             dire("voix_locales.qualite", qualite=QUALITIES.get(entry["quality"], entry["quality"]))]
    if entry["num_speakers"] > 1:
        parts.append(dire("voix_locales.locuteurs", nombre=entry["num_speakers"]))
    parts.append(dire("voix_locales.taille", mo=piper_voix.size_mb(entry)))
    if piper_voix.installed(key):
        parts.append(dire("voix_locales.installee"))
    return ", ".join(parts)


def _voice_actions(key):
    actions = [_action(dire("voix_locales.ecouter_extrait"), action="extrait", cle=key)]
    if piper_voix.installed(key):
        actions.append(_action(dire("voix_locales.retirer"), action="retirer", cle=key))
    else:
        actions.append(_action(dire("voix_locales.installer"), action="installer", cle=key))
    return actions


def make_tree(cat):
    index = []

    def build_roots():
        index.clear()
        mine = []
        for key in piper_out.installed_models():
            entry = cat.get(key, {"name": key, "quality": "", "num_speakers": 1, "files": {}})
            children = []
            if key.startswith("fr_"):
                children.append(_action(dire("voix_locales.ecouter_ici"), action="ecouter", cle=key))
            children += _voice_actions(key) if key in cat else [_action(dire("voix_locales.retirer"), action="retirer", cle=key)]
            lang = (entry.get("language") or {}).get("name_native", key.split("_")[0])
            mine.append(arbre.Node(f"{lang}, {_voice_label(key, entry)}" if key in cat else key, children=children))
        langs = []
        for code, native, country, keys in piper_voix.languages(cat):
            n_in = sum(1 for k in keys if piper_voix.installed(k))
            label = dire("voix_locales.langue", nom=native, pays=country, nombre=len(keys), installees=n_in)
            node = arbre.Node(label, children=[
                arbre.Node(_voice_label(k, cat[k]), children=_voice_actions(k))
                for k in sorted(keys, key=lambda k: (cat[k]["name"], cat[k]["quality"]))])
            langs.append(node)
            index.append((matcher.normalize(native), node))
            index.append((matcher.normalize(code), node))
        return [
            arbre.Node(dire("voix_locales.installees", nombre=len(mine)), children=mine),
            arbre.Node(dire("voix_locales.toutes_langues", nombre=len(langs), voix=len(cat)), children=langs,
                       empty=dire("voix_locales.catalogue_injoignable")),
            _action(dire("voix_locales.source_titre"), action="source"),
        ]
    return build_roots, index


def _play_url(url):
    import audio_lock
    import sound_player
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        r = piper_voix.requests.get(url, headers=piper_voix.HEADERS, timeout=piper_voix.TIMEOUT)
        if not r.ok:
            return dire("voix_locales.extrait_absent")
        with open(path, "wb") as f:
            f.write(r.content)
        lock = audio_lock.acquire("console", timeout=10)
        try:
            sound_player.play_sound_effect(path, timeout_sec=60, label="voix_locales")
        finally:
            audio_lock.release(lock)
        return dire("voix_locales.extrait_fini")
    except Exception as exc:
        return dire("voix_locales.extrait_impossible", raison=exc.__class__.__name__)
    finally:
        os.remove(path)


def _speak_here(key):
    import audio_lock
    ids = [v for v in piper_out.voice_ids(lang=None) if piper_out.resolve(v)[0] == key]
    if not ids:
        return dire("voix_locales.pas_installee")
    lock = audio_lock.acquire("console", timeout=10)
    try:
        for voice in ids:
            piper_out.Speech(f"{piper_out.label(voice)}. {dire('voix_locales.essai')}", voice_id=voice).wait()
    finally:
        audio_lock.release(lock)
    return dire("voix_locales.extrait_fini")


def run(key_source, say, out):
    say(dire("voix_locales.chargement"))
    cat = piper_voix.catalogue()
    consigne = dire("voix_locales.consigne")
    first = True
    while True:
        build_roots, index = make_tree(cat)
        nav = arbre.Navigator(build_roots, index, select=lambda node: node.item)
        item = arbre.run(nav, key_source, say, out, dire("voix_locales.titre"), consigne if first else "",
                         feature="voix_locales", aide=consigne)
        first = False
        if item is None:
            return dire("voix_locales.ferme")
        action, key = item["action"], item.get("cle")
        if action == "source":
            text = dire("voix_locales.source", depot=piper_voix.REPO, version=piper_voix.VERSION)
        elif action == "extrait":
            say(dire("voix_locales.extrait_charge"))
            text = _play_url(piper_voix.sample_url(cat[key]))
        elif action == "ecouter":
            text = _speak_here(key)
        elif action == "installer":
            say(dire("voix_locales.telechargement", mo=piper_voix.size_mb(cat[key])))
            ok, problem = piper_voix.install(key, cat)
            text = dire("voix_locales.installee_ok", nom=_voice_label(key, cat[key])) if ok else \
                dire("voix_locales.installation_ratee", raison=problem)
            if ok and key.startswith("fr_"):
                text += " " + dire("voix_locales.preparation_fond")
        elif action == "retirer":
            if key.startswith("fr_") and len([m for m in piper_out.installed_models() if m.startswith("fr_")]) <= 1:
                text = dire("voix_locales.derniere_francaise")
            else:
                piper_voix.remove(key)
                text = dire("voix_locales.retiree", nom=key)
        else:
            text = ""
        out(f"[{text}]\n")
        say(text + " " + dire("voix_locales.touche_menu"))
        for k in key_source:
            if k == arbre.ESCAPE:
                return dire("voix_locales.ferme")
            break
