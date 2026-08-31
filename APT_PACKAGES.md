# Dépendances système (apt)

## Paquets directement installables

```bash
sudo apt-get update
sudo apt-get install -y \
    espeak \
    ffmpeg \
    rtl-sdr \
    librtlsdr-dev \
    libjansson-dev \
    build-essential \
    cmake \
    git \
    python3-pip
```

- `espeak` : synthèse vocale locale de secours (voir `lib/speak.py`), toujours
  disponible même si le cloud (edge-tts) est indisponible.
- `ffmpeg` : décodage/ré-échantillonnage audio, assemblage des journaux de
  capture (`lib/radio_log.py`), lecture des sons sur le Pi (`radiono/`).
- `rtl-sdr` + `librtlsdr-dev` : outils et bibliothèque de développement pour
  le dongle RTL-SDR (rtl_power, rtl_fm, et compilation des outils ci-dessous).
- `libjansson-dev` : sortie JSON pour `libacars`/`acarsdec` (voir plus bas).
- `build-essential`, `cmake`, `git` : nécessaires pour compiler les
  bibliothèques ci-dessous depuis les sources (aucune n'est empaquetée
  Debian/Raspbian).

## Bibliothèques compilées depuis les sources

Aucun des projets suivants n'existe en paquet Debian/Raspbian pour ce Pi
(architecture `armhf`) — ils doivent être clonés et compilés localement.
Toutes les commandes ci-dessous ont été testées et fonctionnent sur ce Pi.

### RTLSDR-Airband (squelch réel PMR446 + fréquences aéronautiques locales)

```bash
cd /home/vador/autopilot/pi_console
git clone https://github.com/szpajder/RTLSDR-Airband.git rtl_airband
cd rtl_airband/src
mkdir build && cd build
cmake -DNFM=ON ..
make -j4
```
Binaire attendu par `watchers/airband_bridge.py` :
`rtl_airband/src/build/src/rtl_airband` (voir `AIRBAND_BIN`).

### dsd-fme + mbelib (décodage voix numérique DMR/P25/D-STAR...)

```bash
cd /tmp
git clone https://github.com/szechyjs/mbelib.git
cd mbelib && mkdir build && cd build && cmake .. && make -j4
sudo make install && sudo ldconfig

cd /tmp
git clone https://github.com/lwvmobile/dsd-fme.git
cd dsd-fme && mkdir build && cd build && cmake .. && make -j4
sudo make install
```
Binaire attendu : `/usr/local/bin/dsd-fme` (voir `try_digital_voice_decode`
dans `watchers/radio_watch.py`).

### libacars + acarsdec (décodage ACARS)

```bash
cd /tmp
git clone https://github.com/szpajder/libacars.git
cd libacars && mkdir build && cd build
cmake ..    # doit afficher "jansson: ... enabled: TRUE" -- sinon libjansson-dev manque
make -j4
sudo make install && sudo ldconfig

cd /tmp
git clone https://github.com/TLeconte/acarsdec.git
cd acarsdec && mkdir build && cd build
cmake -Drtl=1 ..
make -j4
sudo make install
```
Binaire attendu : `/usr/local/bin/acarsdec` (voir `watchers/acars_watch.py`).
**Piège connu** : `acarsdec -r <device> <freqs...>` doit être en DERNIER sur
la ligne de commande — les fréquences sont des arguments positionnels
consommés jusqu'à la fin ; les placer avant d'autres options les fait
interpréter comme des fréquences supplémentaires ("Frequencies too far
apart").

## Dépendances Python (pip)

Voir `requirements.txt` à la racine. `pip3 install -r requirements.txt`.
