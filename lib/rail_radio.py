"""Presence d'un poste de train sur la bande montante GSM-R.

En Europe le train (ou le portatif d'un agent) emet vers le sol entre
876,2 et 880 MHz. Les relais au bord de la voie emettent plus haut,
921 a 925 MHz, et ils emettent tout le temps : les entendre ne dit pas
qu'un train est la. Le contenu, voix ou donnees, est chiffre. On ne
l'ouvre pas. On dit seulement qu'un canal de cette bande montante
s'est allume apres un silence.
"""
import json
import os
import subprocess
import tempfile

LOW_HZ = 876_200_000
HIGH_HZ = 880_200_000
STEP_HZ = 200_000
# Un poste de train sort nettement du bruit de cette petite bande.
# Au-dela de quelques canaux, ce n'est plus un poste : on ne dit rien.
MARGE_DB = 8.0
MAX_CANAUX = 4


def _chemin():
    import chemins
    return chemins.base("rail_radio.json")


def _charger():
    try:
        with open(_chemin(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _sauver(etat):
    path = _chemin()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(etat, f)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[rail_radio] etat non ecrit : {exc}")


def _canal(freq):
    return int(round(float(freq) / STEP_HZ) * STEP_HZ)


def _plancher(values):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.2))]


def decision(bins, etat):
    """Rend la frequence d'un poste qui vient de s'allumer, ou None.

    La premiere passe ne fait qu'apprendre. Un canal deja allume a la
    passe d'avant n'est pas redit. Trop de canaux a la fois n'est pas
    un poste : on n'annonce pas.
    """
    niveaux = {}
    for freq, db in bins:
        try:
            db = float(db)
        except (TypeError, ValueError):
            continue
        canal = _canal(freq)
        if canal < LOW_HZ or canal > HIGH_HZ:
            continue
        if canal not in niveaux or db > niveaux[canal]:
            niveaux[canal] = db
    if len(niveaux) < 5:
        return None
    floor = _plancher(niveaux.values())
    chauds = sorted(freq for freq, db in niveaux.items() if db >= floor + MARGE_DB)
    if not etat.get("appris"):
        etat["appris"] = True
        etat["chauds"] = chauds
        etat["calme"] = not chauds
        return None
    if len(chauds) > MAX_CANAUX:
        etat["chauds"] = chauds
        etat["calme"] = False
        print(f"[rail_radio] {len(chauds)} canaux a la fois, trop large pour un poste")
        return None
    etait = set(etat.get("chauds") or [])
    etat["chauds"] = chauds
    if not chauds:
        etat["calme"] = True
        return None
    nouveaux = [freq for freq in chauds if freq not in etait]
    calme = bool(etat.get("calme"))
    etat["calme"] = False
    if nouveaux and (calme or not etait):
        return nouveaux[0]
    return None


def _mesurer():
    """Niveaux (freq, dB) de la bande montante, ou None si la cle ne repond pas."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        band = f"{LOW_HZ}:{HIGH_HZ}:{STEP_HZ}"
        result = subprocess.run(
            ["rtl_power", "-f", band, "-i", "2", "-1", path],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        if result.returncode != 0:
            print(f"[rail_radio] rtl_power rc={result.returncode}: {result.stderr.strip()[-160:]}")
            return None
        bins = []
        with open(path, errors="replace") as f:
            for line in f:
                fields = line.strip().split(",")
                if len(fields) < 7:
                    continue
                try:
                    low, step = float(fields[2]), float(fields[4])
                except ValueError:
                    continue
                for i, raw in enumerate(fields[6:]):
                    try:
                        db = float(raw)
                    except ValueError:
                        continue
                    bins.append((low + i * step, db))
        return bins
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[rail_radio] mesure impossible : {exc}")
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _dire():
    from phrases import dire
    import audio_lock
    from speak import speak
    lock = audio_lock.acquire()
    try:
        speak(dire("rail.poste_train"))
    finally:
        audio_lock.release(lock)


def scan_window(dwell_sec=8):
    """Une passe. Rend True seulement quand un poste vient de s'allumer."""
    del dwell_sec  # la passe a sa propre duree (integration de 2 s)
    bins = _mesurer()
    if not bins:
        return False
    etat = _charger()
    freq = decision(bins, etat)
    _sauver(etat)
    if freq is None:
        return False
    print(f"[rail_radio] poste de train sur {freq / 1e6:.1f} MHz (energie seulement)")
    try:
        import veille_donnees as vd
        vd.archive("gsm_r", {"freq_hz": freq, "note": "energie seulement, contenu non ouvert"})
    except Exception as exc:
        print(f"[rail_radio] archive impossible : {exc}")
    try:
        _dire()
    except Exception as exc:
        print(f"[rail_radio] annonce impossible : {exc}")
    return True
