"""Le banc de demodulation quand aucune compagne n'est reliee (2026-11-05).

Meme code qu'au Mac (bridge/banc_demod), meme resultat : un son par modulation,
la preuve mesuree sur l'IQ, et le discriminateur 48 kHz qui part aux tickets, ou
le Pi passe alors lui-meme les decodeurs numeriques. Sans ca, la premiere
modulation qui ouvrait arretait les autres et le reste etait perdu."""
import os
import subprocess
import tempfile
import time

TAUX = 256_000
BLOC = 65_536
SILENCE_FIN_SEC = 3.0


def ecouter(freq_hz, gain, sonde_sec, max_sec):
    """Rend le meme dict que la compagne, ou {"ok": False, "erreur": ...}. Le
    dossier a effacer est dans "_tmp"."""
    import banc_demod
    tmp = tempfile.mkdtemp(prefix="numerique_pi_")
    try:
        proc = subprocess.Popen(["rtl_sdr", "-f", str(int(freq_hz + TAUX // 4)), "-s", str(TAUX), "-g", str(gain), "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return {"ok": False, "erreur": f"rtl_sdr : {exc!r}", "_tmp": tmp}
    banc = banc_demod.Banc(TAUX, -(TAUX // 4))
    debut = dernier = time.time()
    try:
        while True:
            octets = proc.stdout.read(BLOC)
            if not octets:
                break
            banc.ajouter(octets)
            maintenant = time.time()
            if banc.actif:
                dernier = maintenant
            if maintenant - debut >= max_sec:
                break
            if maintenant - debut >= sonde_sec and maintenant - dernier >= SILENCE_FIN_SEC:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    sons = banc.ecrire(tmp)
    if not sons:
        return {"ok": False, "erreur": "aucun IQ recu du dongle", "_tmp": tmp}
    mods, raison, mesures, tranches = banc.preuve()
    flux = os.path.join(tmp, "flux.wav")
    return {"ok": True, "machine": "le Pi", "banc": sons, "flux_wav": flux if banc.ecrire_flux(flux) else None,
            "banc_preuve": {"modulations": mods, "raison": raison, "mesures": mesures, "tranches": tranches,
                            "emission": banc.emission},
            "duree": round(time.time() - debut, 1), "_tmp": tmp}
