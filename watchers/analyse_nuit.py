"""La nuit, la passe d'analyse du Mac reprend les bruts et les rejets du jour
(2026-11-11 : travail "examen_lot" du pont, a la place de l'agent launchd du
Mac que macOS empechait de lire le NAS). Reprend aussi une seule fois les
demandes restees dans l'ancienne file partagee (radio_log/a_examiner.json).
"""
import os


def main():
    if not os.path.ismount("/media/nas/nd1"):
        print("[examen] disque absent, passe de nuit reportee")
        return
    import examen
    examen.reprendre_ancienne_file()
    examen.lancer_nuit()


if __name__ == "__main__":
    main()
