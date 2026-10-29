"""Commande "telegram" (2026-10-18) : s'abonner a des canaux Telegram
PUBLICS, sans compte, pour que la veille terrain les lise a la voix.
Arborescence au clavier (console/arbre.py) :

  Mes canaux                  chaque abonnement : lire les derniers messages,
                              changer ce qui est lu (tout / s'il parle de la
                              ville / seulement l'urgent), se desabonner
  Ajouter un canal            taper son nom, @nom ou lien t.me ; verifie,
                              presente, puis abonne
  Suggestions pour la ville   canaux proposes par Gemini d'apres la ville et
                              les mobilisations du moment, VERIFIES un par un
                              (seuls ceux qui ont un apercu public restent) ;
                              derniers messages, s'abonner
  Renouveler les suggestions  redemande a Gemini (sinon gardees 12 h)

Fleches, Entree, Echap : comme toutes les arborescences de la console.
"""
import sys


import arbre  # noqa: E402
import dmr_cmd  # noqa: E402
import matcher  # noqa: E402
import telegram_public  # noqa: E402
import veille_terrain as vt  # noqa: E402
from phrases import dire  # noqa: E402

LAST_MESSAGES = 3


def _mode_text(mode):
    return dire(f"telegram.mode_{mode}")


def _last_messages(name):
    try:
        info = telegram_public.fetch(name)
    except Exception:
        info = None
    if info is None:
        return dire("telegram.introuvable", nom=name)
    msgs = [m for m in info["messages"] if m["texte"]][-LAST_MESSAGES:]
    if not msgs:
        return dire("telegram.aucun_message", titre=info["titre"])
    parts = [f"{dmr_cmd._when(m['t'])} : {m['texte'][:350]}" for m in reversed(msgs)]
    return dire("telegram.derniers", titre=info["titre"]) + " " + " ; ".join(parts)


def _present(info):
    text = dire("telegram.canal", titre=info["titre"], nom=info["nom"])
    if info.get("abonnes"):
        text += " " + dire("telegram.abonnes", nombre=info["abonnes"])
    if info.get("description"):
        text += " " + info["description"][:250]
    dernier = info.get("dernier") or max((m["t"] for m in info.get("messages", [])), default=0)
    if dernier:
        text += " " + dire("telegram.dernier_message", quand=dmr_cmd._when(dernier))
    return text


def _action(label, **item):
    return arbre.Node(label, item=item)


def build_roots():
    subs = vt.subscriptions()
    mine = []
    for s in subs:
        mine.append(arbre.Node(dire("telegram.abonnement", titre=s.get("titre") or s["nom"], mode=_mode_text(s.get("mode", "tout"))),
                               children=[_action(dire("telegram.lire"), action="lire", nom=s["nom"]),
                                         _action(dire("telegram.changer_mode"), action="mode", nom=s["nom"]),
                                         _action(dire("telegram.desabonner"), action="desabonner", nom=s["nom"])]))

    def load_suggestions():
        try:
            found = vt.suggestions()
        except Exception as exc:
            raise arbre.LoadError(dire("telegram.suggestions_impossibles", raison=repr(exc)))
        subscribed = {s["nom"].lower() for s in vt.subscriptions()}
        nodes = []
        for c in found:
            label = _present(c) + (f" ({c['pourquoi']})" if c.get("pourquoi") else "")
            if c["nom"].lower() in subscribed:
                label += ", " + dire("telegram.deja_abonne")
            nodes.append(arbre.Node(label, children=[
                _action(dire("telegram.lire"), action="lire", nom=c["nom"]),
                _action(dire("telegram.abonner"), action="abonner", nom=c["nom"])]))
        return nodes

    commune = vt.lieu().get("commune", "")
    return [
        arbre.Node(dire("telegram.mes_canaux", nombre=len(subs)), children=mine, empty=dire("telegram.aucun_abonnement")),
        _action(dire("telegram.ajouter"), action="ajouter"),
        arbre.Node(dire("telegram.suggestions", ville=commune), loader=load_suggestions,
                   empty=dire("telegram.aucune_suggestion")),
        _action(dire("telegram.renouveler"), action="renouveler"),
    ]


def _add(key_source, say, read_line):
    say(dire("telegram.demande_nom"))
    name = telegram_public.clean_name(read_line(key_source))
    if not name:
        return dire("telegram.nom_invalide")
    say(dire("telegram.verification", nom=name))
    try:
        info = telegram_public.fetch(name)
    except Exception:
        info = None
    if info is None:
        return dire("telegram.introuvable", nom=name)
    say(_present(info) + " " + dire("telegram.confirmer"))
    if matcher.normalize(read_line(key_source)) not in ("oui", "o", "ok"):
        return dire("telegram.rien_change")
    vt.subscribe(info)
    return dire("telegram.abonne", titre=info["titre"])


def run(key_source, say, out, read_line):
    consigne = dire("telegram.consigne")
    first = True
    while True:
        nav = arbre.Navigator(build_roots, [], select=lambda node: node.item)
        item = arbre.run(nav, key_source, say, out, dire("telegram.titre"), consigne if first else "",
                         feature="telegram")
        first = False
        if item is None:
            return dire("telegram.ferme")
        action, name = item["action"], item.get("nom")
        if action == "lire":
            text = _last_messages(name)
        elif action == "mode":
            text = dire("telegram.mode_change", mode=_mode_text(vt.next_mode(name) or "tout"))
        elif action == "desabonner":
            vt.unsubscribe(name)
            text = dire("telegram.desabonne", nom=name)
        elif action == "abonner":
            try:
                info = telegram_public.fetch(name)
            except Exception:
                info = None
            if info is None:
                text = dire("telegram.introuvable", nom=name)
            else:
                vt.subscribe(info)
                text = dire("telegram.abonne", titre=info["titre"])
        elif action == "ajouter":
            text = _add(key_source, say, read_line)
        elif action == "renouveler":
            say(dire("telegram.recherche_en_cours"))
            try:
                text = dire("telegram.suggestions_trouvees", nombre=len(vt.suggestions(fresh=True)))
            except Exception as exc:
                text = dire("telegram.suggestions_impossibles", raison=repr(exc))
        else:
            text = ""
        # La reponse (parfois longue : derniers messages) est dite jusqu'au bout ;
        # une touche revient au menu (et coupe la lecture), Echap ferme.
        out(f"[{text}]\n")
        say(text + " " + dire("telegram.touche_menu"))
        for key in key_source:
            if key == arbre.ESCAPE:
                return dire("telegram.ferme")
            break
