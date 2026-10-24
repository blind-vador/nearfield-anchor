import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")
import audio_lock
from speak import speak_cloud

# Surveille en continu les deux disques du NAS (nd1/nd2) pendant que le
# systeme tourne -- complementaire a nas_manager/nas.py, qui ne monte les
# disques qu'au demarrage (@reboot). Trouve en session (2026-08-30) : un
# disque peut se deconnecter puis se reconnecter EN COURS DE SESSION sans
# que le Pi ne redemarre -- nas.py ne s'execute alors plus jamais, et Linux
# attribue un nouveau nom de peripherique (/dev/sdc, /dev/sdd...) au disque
# reconnecte, laissant le point de montage colle sur l'ancien nom devenu
# fantome (erreur d'entree/sortie en lecture jusqu'au prochain redemarrage).
# Ce script detecte et corrige automatiquement, sans intervention manuelle.
#
# Montage par UUID (stable, ne depend jamais de l'ordre d'enumeration USB)
# plutot que par nom /dev/sdX -- elimine aussi le probleme historique
# d'inversion des disques evoque dans nas.py (plus besoin de la logique
# nd1.txt/nd2.txt, chaque disque va toujours a son point de montage prevu).

CHECK_INTERVAL_SEC = 30
CHECK_TIMEOUT_SEC = 5
WARNING_REMINDER_EVERY = 10  # ~5 min a 30s d'intervalle, pas a chaque cycle

DISKS = [
    {"mount_point": "/media/nas/nd1", "uuid": "e5f38c63-862e-417b-9951-07dc0a1319b8",
     "label": "disque de sauvegarde"},
    {"mount_point": "/media/nas/nd2", "uuid": "e47991c3-4105-4971-aeb7-8051b3287cbd",
     "label": "disque principal de stockage"},
]

RAPPEL_SEC = 15 * 60


def _son_erreur():
    try:
        import sons
        from sound_player import play_sound_effect
        chemin = sons.chemin("nas_injoignable")
        if chemin:
            play_sound_effect(chemin, label="nas_watch")
    except Exception as exc:
        print(f"[nas_watch] son : {exc!r}")


def _speak(text, repetitive=True):
    """Voix cloud (edge-tts) avec repli espeak automatique -- comme le
    reste du projet, plutot que l'espeak brut d'origine (2026-08-31,
    demande explicite). repetitive=True par defaut : toutes les phrases de
    ce watcher viennent d'un petit ensemble fini (2 disques x quelques
    etats possibles), jamais de texte vraiment libre."""
    lock = audio_lock.acquire()
    try:
        speak_cloud(text, repetitive=repetitive)
    finally:
        audio_lock.release(lock)


def _mount_is_healthy(mount_point):
    """Deux conditions distinctes : (1) quelque chose est reellement monte
    la (os.path.ismount) -- sans ca, "ls" reussit silencieusement sur un
    repertoire local vide et masque un demontage complet (trouve en testant
    ce script : un simple "umount" sans remontage n'etait jamais detecte) ;
    (2) la lecture reussit vraiment ("timeout" en prefixe pour eviter que le
    script ne reste bloque indefiniment si le montage est dans un etat fige
    -- meme esprit que le commentaire de l'utilisateur sur nas.py : "eviter
    que le pi ne se bloque")."""
    if not os.path.ismount(mount_point):
        return False
    try:
        result = subprocess.run(
            ["timeout", str(CHECK_TIMEOUT_SEC), "ls", mount_point],
            capture_output=True, timeout=CHECK_TIMEOUT_SEC + 2,
        )
        return result.returncode == 0
    except Exception:
        return False


def _disk_present(uuid):
    return os.path.exists(f"/dev/disk/by-uuid/{uuid}")


def _remount(disk):
    subprocess.run(
        ["sudo", "-n", "/usr/bin/umount", "-l", disk["mount_point"]],
        capture_output=True, timeout=15,
    )
    result = subprocess.run(
        ["sudo", "-n", "/usr/bin/mount", f"UUID={disk['uuid']}", disk["mount_point"]],
        capture_output=True, timeout=15,
    )
    if result.returncode != 0:
        return False, result.stderr.decode(errors="replace").strip()
    return _mount_is_healthy(disk["mount_point"]), None


def _startup_announcement():
    """Annonce au demarrage quels disques sont trouves -- l'ancien nas.py
    le faisait toujours ("2 disques trouves, disque de sauvegarde en
    ligne..."), mais nas_watch.py, lui, ne parle que sur les CHANGEMENTS
    d'etat (voir la boucle de main()) : si les deux disques sont deja
    sains au lancement, il ne dit alors jamais rien. Signale comme "pas
    normal" par l'utilisateur en preparant le depot git (2026-08-31) --
    corrige ici en reutilisant _mount_is_healthy()/_disk_present(), sans
    nouvelle logique de montage."""
    healthy_count = 0
    parts = []
    for disk in DISKS:
        if _mount_is_healthy(disk["mount_point"]):
            healthy_count += 1
            parts.append(f"{disk['label']} en ligne")
        elif _disk_present(disk["uuid"]):
            parts.append(f"{disk['label']} present mais pas monte")
        else:
            parts.append(f"{disk['label']} introuvable")
    plural = "s" if healthy_count != 1 else ""
    summary = f"{healthy_count} disque{plural} sur {len(DISKS)} en ligne. " + ", ".join(parts) + "."
    print(f"[nas_watch] etat au demarrage : {summary}")
    _speak(summary)


def main():
    print("[nas_watch] demarrage de la surveillance des disques NAS")
    _startup_announcement()
    fail_streak = {disk["mount_point"]: 0 for disk in DISKS}

    while True:
        for disk in DISKS:
            mount_point = disk["mount_point"]
            if _mount_is_healthy(mount_point):
                if fail_streak[mount_point] > 0:
                    print(f"[nas_watch] {mount_point} de nouveau sain")
                fail_streak[mount_point] = 0
                continue

            if not _disk_present(disk["uuid"]):
                fail_streak[mount_point] += 1
                print(f"[nas_watch] {mount_point} : disque absent (UUID introuvable)")
                if fail_streak[mount_point] == 1:
                    # alarme redite de temps en temps tant que le disque manque, jusqu'au "compris" (2026-10-24)
                    import alarmes
                    alarmes.declencher("nas_injoignable", f"Attention, {disk['label']} introuvable. Vérifie la connexion.",
                                       rappel_sec=RAPPEL_SEC,
                                       encore=lambda mp=mount_point: not _mount_is_healthy(mp))
                continue

            print(f"[nas_watch] {mount_point} en erreur -- tentative de remontage automatique")
            ok, error = _remount(disk)
            if ok:
                print(f"[nas_watch] {mount_point} remonte avec succes")
                _speak(f"{disk['label']} reconnecté, remontage automatique réussi.")
                fail_streak[mount_point] = 0
            else:
                fail_streak[mount_point] += 1
                print(f"[nas_watch] echec du remontage de {mount_point} : {error}")
                if fail_streak[mount_point] == 1 or fail_streak[mount_point] % WARNING_REMINDER_EVERY == 0:
                    _speak(f"Attention, échec du remontage automatique du {disk['label']}.")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
