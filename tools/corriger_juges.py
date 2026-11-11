#!/usr/bin/env python3
"""Correction ponctuelle de la memoire des juges (audit du 2026-11-11).

Avant ce jour, la notation donnait un "oui" au juge de la prise quel que soit son
contenu (E1), et les prises numeriques sans juge note creditaient la FM (E2). Les
relais connus (APRS, registre DMR) n'avaient aucun poids sur l'ordre des juges
(E4, E5). Cet outil :
  1. rejoue chaque note : le "oui" pose a tort (meme heure, a 3 min pres) est
     retire, et donne au bon juge s'il y en a un, ou change en "non" si la note
     dit "rien" ou lisibilite 1 ;
  2. note "tonalites/donnees" : un oui pour les decodeurs de donnees ;
  3. pose les a priori des relais deja connus (mode APRS, synchro de relais DMR
     du registre) et retire 144,800 (APRS) des relais vocaux.
Par defaut il ne fait que dire ce qu'il changerait ; --appliquer ecrit (copie
de sauvegarde .avant_correction a cote de chaque fichier).
"""
import argparse
import json
import os
import shutil
import sys
import time

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(ICI, "..", d) for d in ("lib", "console", "config")]
import chemins  # noqa: E402
import notation  # noqa: E402
import propositions  # noqa: E402
import relais  # noqa: E402

FENETRE_SEC = 180


def _t(note):
    try:
        return time.mktime(time.strptime(note.get("note_le") or "", "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _ancien_juge(note):
    m = note.get("mesures") or {}
    return m.get("juge") or m.get("modulation_banc") or note.get("mode")


def _nouveau_juge(note):
    m = note.get("mesures") or {}
    juge = m.get("juge") or m.get("modulation_banc")
    if not juge and m.get("numerique"):
        if note.get("mode") == "tetra":
            return "tetra"
        return propositions.juge_du_protocole(m.get("protocole") or m.get("mode_numerique")) or (
            "-fs" if m.get("veille_dmr") else None)
    return juge or note.get("mode")


def _heure_de_migration(data):
    """Les oui/non d'avant la memoire datee ont tous recu l'heure de la migration
    (propositions._faits_anciens, 2026-11-04) : la plus frequente, si elle l'est nettement."""
    import collections
    n = collections.Counter(round(float(f.get("t") or 0)) for juges in data["frequences"].values()
                            for st in juges.values() if isinstance(st, dict) for f in st.get("faits") or [])
    if not n:
        return None
    t, combien = n.most_common(1)[0]
    return t if combien >= 10 else None


def _retirer_oui(st, t, migration=None):
    """Retire le oui pose par cette note. Rend l'heure du fait retire, ou None."""
    for i, fait in enumerate(st.get("faits") or []):
        ft = float(fait.get("t") or 0)
        meme_note = abs(ft - t) <= FENETRE_SEC
        migre = migration is not None and t <= migration + 1 and abs(ft - migration) <= 1
        if fait.get("bon") and (meme_note or migre):
            del st["faits"][i]
            st["oui"] = max(0, int(st.get("oui") or 0) - 1)
            return ft
    return None


def _ajouter(data, freq, juge, bon, t):
    cle = propositions._cle(freq)
    st = data["frequences"].setdefault(cle, {}).setdefault(juge, {"oui": 0, "non": 0, "faits": []})
    st.setdefault("faits", [])
    st["oui" if bon else "non"] = int(st.get("oui" if bon else "non") or 0) + 1
    st["faits"].append({"t": t, "bon": bool(bon)})
    st["faits"] = sorted(st["faits"], key=lambda f: f.get("t") or 0)[-propositions.FAITS_MAX:]


def rejouer(notes, data, journal):
    migration = _heure_de_migration(data)
    # De la plus recente a la plus ancienne : les oui convertis en bloc a la migration
    # viennent des notes recentes (la memoire des juges date du 2 novembre).
    for note in sorted(notes, key=lambda n: n.get("note_le") or "", reverse=True):
        t, freq = _t(note), note.get("freq_hz")
        if not t or not freq:
            continue
        cle = propositions._cle(freq)
        ancien, nouveau = _ancien_juge(note), _nouveau_juge(note)
        avis = notation.avis_juge(note, note.get("validation_rapide", False))
        contenu = (note.get("degres") or {}).get("contenu") or {}
        if any(contenu.get(c) for c in notation.EXAMEN_CONTENU):
            _ajouter(data, freq, "donnees", True, t)
            journal.append(f"{freq / 1e6:.4f} : donnees +1 (note {note.get('note_le')})")
        st = (data["frequences"].get(cle) or {}).get(ancien)
        if avis == "oui" and nouveau == ancien:
            continue
        retire = _retirer_oui(st, t, migration) if st else None
        if retire is None:
            continue   # pas de oui pour cette note (fiche de rendus, ou deja corrige)
        quoi = f"{freq / 1e6:.4f} : oui retire a {ancien}"
        if avis in ("oui", "non") and nouveau:
            _ajouter(data, freq, nouveau, avis == "oui", retire)
            quoi += f", {avis} a {nouveau}"
        journal.append(quoi + f" (note {note.get('note_le')})")


def relais_connus(journal, appliquer):
    for f, r in relais.load().items():
        juge = relais.JUGE_DU_MODE.get(r.get("mode"))
        if juge:
            journal.append(f"{int(f) / 1e6:.4f} : a priori {juge} (relais {r.get('nom')}, {r.get('mode')})")
            if appliquer:
                relais._annonce_mode(int(f), r.get("mode"), r.get("source") or "aprs")
    try:
        with open(chemins.base("dmr_frequences.json"), encoding="utf-8") as fh:
            registre = json.load(fh)
    except (OSError, ValueError):
        registre = {}
    for f, e in registre.items():
        if e.get("relais"):
            journal.append(f"{int(f) / 1e6:.4f} : a priori -fs (synchro de relais DMR au registre)")
            if appliquer:
                propositions.a_priori(int(f), "-fs", 2.0, "synchro relais DMR")
    data = relais.load()
    faux = [f for f in data if relais._pas_relais(int(f))]
    for f in faux:
        journal.append(f"{int(f) / 1e6:.4f} : retire des relais vocaux (frequence APRS)")
        del data[f]
    if faux and appliquer:
        relais._save(data)


def _sauver(path):
    if os.path.exists(path):
        shutil.copy2(path, path + ".avant_correction")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--appliquer", action="store_true")
    args = ap.parse_args()
    notes = notation.load_notes()
    data = propositions._lire_scores()
    journal = []
    rejouer(notes, data, journal)
    n_notes = len(journal)
    relais_connus(journal, False)
    for ligne in journal:
        print(ligne)
    print(f"\n{n_notes} correction(s) de notes, {len(journal) - n_notes} relais.")
    if not args.appliquer:
        print("Rien n'est ecrit (ajouter --appliquer).")
        return
    _sauver(propositions.SCORES)
    _sauver(relais._path())
    propositions._ecrire_scores(data)
    relais_connus([], True)
    print("Ecrit.")


if __name__ == "__main__":
    main()
