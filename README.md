# RVI Home Assignment – Test Track BLF Post-Processing (dGPS + FrameID)

This project decodes a Test Track BLF log with DBC files, time-aligns signals, exports a merged CSV, builds visualizations, and serves a local upload UI.

## Project layout

```
data/                         # BLF + DBC (+ assignment PDF)
process_blf.py                # Full processing pipeline
web_app.py                    # Local upload + results UI
templates/                    # Flask HTML templates (English)
requirements.txt
output/
  dgps_frameid_export.csv     # Required merged export
  analysis_summary.json       # Numeric summary
  plots/                      # HTML/PNG plots + interactive dashboard
README.md
```

## How to run

### CLI

```bash
pip install -r requirements.txt
python process_blf.py
```

```bash
python process_blf.py --blf data/Logging_2026-07-10_12-01-57.blf --out-dir output
```

### Local web UI

```bash
python web_app.py
```

Then open:
- Upload page: http://127.0.0.1:5000
- Precomputed assignment results: http://127.0.0.1:5000/assignment-results

1. Upload BLF + two DBC files (or re-process sample files from `data/`)
2. After processing you are redirected to `/results/<job_id>` with dashboard, plots, CSV
3. New web runs are stored under `web_runs/` (CLI `output/` is unchanged)

## Before submission

- Include at least: `process_blf.py`, `web_app.py`, `templates/`, `requirements.txt`, `README.md`, `output/dgps_frameid_export.csv`, `output/plots/`
- The BLF (~45MB) can be shared via Drive link if email size is limited
- Make sure reviewers see the **TargetPosLocalX/Y fallback** note below

## Part A – Extract and merge

### DBC sources

| CSV column | DBC | CAN ID | Message |
|------------|-----|--------|---------|
| frameID | ISR_mqttToCan_dbc_251210.dbc | 0x400 (1024) | mqtt_to_can_bridge_... |
| VUT_Velocity | ESP_TT_dbc_250427.dbc | 1539 | Velocity |
| Target2_Velocity | ESP_TT_dbc_250427.dbc | 1795 | TargetVelocity |
| Target2_FW_Distance, RangeTimeToCollisionForward | ESP_TT_dbc_250427.dbc | 1968 | RangeForward |
| VUT_Lat_Meter, VUT_Lng_Meter | ESP_TT_dbc_250427.dbc | 1548 | PosLocal |
| TargetPosLocalX, TargetPosLocalY | ESP_TT_dbc_250427.dbc | 1804 (primary) / 1972 (fallback) | TargetPosLocal / RangeTargetPosLocal |

> **Important note:** In this log, `TargetPosLocal` (1804) is completely missing. CSV columns `TargetPosLocalX/Y` were filled from `Target2_Lng_Meter` / `Target2_Lat_Meter` (1972). Required column names were preserved.

### timestamp_sec – relative or absolute?

**Relative.** `timestamp_sec = timestamp_abs_CAN - t_first`, where `t_first` is the first CAN frame timestamp in the log. Absolute time is kept internally only for merging.

### Time alignment approach

We use **`pandas.merge_asof(..., direction="backward")` on a ~10 ms grid**:

1. Build a dense timeline (FrameID timestamps + synthetic 10 ms grid).
2. For each signal, attach the **last known value** at time <= grid point.
3. Drop fully empty rows, then (for CSV export only) trim leading FrameID-only rows before the first DGPS sample. `timestamp_sec` stays relative to log start.

### Edge cases handled

- short payload / decode failure -> counted and skipped
- unrelated CAN IDs -> skipped (streaming)
- duplicate timestamps -> last value kept
- missing signal -> NaN / documented fallback
- `frameID == 0` -> kept and reported
- FrameID jumps -> detected with exact relative time and marked on plots
- `frameID` exported as integer in CSV

## Part B – Visualization

In `output/plots/`:

1. `01_velocities.html/.png` – VUT vs Target speed (+ parallel* bands)
2. `02_forward_distance.html/.png` – forward distance
3. `03_ttc_forward.html/.png` – forward TTC
4. `04_frameid_monotonicity.html/.png` – FrameID (+ jump annotations)
5. `dashboard.html/.png` – all four plots together (Plotly zoom/pan)
6. `05_trajectories.html` – bonus local XY trajectories

The `parallel*` markers are based on an **assumption** (see below), not a PDF hard requirement.

## Analysis and findings

- Total recording duration: **866.42 s** (~14.44 min). Merged grid rate: **~100.0 Hz**. Raw FrameID rate in the log: **~28.6 Hz** — below the nominal 100 Hz from the DBC/PDF (important sync/logging finding).
- VUT speed range: **0.000 ... 28.420 m/s (mean 4.006)**.
- Target speed range: **0.000 ... 27.920 m/s (mean 3.689)**.
- Parallel motion: Yes (per our **assumption** for parallel detection — not a PDF-defined metric). Segments >= 2.0s:
  - 107.5s-114.0s (duration 6.5s, VUT~10.91, Target~11.11 m/s, mean lateral sep~0.10 m)
  - 771.1s-775.6s (duration 4.5s, VUT~27.82, Target~27.77 m/s, mean lateral sep~0.39 m)
- FrameID non-decreasing? **Yes**. Backward jumps=0, large jumps(>5)=1, zeros=0. Gaps/jumps: at t~0.1s: 297729->298007 (delta=278). Range: 297655 -> 323999 (26068 raw samples).
- **TargetPosLocalX/Y (important for review):** primary CAN message `0x70C` / `TargetPosLocal` **was absent from this log** (0 frames). Columns were filled via documented fallback from `0x7B4` (`fallback:0x7B4/Target2_Lng_Meter`). This keeps CSV completeness; it is not the primary DBC signal.
- Decode errors: 0 of 466987 relevant frames.


## Assumptions (not PDF hard requirements)

> Each item below is **our assumption for analysis/implementation**. It is **not** a mandatory requirement in the assignment PDF.

1. **Parallel motion** – assumption: horizontal speed difference <= `0.5 m/s`, min speed `1.0 m/s`, local lateral separation `|VUT_Lat_Meter - TargetPosLocalY| <= 2.0 m`, contiguous duration >= `2.0 s`. The PDF asks whether such segments exist but does not define numeric thresholds.
2. **FrameID jump threshold** – assumption: `delta > 5` is reported as a hole/sync-drop. The PDF says large jumps are suspicious, without a numeric threshold.
3. **10 ms grid** – implementation choice aligned with the nominal ~100 Hz FrameID rate; `merge_asof` itself is one of the approaches suggested in the PDF.
4. **TargetPosLocal fallback** – engineering assumption when message 1804 is missing: use 1972 under the required CSV column names.
5. **Trim leading FrameID-only rows in CSV** – cleanliness assumption; not required by the PDF.

### Interpretation notes

- TTC can be negative/saturated when there is no real closing scenario — inspect together with forward distance.
- Raw FrameID rate (~tens of Hz) is below nominal 100 Hz — possible logger/broadcast thinning; the merged grid remains ~100 Hz.

## Dependencies

See `requirements.txt` (python-can, cantools, pandas, numpy, plotly, matplotlib, flask).
