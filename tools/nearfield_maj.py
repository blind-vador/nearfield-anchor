#!/usr/bin/env python3
"""Mise a jour du scanner depuis le depot git (OTA, axe 4, 2026-10-29).

Lance periodiquement par nearfield-maj.timer. Si origin/main a avance ET que
ce scanner est en retard, on se met a jour en fast-forward SEULEMENT, on
relance les services qui tournent depuis le depot, et on l'annonce a la voix.

Prudence (jamais de perte de travail local) :
  - arbre de travail sale  -> on s'abstient (un correctif local en cours) ;
  - en avance sur origin    -> rien a tirer (commits locaux non pousses) ;
  - divergence (ni ancetre) -> on ne force pas, on signale ; intervention main.

Memes gestes qu'edge_maj (annonce speak_cloud, relance "sudo -n systemctl
restart <nom>"). Le cadre est encore ferme : pour l'instant ce scanner est
souvent le noeud qui ecrit, donc l'OTA dira surtout "en avance" tant que les
commits ne sont pas pousses ; il tirera vraiment sur les scanners en retard.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIRS = ("lib", "watchers", "tools", "config", "bridge", "console", "nas")


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)


def _ancetre(a, b):
    """a est-il un ancetre de b ? (a..b est un simple fast-forward)"""
    return git("merge-base", "--is-ancestor", a, b).returncode == 0


def annoncer(texte):
    """Voix si possible (comme edge_maj), journal dans tous les cas."""
    print(f"[nearfield_maj] {texte}", flush=True)
    try:
        sys.path.insert(0, os.path.join(REPO, "lib"))
        import speak
        speak.speak_cloud(texte, source="systeme")
    except Exception as exc:
        print(f"[nearfield_maj] annonce vocale impossible : {exc!r}", flush=True)


def services_du_depot():
    """Services EN COURS dont l'ExecStart pointe dans le depot (a relancer)."""
    out = subprocess.run(
        ["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain"],
        capture_output=True, text=True).stdout
    a_relancer = []
    for ligne in out.splitlines():
        if not ligne.split():
            continue
        unite = ligne.split()[0]
        ex = subprocess.run(["systemctl", "show", unite, "-p", "ExecStart", "--value"],
                            capture_output=True, text=True).stdout
        if REPO in ex:
            a_relancer.append(unite)
    return a_relancer


def copier_phrases():
    import shutil
    sys.path.insert(0, os.path.join(REPO, "config"))
    import chemins
    dest = chemins.base("phrases")
    os.makedirs(dest, exist_ok=True)
    src = os.path.join(REPO, "phrases")
    for nom in os.listdir(src):
        if nom.endswith(".ini"):
            shutil.copy2(os.path.join(src, nom), os.path.join(dest, nom))


def main():
    # 1) jamais sur un arbre sale : on ne risque pas d'ecraser un correctif local.
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        print("[nearfield_maj] arbre de travail non propre -- mise a jour reportee", flush=True)
        return
    if git("fetch", "origin", "--quiet").returncode != 0:
        print("[nearfield_maj] git fetch a echoue (reseau ?) -- on reessaiera", flush=True)
        return
    local = git("rev-parse", "HEAD").stdout.strip()
    remote = git("rev-parse", "origin/main").stdout.strip()
    if not remote or local == remote:
        print("[nearfield_maj] deja a jour", flush=True)
        return
    # 2) on ne tire QUE si on est en retard (HEAD ancetre de origin/main).
    if not _ancetre("HEAD", "origin/main"):
        if _ancetre("origin/main", "HEAD"):
            print("[nearfield_maj] en avance sur origin (commits locaux non pousses) -- rien a tirer", flush=True)
        else:
            annoncer("Mise a jour du scanner impossible sans conflit : intervention manuelle requise.")
        return
    changes = git("diff", "--name-only", "HEAD", "origin/main").stdout.split()
    if git("merge", "--ff-only", "origin/main").returncode != 0:
        annoncer("Echec de la mise a jour du scanner.")
        return
    # 3) dependances Python si requirements.txt a bouge.
    if "requirements.txt" in changes:
        subprocess.run(["sudo", "-n", sys.executable, "-m", "pip", "install", "-q", "-r",
                        os.path.join(REPO, "requirements.txt")],
                       env=dict(os.environ, PIP_BREAK_SYSTEM_PACKAGES="1"), timeout=600)
    # 3 bis) phrases : lib/phrases lit des COPIES (chemins.base("phrases"), posees par
    # install.sh). Sans cette copie, une phrase livree par la mise a jour etait dite
    # par sa cle (« examen.pret », 2026-11-11). Relues a chaud (date du fichier).
    if any(c.startswith("phrases/") and c.endswith(".ini") for c in changes):
        copier_phrases()
    # 4) relance des services si du code tournant a change.
    relances = 0
    if any(c.endswith(".py") and c.split("/")[0] in RUNTIME_DIRS for c in changes):
        for unite in services_du_depot():
            nom = unite[:-len(".service")] if unite.endswith(".service") else unite
            if subprocess.run(["sudo", "-n", "systemctl", "restart", nom], timeout=120).returncode == 0:
                relances += 1
    # 5) un rebuild natif (RTLSDR-Airband, dsd-fme, RNNoise...) n'est pas automatique.
    msg = f"Scanner mis a jour, {len(changes)} fichier{'s' if len(changes) > 1 else ''}"
    if relances:
        msg += f", {relances} service{'s' if relances > 1 else ''} relance{'s' if relances > 1 else ''}"
    if "install.sh" in changes:
        msg += ". Attention, install point s h a change : un rebuild des bibliotheques peut etre necessaire"
    annoncer(msg + ".")


if __name__ == "__main__":
    main()
