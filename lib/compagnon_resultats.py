"""Resultats rendus par la machine compagne (lib/compagnon.py), relus par le
Pi toutes les minutes (thread de meteo_watch) : l'image Meteor est decrite
par Gemini (analyse d'image), archivee sur le disque de stockage et annoncee.
"""
import os
import shutil
import time

import compagnon
import veille_donnees as vd

SOUND = "/media/nas/nd1/server/radio_scaner/decoding_numeric.mp3"
METEOR_DIR = "/media/nas/nd2/storage/radio/satellites"
POLL_SEC = 60
STALE_SEC = 12 * 3600  # travail jamais pris : la machine n'est pas revenue


def _meteor(job, result, folder):
    sat = job["params"].get("satellite", "Meteor")
    if result.get("etat") != "fait":
        vd.announce(f"Passage {sat} : {result.get('message') or 'aucune image décodable cette fois'}.",
                    SOUND, "meteor", "compagnon")
        return
    import meteor_traitement
    from PIL import Image
    os.makedirs(METEOR_DIR, exist_ok=True)
    final = os.path.join(METEOR_DIR, f"{job['id']}.png")
    with Image.open(os.path.join(folder, result["sorties"][0])) as im:
        im.save(final)  # BMP de la machine compagne -> PNG, trois fois plus petit
    text = meteor_traitement.describe(final, sat)
    vd.archive("meteor", {"satellite": sat, "image": final, "par": result.get("par"),
                          "detail": result.get("message"), "description": text})
    vd.announce(f"Image du satellite météo {sat} reçue de la machine compagne. {text}",
                SOUND, "meteor", "compagnon")


HANDLERS = {"meteor": _meteor}


def poll_once():
    for job, result, folder in compagnon.finished_jobs():
        try:
            HANDLERS.get(job.get("type"), lambda *a: None)(job, result, folder)
        except Exception as exc:
            print(f"[compagnon] resultat {job.get('id')} : {exc!r}")
        compagnon.mark_read(folder, result)
        shutil.rmtree(folder, ignore_errors=True)  # le pont ne garde que l'en-cours
    compagnon.drop_stale(STALE_SEC)


def watch():
    while True:
        try:
            poll_once()
        except Exception as exc:
            print(f"[compagnon] {exc!r}")
        time.sleep(POLL_SEC)
