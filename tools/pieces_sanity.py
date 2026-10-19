#!/usr/bin/env python3
"""Sanity check pieces -> compagnon decrire_images (simu recherche)."""
import os
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import compagnon
import pieces


def log(msg):
    print(f"[sanity {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    query = " ".join(sys.argv[1:]).strip() or "cat figurine"
    log(f"recherche Printables : {query!r}")
    hits, used = pieces.search(query, limit=5)
    if not hits:
        log("ECHEC : aucun resultat")
        return 1
    log(f"requete utilisee={used!r} — {len(hits)} hit(s)")
    for i, h in enumerate(hits[:5]):
        log(f"  [{i}] {h.get('title')} id={h.get('id')}")

    hit = hits[0]
    log(f"ouverture fiche {hit.get('id')} — {hit.get('title')}")

    # Invalide cache images pour forcer un vrai aller-retour compagnon
    cached = pieces.load_fiche(hit["source"], hit["id"]) or {}
    if cached:
        cached["images_desc"] = None
        cached["images_desc_via"] = "sanity-force"
        cached["images_desc_status"] = None
        cached["images_job"] = None
        cached["images_job_t"] = 0
        pieces.save_fiche(cached)

    fiche = pieces.open_fiche(hit)
    job_id = fiche.get("images_job")
    log(f"status={fiche.get('images_desc_status')} job={job_id}")
    log(f"phrase : {pieces.status_images(fiche)}")
    log(f"images : {len(fiche.get('images') or [])}")

    comps = compagnon.companions()
    for c in comps:
        log(f"compagnon {c.get('nom')} presente={c.get('presente')} "
            f"caps={c.get('capacites')} ips={c.get('ips')}")
    if not job_id:
        if (fiche.get("images_desc") or "").strip():
            log("cache encore chaud — force re-depot")
            fiche["images_desc"] = None
            fiche["images_desc_via"] = "sanity-force"
            fiche["images_desc_status"] = None
            fiche["images_job"] = None
            fiche["images_job_t"] = 0
            pieces.save_fiche(fiche)
            fiche, job_id = pieces.request_images_desc(fiche)
            log(f"re-depot job={job_id}")
        else:
            log("ECHEC : pas de job depose (compagnon dispo ? images ?)")
            return 1

    folder = pieces.job_folder(job_id)
    log(f"dossier travail : {folder}")

    deadline = time.time() + 180
    last_phrase = None
    claimed = False
    while time.time() < deadline:
        prog = pieces.job_progress(job_id)
        phrase = pieces.status_images({
            **fiche,
            "images_desc_status": "en_cours",
            "images_job": job_id,
        })
        if phrase != last_phrase:
            log(f"progres : {phrase} | raw={prog}")
            last_phrase = phrase
        if folder and os.path.isfile(os.path.join(folder, "travail.pris.json")) and not claimed:
            claimed = True
            log("OK claim Mac : travail.pris.json")
        fresh = pieces.refresh_fiche(fiche)
        # Aussi appliquer si resultat NAS present mais pas encore poll meteo
        st = fresh.get("images_desc_status")
        if st == "prete" and (fresh.get("images_desc") or "").strip():
            desc = fresh["images_desc"]
            log(f"OK prete via={fresh.get('images_desc_via')} ({len(desc)} car)")
            log("extrait : " + desc[:280].replace("\n", " "))
            log(f"phrase finale : {pieces.status_images(fresh)}")
            if not claimed:
                log("note : pret sans voir travail.pris (claim tres rapide ou poll)")
            return 0
        if st == "echec":
            log("ECHEC status=echec")
            return 1
        time.sleep(4)

    log("ECHEC : timeout 180s")
    if folder:
        log(f"contenu dossier : {os.listdir(folder) if os.path.isdir(folder) else '?'}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"EXCEPTION {exc!r}")
        raise
