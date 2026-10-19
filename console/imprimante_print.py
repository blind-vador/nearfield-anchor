import os
"""Questionnaire d'impression + controle camera + lancement.

Depuis l'explorateur (action Imprimer) ou une commande dediee.
"""
import os
import sys
import threading
import time

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import conseils  # noqa: E402
import imprimante_materiel as mat  # noqa: E402
import imprimante_tranche as tranche  # noqa: E402
import matcher  # noqa: E402
import octoprint as op  # noqa: E402
import pieces  # noqa: E402
import vision  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

UP, DOWN = "\x1b[A", "\x1b[B"
ESC, ENTER = "\x1b", ("\r", "\n")

# Questions dans l'ordre : (id, options_getter, get, set, help_key, announce_key)
# options = list of (value_id, label_phrase_key_suffix or None)


def _options_couche():
    return [("rapide", "imprimante.param_couche_rapide"),
            ("normale", "imprimante.param_couche_normale"),
            ("fine", "imprimante.param_couche_fine")]


def _options_remplissage():
    return [("leger", "imprimante.param_remplissage_leger"),
            ("normal", "imprimante.param_remplissage_normal"),
            ("solide", "imprimante.param_remplissage_solide")]


def _options_supports():
    return [(False, "imprimante.param_supports_non"),
            (True, "imprimante.param_supports_oui")]


def _options_adherence():
    return [("rien", "imprimante.param_adherence_rien"),
            ("skirt", "imprimante.param_adherence_skirt"),
            ("brim", "imprimante.param_adherence_brim"),
            ("raft", "imprimante.param_adherence_raft")]


QUESTIONS = [
    {
        "id": "couche",
        "titre": "imprimante.q_couche_titre",
        "help": "imprimante.q_couche_aide",
        "options": _options_couche,
        "get": lambda s: s.get("couche") or "normale",
        "set": lambda s, v: s.__setitem__("couche", v),
    },
    {
        "id": "remplissage",
        "titre": "imprimante.q_remplissage_titre",
        "help": "imprimante.q_remplissage_aide",
        "options": _options_remplissage,
        "get": lambda s: s.get("remplissage") or "normal",
        "set": lambda s, v: s.__setitem__("remplissage", v),
    },
    {
        "id": "supports",
        "titre": "imprimante.q_supports_titre",
        "help": "imprimante.q_supports_aide",
        "options": _options_supports,
        "get": lambda s: bool(s.get("supports")),
        "set": lambda s, v: s.__setitem__("supports", bool(v)),
    },
    {
        "id": "adherence",
        "titre": "imprimante.q_adherence_titre",
        "help": "imprimante.q_adherence_aide",
        "options": _options_adherence,
        "get": lambda s: s.get("adherence") or "skirt",
        "set": lambda s, v: s.__setitem__("adherence", v),
    },
]


def declare_materiau(line):
    """Commande : imprimante chargee en PLA / ABS..."""
    code = mat.parse_materiau_line(line)
    if not code:
        # Juste "materiau" / "filament charge" -> etat actuel
        if not any(a in matcher.normalize(line) for a in mat.ALIAS):
            return mat.resume_materiau() + " " + dire("imprimante.materiau_aide_declare")
        return dire("imprimante.materiau_inconnu")
    got = mat.set_materiau(code)
    if not got:
        return dire("imprimante.materiau_inconnu")
    code, preset = got
    return dire(
        "imprimante.materiau_enregistre",
        nom=preset["nom"],
        buse=preset["buse"],
        plateau=preset["plateau"],
    )


def _idx_for(options, value):
    for i, (v, _) in enumerate(options):
        if v == value:
            return i
    return 0


def _announce_question(q, settings):
    options = q["options"]()
    cur = q["get"](settings)
    i = _idx_for(options, cur)
    val_id, phrase_key = options[i]
    return dire(q["titre"]) + " " + dire(phrase_key) + " " + dire(
        "imprimante.q_consigne",
        n=nombre(len(options), "choix", "choix"),
    )


def _questionnaire(key_source, say, out):
    """Parcourt les questions. Rend settings ou None si annule."""
    settings = mat.get_settings()
    qi = 0
    intro = conseils.intro(
        "imprimante_print_q",
        dire("imprimante.q_intro", materiau=mat.materiau_courant()[1]["nom"]),
        dire("imprimante.q_intro_consigne"),
    )
    out(intro + "\n")
    say(intro)
    text = _announce_question(QUESTIONS[qi], settings)
    out(text + "\n")
    say(text)

    while qi < len(QUESTIONS):
        for key in key_source:
            voix.stop()
            q = QUESTIONS[qi]
            options = q["options"]()
            cur = q["get"](settings)
            oi = _idx_for(options, cur)
            if key == ESC:
                return None
            if conseils.is_help(key):
                text = dire(q["help"])
            elif key == UP:
                oi = (oi - 1) % len(options)
                q["set"](settings, options[oi][0])
                text = dire(options[oi][1])
            elif key == DOWN:
                oi = (oi + 1) % len(options)
                q["set"](settings, options[oi][0])
                text = dire(options[oi][1])
            elif key in ENTER:
                mat.save_settings(settings)
                qi += 1
                if qi >= len(QUESTIONS):
                    return settings
                text = dire("imprimante.q_suivant") + " " + _announce_question(QUESTIONS[qi], settings)
            else:
                text = dire(q["help"])
            out(f"[{text}]\n")
            say(text)
        return None
    return settings


def _photo_controle(say, out):
    """Prend une photo + description ; True si OK technique."""
    busy = vision.busy()
    if busy:
        say(busy)
        return False
    done = {"ok": False, "err": None}
    barrier = threading.Event()

    def _say(msg):
        out(msg + "\n")
        say(msg)

    def run():
        try:
            # Reutilise la logique vision de facon bloquante
            _say(dire("vision.photo"))
            try:
                jpeg = vision._snapshot()
            except OSError:
                _say(dire("vision.camera_repond_verifie_webcam"))
                done["err"] = "camera"
                return
            try:
                if vision._brightness(jpeg) < vision.MIN_BRIGHTNESS:
                    _say(dire("vision.photo_trop_sombre_caisson"))
                    done["err"] = "sombre"
                    return
                _say(dire("vision.analyse_cours"))
                description, problem = vision._ask_gemini(
                    jpeg,
                    "Confirme si le plateau est libre, propre, et si le filament "
                    "semble bien engage dans l'extrudeur. Signale tout obstacle visible.",
                )
            finally:
                del jpeg
            _say(description or problem or dire("vision.description_echoue_erreur_inattendue"))
            if description:
                done["ok"] = True
        except Exception as exc:
            print(f"[imprimante_print] photo : {exc!r}", file=sys.stderr)
            _say(dire("vision.description_echoue_erreur_inattendue"))
            done["err"] = "exc"
        finally:
            barrier.set()

    threading.Thread(target=run, daemon=True).start()
    barrier.wait(timeout=120)
    return bool(done["ok"])


def _gate_camera(key_source, say, out, read_line):
    """Invite photo + confirmation 'imprimante ok'. True si pret a lancer."""
    photo_ok = False
    confirm_ok = False
    say(dire("imprimante.cam_invite"))
    out(dire("imprimante.cam_invite") + "\n")

    def ask_ok():
        nonlocal confirm_ok
        say(dire("imprimante.cam_demande_ok"))
        out(dire("imprimante.cam_demande_ok") + "\n" + "? ")
        typed = matcher.normalize(read_line(key_source))
        ok_words = {
            "imprimante ok", "ok", "imprimante okay", "c est bon", "cest bon",
            "pret", "prete", "valide", "oui", "caisson ok", "plateau ok",
        }
        if typed in ok_words or typed.endswith(" ok") or "imprimante ok" in typed or typed.startswith("ok "):
            confirm_ok = True
            return dire("imprimante.cam_ok_recu")
        return dire("imprimante.cam_ok_non_reconnu", dit=typed or dire("imprimante.cam_rien"))

    for key in key_source:
        voix.stop()
        if key == ESC:
            return False
        if conseils.is_help(key):
            text = dire("imprimante.cam_aide")
        elif key in ("p", "P") or (key in ENTER and not photo_ok):
            say(dire("imprimante.cam_prise"))
            photo_ok = _photo_controle(say, out)
            if photo_ok:
                text = dire("imprimante.cam_photo_ok_dis")
                out(f"[{text}]" + "\n")
                say(text)
                text = ask_ok()
            else:
                text = dire("imprimante.cam_photo_echec")
        elif key in ("o", "O"):
            text = ask_ok()
        elif key in ENTER:
            if photo_ok and confirm_ok:
                return True
            if not photo_ok:
                text = dire("imprimante.cam_rappel_photo")
            else:
                text = dire("imprimante.cam_rappel_ok")
                out(f"[{text}]" + "\n")
                say(text)
                text = ask_ok()
                if photo_ok and confirm_ok:
                    out(f"[{text}]" + "\n")
                    say(text)
                    return True
        else:
            if not photo_ok:
                text = dire("imprimante.cam_rappel_photo")
            elif not confirm_ok:
                text = dire("imprimante.cam_rappel_ok")
            else:
                text = dire("imprimante.cam_tout_ok")
        out(f"[{text}]" + "\n")
        say(text)
    return False



def _vider_tampon_clavier():
    """Jette les touches enfoncees pendant un travail bloquant (lancement).

    Sans ca, un Echap tape faute d'echo clavier est lu juste apres le retour
    du wizard et ferme l'explorateur -- sans toucher a l'imprimante, mais
    ca donne l'impression d'avoir "coupe" quelque chose au demarrage.
    """
    import select
    fd = sys.stdin.fileno()
    while True:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            break
        try:
            os.read(fd, 1024)
        except OSError:
            break

def _attendre_chauffe(say, buse_c, bed_c, timeout=300):
    """Attend que buse et plateau approchent la cible. Rend True/False."""
    say(dire("imprimante.lancement_chauffe_attente", buse=buse_c, plateau=bed_c))
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            e = op.etat()
        except op.Erreur:
            time.sleep(2)
            continue
        if not e.get("connecte"):
            return False
        ba = (e.get("buse") or [0, 0])[0] or 0
        pa = (e.get("plateau") or [0, 0])[0] or 0
        if ba >= buse_c - 5 and pa >= bed_c - 5:
            say(dire("imprimante.lancement_chauffe_ok", buse=round(ba), plateau=round(pa)))
            return True
        time.sleep(3)
    return False


def _lancer_sequence(item, params, say, out, confirmer_filament=None):
    """Origine, chauffe, test filament, start print (si gcode)."""
    def announce(msg):
        out(msg + "\n")
        say(msg)

    try:
        e = op.etat()
    except op.Erreur as exc:
        announce(dire("imprimante.pas_connectee") if getattr(exc, "code", "") != "cle" else dire("imprimante.erreur.cle"))
        return False
    if not e.get("connecte"):
        announce(dire("imprimante.pas_connectee"))
        return False

    gcodes = pieces.gcodes_pret(item) if item else []
    announce(dire("imprimante.lancement_origine"))
    try:
        machine = op.detecter_machine(force=True)
        # crash_suspect retire : z_min triggered = sonde, pas buse
        if machine.get("z_probe") and not machine.get("autolevel"):
            announce(dire("imprimante.origine_mode_probe"))
        ok, detail, machine = op.homing_sur("xyz", timeout=180)
        if not ok:
            if detail == "probe_hors_plateau":
                announce(dire("imprimante.origine_probe_hors_plateau"))
            else:
                announce(dire("imprimante.origine_echec", axes=dire("imprimante.axes_tous"), detail=str(detail)))
            return False
    except op.Erreur as exc:
        announce(dire("imprimante.origine_echec", axes=dire("imprimante.axes_tous"), detail=str(exc.detail or exc.code)))
        return False
    announce(dire("imprimante.lancement_origine_ok"))

    announce(dire("imprimante.lancement_chauffe", buse=params["buse"], plateau=params["plateau"],
                  materiau=params["nom_materiau"]))
    try:
        op.cible_buse(params["buse"])
        op.cible_plateau(params["plateau"])
    except op.Erreur as exc:
        announce(dire("imprimante.erreur.injoignable"))
        return False
    if not _attendre_chauffe(say, params["buse"], params["plateau"]):
        announce(dire("imprimante.lancement_chauffe_timeout"))
        return False

    if params.get("test_filament"):
        announce(dire("imprimante.lancement_test_filament"))
        try:
            op.extruder(10)
            time.sleep(2)
            op.extruder(-2)  # leger retrait
        except op.Erreur:
            announce(dire("imprimante.lancement_test_filament_echec"))
            if confirmer_filament is None:
                pass  # ancien comportement si pas de confirmation
            else:
                announce(dire("imprimante.lancement_test_filament_refuse"))
                return False
        else:
            announce(dire("imprimante.lancement_test_filament_ok"))
            if confirmer_filament is not None:
                if not confirmer_filament():
                    announce(dire("imprimante.lancement_test_filament_refuse"))
                    return False

    if not gcodes:
        announce(dire("imprimante.lancement_sans_gcode"))
        # Sauve les params sur la fiche pret
        return True

    f = gcodes[0]
    announce(dire("imprimante.lancement_envoi", nom=f["nom"]))
    try:
        info = op.televerser(
            f["chemin"],
            dossier=f"pieces/{item['source']}_{item['id']}",
            selectionner=False,
        )
        # Demarre l'impression
        fichier = {
            "origine": info.get("origine") or "local",
            "chemin": info.get("chemin") or f["nom"],
        }
        # Accessibilite : pas de dialogue ecran/bouton runout pendant l'impression
        try:
            op.desactiver_runout(persist=True)
        except op.Erreur as exc:
            print(f"[imprimante_print] runout off : {exc!r}", file=sys.stderr)
        op.imprimer(fichier)
    except Exception as exc:
        print(f"[imprimante_print] start : {exc!r}", file=sys.stderr)
        announce(dire("imprimante.lancement_echec_envoi"))
        return False
    announce(dire("imprimante.lancement_ok", nom=f["nom"]))
    return True


def _save_job_on_fiche(item, params):
    fiche = pieces.load_fiche(item["source"], item["id"]) or dict(item.get("fiche") or {})
    fiche = dict(fiche)
    prep = dict(fiche.get("impression_preparee") or {})
    prep["job"] = params
    prep["job_quand"] = time.time()
    fiche["impression_preparee"] = prep
    fiche["id"] = item["id"]
    fiche["source"] = item["source"]
    if not fiche.get("title"):
        fiche["title"] = item.get("title")
    pieces.save_fiche(fiche)



def filament_pret():
    """Reprise / contournement sans bouton physique (M108 + runout off)."""
    fil = op.etat_filament_serial()
    phase = fil.get("phase")
    code, preset = mat.materiau_courant()

    if phase == "action" or fil.get("processing"):
        return dire("imprimante.filament_reprise_action")

    if phase == "ok" and not fil.get("erreur"):
        try:
            op.desactiver_runout(persist=False)
        except op.Erreur:
            pass
        try:
            e = op.etat()
        except op.Erreur as exc:
            return dire("imprimante.pas_connectee") if getattr(exc, "code", "") != "cle" else dire("imprimante.erreur.cle")
        if e.get("connecte") and (e.get("flags") or {}).get("printing"):
            return dire("imprimante.filament_pret_ok")
        if e.get("connecte"):
            return dire("imprimante.filament_pret_pas_en_pause")
        return dire("imprimante.pas_connectee")

    try:
        result = op.avance_dialogue_filament(max_etapes=6, timeout_etape=45)
    except op.Erreur as exc:
        return dire(
            "imprimante.filament_reprise_echec",
            detail=str(exc.detail or exc.code),
        )

    time.sleep(1.0)
    try:
        op.desactiver_runout(persist=True)
    except op.Erreur:
        pass
    fil2 = op.etat_filament_serial()
    if result.get("ok") or fil2.get("phase") == "ok":
        try:
            e = op.etat()
            if e.get("connecte"):
                op.cible_buse(preset["buse"])
                op.cible_plateau(preset["plateau"])
                flags = e.get("flags") or {}
                if flags.get("paused") or flags.get("pausing"):
                    op.travail("resume")
        except op.Erreur:
            pass
        return dire("imprimante.filament_pret_ok")
    if fil2.get("phase") == "action":
        return dire("imprimante.filament_reprise_action")
    return dire("imprimante.filament_boucle_capteur")


def wizard(item, key_source, say, out, read_line):
    """Flux complet : questions -> camera -> lancement. Rend phrase finale."""
    code, preset = mat.materiau_courant()
    say(dire("imprimante.print_debut", titre=item.get("title") or "?", materiau=preset["nom"]))
    out(dire("imprimante.print_debut", titre=item.get("title") or "?", materiau=preset["nom"]) + "\n")

    settings = _questionnaire(key_source, say, out)
    if settings is None:
        return dire("imprimante.print_annule")

    params = mat.job_params_from_settings()
    _save_job_on_fiche(item, params)
    resume = dire(
        "imprimante.print_resume",
        materiau=params["nom_materiau"],
        couche=dire(f"imprimante.param_couche_{params['couche']}"),
        remplissage=dire(f"imprimante.param_remplissage_{params['remplissage']}"),
        supports=dire("imprimante.param_supports_oui" if params["supports"] else "imprimante.param_supports_non"),
        adherence=dire(f"imprimante.param_adherence_{params['adherence']}"),
    )
    say(resume)
    out(resume + "\n")

    # Tranchage automatique STL -> G-code
    gcodes = pieces.gcodes_pret(item)
    if not gcodes:
        if not tranche.disponible():
            msg = dire("imprimante.tranche_absent")
            say(msg)
            out(msg + "\n")
            return msg
        say(dire("imprimante.tranche_en_cours"))
        out(dire("imprimante.tranche_en_cours") + "\n")
        gpath, err = tranche.trancher(item, params)
        if err or not gpath:
            msg = dire("imprimante.tranche_echec")
            say(msg)
            out(msg + "\n")
            return msg
        nom = os.path.basename(gpath)
        item = dict(item)
        fichiers = list(item.get("fichiers") or [])
        fichiers.append({"nom": nom, "octets": os.path.getsize(gpath), "chemin": gpath})
        item["fichiers"] = fichiers
        say(dire("imprimante.tranche_ok", nom=nom))
        out(dire("imprimante.tranche_ok", nom=nom) + "\n")
    else:
        say(dire("imprimante.tranche_deja", nom=gcodes[0]["nom"]))
        out(dire("imprimante.tranche_deja", nom=gcodes[0]["nom"]) + "\n")

    if not _gate_camera(key_source, say, out, read_line):
        return dire("imprimante.print_annule")

    say(dire("imprimante.lancement_go"))
    out(dire("imprimante.lancement_go") + "\n")
    say(dire("imprimante.lancement_clavier_occupe"))
    out(dire("imprimante.lancement_clavier_occupe") + "\n")

    def confirmer_filament():
        _vider_tampon_clavier()
        q = dire("imprimante.lancement_test_filament_demande")
        say(q)
        out(q + "\n? ")
        typed = matcher.normalize(read_line(key_source))
        oui = typed in {"oui", "o", "ok", "yes", "y", "si", "sì"} or typed.startswith("oui")
        return oui

    # Sync : le clavier doit pouvoir repondre au test filament (oui/non).
    result_ok = _lancer_sequence(item, params, say, out, confirmer_filament)
    _vider_tampon_clavier()
    if result_ok:
        gcodes = pieces.gcodes_pret(item)
        if gcodes:
            return dire("imprimante.print_termine_lance")
        return dire("imprimante.print_termine_prep")
    return dire("imprimante.print_echec")
