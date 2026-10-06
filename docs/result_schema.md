# `result.json` schema (version 1)

One file per analysed recording: `results/<stem>/result.json`, written by `barlab analyze`.
Numbers are rounded (frequencies 0.1 Hz, levels 0.1 dB, Q/tau/spreads 3 significant digits);
`null` means "not available". Units are in the key names (`_hz`, `_ms`, `_s`, `_db`, `_dbfs`,
`_mm`). Frequencies are linear, 0 to Nyquist. Every key the code emits is listed here
(`tests/test_schema_doc.py` enforces it); bump `schema_version` on incompatible changes.

Measured vs inferred: everything under `file`, `segmentation`, `hits`, `spectra` and the
measured parts of `peaks` is measured from the audio; `class`, `flags`, `reasons`, `theory`
and `summary` are rule-based inferences from those measurements (rules in `barlab/classify.py`).

## Top level

| key | meaning |
|---|---|
| `schema_version` | integer, this document's version (1) |
| `barlab_version` | package version that wrote the file |
| `analyzed_at` | ISO 8601 timestamp with UTC offset |
| `file` | the recording (below) |
| `warnings` | list of `{severity, code, message}`; sorted CRITICAL, WARNING, INFO |
| `meta` | metadata used (sidecars / CLI), below |
| `segmentation` | impact detection and windows, below |
| `hits` | one entry per detected impact, below |
| `spectra` | Welch settings and achieved resolution, below (`null` if no hits) |
| `peaks` | detected spectral peaks in frequency order, below |
| `theory` | expected bar modes (if bar length known), below |
| `summary` | the deterministic headline, below |
| `params` | every analysis parameter used (see `barlab/params.py`), below |
| `plots` | figure file names in the same directory, below |

## `file`

| key | meaning |
|---|---|
| `path` | path relative to the project root |
| `stem` | file name without extension |
| `sha256` | SHA-256 of the file bytes (the index key) |
| `source` | `raw` (data/raw), `synthetic` (data/synthetic) or `other` |
| `result_dir` | where this result lives (`results/<stem>`, or `results/<stem>_<hash8>` on a name clash) |
| `fs_hz`, `nyquist_hz` | native sample rate (never resampled) and fs/2 |
| `format`, `subtype` | container and sample format as reported by libsndfile (e.g. `WAV`, `PCM_16`) |
| `bit_depth` | nominal bits per sample of `subtype` |
| `effective_bits` | bits actually used by the integer samples (null for float) |
| `channels`, `channel_used` | channel count and the channel analysed (clearest hits) |
| `duration_s`, `n_frames` | length |
| `recorded_at` | GUANO `Timestamp` if present |
| `device` | free-form GUANO metadata (make, model, app ...); location keys are never copied |
| `channel_summary` | per channel: `channel`, `n_hits`, `median_rise_db`, `peak_dbfs`, `rms_dbfs`, `dc_offset_fs`, `clip_fraction` |

## `warnings`

Each entry: `severity` (`INFO` / `WARNING` / `CRITICAL`), `code`, `message`. Codes:

| code | severity | meaning |
|---|---|---|
| `FS_BELOW_96K` | CRITICAL | fs < 96 kHz: cannot represent >fs/2; ultrasonic bar modes not observable; any peak found may be aliased |
| `TOO_SHORT` | CRITICAL | less than 50 ms of audio |
| `FS_BELOW_192K` | INFO | 96 kHz <= fs < 192 kHz: peaks above 0.9 Nyquist are flagged as possible aliases |
| `LOW_BIT_DEPTH` | WARNING | 8-bit (or coarser) samples |
| `EFFECTIVE_BITS` | INFO | container wider than the significant bits |
| `CLIPPING` | WARNING | samples at full scale somewhere in the file |
| `CLIPPING_RINGDOWN` | WARNING | clipping after the attack: the ringdown restarts `clip_guard_ms` after the last clipped sample (hit excluded if too little remains); clipping spreads energy over the whole band |
| `CLIPPING_ATTACK_ONLY` | INFO | clipping only during attacks (harmless for the ringdown) |
| `DC_OFFSET` | WARNING | mean above 1 % of full scale (removed before analysis) |
| `LOW_LEVEL` | WARNING | peak below -40 dBFS |
| `NO_HITS` | WARNING | no impacts detected |
| `NOISE_FALLBACK` | WARNING | no quiet segment: noise reference from pre-hit samples (environment class less reliable) |
| `NO_ULTRASONIC_ENERGY` | WARNING | above 20 kHz the ringdown never rises 10 dB over the noise: mic roll-off / anti-alias filter |
| `NO_ULTRASONIC_BAND` | WARNING | Nyquist <= 20 kHz |
| `RESOLUTION_COARSE` | INFO | ringdowns too short for <= 50 Hz PSD resolution (frequencies still come from phase fits) |

## `meta`

| key | meaning |
|---|---|
| `sources` | sidecar files read (`data/meta/_defaults.toml`, `data/meta/<stem>.toml`) |
| `recording` | `mic`, `temperature_c`, `striking`, `support`, `distance_m`, `operator`, `notes` (only those given); unknown keys under `extra` |
| `bars` | per bar: `name`, `L_mm` (null = unknown), `d_mm`, `material`, `E_gpa`, `rho_kg_m3`, `nu`, `bc` (`free-free` / `clamped-free`) |

## `segmentation`

| key | meaning |
|---|---|
| `n_hits` | number of impacts |
| `threshold_db` | rise threshold actually used: max(median + k MAD of the rise function, `min_rise_db`) |
| `quiet_level_dbfs` | 10th percentile of the 5 ms envelope (quiet reference) |
| `end_threshold_db` | ringdown-end threshold on the narrowband excess (noise 99th percentile + margin) |
| `end_frame_ms` | STFT frame used by the ringdown-end detector |
| `noise_s` | noise-reference segments `[start, end]` in seconds |
| `noise_total_s` | their total length |
| `noise_fallback` | true when pre-hit samples had to be used |

## `hits[]`

| key | meaning |
|---|---|
| `i` | hit index (0-based, time order) |
| `onset_s` | detected onset; attack = onset .. onset + `attack_ms` |
| `peak_dbfs` | short-envelope (RMS) peak of the hit |
| `rise_db` | rise over the trailing 20 ms level at detection |
| `ringdown_ms` | ringdown window length (attack end -> end) |
| `end_reason` | `noise` (decayed into the noise), `next_hit`, `max_length`, `file_end` |
| `clip_trim_ms` | ringdown start moved this far past the attack because of clipping (0 = none) |
| `clipped` | clipping persists through the ringdown (hit excluded from decay fits) |

## `spectra`

| key | meaning |
|---|---|
| `nperseg` | median Welch segment length over hits (Hann window, 50 % overlap) |
| `nperseg_target` | smallest power of two giving fs/nperseg <= `target_res_hz` (the coarsest allowed); the segment actually used is the largest power of two within the median ringdown, between that and `fine_res_hz` |
| `nperseg_noise` | segment length used for the noise PSD |
| `nfft` | FFT length (common grid for all PSDs) |
| `bin_hz` | grid spacing fs/nfft |
| `resolution_hz` | achieved resolution fs/`nperseg` |
| `enbw_hz` | equivalent noise bandwidth (1.5 x resolution for Hann) |
| `resolution_ok` | resolution <= `target_res_hz` |
| `n_hits_used` | hits averaged (ringdowns shorter than half the median are skipped) |
| `noise_s` | seconds of noise used for the noise PSD |
| `ultrasonic_peak_snr_db` | strongest per-bin ringdown/noise excess above 20 kHz |

## `peaks[]`

Measured:

| key | meaning |
|---|---|
| `i` | peak index (frequency order; also the index.csv `peak_idx`) |
| `f_hz` | best frequency: median of the per-hit phase-slope frequencies after rejecting outliers (> 5 robust SD from the median); else the PSD estimate |
| `f_psd_hz` | parabolic-interpolated peak of the averaged ringdown PSD |
| `f_std_hz`, `f_sem_hz` | ordinary SD (ddof 1) of the outlier-cleaned per-hit frequencies, and 1.2533 SD / sqrt(n), the standard error of their median (checked against simulations in tests/test_decay.py) |
| `f_range_rel` | cross-hit consistency: (P90 - P10) / median of per-hit frequencies with >= 5 hits, (max - min) / median with fewer |
| `level_db`, `floor_db` | ringdown PSD at the peak and the smoothed noise floor (dB re FS^2/Hz) |
| `above_floor_db` | detection SNR: `level_db` - `floor_db` (must be >= `min_above_floor_db`) |
| `snr_db` | per-bin ringdown / noise PSD at the peak (low = also present in the noise window) |
| `noise_line_db` | noise PSD above its own floor at the peak (a line in the noise window) |
| `prominence_db` | peak prominence in the ringdown PSD |
| `width_hz`, `q_width` | -3 dB width and f/width (a lower bound on Q when resolution-limited) |
| `decay` | ringdown fits, below |

`decay` (per-peak band-pass + Hilbert envelope + weighted log-linear fit per hit):

| key | meaning |
|---|---|
| `band_hz` | band-pass edges |
| `n_tried`, `n_fitted`, `n_freq` | hits tried; decay fits with R^2 >= `r2_min`; hits with a frequency estimate |
| `tau_ms`, `tau_std_ms` | amplitude decay time (median) and robust spread |
| `q`, `q_std`, `q_range` | Q = pi f tau: median, robust spread, `[min, max]` over fitted hits |
| `r2` | median R^2 of the fits used |
| `q_source` | `hits` (median of per-hit fits) or `stack` (fit of the hit-averaged envelope, used when < 3 hits fit) |
| `q_stack` | Q of the stacked fit (cross-check) |
| `q_lower` | when no Q could be measured because the decay within the windows is < `min_decay_db`: lower bound Q > pi f T 8.69 / `min_decay_db` |
| `steady_s` | when the line does not decay at all over this many seconds: a steady tone, not a struck-bar ringdown |
| `per_hit` | strongest 3 peaks only: `hit`, `f_hz`, `q`, `r2`, `reason` (why a hit was not used) |

Inferred:

| key | meaning |
|---|---|
| `class` | `bar_mode_candidate`, `broadband_resonance`, `noise_or_environment`, `possible_alias`, `unresolved` |
| `flags` | evidence: `fs_below_96k`, `near_nyquist`, `line_in_noise`, `resolution_limited`, `wide`, `high_q`, `q_lower_bound`, `steady`, `consistent`, `not_fitted`, `q_from_stack`, `narrow_band`, `single_hit`, `matches_theory` |
| `reasons` | why the class was chosen (or why not a candidate) |
| `theory` | `nearest`, `matches`, `implied_L_mm`, below |

`theory` per peak: `nearest` is `null` unless a bar length is known, else an object with
`bar`, `family`, `n`, `model`, `f_hz`, `rel_err`, `unambiguous`, `next_family`, `next_n`,
`next_f_hz` (`rel_err` = (f - f_mode) / f_mode; `unambiguous` = nearest mode at least 3x closer
than the next one, which is described by the `next_*` keys);
`matches` = |`rel_err`| <= `theory_tol` and unambiguous; `implied_L_mm` (when L is unknown,
for candidates/unresolved peaks): list of `{family, n, L_mm}` - the bar length that would put
that mode at `f_hz`.

## `theory`

| key | meaning |
|---|---|
| `available` | a mode table exists (some bar has a known length) |
| `bars_known` | same, as a flag |
| `validity` | model-validity notes (e.g. Euler-Bernoulli unreliable for L/d < 10) |
| `modes` | up to 40 modes below Nyquist: `bar`, `family` (`longitudinal`/`flexural`/`torsional`), `n`, `model` (`rayleigh-love`/`timoshenko`/`exact-circular`), `f_hz`, `f_simple_hz` (n c/2L or Euler-Bernoulli) |
| `n_modes` | total modes below Nyquist |

## `summary`

| key | meaning |
|---|---|
| `determinable` | true when fs >= 96 kHz, hits exist and at least one `bar_mode_candidate` was found |
| `reason` | one-line explanation |
| `critical` | CRITICAL warning codes |
| `n_candidates` | number of bar-mode candidates |
| `primary_peak` | index of the strongest candidate (highest `above_floor_db`) |
| `ringing_hz`, `ringing_std_hz`, `ringing_sem_hz` | its frequency, cross-hit spread, standard error |
| `q` | its Q |
| `q_lower` | its Q lower bound when the decay was too slow to measure (`q` is then null) |
| `theory_match` | nearest theoretical mode of the primary peak, e.g. `longitudinal n=1 (+0.00%)` |

## `params`

All fields of `barlab.params.Params`, e.g. `fs_min_hz`, `dc_max_fs`, `low_level_dbfs`, `ultra_hz`,
`ultra_min_db`, `hp_hz`, `env_win_ms`, `trail_win_ms`, `trail_gap_ms`, `k_mad`, `min_rise_db`,
`min_hit_snr_db`, `min_spacing_ms`, `attack_ms`, `pre_guard_ms`, `max_ringdown_s`,
`min_ringdown_ms`, `clip_guard_ms`, `end_frame_ms`, `end_margin_db`, `end_hold_frames`, `quiet_win_ms`,
`quiet_percentile`, `quiet_margin_db`, `min_noise_ms`, `max_noise_s`, `fallback_noise_ms`,
`edge_ms`, `target_res_hz`, `fine_res_hz`, `floor_smooth_hz`, `fmin_hz`, `prominence_db`,
`min_above_floor_db`, `max_peaks`, `leak_guard_res`, `leak_keep_db`, `bw_factor`, `bw_min_hz`,
`bw_max_frac`, `filt_order`, `fit_floor_db`, `min_decay_db`, `steady_min_s`, `q_max_plausible`, `r2_min`, `max_decay_peaks`,
`q_min`, `f_spread_max`, `min_hits_fitted`, `env_snr_db`, `noise_line_db`, `alias_frac`,
`theory_tol`, `theory_unambiguous`. Meanings are commented in `barlab/params.py`; override any of
them with `barlab analyze --set KEY=VALUE`.

## `plots`

File names (same directory) keyed `waveform`, `spectrogram`, `psd`, `decay`, `hit_zoom`;
empty when run with `--no-plots`.

## `results/index.csv`

One row per (`sha256`, `peak_idx`); a file without peaks has one row with `peak_idx` = -1 and
`class` = none. Columns: sha256, peak_idx, stem, path, source, recorded_at, analyzed_at, fs_hz,
channel, n_hits, max_warning, determinable, f_hz, f_std_hz, f_sem_hz, f_range_rel, q, q_std, tau_ms,
snr_db, above_floor_db, width_hz, n_fitted, class, flags (`;`-separated), theory_family,
theory_n, theory_f_hz, theory_rel_err, barlab_version, schema_version.
