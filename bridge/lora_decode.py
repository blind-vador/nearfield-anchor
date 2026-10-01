#!/usr/bin/env python3
"""Decode une rafale LoRa enregistree (I/Q 8 bits non signes) avec
gr-lora_sdr : rend les trames trouvees en JSON sur la sortie standard.

Commun au Pi (lance par ~/autopilot/bin/python3-gnuradio, en secours) et a
la machine compagne (Python de l'environnement GNU Radio du module).

  lora_decode.py rafale.cu8 <taux> <largeur> <sync> <sf,sf,...>
  -> [{"sf": 12, "octets": "3cff01...", "crc_ok": true}, ...]

On essaie les facteurs d'etalement dans l'ordre donne (celui du profil
d'abord) et on s'arrete au premier qui donne une trame valide.
"""
import json
import sys

import numpy as np
from gnuradio import blocks, filter, gr
from gnuradio.filter import firdes
from gnuradio.lora_sdr import lora_sdr_lora_rx
import pmt

OS_FACTOR = 2  # echantillons par "chip" attendus par frame_sync


def _iq(path):
    raw = np.fromfile(path, dtype=np.uint8).astype(np.float32) - 127.5
    x = (raw[0::2] + 1j * raw[1::2]) / 128.0
    return x.astype(np.complex64)


def decode_sf(samples, rate, bw, sync, sf):
    out_rate = bw * OS_FACTOR
    decim = int(round(rate / out_rate))
    tb = gr.top_block()
    pad = np.zeros(int(rate * 0.5), dtype=np.complex64)  # vide le flux en fin de fichier
    src = blocks.vector_source_c(np.concatenate([samples, pad]).tolist(), False)
    taps = firdes.low_pass(1.0, rate, bw * 0.6, bw * 0.2)
    chan = filter.fir_filter_ccf(decim, taps)
    chan.set_min_output_buffer(1 << 16)  # SF12 : frame_sync demande plus de 8192 echantillons d'un coup
    rx = lora_sdr_lora_rx(center_freq=868_000_000, bw=bw, cr=1, has_crc=True, impl_head=False, pay_len=255,
                          samp_rate=rate // decim, sf=sf, sync_word=[sync], soft_decoding=True, ldro_mode=2,
                          print_rx=[False, False])
    sink = blocks.vector_sink_b()
    tb.connect(src, chan, rx, sink)
    tb.run()
    data = bytes(sink.data())
    frames = []
    tags = sorted((t for t in sink.tags() if pmt.symbol_to_string(t.key) == "frame_info"), key=lambda t: t.offset)
    for tag in tags:
        info = tag.value
        length = pmt.to_long(pmt.dict_ref(info, pmt.intern("pay_len"), pmt.from_long(0)))
        valid = pmt.dict_ref(info, pmt.intern("crc_valid"), pmt.PMT_NIL)
        start = tag.offset
        frames.append({"sf": sf, "octets": data[start:start + length].hex(),
                       "crc_ok": None if pmt.is_null(valid) else pmt.to_bool(valid)})
    return frames


def main(path, rate, bw, sync, sfs):
    samples = _iq(path)
    found = []
    for sf in sfs:
        frames = [f for f in decode_sf(samples, rate, bw, sync, sf) if f["octets"]]
        found += frames
        if any(f["crc_ok"] for f in frames):
            break
    print(json.dumps(found))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4], 0),
         [int(s) for s in sys.argv[5].split(",")])
