"""Commande "antenne" (2026-10-19) : mesure de ce que recoit la cle radio
(lib/mesure_antenne.py). "antenne" : le verdict (antenne branchee ou non,
stations FM, bruit de fond par rapport a d'habitude, bandes au-dessus du
seuil en ce moment) ; "antenne detail" : chaque bande, bruit de fond, pic et
ecart au seuil de detection du scanner. Un detail demande juste apres une
mesure reprend cette mesure.

La mesure se fait en arriere-plan (2026-10-20) : elle bloquait le clavier
une a deux minutes, les touches tapees entre-temps etaient rejouees a
l'arrivee du verdict et leur echo le coupait aussitot -- verdict affiche,
jamais entendu. Le clavier reste libre, annoncer() dit le verdict.

Audiographes (2026-10-22, demande utilisateur) : apres le verdict de
"antenne", la mesure est rendue en son (lib/audiographe.py) -- deux sons de
reference, le balayage FM puis UHF, un carillon par trame de capteur
entendue. "antenne son" rejoue l'audiographe de la derniere mesure. Le
verdict s'appuie d'abord sur l'ecoute des capteurs en direct : la FM entre
meme par le cable USB, elle ne prouve rien (coax du 22/10).

Carte entretenue en continu (2026-11-14, lib/carte_antenne.py) : chaque passe du
scanner tient a jour la carte de toutes les bandes.
  "antenne son"     l'audiographe ACTUALISE : toute la carte, bande par bande,
                    du bas au haut du spectre (une touche l'arrete) ;
  "antenne direct"  le scanner repasse une bande en boucle et chaque passe est
                    rendue en son : balayage, force (aigu = fort), carillon quand
                    l'antenne fait mieux que tout ce qu'on a eu (centrage), ton
                    grave quand elle perd. Gauche / droite : la bande ; espace :
                    les chiffres ; F1 : les touches ; Echap : fin.
"""
import os
import subprocess
import sys
import threading
import time


import audiographe  # noqa: E402
import audio_lock  # noqa: E402
import carte_antenne  # noqa: E402
import matcher  # noqa: E402
import mesure_antenne  # noqa: E402
import voix  # noqa: E402
from phrases import dire, nombre  # noqa: E402

ATTENTE_SEC = 420
REPRISE_SEC = 600   # "antenne detail" juste apres : meme mesure
MOTS_DETAIL = {"detail", "details", "bandes", "bande"}
MOTS_SON = {"son", "sons", "audiographe", "audiographes", "ecouter", "ecoute"}
MOTS_DIRECT = {"direct", "directe", "live", "orienter", "orientation", "suivi"}
CARTE_BALAYAGE_SEC = 2.5    # chaque bande de la carte : un balayage court
DIRECT_DEFAUT = "uhf_400_470"   # les capteurs 433 MHz : la meilleure preuve d'antenne
DIRECT_MIEUX_DB = 1.0       # carillon : au moins 1 dB de force de plus que le meilleur de la seance
DIRECT_MOINS_DB = 3.0       # ton grave : 3 dB de moins que le meilleur
DIRECT_MUET_SEC = 20        # rien du scanner depuis : on le dit
LEFT, RIGHT, ESC = "\x1b[D", "\x1b[C", "\x1b"


def _fr(x, decimales=1):
    s = f"{x:.{decimales}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _scanner_actif():
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "radio-watch"], timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _mesurer():
    """(resultat, None) ou (None, phrase du probleme)."""
    if _scanner_actif():
        asked = mesure_antenne.request()
        res = mesure_antenne.wait(asked, ATTENTE_SEC)
        if res is None:
            return None, dire("antenne.scanner_muet")
        return res, None
    import radio_watch
    import chaine_audio
    return mesure_antenne.measure(radio_watch.BANDS,
                                  lambda n: chaine_audio.settings(n)["marge_detection_db"]), None


def _trop_chaud():
    """Scanner en pause thermique au-dessus de la limite de la veille meteo : rien ne passera."""
    if not _scanner_actif():
        return False
    import meteo_locale
    return (meteo_locale._read_json(meteo_locale.SCANNER_STATE_PATH) or {}).get("etat") == "pause_thermique"


def _ligne_bande(b):
    if "erreur" in b:
        return dire("antenne.bande_echec", bande=b["dit"])
    cle = "antenne.bande_dessus" if b["ecart"] > 0 else "antenne.bande_dessous"
    return dire(cle, bande=b["dit"], plancher=_fr(b["plancher"]), pic=_fr(b["pic_mhz"], 3),
                ecart=_fr(abs(b["ecart"])))


def _resume(res):
    v = res["verdict"]
    fm = next((b for b in res["bandes"] if b["nom"] == mesure_antenne.FM_BAND), {})
    stations = fm.get("stations") or []
    parts = []
    dernier = mesure_antenne.dernier_capteur()
    silence_h = (time.time() - dernier) / 3600 if dernier else None
    trames = v.get("trames")
    if trames:
        # des capteurs entendus en direct : la vraie preuve de reception UHF
        parts.append(dire("antenne.capteurs_entendus", trames=nombre(trames, "trame"),
                          capteurs=nombre(v.get("capteurs") or 0, "capteur"),
                          secondes=mesure_antenne.ECOUTE_CAPTEURS_SEC))
    elif trames == 0 and v["etat"] in ("bon", "faible"):
        parts.append(dire("antenne.capteurs_muets", stations=nombre(len(stations), "station"),
                          secondes=mesure_antenne.ECOUTE_CAPTEURS_SEC))
    elif v["etat"] in ("bon", "faible") and _scanner_actif() and (silence_h is None or silence_h >= mesure_antenne.CAPTEURS_SILENCE_H):
        # la FM passe, mais rien n'est decode au-dessus : antenne mal vissee
        parts.append(dire("antenne.suspecte", stations=nombre(len(stations), "station"),
                          depuis=(dire("antenne.depuis_heures", heures=nombre(round(silence_h), "heure"))
                                  if silence_h is not None and silence_h < 72 else dire("antenne.depuis_longtemps"))))
    elif v["etat"] == "bon":
        f, db = stations[0]
        parts.append(dire("antenne.bon", stations=nombre(len(stations), "station"), frequence=_fr(f), ecart=_fr(db, 0)))
    elif v["etat"] == "faible":
        f, db = stations[0]
        parts.append(dire("antenne.faible", stations=nombre(len(stations), "station"), frequence=_fr(f), ecart=_fr(db, 0)))
    elif v["etat"] == "rien":
        parts.append(dire("antenne.rien"))
    else:
        parts.append(dire("antenne.fm_inconnue"))
    d = v["derive_plancher"]
    if parts[0].startswith(dire("antenne.suspecte_debut")):
        d = None  # reference prise peut-etre antenne deja mal branchee : on ne la cite pas
    if not v["mesures_avant"]:
        parts.append(dire("antenne.premiere"))
    elif d is not None and d <= -mesure_antenne.DERIVE_DB:
        parts.append(dire("antenne.plancher_bas", ecart=_fr(-d)))
    elif d is not None and d >= mesure_antenne.DERIVE_DB:
        parts.append(dire("antenne.plancher_haut", ecart=_fr(d)))
    elif d is not None:
        parts.append(dire("antenne.plancher_normal"))
    dessus = sorted((b for b in res["bandes"] if b.get("ecart", -1) > 0 and b["nom"] != mesure_antenne.FM_BAND),
                    key=lambda b: -b["ecart"])
    if dessus:
        parts.append(dire("antenne.au_dessus", nombre=nombre(len(dessus), "bande"), bandes=", ".join(
            f"{b['dit']} {dire('antenne.plus_db', ecart=_fr(b['ecart']))}" for b in dessus[:3])))
    else:
        parts.append(dire("antenne.rien_au_dessus"))
    echecs = [b["dit"] for b in res["bandes"] if "erreur" in b]
    if echecs:
        parts.append(dire("antenne.echecs", bandes=", ".join(echecs)))
    parts.append(dire("antenne.detail_possible"))
    return " ".join(parts)


def _texte(res, detail, out):
    if res.get("erreur"):
        return dire(f"antenne.{res['erreur']}")
    lignes = [_ligne_bande(b) for b in res["bandes"]]
    out("\n".join(lignes) + "\n")
    if detail:
        return " ".join(lignes)
    return _resume(res)


def _audiographe(res):
    """La mesure rendue en son, presentee par la voix de la console. Les sons
    sont prepares avant de prendre le verrou audio ; une touche coupe la
    voix, pas les sons (quelques secondes chacun)."""
    courbes = [c for c in res.get("courbes") or [] if c.get("db")]
    trames = res.get("capteurs")
    if not courbes and trames is None:
        return False
    etapes = [(dire("audiographe.intro"), None),
              (dire("audiographe.bruit"), audiographe.wav_bruit()),
              (dire("audiographe.seuil"), audiographe.wav_seuil())]
    for c in courbes:
        etapes.append((dire("audiographe.bande", bande=dire(f"audiographe.nom_{c['nom']}"),
                            debut=_fr(c["f0"] / 1e6, 0), fin=_fr(c["f1"] / 1e6, 0),
                            graduation=_fr(c["graduation"], 0), pics=nombre(audiographe.pics(c), "pic")),
                       audiographe.wav_balayage(c, c["graduation"])))
    if trames:
        etapes.append((dire("audiographe.capteurs", trames=nombre(len(trames), "trame"),
                            capteurs=nombre(len({(t["modele"], t["id"]) for t in trames}), "capteur")),
                       audiographe.wav_carillons(len(trames))))
    elif trames is not None:
        etapes.append((dire("audiographe.aucun_capteur"), None))
    etapes.append((dire("audiographe.fin"), None))
    ticket = audio_lock.acquire("console")
    try:
        for phrase, son in etapes:
            voix.say_blocking(phrase)
            if son:
                audiographe.jouer(son)
    finally:
        audio_lock.release(ticket)
        for _, son in etapes:
            if son and os.path.exists(son):
                os.remove(son)
    return True


def _age(t):
    minutes = max(0, round((time.time() - t) / 60))
    if minutes < 90:
        return nombre(minutes, "minute")
    return nombre(round(minutes / 60), "heure")


def _carte_phrase(e):
    phrase = dire("audiographe.carte_bande", bande=e["dit"], age=_age(e["t"]), pics=nombre(e.get("pics", 0), "pic"))
    d = carte_antenne.ecart_habituel(e)
    if d is not None and abs(d) >= mesure_antenne.DERIVE_DB:
        phrase += " " + dire("audiographe.carte_plancher_haut" if d > 0 else "audiographe.carte_plancher_bas",
                             ecart=_fr(abs(d)))
    return phrase


def _audiographe_carte():
    """L'audiographe actualise : toute la carte entretenue par le scanner, du bas au
    haut du spectre. Une touche (voix.stop) l'arrete a la bande suivante."""
    segs = carte_antenne.lire()
    if not segs:
        voix.say(dire("audiographe.carte_vide"))
        return False
    plus_vieux = min(e["t"] for e in segs)
    ticket = audio_lock.acquire("console")
    try:
        voix.say_blocking(dire("audiographe.carte_intro", bandes=nombre(len(segs), "bande"), age=_age(plus_vieux)))
        geste = voix._generation
        for phrase, son in ((dire("audiographe.bruit"), audiographe.wav_bruit()),
                            (dire("audiographe.seuil"), audiographe.wav_seuil())):
            voix.say_blocking(phrase)
            geste = voix._generation
            audiographe.jouer(son)
        for e in segs:
            if voix._generation != geste:
                return False        # une touche : on s'arrete la
            son = audiographe.wav_balayage(carte_antenne.en_courbe(e), carte_antenne.graduation(e), CARTE_BALAYAGE_SEC)
            voix.say_blocking(_carte_phrase(e))
            geste = voix._generation
            audiographe.jouer(son)
        voix.say_blocking(dire("audiographe.fin"))
    finally:
        audio_lock.release(ticket)
    return True


def _chiffres(e, meilleure):
    if not e:
        return dire("antenne_direct.pas_encore")
    return dire("antenne_direct.chiffres", force=_fr(e.get("force", 0)), meilleure=_fr(meilleure or 0),
                pics=nombre(e.get("pics", 0), "pic"), pic=_fr(e.get("pic_mhz", 0), 3), plancher=_fr(e.get("plancher", 0)))


def direct(key_source, say, out):
    """Antenne en direct : la bande choisie est repassee en boucle par le scanner
    (ou par la console s'il est arrete), chaque passe rendue en son."""
    import conseils
    import keys
    segs = carte_antenne.segments_direct()
    if not segs:
        return dire("antenne_direct.aucune_bande")
    if _trop_chaud():
        return dire("antenne.trop_chaud")
    i = next((k for k, sg in enumerate(segs) if sg["nom"] == DIRECT_DEFAUT), 0)
    scanner = _scanner_actif()
    etat = {"nom": segs[i]["nom"], "meilleure": None, "dernier": None, "seq": None, "vu": time.time(),
            "muet_dit": False}
    fin = threading.Event()

    def nouvelle_bande():
        etat.update(meilleure=None, dernier=None, vu=time.time(), muet_dit=False)
        carte_antenne.demander_direct(etat["nom"]) if scanner else None

    def suivre():
        demande = 0.0
        while not fin.is_set():
            nom = etat["nom"]
            if scanner:
                if time.time() - demande >= 2:
                    carte_antenne.demander_direct(nom)
                    demande = time.time()
                res = carte_antenne.lire_direct()
                if not res or res.get("seq") == etat["seq"] or res.get("nom") != nom:
                    if time.time() - etat["vu"] > DIRECT_MUET_SEC and not etat["muet_dit"]:
                        etat["muet_dit"] = True
                        say(dire("antenne_direct.scanner_muet"))
                    time.sleep(0.2)
                    continue
                etat["seq"] = res.get("seq")
            else:
                res = carte_antenne.mesure_directe(nom, segs)
            if fin.is_set() or nom != etat["nom"]:
                continue
            etat["vu"] = time.time()
            if res.get("erreur"):
                if res["erreur"] in ("cle_absente", "cle_occupee") and not etat["muet_dit"]:
                    etat["muet_dit"] = True
                    say(dire(f"antenne.{res['erreur']}"))
                time.sleep(1)
                continue
            f, meilleure = res.get("force", 0.0), etat["meilleure"]
            mieux = meilleure is not None and f >= meilleure + DIRECT_MIEUX_DB
            moins = meilleure is not None and f <= meilleure - DIRECT_MOINS_DB
            etat["meilleure"] = f if meilleure is None else max(meilleure, f)
            etat["dernier"] = res
            out(f"{res['dit']} : force {f} dB, pics {res.get('pics')}, pic {res.get('pic_mhz')} MHz\n")
            audiographe.jouer(audiographe.wav_direct(carte_antenne.en_courbe(res), carte_antenne.graduation(res),
                                                     f, mieux=mieux, moins=moins))

    consigne = dire("antenne_direct.consigne")
    say(f"{conseils.intro('antenne_direct', dire('antenne_direct.titre'), consigne)} {segs[i]['dit']}.")
    nouvelle_bande()
    fil = threading.Thread(target=suivre, daemon=True)
    fil.start()
    try:
        for key in key_source:
            if key == keys.WAKE:
                continue
            if conseils.is_help(key):
                say(consigne)
            elif key == ESC:
                break
            elif key in (LEFT, RIGHT):
                n = i + (1 if key == RIGHT else -1)
                if 0 <= n < len(segs):
                    i = n
                    etat["nom"] = segs[i]["nom"]
                    nouvelle_bande()
                    say(segs[i]["dit"])
                else:
                    say(f"{dire('commun.debut_liste') if n < 0 else dire('commun.fin_liste')} {segs[i]['dit']}")
            elif key == " ":
                say(_chiffres(etat["dernier"], etat["meilleure"]))
            else:
                say(dire("antenne_direct.aide"))
    finally:
        fin.set()
        carte_antenne.arreter_direct()
        fil.join(timeout=5)
    return dire("antenne_direct.fin")


def run(line, out, annoncer, key_source=None, say=None):
    """Rend la phrase a dire tout de suite ; une mesure fraiche est faite en
    arriere-plan et son verdict passe a annoncer(texte), suivi de
    l'audiographe."""
    mots = set(matcher.normalize(line).split())
    detail = bool(MOTS_DETAIL & mots)
    if MOTS_DIRECT & mots and key_source is not None:
        return direct(key_source, say or voix.say, out)
    if MOTS_SON & mots and not detail:
        threading.Thread(target=_audiographe_carte, daemon=True).start()
        return ""
    res = mesure_antenne.derniere() if detail else None
    if res and not res.get("erreur") and time.time() - res.get("t", 0) <= REPRISE_SEC:
        return _texte(res, detail, out)
    if _trop_chaud():
        return dire("antenne.trop_chaud")

    def travail():
        try:
            res, probleme = _mesurer()
            texte = probleme or _texte(res, detail, out)
        except Exception as exc:  # la console ne doit pas rester sans reponse
            print(f"[antenne] {exc!r}", file=sys.stderr)
            res, texte = None, dire("antenne.rtl_power")
        annoncer(texte)
        if res and not res.get("erreur") and not detail:
            try:
                _audiographe(res)
            except Exception as exc:
                print(f"[antenne] audiographe : {exc!r}", file=sys.stderr)
    threading.Thread(target=travail, daemon=True).start()
    return dire("antenne.demande" if _scanner_actif() else "antenne.directe")
