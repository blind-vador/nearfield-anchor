"""Commande "tetra" (2026-10-27) : ce que le scanner sait des reseaux TETRA
captes (lib/tetra.py, registre) -- postes actifs, inscriptions, messages
d'etat et SDS en clair (taille seulement), appels et porteuses -- comme la
commande "dmr" pour le DMR.
"""
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import tetra  # noqa: E402
from phrases import dire, nombre  # noqa: E402


def _quand(t):
    jours = int((time.time() - t) // 86400)
    hhmm = time.strftime("%H h %M", time.localtime(t))
    if jours == 0 and time.localtime(t).tm_mday == time.localtime().tm_mday:
        return dire("dmr.quand_aujourdhui", heure=hhmm)
    if jours <= 1:
        return dire("dmr.quand_hier", heure=hhmm)
    return dire("dmr.quand_jours", jours=jours)


def _mhz(hz):
    return f"{int(hz) / 1e6:.4f}".rstrip("0").replace(".", ",")


def _liste(items):
    items = list(items)
    if not items:
        return "?"
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " " + dire("dmr.et") + " " + items[-1]


def decrire(cle, r):
    debut_jour = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    zones = sorted(r.get("zones") or {}, key=lambda z: -r["zones"][z])
    parts = [dire("tetra.reseau", reseau=cle,
                  zones=_liste([f"zone {z}" for z in zones]) if zones else "zone inconnue",
                  visites=nombre(r.get("visites", 0), "fois", "fois"),
                  quand=_quand(r.get("derniere", time.time())))]
    postes = r.get("postes") or {}
    if postes:
        parts.append(dire("tetra.postes", total=nombre(len(postes), "poste"),
                          aujourdhui=sum(1 for p in postes.values() if p.get("derniere", 0) >= debut_jour),
                          inscriptions=nombre(r.get("inscriptions", 0), "inscription")))
    sds = r.get("sds") or {}
    etats = r.get("etats") or {}
    if sds or etats:
        sources = {}
        for src, v in sds.items():
            sources[src] = sources.get(src, 0) + v.get("n", 0)
        for cle_etat, v in etats.items():
            src = cle_etat.split(":")[0]
            sources[src] = sources.get(src, 0) + v.get("n", 0)
        parts.append(dire("tetra.donnees",
                          sds=nombre(sum(v.get("n", 0) for v in sds.values()), "message court", "messages courts"),
                          etats=nombre(sum(v.get("n", 0) for v in etats.values()), "message d'état",
                                       "messages d'état"),
                          source=max(sources, key=sources.get)))
    m = r.get("messages") or {}
    if m.get("natures"):
        natures = sorted(m["natures"].items(), key=lambda kv: -kv[1])[:4]
        parts.append(dire("tetra.messages", natures=_liste([f"{n} {nom}" for nom, n in natures])))
    if m.get("textes"):
        parts.append(dire("tetra.derniers_textes",
                          textes=" ; ".join(f"{x['src']} : {x['texte']}" for x in m["textes"][-3:])))
    if m.get("positions"):
        parts.append(dire("tetra.positions", nombre=nombre(len(m["positions"]), "poste")))
    a = r.get("appels") or {}
    if a.get("total"):
        parts.append(dire("tetra.appels", total=a["total"], clairs=a.get("clairs", 0), chiffres=a.get("chiffres", 0),
                          prives=a.get("prives", 0), suivis=a.get("suivis", 0)))
    else:
        parts.append(dire("tetra.aucun_appel"))
    controle = [_mhz(f) for f in (r.get("controle") or {})]
    trafic = [_mhz(f) for f in sorted(r.get("porteuses_trafic") or {}, key=int)]
    if controle or trafic:
        parts.append(dire("tetra.porteuses", controle=_liste(controle) if controle else "?",
                          trafic=_liste(trafic) if trafic else "aucune encore"))
    heures = r.get("heures") or []
    if sum(heures) >= 3:
        top = sorted(range(24), key=lambda h: heures[h], reverse=True)[:2]
        parts.append(dire("tetra.heures", heures=_liste([dire("dmr.heure", heure=h) for h in sorted(top)])))
    return " ".join(parts)


def run(line):
    reseaux = (tetra.registre().get("reseaux") or {})
    if not reseaux:
        return dire("tetra.registre_vide")
    ordre = sorted(reseaux.items(), key=lambda kv: kv[1].get("derniere", 0), reverse=True)
    return " ".join(decrire(cle, r) for cle, r in ordre)
