"""engine.py - CuteVoice v4 voice DSP, built on Spotify's `pedalboard`.

Signal chain (mono, SR = 48 kHz, processed in whole hops):

  NoiseGate -> HighpassFilter(Low Cut) -> Compressor -> PitchShift -> Chorus(thickness)
            -> Chorus(vibrato, 100 % wet) -> LowShelfFilter -> PeakFilter(presence)
            -> HighShelfFilter -> Reverb -> [smooth volume ramp] -> Limiter

Every number in the chain is driven by a GUI slider (see app.py).  The GUI only writes numbers into
`engine.params` (a dict).  At the start of every audio block `process()` converts them into target
values for the plugins and glides the real plugin values towards those targets (~60 ms time
constant), so dragging a slider while talking never clicks or zippers.  Plugins are only touched
from the worker thread (the thread that runs them), so no locks are needed.

Interface expected by app.py / audio_io.py:
  SR, Engine(params, mode), .params, .mode, .set_mode(), .hop, .latency_samples,
  .peak (mic level), .out_peak (output level), .process(block) -> block
"""
import math

import numpy as np
from pedalboard import (Chorus, Compressor, HighpassFilter, HighShelfFilter, Limiter,
                        LowShelfFilter, NoiseGate, Pedalboard, PeakFilter, PitchShift, Reverb)

SR = 48000
HOPS = {"fast": 480, "balanced": 960, "safe": 1920}      # 10 / 20 / 40 ms per block
GLIDE_SECONDS = 0.06                                      # time constant of the slider smoothing
VIB_CENTRE_MS = 8.0                                       # fixed delay of the vibrato stage
CHORUS_CENTRE_MS = 7.0

_latency_cache = {}


def _measure_latency():
    """Delay of PitchShift in samples, measured once with noise (so the delay readout is honest)."""
    if "v" in _latency_cache:
        return _latency_cache["v"]
    try:
        rng = np.random.default_rng(1)
        sig = (rng.standard_normal(SR // 2) * 0.1).astype(np.float32)
        board = Pedalboard([PitchShift(semitones=0.0)])
        outs = []
        for i in range(0, len(sig) - 479, 480):
            y = board(sig[i:i + 480][None, :], SR, reset=(i == 0))
            outs.append(np.asarray(y, np.float32).reshape(-1))
        out = np.concatenate(outs)
        sig = sig[:len(out)]
        n = 1 << int(np.ceil(np.log2(2 * len(out))))
        corr = np.fft.irfft(np.fft.rfft(out, n) * np.conj(np.fft.rfft(sig, n)), n)
        lat = int(np.argmax(corr[:16384]))
    except Exception:
        lat = 2688
    lat += int((CHORUS_CENTRE_MS + VIB_CENTRE_MS) / 1000.0 * SR)
    _latency_cache["v"] = lat
    return lat


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class Engine:
    def __init__(self, params, mode="balanced"):
        self.params = params
        self.peak = 0.0
        self.out_peak = 0.0
        self._cur = {}                       # last value written to each plugin attribute
        self._sm = None                      # smoothed (current) control values
        self._gain = None                    # current linear volume (for the click-free ramp)
        self._reset = True

        self.gate = NoiseGate(threshold_db=-100.0, ratio=10.0, attack_ms=1.0, release_ms=120.0)
        self.lowcut = HighpassFilter(cutoff_frequency_hz=20.0)
        self.comp = Compressor(threshold_db=-20.0, ratio=4.0, attack_ms=5.0, release_ms=120.0)
        self.pitch = PitchShift(semitones=0.0)
        self.chorus = Chorus(rate_hz=0.8, depth=0.25, centre_delay_ms=CHORUS_CENTRE_MS, feedback=0.0, mix=0.0)
        self.vibrato = Chorus(rate_hz=5.0, depth=0.0, centre_delay_ms=VIB_CENTRE_MS, feedback=0.0, mix=1.0)
        self.low = LowShelfFilter(cutoff_frequency_hz=150.0, gain_db=0.0, q=0.7071)
        self.presence = PeakFilter(cutoff_frequency_hz=2500.0, gain_db=0.0, q=0.9)
        self.high = HighShelfFilter(cutoff_frequency_hz=3000.0, gain_db=0.0, q=0.7071)
        self.reverb = Reverb(room_size=0.35, damping=0.6, wet_level=0.0, dry_level=1.0, width=1.0)
        self.limiter = Limiter(threshold_db=-1.0, release_ms=80.0)
        self.board = Pedalboard([self.gate, self.lowcut, self.comp, self.pitch, self.chorus, self.vibrato,
                                 self.low, self.presence, self.high, self.reverb])
        self.limiter_board = Pedalboard([self.limiter])

        # (plugin, attribute, control key): one line per adjustable plugin value
        self._map = [
            (self.gate, "threshold_db", "gate_db"),
            (self.lowcut, "cutoff_frequency_hz", "lowcut_hz"),
            (self.comp, "threshold_db", "comp_thr"),
            (self.comp, "ratio", "comp_ratio"),
            (self.comp, "attack_ms", "comp_attack"),
            (self.comp, "release_ms", "comp_release"),
            (self.pitch, "semitones", "semitones"),
            (self.chorus, "mix", "chorus_mix"),
            (self.chorus, "depth", "chorus_depth"),
            (self.chorus, "rate_hz", "chorus_rate"),
            (self.vibrato, "depth", "vib_depth"),
            (self.vibrato, "rate_hz", "vib_rate"),
            (self.low, "cutoff_frequency_hz", "low_hz"),
            (self.low, "gain_db", "low_db"),
            (self.presence, "cutoff_frequency_hz", "presence_hz"),
            (self.presence, "gain_db", "mid_db"),
            (self.high, "cutoff_frequency_hz", "high_hz"),
            (self.high, "gain_db", "high_db"),
            (self.reverb, "room_size", "room"),
            (self.reverb, "wet_level", "reverb_wet"),
            (self.limiter, "threshold_db", "ceiling"),
        ]

        self.mode = None
        self.hop = HOPS["balanced"]
        self._alpha = 0.3
        self.set_mode(mode)
        self.latency_samples = _measure_latency()

    # ------------------------------------------------------------------ mode
    def set_mode(self, mode):
        self.mode = mode if mode in HOPS else "balanced"
        self.hop = HOPS[self.mode]
        self._alpha = 1.0 - math.exp(-(self.hop / float(SR)) / GLIDE_SECONDS)
        self._reset = True                    # flush plugin state on the next block

    # ------------------------------------------------------------------ slider -> target values
    @staticmethod
    def _targets(p):
        def g(k, d=0.0):
            try:
                return float(p.get(k, d))
            except (TypeError, ValueError):
                return d

        on = bool(p.get("pers_on", False))
        mix = g("mix", 100.0) / 100.0 if on else 0.0
        thick = g("thick") / 100.0 * mix                   # -1 thin ... +1 thick
        persona = g("persona") / 100.0 * mix               # -1 ... +1
        semis = _clamp(g("pitch") + persona * 6.0, -24.0, 24.0)
        tilt = (g("timbre", 50.0) - 50.0) / 50.0           # -1 dark/heavy ... +1 light/bright
        pos_thick = max(thick, 0.0)

        low_db = g("bass") - tilt * 4.0 + thick * 4.0      # heavy bass / body
        clarity = g("clarity", 100.0) / 100.0              # auto-restore of clarity when pitched down
        bright = min(max(-semis, 0.0) * 0.4, 4.0) * clarity
        high_db = g("treble") + tilt * 3.0 - thick * 2.0 + bright

        gate = g("gate", -80.0)
        return dict(
            gate_db=-100.0 if gate <= -80.0 else _clamp(gate, -100.0, -20.0),
            lowcut_hz=_clamp(g("lowcut", 20.0), 20.0, 300.0),
            comp_thr=_clamp(g("comp_thr", 0.0), -60.0, 0.0),
            comp_ratio=_clamp(g("comp_ratio", 1.0), 1.0, 20.0),
            comp_attack=_clamp(g("comp_attack", 5.0), 0.5, 200.0),
            comp_release=_clamp(g("comp_release", 120.0), 5.0, 1000.0),
            semitones=semis,
            chorus_mix=_clamp(g("chorus_mix", 0.0) / 100.0 + pos_thick * 0.45, 0.0, 1.0),
            chorus_depth=_clamp(g("chorus_depth", 25.0) / 100.0 + 0.15 * pos_thick, 0.0, 1.0),
            chorus_rate=_clamp(g("chorus_rate", 0.8), 0.05, 10.0),
            vib_depth=_clamp(g("vib_depth", 0.0) / 100.0 * 0.6, 0.0, 1.0),
            vib_rate=_clamp(g("vib_rate", 5.0), 0.5, 15.0),
            low_hz=_clamp(g("low_hz", 150.0), 40.0, 600.0),
            low_db=_clamp(low_db, -18.0, 18.0),
            presence_hz=_clamp(g("presence_hz", 2500.0), 500.0, 8000.0),
            mid_db=_clamp(g("mid"), -12.0, 12.0),
            high_hz=_clamp(g("high_hz", 3000.0), 1000.0, 12000.0),
            high_db=_clamp(high_db, -18.0, 18.0),
            room=_clamp(g("room", 35.0) / 100.0, 0.0, 1.0),
            reverb_wet=_clamp(g("reverb") / 100.0 * 0.8, 0.0, 0.5),
            ceiling=_clamp(g("ceiling", -1.0), -12.0, 0.0),
        ), _clamp(g("volume", 100.0) / 100.0, 0.0, 2.0)

    def _write(self, plugin, attr, value):
        value = round(float(value), 3)
        key = (id(plugin), attr)
        if self._cur.get(key) != value:
            try:
                setattr(plugin, attr, value)
            except Exception:                              # out-of-range / unsupported value: keep the old one
                pass
            self._cur[key] = value

    def _sync(self):
        target, volume = self._targets(self.params)
        if self._sm is None:                               # first block: jump straight there
            self._sm = dict(target)
        else:
            a = self._alpha
            for k, t in target.items():
                c = self._sm[k]
                d = t - c
                self._sm[k] = t if abs(d) < max(1e-3, 1e-3 * abs(t)) else c + d * a
        for plugin, attr, key in self._map:
            self._write(plugin, attr, self._sm[key])
        return volume

    # ------------------------------------------------------------------ audio
    def process(self, x):
        x = np.asarray(x, np.float32).reshape(-1)
        n = len(x)
        if n == 0:
            return x
        volume = self._sync()
        self.peak = float(np.max(np.abs(x)))

        y = self.board(x[None, :], SR, reset=self._reset)
        self._reset = False
        y = np.asarray(y, np.float32).reshape(-1)
        if len(y) != n:                                    # defensive: keep block size exact
            y = np.pad(y, (0, max(0, n - len(y))))[:n]

        # volume: ramp across the block, so dragging the slider never clicks
        if self._gain is None:
            self._gain = volume
        y *= np.linspace(self._gain, volume, n, endpoint=False, dtype=np.float32)
        self._gain = volume

        y = np.asarray(self.limiter_board(y[None, :], SR, reset=False), np.float32).reshape(-1)
        np.nan_to_num(y, copy=False)
        np.clip(y, -1.0, 1.0, out=y)                       # hard safety net: the limiter alone can overshoot
        self.out_peak = float(np.max(np.abs(y)))
        return y
