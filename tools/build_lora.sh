#!/bin/bash
# Compile gr-lora_sdr (decodage LoRa/Meshtastic differe) dans
# ~/autopilot/gnuradio, sans droits root. Long (~2 h sur le Pi 4) : attend
# que le Pi soit sous 65 C, compile a basse priorite. A lancer hors du
# scanner : systemd-run --user --unit=build-lora bash tools/build_lora.sh
set -e
while [ "$(cat /sys/class/thermal/thermal_zone0/temp)" -gt 65000 ]; do sleep 30; done
cd /tmp && rm -rf gr-lora_sdr && git clone -q --depth 1 https://github.com/tapparelj/gr-lora_sdr.git
mkdir -p gr-lora_sdr/build && cd gr-lora_sdr/build
cmake .. -DCMAKE_INSTALL_PREFIX="$HOME/autopilot/gnuradio" -DPYTHON_EXECUTABLE="$HOME/autopilot/bin/python3-gnuradio"
nice -n 19 make -j2
make install
rm -rf /tmp/gr-lora_sdr
echo LORA_BUILD_OK
