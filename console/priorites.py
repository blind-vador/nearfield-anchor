"""Commande "priorites" : ordre des sources sur les haut-parleurs et leur
comportement quand une source plus importante parle (lib/audio_lock.py).

  fleches haut / bas                  parcourir
  Majuscule + fleche haut / bas       monter / descendre la source
  page haut / page bas                idem (secours)
  fleches gauche / droite             comportement : attend son tour,
                                      joue attenuee, abandonne si occupe
  Entree                              enregistre      Echap : annule
  F1                                  redit les touches

La console ne recoit pas la touche Majuscule avec les fleches (le terminal
envoie la meme sequence) : son etat est lu directement sur le clavier par
evdev au moment de la fleche, comme le mode reperage des touches.
Les services relisent les reglages d'eux-memes (10 s au plus), rien a
relancer.
"""
import sys

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import audio_lock  # noqa: E402
import conseils  # noqa: E402

UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
PAGE_UP, PAGE_DOWN = "\x1b[5~", "\x1b[6~"
MODE_ORDER = ["attend", "attenue", "abandonne"]
TITLE = "Priorités des haut-parleurs, de la plus importante à la moins importante."
CONSIGNE = ("Flèches haut et bas pour parcourir. Majuscule plus flèche, ou page haut et page bas, "
            "pour monter ou descendre la source. Gauche et droite : son comportement quand une source "
            "plus importante parle. Entrée enregistre, échap annule. F1 redit ces touches.")
SHIFT_CODES = None


def _shift_held():
    """Vrai si une touche Majuscule est enfoncee, sur n'importe quel clavier."""
    global SHIFT_CODES
    try:
        import evdev
        if SHIFT_CODES is None:
            SHIFT_CODES = {evdev.ecodes.KEY_LEFTSHIFT, evdev.ecodes.KEY_RIGHTSHIFT}
        for path in evdev.list_devices():
            dev = evdev.InputDevice(path)
            try:
                if SHIFT_CODES & set(dev.active_keys()):
                    return True
            except OSError:
                continue
            finally:
                dev.close()  # ouvert a chaque fleche : toujours referme
    except Exception:
        return False
    return False


def _label(order, modes, i):
    source = order[i]
    return f"{i + 1}, {audio_lock.SOURCES[source]}, {audio_lock.MODES[modes[source]]}"


def run(key_source, say, out):
    conf = audio_lock.settings(fresh=True)
    order = list(conf["ordre"])
    modes = dict(conf["comportements"])
    i = 0
    intro = conseils.intro("priorites", TITLE, CONSIGNE)
    out(intro + "\n")
    say(f"{intro} {_label(order, modes, i)}")
    for key in key_source:
        if key == "\x1b":
            return "Annulé, priorités inchangées."
        if key in ("\r", "\n"):
            data = {"ordre": order, "comportements": modes, "volume_attenue": conf["volume_attenue"]}
            if not audio_lock.save_settings(data):
                return "Le disque du NAS est inaccessible : priorités gardées sur le Pi seulement."
            first = audio_lock.SOURCES[order[0]]
            return f"Priorités enregistrées. La plus importante : {first}."
        move = 0
        if key in (PAGE_UP, PAGE_DOWN):
            move = -1 if key == PAGE_UP else 1
        elif key in (UP, DOWN) and _shift_held():
            move = -1 if key == UP else 1
        if move:
            j = i + move
            if 0 <= j < len(order):
                order[i], order[j] = order[j], order[i]
                i = j
                text = f"{'Monte' if move < 0 else 'Descend'} : {_label(order, modes, i)}"
            else:
                text = "Déjà tout en haut." if move < 0 else "Déjà tout en bas."
        elif key in (UP, DOWN):
            j = i + (-1 if key == UP else 1)
            if 0 <= j < len(order):
                i = j
                text = _label(order, modes, i)
            else:
                text = ("Début de liste, " if j < 0 else "Fin de liste, ") + _label(order, modes, i)
        elif key in (LEFT, RIGHT):
            source = order[i]
            k = MODE_ORDER.index(modes[source]) + (1 if key == RIGHT else -1)
            modes[source] = MODE_ORDER[k % len(MODE_ORDER)]
            text = audio_lock.MODES[modes[source]]
        elif conseils.is_help(key):
            text = CONSIGNE
        else:
            text = "Flèches, majuscule plus flèche, entrée ou échap. F1 pour l'aide."
        out(f"[{text}]\n")
        say(text)
    return "Priorités fermées."
