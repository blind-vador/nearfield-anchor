"""Resultats rendus par la machine compagne (lib/compagnon.py), relus par
compagnon_serveur des qu'elle dit "fini" : l'image Meteor est decrite par
Gemini (analyse d'image), archivee sur le disque de stockage et annoncee.
"""
import os
import shutil
import time

import compagnon
from phrases import dire
import veille_donnees as vd

SOUND = "/media/nas/nd1/server/radio_scaner/decoding_numeric.mp3"
METEOR_DIR = "/media/nas/nd2/storage/radio/satellites"
STALE_SEC = 36 * 3600  # travail jamais pris : la machine n'est pas revenue (mode differe : 12 h)


def _meteor(job, result, folder):
    sat = job["params"].get("satellite", "Meteor")
    if result.get("etat") != "fait":
        vd.announce(dire("compagnon.passage", sat=sat, v1=result.get('message') or 'aucune image décodable cette fois'),
                    SOUND, "meteor", "compagnon")
        return
    import meteor_traitement
    from PIL import Image
    os.makedirs(METEOR_DIR, exist_ok=True)
    final = os.path.join(METEOR_DIR, f"{job['id']}.png")
    with Image.open(os.path.join(folder, next(iter(result["sorties"])))) as im:
        im.save(final)  # BMP de la machine compagne -> PNG, trois fois plus petit
    text = meteor_traitement.describe(final, sat)
    vd.archive("meteor", {"satellite": sat, "image": final, "par": result.get("compagnon"),
                          "detail": result.get("message"), "description": text})
    vd.announce(dire("compagnon.image_satellite_meteo_recue", sat=sat, text=text),
                SOUND, "meteor", "compagnon")


def _lora(job, result, folder):
    import json
    import lora_messages
    frames = []
    if result.get("etat") == "fait" and result.get("sorties"):
        with open(os.path.join(folder, next(iter(result["sorties"]))), encoding="utf-8") as f:
            frames = json.load(f)
    card = compagnon.trusted().get(result.get("compagnon"), {})
    lora_messages.handle(job, frames, card.get("nom") or dire("compagnon.la_machine"))


HANDLERS = {"meteor": _meteor, "lora": _lora}


def poll_once():
    for job, result, folder in compagnon.finished_jobs():
        try:
            HANDLERS.get(job.get("type"), lambda *a: None)(job, result, folder)
        except Exception as exc:
            print(f"[compagnon] resultat {job.get('id')} : {exc!r}")
        card = compagnon.trusted().get(result["compagnon"], {})
        stats = card.get("stats", {})
        ok = result.get("etat") == "fait"
        compagnon.update_card(result["compagnon"], stats={
            "faits": stats.get("faits", 0) + ok, "echecs": stats.get("echecs", 0) + (not ok),
            "secondes": stats.get("secondes", 0) + result.get("duree", 0), "dernier": time.time()})
        shutil.rmtree(folder, ignore_errors=True)  # le pont ne garde que l'en-cours
    compagnon.drop_stale(STALE_SEC)
