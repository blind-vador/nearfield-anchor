"""Sons de l'imprimante et de la camera (Epidemic Sound, 2026-10-18), sur le
NAS : server/printer et server/camera. Les noms disent leur usage.

  boucles   printer_moving_head_loop       la tete (ou Z) bouge
            printer_moving_bed_loop        le plateau bouge (axe Y de l'Ender 3)
            printer_pushing_filament_loop  l'extrudeur pousse ou tire le filament
            printer_waiting_temperature_loop  chauffe en cours, temperature pas atteinte
            camera_loading_data_loop       une photo est en cours d'analyse
  ponctuels printer_warning, printer_job_finished, printer_progression_notification,
            printer_filament_in, printer_filament_ejected, camera_take_picture

Une seule boucle a la fois par processus ; une nouvelle remplace l'ancienne.
Les boucles de mouvement (suivre_mouvement) s'arretent quand l'imprimante
confirme la fin du mouvement : apres les commandes, on envoie M400 (attendre
la fin des mouvements) puis M118 E1 <jeton>, que Marlin repete une fois tout
termine ; on guette ce jeton dans le websocket d'OctoPrint. Garde-fou :
MAX_BOUCLE_SEC.
"""
import contextlib
import os
import re
import subprocess
import threading
import uuid

from sound_paths import NAS_BASE

PRINTER_DIR = os.path.join(NAS_BASE, "printer")
CAMERA_DIR = os.path.join(NAS_BASE, "camera")
VOLUME_BOUCLE = 0.45   # sous la voix : on doit toujours entendre les consignes
VOLUME_PONCTUEL = 0.8
MAX_BOUCLE_SEC = 300
MOVE_RE = re.compile(r"^\s*G(0|1|28|29)\b", re.I)

_courante = {"proc": None, "nom": None, "jeton": None}
_garde = threading.Lock()


def chemin(nom):
    dossier = CAMERA_DIR if nom.startswith("camera_") else PRINTER_DIR
    return os.path.join(dossier, nom + ".mp3")


def _lecteur(path, volume, boucle):
    """ffmpeg (decodage, volume, boucle) | aplay ; rend le processus aplay."""
    loop = ["-stream_loop", "-1"] if boucle else []
    dec = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"] + loop +
                           ["-i", path, "-af", f"volume={volume}", "-f", "s16le", "-ac", "2", "-ar", "44100", "-"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    play = subprocess.Popen(["aplay", "-q", "-D", "default", "-r", "44100", "-f", "S16_LE", "-c", "2"],
                            stdin=dec.stdout, stderr=subprocess.DEVNULL)
    dec.stdout.close()
    play.decodeur = dec
    return play


def _tuer(proc):
    if proc is None:
        return
    for p in (proc, getattr(proc, "decodeur", None)):
        if p is not None and p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def jouer(nom, attendre=False):
    """Son ponctuel (n'interrompt pas une boucle en cours)."""
    path = chemin(nom)
    if not os.path.exists(path):
        return
    try:
        proc = _lecteur(path, VOLUME_PONCTUEL, False)
        if attendre:
            proc.wait(timeout=15)
    except Exception as exc:
        print(f"[sons_imprimante] {nom} : {exc!r}")


def demarrer_boucle(nom):
    """Lance la boucle `nom` (remplace celle en cours) ; rend son jeton."""
    path = chemin(nom)
    jeton = uuid.uuid4().hex[:10]
    with _garde:
        if _courante["nom"] == nom and _courante["proc"] is not None and _courante["proc"].poll() is None:
            _courante["jeton"] = jeton  # meme boucle : elle continue, avec la nouvelle fin a attendre
            return jeton
        _tuer(_courante["proc"])
        proc = None
        if os.path.exists(path):
            try:
                proc = _lecteur(path, VOLUME_BOUCLE, True)
            except Exception as exc:
                print(f"[sons_imprimante] boucle {nom} : {exc!r}")
        _courante.update(proc=proc, nom=nom, jeton=jeton)
        garde = threading.Timer(MAX_BOUCLE_SEC, arreter_boucle, args=(jeton,))
        garde.daemon = True
        garde.start()
    return jeton


def arreter_boucle(jeton=None):
    """Arrete la boucle (seulement si c'est encore celle de ce jeton)."""
    with _garde:
        if jeton is not None and _courante["jeton"] != jeton:
            return
        _tuer(_courante["proc"])
        _courante.update(proc=None, nom=None, jeton=None)


def boucle_en_cours():
    with _garde:
        proc = _courante["proc"]
        return _courante["nom"] if proc is not None and proc.poll() is None else None


@contextlib.contextmanager
def boucle(nom):
    """with boucle("printer_waiting_temperature_loop"): ... (rien si nom est None)."""
    if not nom:
        yield
        return
    jeton = demarrer_boucle(nom)
    try:
        yield
    finally:
        arreter_boucle(jeton)


# --- mouvements de l'imprimante ---------------------------------------------------------

def genre(gcodes):
    """Boucle qui correspond a ces commandes, ou None (pas de mouvement)."""
    axes = set()
    for g in gcodes:
        if not MOVE_RE.match(g):
            continue
        mots = set(re.findall(r"\b([XYZE])\s*-?[\d.]", g.upper()))
        if re.match(r"\s*G(28|29)\b", g, re.I):
            mots |= set(re.findall(r"\b([XYZ])\b", g.upper())) or {"X", "Y", "Z"}
        axes |= mots
    if axes & {"X", "Z"}:
        return "printer_moving_head_loop"
    if "Y" in axes:
        return "printer_moving_bed_loop"
    if "E" in axes:
        return "printer_pushing_filament_loop"
    return None


def suivre_mouvement(nom, envoyer):
    """Boucle `nom` jusqu'a la fin reelle des mouvements deja envoyes.
    envoyer(*gcodes) : envoi brut a l'imprimante (sans repasser par ici)."""
    jeton = demarrer_boucle(nom)

    def guetter():
        try:
            import octoprint
            flux = octoprint.Flux()
            if not flux.demarrer():
                raise RuntimeError("websocket OctoPrint injoignable")
            depuis = flux.numero
            envoyer("M400", f"M118 E1 son{jeton}")
            flux.attendre(re.compile(r"echo:\s*son" + jeton), depuis, MAX_BOUCLE_SEC, echec=False)
            flux.arreter()
        except Exception as exc:
            print(f"[sons_imprimante] fin de mouvement non suivie : {exc!r}")
        finally:
            arreter_boucle(jeton)
    threading.Thread(target=guetter, daemon=True).start()
