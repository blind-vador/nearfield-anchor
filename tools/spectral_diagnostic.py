"""Mode diagnostic spectral generique, activable a la demande (2026-08-31,
demande explicite de l'utilisateur suite a l'investigation du sifflement
sur la bande aviation civile VHF).

But : identifier ou naissent des chevauchements de porteuses (deux
emetteurs proches en frequence causant un battement/sifflement) sur
N'IMPORTE QUELLE bande deja definie dans radio_watch.BANDS -- pas
seulement l'aviation. Ce genre de "sac de noeuds" cree des faux positifs
et fait perdre de l'efficacite au scanner ; il y en aura probablement
d'autres a demeler plus tard sur d'autres bandes.

Point cle par rapport aux tentatives manuelles precedentes (qui ont
toutes rate la fenetre de tir) : ce script reutilise le MEME mecanisme
de detection que la production (sweep_band_peak_relative) puis affine
IMMEDIATEMENT sur le candidat trouve, dans le meme processus, sans aucun
aller-retour reseau/script entre les deux etapes. C'est ce delai qui
faisait systematiquement rater les transmissions aviation (trop
sporadiques). Le script reessaie plusieurs fois automatiquement car le
trafic reste sporadique meme comme ca.

Usage (le dongle doit etre libre -- arreter radio-watch d'abord) :
    sudo systemctl stop radio-watch
    python3 spectral_diagnostic.py aviation
    python3 spectral_diagnostic.py vhf_business --attempts 30 --fine-step-hz 250
    sudo systemctl start radio-watch

Resultats persistes dans config.sound_paths.DIAGNOSTIC_DIR (NAS) : un
CSV brut (freq_hz, db) + un resume texte par capture reussie, nommes par
bande + horodatage lisible NVDA (meme convention que radio_log.py).
S'accumulent au fil des sessions -- pas besoin d'un seul essai chanceux.
"""

import argparse
import csv
import datetime
import os
import statistics
import subprocess
import sys
import tempfile

# rtl_power laisse parfois un descendant qui garde un pipe stdout/stderr
# ouvert meme apres que subprocess.run() ait vu le process principal
# se terminer (constate en session : le meme phenomene bloquait deja le
# dongle avec un rtl_power "orphelin" pendant l'investigation du
# sifflement aviation). Consequence concrete ici : le script termine
# tous ses essais, imprime le resume, puis reste bloque indefiniment en
# do_sys_poll au lieu de rendre la main -- inacceptable pour un outil
# "activable a la demande" cense rendre le dongle a radio-watch juste
# apres. os._exit() court-circuite l'arret normal de l'interpreteur
# (qui attendrait ce pipe) ; tout ce qu'on doit vraiment persister (CSV
# + resume par capture) est deja ferme via "with open(...)" avant d'en
# arriver la, donc rien n'est perdu.

sys.path.insert(0, "/home/vador/autopilot/pi_console")

import radio_watch as rw
from radio_log import _readable_timestamp
from sound_paths import DIAGNOSTIC_DIR


def _fine_sweep(freq_hz, half_span_hz=60_000, step_hz=500, integration_sec=1):
    """Balayage fin centre sur freq_hz. Retourne une liste de (freq, db)
    ou None en cas d'echec."""
    low = int(freq_hz - half_span_hz)
    high = int(freq_hz + half_span_hz)
    fd, tmp_path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        result = subprocess.run(
            ["rtl_power", "-f", f"{low}:{high}:{step_hz}", "-i", str(integration_sec), "-1", tmp_path],
            capture_output=True, text=True, errors="replace", timeout=integration_sec + 15,
        )
        if result.returncode != 0:
            return None
        with open(tmp_path, newline="") as f:
            row = next(csv.reader(f))
        row_low = float(row[2])
        row_step = float(row[4])
        bins = []
        for i, v in enumerate(row[6:]):
            v = v.strip()
            if not v or v.lower() == "nan":
                continue
            bins.append((row_low + i * row_step, float(v)))
        return bins if bins else None
    except Exception:
        return None
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _find_distinct_carriers(bins, min_separation_hz=5000, margin_db=3.0):
    """Detecte les pics locaux distincts au-dessus du plancher (mediane)
    + margin_db, fusionnes s'ils sont trop proches (< min_separation_hz)
    -- des pics separes indiquent des porteuses reellement distinctes,
    pas juste un lobe elargi d'une seule emission.

    margin_db par defaut volontairement prudent : verifie sur une vraie
    capture (2026-08-31) que l'ecart-type du plancher a 1s d'integration/
    500Hz de pas est ~0.9dB -- un seuil a 3dB (~3.3 sigma sur 250+ bins)
    laisse passer plusieurs faux positifs par pur bruit statistique.
    run_diagnostic() passe la marge calibree par bande (meme valeur que
    la detection de production, deja validee par le nivelage) plutot que
    ce defaut generique."""
    vals = [v for _, v in bins]
    floor = statistics.median(vals)
    peaks = []
    for i in range(1, len(bins) - 1):
        f, v = bins[i]
        if v > floor + margin_db and v >= bins[i - 1][1] and v >= bins[i + 1][1]:
            peaks.append((f, v))
    peaks.sort(key=lambda p: -p[1])
    distinct = []
    for f, v in peaks:
        if not any(abs(f - pf) < min_separation_hz for pf, _ in distinct):
            distinct.append((f, v))
    return distinct, floor


def _safe_name(text):
    return "".join(c if c.isalnum() else "_" for c in text).strip("_")


def run_diagnostic(band, attempts=15, half_span_hz=60_000, fine_step_hz=500):
    os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)
    found_count = 0
    for attempt in range(1, attempts + 1):
        freq, db = rw.sweep_band_peak_relative(
            band["low_hz"], band["high_hz"], band["step_hz"], margin_db=band.get("margin_db"),
        )
        if freq is None:
            print(f"[spectral_diagnostic] essai {attempt}/{attempts} : rien")
            continue

        print(f"[spectral_diagnostic] essai {attempt}/{attempts} : candidat {freq / 1e6:.6f} MHz "
              f"({db:.1f} dB) -- affinage immediat")
        bins = _fine_sweep(freq, half_span_hz=half_span_hz, step_hz=fine_step_hz)
        if not bins:
            print("[spectral_diagnostic] echec du balayage fin (transmission probablement deja terminee)")
            continue

        carrier_margin_db = band.get("margin_db", 6.0)
        distinct, floor = _find_distinct_carriers(bins, margin_db=carrier_margin_db)
        timestamp = _readable_timestamp(datetime.datetime.now())
        base_name = f"diag_{_safe_name(band['name'])}_{timestamp}"

        csv_path = os.path.join(DIAGNOSTIC_DIR, base_name + ".csv")
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["freq_hz", "db"])
            writer.writerows(bins)

        summary_path = os.path.join(DIAGNOSTIC_DIR, base_name + "_resume.txt")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"Bande : {band['name']} ({band.get('name_spoken', band['name'])})\n")
            f.write(f"Candidat detecte : {freq / 1e6:.6f} MHz ({db:.1f} dB)\n")
            f.write(f"Plage affinee : {(freq - half_span_hz) / 1e6:.6f} - "
                    f"{(freq + half_span_hz) / 1e6:.6f} MHz, pas {fine_step_hz} Hz\n")
            f.write(f"Plancher (mediane) : {floor:.2f} dB\n")
            f.write(f"Marge de detection porteuse : {carrier_margin_db:.1f} dB (calibree par bande)\n")
            f.write(f"Porteuses distinctes trouvees : {len(distinct)}\n")
            for fr, v in distinct:
                f.write(f"  {fr / 1e6:.6f} MHz : {v:.2f} dB (+{v - floor:.1f} au-dessus du plancher)\n")
            if len(distinct) >= 2:
                ecarts = [abs(distinct[i][0] - distinct[i + 1][0]) for i in range(len(distinct) - 1)]
                f.write(f"Ecart(s) entre porteuses consecutives : {', '.join(f'{e:.0f} Hz' for e in ecarts)}\n")
                f.write(">>> PLUSIEURS PORTEUSES DISTINCTES DETECTEES -- chevauchement possible <<<\n")

        print(f"[spectral_diagnostic] capture enregistree : {base_name} "
              f"({len(distinct)} porteuse(s) distincte(s))")
        found_count += 1

    print(f"[spectral_diagnostic] termine : {found_count}/{attempts} capture(s) reussie(s)")
    return found_count


def main():
    parser = argparse.ArgumentParser(
        description="Mode diagnostic spectral generique -- identifie les chevauchements de "
                    "porteuses sur une bande donnee (dongle doit etre libre)."
    )
    parser.add_argument("band", help="Nom de la bande (voir radio_watch.BANDS, ex: aviation, milair, vhf_business...)")
    parser.add_argument("--attempts", type=int, default=15, help="Nombre d'essais de detection avant d'abandonner")
    parser.add_argument("--half-span-hz", type=int, default=60_000, help="Demi-largeur du balayage fin autour du candidat detecte")
    parser.add_argument("--fine-step-hz", type=int, default=500, help="Resolution du balayage fin")
    args = parser.parse_args()

    band = next((b for b in rw.BANDS if b["name"] == args.band), None)
    if band is None:
        noms = [b["name"] for b in rw.BANDS]
        print(f"[spectral_diagnostic] bande inconnue : {args.band!r}. Bandes disponibles : {noms}")
        sys.exit(1)

    run_diagnostic(band, attempts=args.attempts, half_span_hz=args.half_span_hz, fine_step_hz=args.fine_step_hz)

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
