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
Reglages a la main (commande "reglages son", 2026-10-14) : chaque couche peut
etre coupee ou reglee, y compris le filtre vocal (alors coupe seulement pour
ce qu'on entend) et le pilote FM ; en FM, le nettoyage n'est ajoute que si on
l'a demande a la main.
"""
import audio_filters
import profils_bruit
import reglages_audio


def settings(target, freq_hz=None):
    try:
        return reglages_audio.get(target, freq_hz)
    except Exception as exc:
        print(f"[chaine_audio] reglages illisibles ({target}) : {exc!r}")
        return dict(reglages_audio.DEFAULTS)


def _wfm_cleaning_wanted(s):
    """Radio FM reglee a la main (lib/reglages_audio.py, "manuel") : le
    nettoyage n'y est ajoute que si une de ses couches est allumee."""
    return (s["reduction_bruit_db"] > 0 or s["anti_crachotements"] > 0 or s["anti_sifflement"]
            or s["anti_bourdonnement"] or s["gain_db"] != 0)


class ChaineAudio:
    def __init__(self, target, freq_hz, sample_rate, mode):
        self.target, self.sample_rate, self.mode = target, sample_rate, mode
        self.settings = {**reglages_audio.DEFAULTS, **settings(target, freq_hz)}
        self.voice = None
        self.pilot = None
        self.cleaner = None
        # Couche "filtre voix" coupee (reglage a la main) : le filtre vocal sert
        # encore a l'analyse (RNNoise, empreintes), mais plus a ce qu'on entend.
        self.voice_heard = self.settings["filtre_voix"]
        try:
            if mode == "wfm":
                if self.settings["pilote_fm"]:
                    self.pilot = audio_filters.FmPilotFilter(sample_rate)
                if reglages_audio.manual(target)["contenu"] != "auto" or reglages_audio.manual(target)["couches"]:
                    if _wfm_cleaning_wanted(self.settings):
                        self.cleaner = audio_filters.cleaner_for(sample_rate, profils_bruit.get(target, freq_hz, sample_rate),
                                                                 self.settings)
            else:
                self.voice = audio_filters.VoiceBandpassFilter(sample_rate, lowpass_hz=self.settings["passe_bas_hz"])
                noise = profils_bruit.get(target, freq_hz, sample_rate)
                self.cleaner = audio_filters.cleaner_for(sample_rate, noise, self.settings)
        except Exception as exc:
            print(f"[chaine_audio] nettoyage indisponible ({target}) : {exc!r}")

    def _run(self, chunk, fmt):
        filtered = getattr(self.voice, "process_" + fmt)(chunk) if self.voice else chunk
        cleaned = filtered if self.voice_heard else chunk
        for stage in ("pilot", "cleaner"):
            f = getattr(self, stage)
            if f is None:
                continue
            try:
                cleaned = getattr(f, "process_" + fmt)(cleaned)
            except Exception as exc:
                print(f"[chaine_audio] {stage} abandonne ({self.target}) : {exc!r}")
                setattr(self, stage, None)
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
