"""Le soir, les prises validees dans la journee partent sur le Mac pour Whisper.
Le texte revient par le pont. On ne transcrit pas une prise chiffree, ni une
prise deja envoyee. Huit au plus, pour ne pas occuper la machine toute la nuit.
"""
import json
import os
import shutil
import time

NOTES = "/media/nas/nd1/server/radio_notes.json"
JOURNAL = "/media/nas/nd1/server/radio_log"
MAX_SOIR = 8
MIN_OCTETS = 8000


def _notes():
    try:
        with open(NOTES, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError) as exc:
        print(f"[whisper] notes illisibles : {exc!r}")
        return []


def _choisir():
    import noms_whisper
    jour = time.strftime("%Y-%m-%d")
    choix = []
    for note in reversed(_notes()):
        fichier = note.get("fichier") or ""
        if not note.get("validation_rapide"):
            continue
        if not str(note.get("note_le") or "").startswith(jour):
            continue
        if (note.get("mesures") or {}).get("chiffre") is True:
            print(f"[whisper] {fichier} : chiffre, laisse")
            continue
        if (note.get("reponses") or {}).get("contenu") in ("musique", "tonalites", "rien"):
            continue
        if noms_whisper.deja(fichier):
            continue
        chemin = os.path.join(JOURNAL, os.path.basename(fichier))
        if not os.path.isfile(chemin) or os.path.getsize(chemin) < MIN_OCTETS:
            continue
        choix.append((note, chemin))
        if len(choix) >= MAX_SOIR:
            break
    return choix


def main():
    if not os.path.ismount("/media/nas/nd1"):
        print("[whisper] disque absent, soir reporte")
        return
    choix = _choisir()
    if not choix:
        print("[whisper] aucune prise validee a transcrire")
        return
    import compagnon
    import noms_whisper
    if not compagnon.rank("whisper", 1):
        print("[whisper] aucune machine compagne ne transcrit ce soir")
        return
    for note, chemin in choix:
        params = {
            "fichier": "son.mp3",
            "origine": os.path.basename(note["fichier"]),
            "freq_hz": note.get("freq_hz"),
            "mode": note.get("mode"),
            "bande": note.get("bande"),
            "label": note.get("label") or "",
        }
        job_id, dossier, job = compagnon.new_job("whisper", params)
        shutil.copyfile(chemin, os.path.join(dossier, "son.mp3"))
        compagnon.submit(job)
        noms_whisper.marquer_envoye(params["origine"])
        print(f"[whisper] {params['origine']} confie ({job_id})")


if __name__ == "__main__":
    main()
