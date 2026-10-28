"""Commande "dmr" : ce que le scanner sait des frequences DMR captees
(lib/dmr_registre.py), "dmr explications" pour les mots du DMR, et
"dmr aujourd'hui" (ou "dmr hier") pour les connexions des postes du jour.
"""
import datetime
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import dmr_registre  # noqa: E402
import matcher  # noqa: E402
from phrases import dire, dire_ou, nombre  # noqa: E402

MAX_FREQS = 5
MAX_GROUPS = 4
HELP_WORDS = {"explication", "explications", "aide", "glossaire", "definitions", "mots", "comprendre"}
DAY_WORDS = {"aujourd", "aujourdhui", "hui", "hier", "connexion", "connexions", "resume", "journee", "postes"}


def _when(t):
    days = int((time.time() - t) // 86400)
    hhmm = time.strftime("%H h %M", time.localtime(t))
    if days == 0 and time.localtime(t).tm_mday == time.localtime().tm_mday:
        return dire("dmr.quand_aujourdhui", heure=hhmm)
    if days <= 1:
        return dire("dmr.quand_hier", heure=hhmm)
    return dire("dmr.quand_jours", jours=days)


def _usual_hours(e):
    hours = e.get("heures") or []
    if sum(hours) < 3:
        return ""
    top = sorted(range(24), key=lambda h: hours[h], reverse=True)[:2]
    return dire("dmr.heures_habituelles", heures=_join([dire("dmr.heure", heure=h) for h in sorted(top)]))


def _join(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " " + dire("dmr.et") + " " + items[-1]


def _emetteurs(ids):
    """Liste d'emetteurs : indicatif radioamateur + ville quand on les connait
    (radioid.net), sinon le numero. Jamais le nom de la personne."""
    out = []
    for src in list(ids)[:4]:
        info = dmr_registre.resolve_callsign(src)
        if info and info.get("indicatif"):
            ville = info.get("ville")
            out.append(dire("dmr.emetteur_connu", indicatif=info["indicatif"], ville=ville) if ville
                       else info["indicatif"])
        else:
            out.append(str(src))
    return ", ".join(out)


def describe(e):
    mhz = f"{e['freq_hz'] / 1e6:.4f}".replace(".", ",")
    parts = [dire("dmr.frequence", frequence=mhz,
                  nature=dire("dmr.relais") if e.get("relais") else dire("dmr.direct"),
                  quand=_when(e.get("derniere", time.time())), sessions=nombre(e.get("sessions", 0), "écoute"))]
    cc = dmr_registre.main_cc(e)
    if cc is not None:
        parts.append(dire("dmr.couleur", couleur=int(cc)))
    if e.get("systeme"):
        parts.append(dire("dmr.systeme", systeme=e["systeme"]))
    groups = sorted((e.get("groupes") or {}).items(), key=lambda kv: kv[1].get("n", 0), reverse=True)
    if groups:
        listed = [dire("dmr.groupe", groupe=tg, emetteurs=_emetteurs(g.get("emetteurs", [])),
                       intervalle=_join(g.get("slots", []) or ["?"]))
                  for tg, g in groups[:MAX_GROUPS]]
        parts.append(dire("dmr.groupes", nombre=nombre(len(groups), "groupe"), liste=" ; ".join(listed)))
    if e.get("prive"):
        parts.append(dire("dmr.prives", nombre=nombre(e["prive"], "appel de poste à poste", "appels de poste à poste")))
    if e.get("chiffre"):
        parts.append(dire("dmr.chiffres", nombre=nombre(e["chiffre"], "appel chiffré", "appels chiffrés")))
    hours = _usual_hours(e)
    if hours:
        parts.append(hours)
    return " ".join(parts)


def run(line):
    words = set(matcher.normalize(line).split())
    if words & HELP_WORDS:
        return dire("dmr.explications")
    if words & DAY_WORDS:
        # Connexions des postes au serveur de dispatch (lib/dmr_registre.py, ARS)
        day = datetime.date.today() - datetime.timedelta(days=1 if "hier" in words else 0)
        return dmr_registre.daily_summary(day) or dire_ou("dmr.ars_rien", "Aucune connexion DMR notée ce jour-là.")
    known = dmr_registre.targets()
    if not known:
        return dire("dmr.rien")
    texts = [dire("dmr.titre", nombre=nombre(len(known), "fréquence DMR", "fréquences DMR"))]
    texts += [describe(e) for e in known[:MAX_FREQS]]
    for net in dmr_registre.networks():
        freqs = _join([f"{f / 1e6:.4f}".replace(".", ",") for f in sorted(net["freqs_hz"])])
        texts.append(dire("dmr.reseau_multi", couleur=int(net["cc"]),
                          nombre=nombre(len(net["freqs_hz"]), "fréquence", "fréquences"), freqs=freqs))
    texts.append(dire("dmr.dis_explications"))
    return " ".join(texts)
