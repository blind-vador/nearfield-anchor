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



def _decrire_images(job, result, folder):
    """Range la description VLM dans le cache fiche pieces (pas d'annonce vocale)."""
    import pieces
    params = job.get("params") or {}
    source = params.get("source") or "printables"
    piece_id = str(params.get("piece_id") or "")
    if not piece_id:
        print(f"[compagnon] decrire_images sans piece_id : {job.get('id')}")
        return
    if result.get("etat") != "fait":
        fiche = pieces.load_fiche(source, piece_id) or {"id": piece_id, "source": source}
        fiche["images_desc_status"] = "echec"
        pieces.save_fiche(fiche)
        print(f"[compagnon] decrire_images echec {job.get('id')} : {result.get('message')}")
        return
    texte = (result.get("message") or "").strip()
    sorties = result.get("sorties") or {}
    for name in sorties:
        path = os.path.join(folder, name)
        if name.endswith(".json") and os.path.isfile(path):
            import json
            try:
                data = json.load(open(path, encoding="utf-8"))
                texte = (data.get("texte") or texte).strip()
            except (OSError, ValueError) as exc:
                print(f"[compagnon] description.json : {exc!r}")
            break
    pieces.apply_images_desc(source, piece_id, texte, job_id=job.get("id"))
    print(f"[compagnon] decrire_images OK {piece_id} ({len(texte)} car)")


def _voix(job, result, folder):
    """Sons Piper calcules par la machine compagne : chacun mis a sa place
    (cache des annonces sur le NAS, cache de la console), sans ecraser un son
    fait entre-temps par le Pi."""
    import json
    import tarfile
    import tempfile
    if result.get("etat") != "fait":
        print(f"[compagnon] voix : {result.get('message')}")
        return
    carte = json.load(open(os.path.join(folder, "carte.json"), encoding="utf-8"))
    faits = 0
    with tempfile.TemporaryDirectory(prefix="voix_compagne_") as tmp:
        with tarfile.open(os.path.join(folder, "sons.tar")) as tar:
            membres = [m for m in tar.getmembers() if m.isfile() and m.name.lstrip("./").removesuffix(".wav").isdigit()]
            tar.extractall(tmp, members=membres)  # seulement "<n>.wav" : rien ne sort du dossier
        for n, dest in carte.items():
            src = os.path.join(tmp, f"{n}.wav")
            if os.path.exists(dest) or not os.path.exists(src) or os.path.getsize(src) < 1000:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(src, dest + ".compagne.tmp")
            os.replace(dest + ".compagne.tmp", dest)
            faits += 1
    print(f"[compagnon] voix : {faits} sons installes ({result.get('message')})")
    try:
        import voix_preparation
        voix_preparation.kick()  # s'il reste du travail, on le confie aussitot
    except Exception:
        pass


HANDLERS = {"meteor": _meteor, "lora": _lora, "decrire_images": _decrire_images, "voix": _voix}


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
