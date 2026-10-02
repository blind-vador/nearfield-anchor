"""Chaine de filtres UNIQUE du son capte (demande utilisateur 2026-10-02) :
le balayage (watchers/radio_watch.py), les canaux RTLSDR-Airband
(watchers/airband_bridge.py) et la touche F du mode manuel
(lib/manual_tuner.py) passent tous par ici. Meme noyau (lib/audio_filters.py),
memes reglages appris des notes (lib/reglages_audio.py), meme empreinte du
bruit de la frequence (lib/profils_bruit.py) : ce que tu entends en manuel
avec F est exactement ce que le scanner fait en direct.

Une chaine par ecoute (les filtres gardent un etat d'un morceau a l'autre),
toutes fabriquees par nouvelle().

  filtre    filtre vocal seul (300 Hz a "aigus gardes") : ce que juge RNNoise,
            ce qui sert aux empreintes (parasites, bruit de fond) ;
  nettoye   + nettoyage doux (bourdonnement, gresillements, souffle appris,
            sifflement, volume) : ce que l'on entend et ce qu'on enregistre.
En FM large (radio), pas de filtre vocal : seul le pilote stereo a 19 kHz est
retire.
"""
import audio_filters
import profils_bruit
import reglages_audio


def settings(target):
    try:
        return reglages_audio.get(target)
    except Exception as exc:
        print(f"[chaine_audio] reglages illisibles ({target}) : {exc!r}")
        return dict(reglages_audio.DEFAULTS)


class ChaineAudio:
    def __init__(self, target, freq_hz, sample_rate, mode):
        self.target, self.sample_rate, self.mode = target, sample_rate, mode
        self.settings = settings(target)
        self.voice = None
        self.cleaner = None
        try:
            if mode == "wfm":
                self.cleaner = audio_filters.FmPilotFilter(sample_rate)
            else:
                self.voice = audio_filters.VoiceBandpassFilter(sample_rate, lowpass_hz=self.settings["passe_bas_hz"])
                noise = profils_bruit.get(target, freq_hz, sample_rate)
                self.cleaner = audio_filters.cleaner_for(sample_rate, noise, self.settings)
        except Exception as exc:
            print(f"[chaine_audio] nettoyage indisponible ({target}) : {exc!r}")

    def _run(self, chunk, fmt):
        filtered = getattr(self.voice, "process_" + fmt)(chunk) if self.voice else chunk
        cleaned = filtered
        if self.cleaner is not None:
            try:
                cleaned = getattr(self.cleaner, "process_" + fmt)(filtered)
            except Exception as exc:
                print(f"[chaine_audio] nettoyage abandonne ({self.target}) : {exc!r}")
                self.cleaner = None
        return filtered, cleaned

    def process_s16le(self, chunk):
        """-> (filtre, nettoye), en PCM 16 bits."""
        return self._run(chunk, "s16le")

    def process_f32le(self, chunk):
        """-> (filtre, nettoye), en flottants 32 bits (RTLSDR-Airband)."""
        return self._run(chunk, "f32le")


def nouvelle(target, freq_hz, sample_rate, mode):
    """La chaine d'une ecoute : seule facon de fabriquer des filtres."""
    return ChaineAudio(target, freq_hz, sample_rate, mode)
