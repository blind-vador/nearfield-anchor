import datetime
import glob
import os
import shutil
import subprocess
import tempfile
import threading
import time

from sound_paths import RADIO_LOG_DIR

# --- Journal audio des captures radio ---
# Ajoute en session (2026-08-30/31, demande explicite de l'utilisateur) :
# chaque capture reelle (PMR446, aviation, bandes sweep, decodage
# numerique) est enregistree en un seul fichier audio agregeant habillage
# sonore + annonce TTS + contenu capte, purge automatique de tout ce qui
# depasse RETENTION_HOURS a minuit. Place sous server/ (2026-08-31, fix) :
# le premier emplacement (/media/nas/nd1/radio_log/, hors de server/)
# etait invisible depuis le partage Samba [nd1], dont le chemin est
# /media/nas/nd1/server -- meme raison que flight_scaner/meteo_scaner/
# radio_scaner/train_scaner, deja tous places ici pour rester accessibles
# depuis Windows. Chemin de base centralise dans sound_paths.py (2026-08-31).
RETENTION_HOURS = 48
JOURNAL_UPDATE_FLAG = "/tmp/journal_maj"


def _touch(path):
    try:
        with open(path, "a"):
            os.utime(path)
    except OSError:
        pass

# Sample rate commun de sortie -- tous les segments (chimes mp3, annonces
# TTS mp3, contenu radio brut a des sample rates varies selon le chemin
# rtl_fm/dsd-fme/RTLSDR-Airband) sont reechantillonnes vers cette valeur
# unique avant assemblage, pour que la concatenation ffmpeg finale n'ait
# pas a gerer de changement de sample rate en cours de fichier.
LOG_SAMPLE_RATE = 22050

# Noms de jours/mois en francais pour des noms de fichiers lisibles par
# NVDA (2026-08-31, demande explicite de l'utilisateur) -- l'ancien format
# YYYYMMDD_HHMMSS ("20260831_091725") est lu chiffre par chiffre ou comme
# un grand nombre illisible par un lecteur d'ecran. Pas de dependance a la
# locale systeme (souvent absente/mal configuree sur ce Pi) -- tables
# explicites.
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_FR = [
    "janvier", "fevrier", "mars", "avril", "mai", "juin",
    "juillet", "aout", "septembre", "octobre", "novembre", "decembre",
]


def _readable_timestamp(dt):
    """Bande/libelle EN PREMIER (fait par l'appelant), puis jour en
    lettres, numero du jour, mois en lettres, annee en chiffres -- ordre
    exact demande par l'utilisateur -- puis l'heure a la fin pour garder
    des noms de fichiers uniques (plusieurs captures possibles le meme
    jour sur la meme bande)."""
    jour_nom = JOURS_FR[dt.weekday()]
    mois_nom = MOIS_FR[dt.month - 1]
    return f"{jour_nom}_{dt.day}_{mois_nom}_{dt.year}_{dt.hour:02d}h{dt.minute:02d}m{dt.second:02d}s"


class RadioLogSession:
    """Une session = un evenement de capture (candidat detecte -> ... ->
    fin). Accumule les segments audio joues (habillage, annonce, contenu
    capte) sous forme de petits fichiers WAV temporaires normalises, DANS
    L'ORDRE ou ils sont joues, puis les concatene en un seul fichier final
    MP3 au moment de finalize(). discard() efface tout sans rien
    sauvegarder (candidat ecarte comme bruit, pas un vrai signal)."""

    def __init__(self, label, exiger_contenu=False):
        self.label = label
        self.segments = []
        # Prises du scanner (2026-11-01) : sans aucun son capte (habillage et
        # annonces seuls), la prise n'est pas gardee -- plus d'entrees vides au
        # journal, meme si un chemin oublie de la jeter.
        self.exiger_contenu = exiger_contenu
        self.contenu = False
        self.contenus = []      # segments de son capte seul (sans habillage) : le .brut.mp3
        self.flux = None
        self._tmpdir = tempfile.mkdtemp(prefix="radio_log_")

    def noter_flux(self, chemin):
        """Flux non décodé, 48 kHz, à garder à côté du journal pour la touche R."""
        if chemin:
            self.flux = chemin

    def add_audio_file(self, path, contenu=False):
        """Convertit un fichier audio existant (chime mp3, annonce TTS
        mp3...) en segment WAV normalise et l'ajoute a la sequence. contenu :
        son capte (voix rendue par une compagne...), pas un habillage."""
        idx = len(self.segments)
        out_path = os.path.join(self._tmpdir, f"{idx:03d}.wav")
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", path, "-ar", str(LOG_SAMPLE_RATE), "-ac", "1", out_path],
                capture_output=True, timeout=60,
            )
            if result.returncode == 0 and os.path.getsize(out_path) > 0:
                self.segments.append(out_path)
                self.contenu = self.contenu or contenu
                if contenu:
                    self.contenus.append(out_path)
        except Exception as exc:
            print(f"[radio_log] segment ignore ({path}) : {exc}")

    def add_raw_pcm(self, raw_bytes, sample_rate, sample_format, channels=1):
        """Convertit un buffer de PCM brut (contenu radio capte, deja
        accumule pendant le relais en direct) en segment WAV normalise.
        sample_format au format ffmpeg ("s16le", "f32le"...)."""
        if not raw_bytes:
            return
        idx = len(self.segments)
        raw_path = os.path.join(self._tmpdir, f"{idx:03d}.raw")
        out_path = os.path.join(self._tmpdir, f"{idx:03d}.wav")
        try:
            with open(raw_path, "wb") as f:
                f.write(raw_bytes)
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", sample_format, "-ar", str(sample_rate), "-ac", str(channels),
                 "-i", raw_path, "-ar", str(LOG_SAMPLE_RATE), "-ac", "1", out_path],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and os.path.getsize(out_path) > 0:
                self.segments.append(out_path)
                self.contenu = True
                self.contenus.append(out_path)
        except Exception as exc:
            print(f"[radio_log] segment PCM ignore : {exc}")
        finally:
            try:
                os.remove(raw_path)
            except OSError:
                pass

    def finalize(self, subdir=None):
        """Concatene tous les segments en un seul fichier MP3 dans
        RADIO_LOG_DIR (ou son sous-dossier `subdir`, ex. "rejets"), nomme par
        libelle assaini + horodatage lisible (voir _readable_timestamp).
        Retourne le chemin final ou None si rien a sauvegarder/echec. Nettoie
        le dossier temporaire dans tous les cas."""
        try:
            if not self.segments:
                return None
            if self.exiger_contenu and not self.contenu:
                print(f"[radio_log] prise sans son capte (habillage seul), non gardee : {self.label}")
                return None
            out_dir = os.path.join(RADIO_LOG_DIR, subdir) if subdir else RADIO_LOG_DIR
            os.makedirs(out_dir, exist_ok=True)
            concat_list = os.path.join(self._tmpdir, "concat.txt")
            with open(concat_list, "w", encoding="utf-8") as f:
                for seg in self.segments:
                    f.write(f"file '{seg}'\n")
            safe_label = "".join(c if c.isalnum() else "_" for c in self.label).strip("_")[:40] or "capture"
            timestamp = _readable_timestamp(datetime.datetime.now())
            out_path = os.path.join(out_dir, f"{safe_label}_{timestamp}.mp3")
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "concat", "-safe", "0", "-i", concat_list,
                 "-ar", str(LOG_SAMPLE_RATE), "-ac", "1", out_path],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                print(f"[radio_log] capture enregistree : {out_path}")
                _poser_flux(self.flux, out_path)
                if subdir != "rejets":      # un rejet est deja son brut seul
                    self._poser_brut(out_path)
                _touch(JOURNAL_UPDATE_FLAG)  # le journal ouvert se met a jour
                return out_path
            print(f"[radio_log] assemblage echoue : {result.stderr.decode(errors='replace')[-300:]}")
            return None
        except Exception as exc:
            print(f"[radio_log] finalisation echouee : {exc}")
            return None
        finally:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _poser_brut(self, journal):
        """Le son capte seul, sans carillon, annonce ni shim (2026-11-11) : l'examen
        du Mac et la passe de la nuit ne doivent nettoyer et transcrire que lui,
        jamais notre habillage. Un chemin qui garde deja son brut (banc, flux
        rtl_fm : _save_raw) le remplace ensuite par le sien, pris avant filtrage."""
        dest = journal[:-4] + ".brut.mp3"
        if not self.contenus or os.path.exists(dest):
            return
        liste = os.path.join(self._tmpdir, "contenu.txt")
        try:
            with open(liste, "w", encoding="utf-8") as f:
                for seg in self.contenus:
                    f.write(f"file '{seg}'\n")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", liste, "-ar", str(LOG_SAMPLE_RATE), "-ac", "1", dest],
                           capture_output=True, timeout=30)
        except Exception as exc:
            print(f"[radio_log] son brut non garde : {exc}")

    def discard(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)


def _poser_flux(src, journal):
    """Copie le flux non décodé à côté du mp3. Même dossier, même purge."""
    if not src or not journal or not os.path.isfile(src):
        return
    try:
        if os.path.getsize(src) < 48_000:
            return
        dest = journal[:-4] + ".flux.wav" if journal.endswith(".mp3") else journal + ".flux.wav"
        shutil.copyfile(src, dest)
        print(f"[radio_log] flux non decode : {dest}")
    except OSError as exc:
        print(f"[radio_log] flux non garde : {exc}")


def purge_old_logs():
    """Supprime tout fichier de RADIO_LOG_DIR plus vieux que
    RETENTION_HOURS. Base sur la date de modification du fichier (donc
    l'heure de la capture, pas de creation d'un index separe)."""
    try:
        if not os.path.isdir(RADIO_LOG_DIR):
            return
        cutoff = time.time() - RETENTION_HOURS * 3600
        removed = 0
        # RADIO_LOG_DIR/*.mp3 et le sous-dossier rejets/ (journal des rejets)
        for path in (glob.glob(os.path.join(RADIO_LOG_DIR, "*.mp3"))
                     + glob.glob(os.path.join(RADIO_LOG_DIR, "rejets", "*.mp3"))
                     + glob.glob(os.path.join(RADIO_LOG_DIR, "*.flux.wav"))
                     + glob.glob(os.path.join(RADIO_LOG_DIR, "*", "*.flux.wav"))):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        try:
            import rejets
            rejets.purge(cutoff)
        except Exception:
            pass
        if removed:
            print(f"[radio_log] purge : {removed} fichier(s) de plus de {RETENTION_HOURS}h supprime(s)")
    except Exception as exc:
        print(f"[radio_log] purge echouee : {exc}")


def _purge_loop():
    while True:
        now = datetime.datetime.now()
        next_midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        time.sleep(max(1.0, (next_midnight - now).total_seconds()))
        purge_old_logs()


def start_purge_scheduler():
    """A appeler une fois au demarrage du service -- purge immediate (au
    cas ou le service serait reste hors service plusieurs jours), puis un
    thread qui repurge chaque jour a minuit pile."""
    purge_old_logs()
    thread = threading.Thread(target=_purge_loop, daemon=True)
    thread.start()
