"""Analysis parameters. Every value used by the pipeline lives here and is written
to result.json under `params`, so any result can be reproduced exactly."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace


@dataclass(frozen=True)
class Params:
    # --- io checks ---
    fs_min_hz: float = 96_000.0       # below this: CRITICAL, ultrasonic modes not observable
    dc_max_fs: float = 0.01           # |mean| above this fraction of full scale -> warning
    low_level_dbfs: float = -40.0     # peak below this -> warning
    ultra_hz: float = 20_000.0        # "ultrasonic energy" check starts here
    ultra_min_db: float = 10.0        # strongest ringdown/noise excess above ultra_hz must exceed this

    # --- impact detection (segment.py) ---
    hp_hz: float = 2_000.0            # zero-phase high-pass before the envelope
    env_win_ms: float = 0.25          # short RMS envelope window
    trail_win_ms: float = 20.0        # trailing reference window for the rise detector
    trail_gap_ms: float = 1.0         # gap between trailing window and current sample
    k_mad: float = 10.0               # threshold = median + k * MAD of the rise function
    min_rise_db: float = 10.0         # threshold floor
    min_hit_snr_db: float = 10.0      # hit envelope peak must exceed the quiet level by this
    min_spacing_ms: float = 50.0      # minimum inter-hit spacing (merges bounces)

    # --- windows (segment.py) ---
    attack_ms: float = 1.0
    pre_guard_ms: float = 2.0         # gap kept before each onset
    max_ringdown_s: float = 5.0
    clip_guard_ms: float = 1.0        # clipped ringdowns restart this long after the last clipped sample
    min_ringdown_ms: float = 5.0
    end_frame_ms: float = 5.0         # STFT frame for the narrowband ringdown-end detector
    end_margin_db: float = 3.0        # end threshold = noise 99th percentile + margin
    end_hold_frames: int = 3          # frames the excess must stay below threshold
    quiet_win_ms: float = 5.0         # RMS window for quiet-segment search
    quiet_percentile: float = 10.0    # quiet level = this percentile of the 5 ms envelope
    quiet_margin_db: float = 3.0      # quiet = envelope within this of the quiet level
    min_noise_ms: float = 50.0
    max_noise_s: float = 2.0
    fallback_noise_ms: float = 20.0   # pre-hit samples used when no quiet segment exists
    edge_ms: float = 5.0              # ignore this much at file start/end (filter edges)

    # --- spectra (spectra.py) ---
    target_res_hz: float = 50.0       # coarsest acceptable Welch resolution (fs / nperseg), when windows allow
    fine_res_hz: float = 5.0          # finest resolution used for long ringdowns (segment ~ the median ringdown)
    floor_smooth_hz: float = 1_000.0  # median-filter width for the noise floor
    fmin_hz: float = 1_000.0          # lowest peak frequency considered
    prominence_db: float = 6.0
    min_above_floor_db: float = 10.0  # peak must sit this far above the noise floor
    max_peaks: int = 20
    leak_guard_res: float = 4.0       # weaker peaks within this many resolutions of a stronger one...
    leak_keep_db: float = 15.0        # ...are dropped unless within this many dB of it (leakage)

    # --- decay fits (decay.py) ---
    bw_factor: float = 8.0            # band-pass width = bw_factor * max(peak width, resolution)
    bw_min_hz: float = 400.0
    bw_max_frac: float = 0.3          # band-pass width at most this fraction of f
    filt_order: int = 3               # Butterworth order (applied forward-backward)
    fit_floor_db: float = 3.0         # fit while noise-subtracted power exceeds noise power by this
    min_decay_db: float = 6.0         # fit must span at least this much decay (else: Q lower bound only)
    steady_min_s: float = 0.2         # no decay over at least this long ...
    q_max_plausible: float = 1e6      # ... (or an implied Q above this) -> "steady": not a struck-bar ringdown
    r2_min: float = 0.9               # fits below this R^2 are excluded from aggregates
    max_decay_peaks: int = 12         # decay fits for the strongest N peaks only

    # --- classification (classify.py) ---
    q_min: float = 100.0              # bar_mode_candidate needs Q >= q_min
    f_spread_max: float = 0.01        # ...and (max-min)/median of per-hit f below this
    min_hits_fitted: int = 2
    env_snr_db: float = 6.0           # ringdown within this of the noise PSD -> noise_or_environment
    noise_line_db: float = 6.0        # noise PSD this far above its floor -> "line_in_noise" flag
    alias_frac: float = 0.9           # f above alias_frac * Nyquist -> possible_alias
    theory_tol: float = 0.02          # |rel. error| for a theory match
    theory_unambiguous: float = 3.0   # nearest mode must be this many times closer than the next

    def to_dict(self) -> dict:
        return asdict(self)

    def with_overrides(self, **kw) -> "Params":
        known = {f.name for f in fields(self)}
        bad = set(kw) - known
        if bad:
            raise ValueError(f"unknown parameter(s): {sorted(bad)}")
        return replace(self, **{k: v for k, v in kw.items() if v is not None})
