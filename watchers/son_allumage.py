"""Sons d'allumage et d'extinction du Pi (2026-10-23), joues par
son-allumage.service des que la carte son USB est la, sans attendre le NAS
(copies de la carte SD, sons.py).

  son_allumage.py demarrage : son de demarrage ; si la session precedente ne
                              s'est pas terminee proprement (coupure de courant,
                              plantage), le dit, avec l'heure du dernier signe
                              de vie.
  son_allumage.py arret     : son d'arret (arret ou redemarrage programme).

Arret propre reconnu dans le journal systeme : sa derniere ligne est
"Journal stopped" ; apres une coupure, elle est quelconque."""
import os
import subprocess
import sys
import time

import sons

CARTE_ATTENTE_SEC = 30
VOIX_ATTENTE_SEC = 180


def _carte_prete():
    try:
        with open("/proc/asound/cards", encoding="utf-8") as f:
            return "USB Audio" in f.read()
    except OSError:
        return False


def _jouer(evenement):
    chemin = sons.chemin(evenement)
    if not chemin:
        return
    fin = time.time() + CARTE_ATTENTE_SEC
    while not _carte_prete() and time.time() < fin:
        time.sleep(0.5)
    subprocess.run(["mpg123", "-q", "-o", "alsa", "-a", "default", chemin], timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _derniere_ligne_precedente():
    try:
        r = subprocess.run(["journalctl", "-b", "-1", "-n", "1", "--no-pager", "-o", "short-iso"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lignes = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith("--")]
    return lignes[-1] if r.returncode == 0 and lignes else None


def _annoncer_coupure(ligne):
    from phrases import dire
    try:
        heure = time.strftime("%H heures %M", time.strptime(ligne[:19], "%Y-%m-%dT%H:%M:%S"))
        texte = dire("allumage.coupure_heure", heure=heure)
    except ValueError:
        texte = dire("allumage.coupure")
    print(f"[son_allumage] {texte} ({ligne[:120]})", flush=True)
    fin = time.time() + VOIX_ATTENTE_SEC  # la voix de secours peut vivre sur le NAS
    while not os.path.ismount("/media/nas/nd1") and time.time() < fin:
        time.sleep(5)
    import speak
    speak.speak(texte, source="systeme")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "demarrage"
    if action == "arret":
        _jouer("arret")
        return
    _jouer("demarrage")
    ligne = _derniere_ligne_precedente()
    if ligne and "Journal stopped" not in ligne:
        _annoncer_coupure(ligne)


if __name__ == "__main__":
    main()
