"""
RVI home assignment: decode BLF + DBC, time-align signals, export CSV, plot, analyze.

Pipeline:
  1) Load both DBC databases and merge message definitions
  2) Stream-decode only the relevant CAN IDs from the BLF
  3) Build one DataFrame per signal source (different message rates)
  4) Align to a 10 ms grid with merge_asof (backward / last-known-value)
  5) Export CSV, interactive HTML dashboard + PNG plots, and analysis summary
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import can
import cantools
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Required signals -> (CAN arbitration ID, signal name in DBC)
# ---------------------------------------------------------------------------
# Primary (msg_id, signal_name) plus optional fallbacks when the primary
# message is absent from a given log (observed for TargetPosLocal 0x70C).
SIGNAL_SOURCES: dict[str, list[tuple[int, str]]] = {
    "frameID": [(0x400, "frameID")],  # 1024 – ISR mqttToCan
    "VUT_Velocity": [(1539, "VUT_Velocity")],
    "Target2_Velocity": [(1795, "Target2_Velocity")],
    "Target2_FW_Distance": [(1968, "Target2_FW_Distance")],
    "RangeTimeToCollisionForward": [(1968, "RangeTimeToCollisionForward")],
    "VUT_Lat_Meter": [(1548, "VUT_Lat_Meter")],
    "VUT_Lng_Meter": [(1548, "VUT_Lng_Meter")],
    # Primary: TargetPosLocal (1804). Fallback: RangeTargetPosLocal (1972)
    # which carries the same local XY of the target under different names.
    "TargetPosLocalX": [(1804, "TargetPosLocalX"), (1972, "Target2_Lng_Meter")],
    "TargetPosLocalY": [(1804, "TargetPosLocalY"), (1972, "Target2_Lat_Meter")],
}

CSV_COLUMNS = [
    "timestamp_sec",
    "frameID",
    "VUT_Velocity",
    "Target2_Velocity",
    "Target2_FW_Distance",
    "RangeTimeToCollisionForward",
    "VUT_Lat_Meter",
    "VUT_Lng_Meter",
    "TargetPosLocalX",
    "TargetPosLocalY",
]

# ---------------------------------------------------------------------------
# ASSUMPTIONS (not stated as hard requirements in the assignment PDF).
# Documented again in README under "הנחות יסוד".
# ---------------------------------------------------------------------------
# ASSUMPTION: "נסיעה במקביל" = similar horizontal speeds for a contiguous duration.
PARALLEL_SPEED_DIFF_MPS = 0.5
PARALLEL_MIN_SPEED_MPS = 1.0
PARALLEL_MIN_DURATION_S = 2.0
# ASSUMPTION: also require small lateral separation in local Y (same local frame).
PARALLEL_LAT_DIFF_M = 2.0
# ASSUMPTION: FrameID delta above this is reported as a sync-drop / hole.
FRAMEID_LARGE_JUMP = 5
# ASSUMPTION: 10 ms grid matches the nominal FrameID rate from the PDF (~100 Hz).
GRID_STEP_S = 0.010
# Core DGPS columns used to trim leading FrameID-only rows (export cleanliness).
CORE_DGPS_COLS = ("VUT_Velocity", "Target2_Velocity")


def load_databases(dbc_paths: list[Path]) -> cantools.database.Database:
    """Load and merge DBC files. Raises if a required message/signal is missing."""
    db = cantools.database.Database()
    for path in dbc_paths:
        if not path.exists():
            raise FileNotFoundError(f"DBC not found: {path}")
        db.add_dbc_file(str(path))

    missing: list[str] = []
    for col, sources in SIGNAL_SOURCES.items():
        ok = False
        for msg_id, sig_name in sources:
            try:
                msg = db.get_message_by_frame_id(msg_id)
            except KeyError:
                continue
            if sig_name in {s.name for s in msg.signals}:
                ok = True
                break
        if not ok:
            missing.append(f"{col}: none of the candidate signals found in DBC")
    if missing:
        raise ValueError("DBC validation failed:\n  - " + "\n  - ".join(missing))
    return db


def decode_blf(
    blf_path: Path,
    db: cantools.database.Database,
) -> tuple[dict[str, pd.DataFrame], dict]:
    """
    Stream-decode BLF. Returns one DataFrame per CSV column (except timestamp_sec),
    each with columns: timestamp_abs, <signal>.

    Edge cases handled:
      - unknown / unused IDs skipped
      - truncated / undecodable frames skipped (counted)
      - empty payload / DecodeError skipped
      - absolute CAN timestamps preserved; relative time derived later
    """
    if not blf_path.exists():
        raise FileNotFoundError(f"BLF not found: {blf_path}")

    wanted_ids: set[int] = set()
    for sources in SIGNAL_SOURCES.values():
        for msg_id, _ in sources:
            wanted_ids.add(msg_id)

    # Collect every candidate stream separately, then pick primary-if-present.
    # key = (column, msg_id, signal_name)
    raw_buffers: dict[tuple[str, int, str], list[tuple[float, float]]] = {}
    for col, sources in SIGNAL_SOURCES.items():
        for msg_id, sig_name in sources:
            raw_buffers[(col, msg_id, sig_name)] = []

    # msg_id -> list of (column_name, signal_name)
    id_to_cols: dict[int, list[tuple[str, str]]] = {}
    for col, sources in SIGNAL_SOURCES.items():
        for msg_id, sig_name in sources:
            id_to_cols.setdefault(msg_id, []).append((col, sig_name))

    stats = {
        "total_frames": 0,
        "wanted_frames": 0,
        "decoded_ok": 0,
        "decode_errors": 0,
        "t_first": None,
        "t_last": None,
    }

    msg_by_id = {mid: db.get_message_by_frame_id(mid) for mid in wanted_ids}

    with can.BLFReader(str(blf_path)) as reader:
        for frame in reader:
            stats["total_frames"] += 1
            t = float(frame.timestamp)
            if stats["t_first"] is None:
                stats["t_first"] = t
            stats["t_last"] = t

            arb_id = int(frame.arbitration_id)
            if arb_id not in wanted_ids:
                continue
            stats["wanted_frames"] += 1

            msg_def = msg_by_id[arb_id]
            data = bytes(frame.data)
            # Edge case: shorter/longer payload than DBC expects
            if len(data) < msg_def.length:
                stats["decode_errors"] += 1
                continue
            try:
                decoded = msg_def.decode(data[: msg_def.length], decode_choices=False)
            except Exception:
                stats["decode_errors"] += 1
                continue

            stats["decoded_ok"] += 1
            for col, sig_name in id_to_cols[arb_id]:
                if sig_name not in decoded:
                    continue
                value = decoded[sig_name]
                if value is None:
                    continue
                raw_buffers[(col, arb_id, sig_name)].append((t, float(value)))

    per_signal: dict[str, pd.DataFrame] = {}
    source_used: dict[str, str] = {}
    for col, sources in SIGNAL_SOURCES.items():
        chosen_rows: list[tuple[float, float]] | None = None
        chosen_label = "none"
        for i, (msg_id, sig_name) in enumerate(sources):
            rows = raw_buffers[(col, msg_id, sig_name)]
            if rows:
                chosen_rows = rows
                kind = "primary" if i == 0 else "fallback"
                chosen_label = f"{kind}:0x{msg_id:X}/{sig_name}"
                break
        source_used[col] = chosen_label
        if not chosen_rows:
            per_signal[col] = pd.DataFrame(columns=["timestamp_abs", col])
        else:
            df = pd.DataFrame(chosen_rows, columns=["timestamp_abs", col])
            df = df.sort_values("timestamp_abs", kind="mergesort").drop_duplicates(
                subset=["timestamp_abs"], keep="last"
            )
            per_signal[col] = df.reset_index(drop=True)

    stats["signals_nonempty"] = {c: int(len(df)) for c, df in per_signal.items()}
    stats["source_used"] = source_used
    print(
        f"[decode] total={stats['total_frames']:,} wanted={stats['wanted_frames']:,} "
        f"ok={stats['decoded_ok']:,} errors={stats['decode_errors']:,}"
    )
    for col, n in stats["signals_nonempty"].items():
        print(f"  {col}: {n:,} samples ({source_used.get(col, 'none')})")

    return per_signal, stats


def time_align(
    per_signal: dict[str, pd.DataFrame],
    t_first: float,
    t_last: float,
    grid_step_s: float = GRID_STEP_S,
) -> pd.DataFrame:
    """
    Align asynchronous CAN signals onto a uniform 10 ms grid.

    Approach: pandas.merge_asof(..., direction='backward')
      - for each grid timestamp, take the latest known value of each signal
        at or before that time (equivalent to forward-fill of last sample)
      - tolerance=None (unbounded) so early gaps stay NaN until first sample

    timestamp_sec is RELATIVE to the first CAN frame in the log (t0 = 0).
    Absolute CAN time remains available internally as timestamp_abs.
    """
    if t_first is None or t_last is None or t_last < t_first:
        raise ValueError("Invalid BLF time range for alignment")

    # Prefer FrameID timeline as the natural clock if available and dense enough;
    # otherwise fall back to a synthetic 10 ms grid spanning the log.
    frame_df = per_signal.get("frameID")
    use_frame_grid = (
        frame_df is not None
        and not frame_df.empty
        and len(frame_df) >= 10
    )

    if use_frame_grid:
        grid = frame_df[["timestamp_abs"]].copy()
        # Also densify to 10 ms so DGPS gaps between FrameID ticks are filled
        t0, t1 = float(grid["timestamp_abs"].iloc[0]), float(grid["timestamp_abs"].iloc[-1])
        synthetic = pd.DataFrame(
            {"timestamp_abs": np.arange(t0, t1 + grid_step_s * 0.5, grid_step_s)}
        )
        grid = (
            pd.concat([grid, synthetic], ignore_index=True)
            .drop_duplicates(subset=["timestamp_abs"])
            .sort_values("timestamp_abs", kind="mergesort")
            .reset_index(drop=True)
        )
    else:
        grid = pd.DataFrame(
            {
                "timestamp_abs": np.arange(
                    t_first, t_last + grid_step_s * 0.5, grid_step_s
                )
            }
        )

    merged = grid.copy()
    for col in SIGNAL_SOURCES:
        src = per_signal[col]
        if src.empty:
            merged[col] = np.nan
            continue
        right = src.sort_values("timestamp_abs", kind="mergesort")
        merged = pd.merge_asof(
            merged,
            right,
            on="timestamp_abs",
            direction="backward",
        )

    merged["timestamp_sec"] = merged["timestamp_abs"] - t_first
    # Drop rows where every required signal is still NaN
    useful = [c for c in SIGNAL_SOURCES if c in merged.columns]
    merged = merged.dropna(subset=useful, how="all").reset_index(drop=True)
    return merged[CSV_COLUMNS + ["timestamp_abs"]]


def trim_leading_without_dgps(df: pd.DataFrame) -> pd.DataFrame:
    """
    ASSUMPTION (export cleanliness only): drop leading FrameID-only rows
    before the first DGPS sample. Used for CSV export; plots keep the full
    aligned series so early FrameID jumps remain visible.
    """
    core_present = [c for c in CORE_DGPS_COLS if c in df.columns]
    if not core_present:
        return df
    has_dgps = df[core_present].notna().any(axis=1)
    if not has_dgps.any():
        return df
    first_dgps = int(has_dgps.to_numpy().argmax())
    if first_dgps <= 0:
        return df
    return df.iloc[first_dgps:].reset_index(drop=True)


def analyze_frameid(
    raw_frame: pd.DataFrame | None,
    aligned_frameid: pd.Series,
    t_first: float | None,
) -> dict:
    """Detect FrameID monotonicity issues on raw CAN samples (not grid-filled)."""
    if raw_frame is not None and not raw_frame.empty:
        work = raw_frame[["timestamp_abs", "frameID"]].dropna().copy()
        s = work["frameID"]
        t_abs = work["timestamp_abs"]
    else:
        s = aligned_frameid.dropna()
        s = s[s.diff().fillna(1) != 0]
        t_abs = None

    if s.empty:
        return {
            "count": 0,
            "monotonic_non_decreasing": False,
            "zero_count": 0,
            "backward_jumps": 0,
            "large_jumps": 0,
            "max_delta": None,
            "unique_values": 0,
            "large_jump_events": [],
        }

    deltas = s.diff()
    delta_vals = deltas.dropna()
    backward = int((delta_vals < 0).sum())
    large_mask = delta_vals > FRAMEID_LARGE_JUMP
    large = int(large_mask.sum())
    zeros = int((s == 0).sum())

    events: list[dict] = []
    if large and t_abs is not None and t_first is not None:
        for idx in delta_vals.index[large_mask.to_numpy()]:
            pos = s.index.get_loc(idx)
            prev_idx = s.index[pos - 1] if pos > 0 else idx
            t_sec = float(t_abs.loc[idx] - t_first)
            events.append(
                {
                    "t_sec": round(t_sec, 3),
                    "frameID_before": int(s.loc[prev_idx]),
                    "frameID_after": int(s.loc[idx]),
                    "delta": int(s.loc[idx] - s.loc[prev_idx]),
                }
            )

    return {
        "count": int(len(s)),
        "monotonic_non_decreasing": bool((delta_vals >= 0).all()) if len(delta_vals) else True,
        "zero_count": zeros,
        "backward_jumps": backward,
        "large_jumps": large,
        "max_delta": float(delta_vals.max()) if len(delta_vals) else 0.0,
        "min_delta": float(delta_vals.min()) if len(delta_vals) else 0.0,
        "unique_values": int(s.nunique()),
        "first": int(s.iloc[0]),
        "last": int(s.iloc[-1]),
        "large_jump_events": events,
    }


def find_parallel_segments(df: pd.DataFrame) -> list[dict]:
    """
    ASSUMPTION (not an assignment-defined metric):
      "parallel motion" ≈ similar horizontal speeds AND small lateral separation
      for a contiguous duration.
    Criteria:
      both speeds >= PARALLEL_MIN_SPEED_MPS
      |VUT - Target| <= PARALLEL_SPEED_DIFF_MPS
      |VUT_Lat_Meter - TargetPosLocalY| <= PARALLEL_LAT_DIFF_M (when both available)
      contiguous duration >= PARALLEL_MIN_DURATION_S
    """
    cols = [
        "timestamp_sec",
        "VUT_Velocity",
        "Target2_Velocity",
        "VUT_Lat_Meter",
        "TargetPosLocalY",
    ]
    work = df[[c for c in cols if c in df.columns]].dropna(
        subset=["timestamp_sec", "VUT_Velocity", "Target2_Velocity"]
    ).copy()
    if work.empty:
        return []

    speed_diff = (work["VUT_Velocity"] - work["Target2_Velocity"]).abs()
    mask = (
        (work["VUT_Velocity"] >= PARALLEL_MIN_SPEED_MPS)
        & (work["Target2_Velocity"] >= PARALLEL_MIN_SPEED_MPS)
        & (speed_diff <= PARALLEL_SPEED_DIFF_MPS)
    )
    if {"VUT_Lat_Meter", "TargetPosLocalY"}.issubset(work.columns):
        lat_ok = work["VUT_Lat_Meter"].notna() & work["TargetPosLocalY"].notna()
        lat_diff = (work["VUT_Lat_Meter"] - work["TargetPosLocalY"]).abs()
        mask = mask & (~lat_ok | (lat_diff <= PARALLEL_LAT_DIFF_M))

    work["parallel"] = mask.to_numpy()
    segments: list[dict] = []
    if not work["parallel"].any():
        return segments

    group_id = (work["parallel"] != work["parallel"].shift(fill_value=False)).cumsum()
    for _, g in work[work["parallel"]].groupby(group_id):
        t0 = float(g["timestamp_sec"].iloc[0])
        t1 = float(g["timestamp_sec"].iloc[-1])
        dur = t1 - t0
        if dur + GRID_STEP_S < PARALLEL_MIN_DURATION_S:
            continue
        lat_mean = None
        if {"VUT_Lat_Meter", "TargetPosLocalY"}.issubset(g.columns):
            lat_mean = float(
                (g["VUT_Lat_Meter"] - g["TargetPosLocalY"]).abs().mean()
            )
        segments.append(
            {
                "t_start_s": round(t0, 3),
                "t_end_s": round(t1, 3),
                "duration_s": round(dur, 3),
                "vut_mean_mps": round(float(g["VUT_Velocity"].mean()), 3),
                "target_mean_mps": round(float(g["Target2_Velocity"].mean()), 3),
                "lat_sep_mean_m": None if lat_mean is None else round(lat_mean, 3),
            }
        )
    return segments


def analyze(
    df: pd.DataFrame,
    decode_stats: dict,
    raw_frame: pd.DataFrame | None = None,
) -> dict:
    duration = (
        float(df["timestamp_sec"].iloc[-1] - df["timestamp_sec"].iloc[0]) if len(df) else 0.0
    )
    if len(df) > 1:
        dt = df["timestamp_sec"].diff().median()
        est_hz = float(1.0 / dt) if dt and dt > 0 else float("nan")
    else:
        est_hz = float("nan")

    raw_hz = float("nan")
    if raw_frame is not None and len(raw_frame) > 1:
        dt_raw = raw_frame["timestamp_abs"].diff().median()
        if dt_raw and dt_raw > 0:
            raw_hz = float(1.0 / dt_raw)

    def speed_range(col: str) -> dict:
        s = df[col].dropna()
        if s.empty:
            return {"min": None, "max": None, "mean": None, "n": 0}
        return {
            "min": float(s.min()),
            "max": float(s.max()),
            "mean": float(s.mean()),
            "n": int(len(s)),
        }

    report = {
        "duration_s": duration,
        "n_rows": int(len(df)),
        "estimated_grid_hz": est_hz,
        "estimated_frameid_hz": raw_hz,
        "decode": {
            "total_frames": decode_stats.get("total_frames"),
            "wanted_frames": decode_stats.get("wanted_frames"),
            "decoded_ok": decode_stats.get("decoded_ok"),
            "decode_errors": decode_stats.get("decode_errors"),
            "signals_nonempty": decode_stats.get("signals_nonempty"),
            "source_used": decode_stats.get("source_used"),
            "t_first_abs": decode_stats.get("t_first"),
            "t_last_abs": decode_stats.get("t_last"),
        },
        "VUT_Velocity": speed_range("VUT_Velocity"),
        "Target2_Velocity": speed_range("Target2_Velocity"),
        "Target2_FW_Distance": speed_range("Target2_FW_Distance"),
        "RangeTimeToCollisionForward": speed_range("RangeTimeToCollisionForward"),
        "frameID": analyze_frameid(
            raw_frame, df["frameID"], decode_stats.get("t_first")
        ),
        "parallel_segments": find_parallel_segments(df),
        "nan_counts": {
            c: int(df[c].isna().sum()) for c in CSV_COLUMNS if c != "timestamp_sec"
        },
    }
    return report


def export_csv(df: pd.DataFrame, path: Path) -> None:
    """Write required CSV with integer frameID and tidy floats."""
    out = df[CSV_COLUMNS].copy()
    out["frameID"] = pd.to_numeric(out["frameID"], errors="coerce").round().astype("Int64")
    out.to_csv(path, index=False, float_format="%.6f")


def _add_jump_markers_plotly(fig, events: list[dict], row: int | None = None) -> None:
    for ev in events:
        vline_kwargs = dict(
            x=ev["t_sec"],
            line_width=1.5,
            line_dash="dash",
            line_color="crimson",
        )
        annotation = dict(
            x=ev["t_sec"],
            y=ev["frameID_after"],
            text=f"Δ={ev['delta']} @ {ev['t_sec']:.1f}s",
            showarrow=True,
            arrowhead=2,
            ax=40,
            ay=-40,
            font=dict(size=11, color="crimson"),
        )
        if row is None:
            fig.add_vline(**vline_kwargs)
            fig.add_annotation(**annotation)
        else:
            fig.add_vline(**vline_kwargs, row=row, col=1)
            fig.add_annotation(**annotation, row=row, col=1)


def _add_parallel_bands_plotly(fig, segments: list[dict], row: int = 1) -> None:
    for seg in segments:
        fig.add_vrect(
            x0=seg["t_start_s"],
            x1=seg["t_end_s"],
            fillcolor="rgba(46, 160, 67, 0.12)",
            line_width=0,
            row=row,
            col=1,
            annotation_text="parallel*",
            annotation_position="top left",
        )


def _export_pngs_matplotlib(
    df: pd.DataFrame,
    out_dir: Path,
    plots: list,
    jump_events: list[dict] | None = None,
    parallel_segments: list[dict] | None = None,
) -> None:
    """Reliable PNG export via matplotlib."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[plot] matplotlib not installed – PNG export skipped")
        return

    jump_events = jump_events or []
    parallel_segments = parallel_segments or []
    t = df["timestamp_sec"]

    for stem, title, series, ylabel in plots:
        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=150)
        for col, name in series:
            ax.plot(t, df[col], label=name, linewidth=0.8)
        if stem.startswith("01_"):
            for seg in parallel_segments:
                ax.axvspan(seg["t_start_s"], seg["t_end_s"], color="green", alpha=0.12)
        if stem.startswith("04_"):
            for ev in jump_events:
                ax.axvline(ev["t_sec"], color="crimson", linestyle="--", linewidth=1.2)
                ax.annotate(
                    f"Δ={ev['delta']} @ {ev['t_sec']:.1f}s",
                    xy=(ev["t_sec"], ev["frameID_after"]),
                    xytext=(10, 20),
                    textcoords="offset points",
                    color="crimson",
                    fontsize=9,
                    arrowprops=dict(arrowstyle="->", color="crimson"),
                )
        ax.set_title(title)
        ax.set_xlabel("Time [s] (relative to log start)")
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        if len(series) > 1:
            ax.legend()
        fig.tight_layout()
        fig.savefig(out_dir / f"{stem}.png")
        plt.close(fig)

    fig, axes = plt.subplots(4, 1, figsize=(12, 14), dpi=140, sharex=True)
    for ax, (stem, title, series, ylabel) in zip(axes, plots):
        for col, name in series:
            ax.plot(t, df[col], label=name, linewidth=0.8)
        if stem.startswith("01_"):
            for seg in parallel_segments:
                ax.axvspan(seg["t_start_s"], seg["t_end_s"], color="green", alpha=0.12)
        if stem.startswith("04_"):
            for ev in jump_events:
                ax.axvline(ev["t_sec"], color="crimson", linestyle="--", linewidth=1.2)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        if len(series) > 1:
            ax.legend()
    axes[-1].set_xlabel("Time [s] (relative to log start)")
    fig.suptitle("RVI Test Track – Post-Processing Dashboard", y=0.995)
    fig.tight_layout()
    fig.savefig(out_dir / "dashboard.png")
    plt.close(fig)


def build_figures(
    df: pd.DataFrame,
    out_dir: Path,
    report: dict | None = None,
) -> Path:
    """Create interactive HTML dashboard + PNG exports for the 4 required plots."""
    out_dir.mkdir(parents=True, exist_ok=True)
    t = df["timestamp_sec"]
    jump_events = (report or {}).get("frameID", {}).get("large_jump_events", []) or []
    parallel_segments = (report or {}).get("parallel_segments", []) or []

    plots = [
        (
            "01_velocities",
            "Velocity profiles: VUT vs Target",
            [
                ("VUT_Velocity", "VUT [m/s]"),
                ("Target2_Velocity", "Target [m/s]"),
            ],
            "Velocity [m/s]",
        ),
        (
            "02_forward_distance",
            "Forward distance (Target2_FW_Distance)",
            [("Target2_FW_Distance", "FW distance [m]")],
            "Distance [m]",
        ),
        (
            "03_ttc_forward",
            "Forward Time-To-Collision",
            [("RangeTimeToCollisionForward", "TTC forward [s]")],
            "TTC [s]",
        ),
        (
            "04_frameid_monotonicity",
            "FrameID over time (monotonicity check)",
            [("frameID", "frameID")],
            "FrameID",
        ),
    ]

    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        subplot_titles=[p[1] for p in plots],
    )
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]

    for i, (stem, title, series, ylabel) in enumerate(plots, start=1):
        single = go.Figure()
        for j, (col, name) in enumerate(series):
            color = colors[j % len(colors)]
            trace_kwargs = dict(
                x=t,
                y=df[col],
                name=name,
                mode="lines",
                line=dict(width=1.5, color=color),
            )
            fig.add_trace(
                go.Scatter(showlegend=(i == 1 or len(series) > 1), **trace_kwargs),
                row=i,
                col=1,
            )
            single.add_trace(go.Scatter(**trace_kwargs))

        if stem.startswith("01_") and parallel_segments:
            for seg in parallel_segments:
                single.add_vrect(
                    x0=seg["t_start_s"],
                    x1=seg["t_end_s"],
                    fillcolor="rgba(46, 160, 67, 0.12)",
                    line_width=0,
                    annotation_text="parallel*",
                    annotation_position="top left",
                )
            _add_parallel_bands_plotly(fig, parallel_segments, row=1)

        if stem.startswith("04_") and jump_events:
            _add_jump_markers_plotly(single, jump_events)
            _add_jump_markers_plotly(fig, jump_events, row=4)

        single.update_layout(
            title=title,
            xaxis_title="Time [s] (relative to log start)",
            yaxis_title=ylabel,
            template="plotly_white",
            hovermode="x unified",
        )
        single.write_html(str(out_dir / f"{stem}.html"), include_plotlyjs="cdn")
        fig.update_yaxes(title_text=ylabel, row=i, col=1)

    fig.update_xaxes(title_text="Time [s] (relative to log start)", row=4, col=1)
    fig.update_layout(
        height=1400,
        width=1200,
        title_text="RVI Test Track – Post-Processing Dashboard",
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    dashboard = out_dir / "dashboard.html"
    fig.write_html(str(dashboard), include_plotlyjs="cdn")
    _export_pngs_matplotlib(df, out_dir, plots, jump_events, parallel_segments)

    traj = go.Figure()
    if df["VUT_Lng_Meter"].notna().any():
        traj.add_trace(
            go.Scatter(
                x=df["VUT_Lng_Meter"],
                y=df["VUT_Lat_Meter"],
                mode="lines",
                name="VUT",
                line=dict(width=2),
            )
        )
    if df["TargetPosLocalX"].notna().any():
        traj.add_trace(
            go.Scatter(
                x=df["TargetPosLocalX"],
                y=df["TargetPosLocalY"],
                mode="lines",
                name="Target",
                line=dict(width=2),
            )
        )
    traj.update_layout(
        title="Local XY trajectories (extra)",
        xaxis_title="X / Lng [m]",
        yaxis_title="Y / Lat [m]",
        template="plotly_white",
        yaxis_scaleanchor="x",
    )
    traj.write_html(str(out_dir / "05_trajectories.html"), include_plotlyjs="cdn")

    return dashboard


def write_analysis_json(report: dict, path: Path) -> None:
    def _sanitize(obj):
        if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
            return None
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj

    path.write_text(
        json.dumps(_sanitize(report), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def format_readme(report: dict) -> str:
    vut = report["VUT_Velocity"]
    tgt = report["Target2_Velocity"]
    fid = report["frameID"]
    segs = report["parallel_segments"]
    events = fid.get("large_jump_events") or []

    def fmt_range(r: dict) -> str:
        if r["n"] == 0 or r["min"] is None:
            return "אין נתונים"
        return f"{r['min']:.3f} … {r['max']:.3f} m/s (ממוצע {r['mean']:.3f})"

    if segs:
        seg_lines = "\n".join(
            f"  - {s['t_start_s']:.1f}s–{s['t_end_s']:.1f}s "
            f"(משך {s['duration_s']:.1f}s, VUT≈{s['vut_mean_mps']:.2f}, "
            f"Target≈{s['target_mean_mps']:.2f} m/s"
            + (
                f", הפרש צידי ממוצע≈{s['lat_sep_mean_m']:.2f} m"
                if s.get("lat_sep_mean_m") is not None
                else ""
            )
            + ")"
            for s in segs
        )
        parallel_text = (
            f"כן (לפי **הנחת יסוד** לזיהוי מקביליות – לא הגדרה מהמפרט). "
            f"קטעים ≥{PARALLEL_MIN_DURATION_S}s:\n{seg_lines}"
        )
    else:
        parallel_text = (
            f"לא זוהו קטעים לפי **הנחת היסוד** "
            f"(הפרש מהירות ≤{PARALLEL_SPEED_DIFF_MPS} m/s, "
            f"מהירות ≥{PARALLEL_MIN_SPEED_MPS} m/s, "
            f"הפרש צידי ≤{PARALLEL_LAT_DIFF_M} m, משך ≥{PARALLEL_MIN_DURATION_S}s)."
        )

    mono = "כן" if fid.get("monotonic_non_decreasing") else "לא"
    if events:
        jump_detail = "; ".join(
            f"ב־t≈{e['t_sec']:.1f}s: {e['frameID_before']}→{e['frameID_after']} (Δ={e['delta']})"
            for e in events
        )
    else:
        jump_detail = "לא זוהו קפיצות גדולות"

    duration = report["duration_s"]
    hz = report["estimated_grid_hz"]
    fid_hz = report.get("estimated_frameid_hz", float("nan"))
    src = report["decode"].get("source_used") or {}
    target_src = src.get("TargetPosLocalX", "n/a")

    analysis = f"""## ניתוח וממצאים

- משך ההקלטה הכולל: **{duration:.2f} s** (~{duration/60:.2f} דקות). קצב הגריד הממוזג: **~{hz:.1f} Hz**. קצב FrameID הגולמי בלוג: **~{fid_hz:.1f} Hz** — נמוך מהנומינלי 100 Hz שב-DBC/PDF (ממצא חשוב לבדיקת sync/logging).
- טווח מהירות VUT: **{fmt_range(vut)}**.
- טווח מהירות Target: **{fmt_range(tgt)}**.
- נסיעה במקביל: {parallel_text}
- FrameID מונוטוני לא-יורד? **{mono}**. קפיצות לאחור={fid.get('backward_jumps', 0)}, קפיצות גדולות(>{FRAMEID_LARGE_JUMP})={fid.get('large_jumps', 0)}, ערכי 0={fid.get('zero_count', 0)}. חורים/קפיצות: {jump_detail}. טווח: {fid.get('first')} → {fid.get('last')} ({fid.get('count')} דגימות גולמיות).
- **TargetPosLocalX/Y (חשוב להגשה):** הודעת CAN הראשית `0x70C` / `TargetPosLocal` **לא הופיעה בלוג** (0 פריימים). העמודות מולאו ב-fallback מתועד מ-`0x7B4` (`{target_src}`). זה פתרון הנדסי לשמירת שלמות ה-CSV — לא הסיגנל הראשי מהמפרט.
- שגיאות פענוח: {report['decode'].get('decode_errors')} מתוך {report['decode'].get('wanted_frames')} פריימים רלוונטיים.
"""
    return analysis


def build_readme(report: dict) -> str:
    analysis = format_readme(report)
    return f"""# RVI Home Assignment – Post-Processing של לוג Test Track (BLF)

משימה זו מדמה תהליך Post-Processing של נתוני מסלול ניסוי: פענוח BLF+DBC, מיזוג על ציר זמן, ויזואליזציה והפקת תובנות.

## מבנה הפרויקט

```
data/                         # BLF + DBC (+ PDF הנחיות)
process_blf.py                # צינור עיבוד מלא
requirements.txt
output/
  dgps_frameid_export.csv     # ייצוא ממוזג (חובה)
  analysis_summary.json       # סיכום מספרי לניתוח
  plots/                      # גרפים HTML/PNG + דשבורד אינטראקטיבי
README.md
```

## הרצה

```bash
pip install -r requirements.txt
python process_blf.py
```

```bash
python process_blf.py --blf data/Logging_2026-07-10_12-01-57.blf --out-dir output
```

## לפני הגשה (אריזה)

- להגיש לפחות: `process_blf.py`, `requirements.txt`, `README.md`, `output/dgps_frameid_export.csv`, `output/plots/` (HTML+PNG).
- קובץ ה-BLF (~45MB) אפשר לשלוח בקישור Drive (כמו במייל המקורי) במקום לצרף למייל — אלא אם התבקש במפורש לצרף את הקובץ.
- לוודא שהבדיקה רואה את ההערה הבולטת על **fallback של TargetPosLocalX/Y**.

## חלק א' – חילוץ ומיזוג

### מקורות DBC

| עמודה | DBC | CAN ID | הודעה |
|-------|-----|--------|-------|
| frameID | ISR_mqttToCan_dbc_251210.dbc | 0x400 (1024) | mqtt_to_can_bridge_... |
| VUT_Velocity | ESP_TT_dbc_250427.dbc | 1539 | Velocity |
| Target2_Velocity | ESP_TT_dbc_250427.dbc | 1795 | TargetVelocity |
| Target2_FW_Distance, RangeTimeToCollisionForward | ESP_TT_dbc_250427.dbc | 1968 | RangeForward |
| VUT_Lat_Meter, VUT_Lng_Meter | ESP_TT_dbc_250427.dbc | 1548 | PosLocal |
| TargetPosLocalX, TargetPosLocalY | ESP_TT_dbc_250427.dbc | 1804 (primary) / 1972 (fallback) | TargetPosLocal / RangeTargetPosLocal |

> **הערה בולטת:** בלוג זה `TargetPosLocal` (1804) חסר לחלוטין. העמודות `TargetPosLocalX/Y` ב-CSV מולאו מ-`Target2_Lng_Meter` / `Target2_Lat_Meter` (1972). שמות העמודות נשמרו כנדרש במפרט.

### timestamp_sec – יחסי או מוחלט?

**יחסי.** `timestamp_sec = timestamp_abs_CAN - t_first`, כאשר `t_first` הוא חותמת הזמן של פריים ה-CAN הראשון בלוג. הזמן המוחלט נשמר פנימית בלבד למיזוג.

### גישת Time Alignment

נבחר **`pandas.merge_asof(..., direction="backward")` על גריד ~10 ms**:

1. בונים ציר זמן צפוף (חותמות FrameID + גריד סינתטי 10 ms).
2. לכל סיגנל מחברים את **הערך האחרון הידוע** בזמן ≤ לנקודת הגריד.
3. מסירים שורות ריקות לחלוטין, ואז גוזמים שורות פתיחה שבהן יש רק FrameID בלי DGPS (ייצוא נקי יותר). `timestamp_sec` נשאר יחסי לתחילת הלוג.

### מקרי קצה שטופלו בקוד

- payload קצר / כשל פענוח → נספר ומדולג
- CAN IDs לא רלוונטיים → מדולגים (סטרימינג)
- כפילויות על אותה חותמת זמן → ערך אחרון
- סיגנל חסר → NaN / fallback מתועד
- `frameID == 0` → נשמר ומדווח
- קפיצות FrameID → מזוהות עם **זמן יחסי מדויק** ומסומנות בגרף
- `frameID` מיוצא כמספר שלם (Int) ב-CSV

## חלק ב' – ויזואליזציה

בתיקייה `output/plots/`:

1. `01_velocities.html/.png` – מהירות VUT מול Target (+ סימון קטעי parallel*)
2. `02_forward_distance.html/.png` – מרחק קדמי
3. `03_ttc_forward.html/.png` – TTC קדמי
4. `04_frameid_monotonicity.html/.png` – מונוטוניות FrameID (+ annotation לקפיצות)
5. `dashboard.html/.png` – ארבעת הגרפים יחד (Plotly Zoom/Pan)
6. `05_trajectories.html` – בונוס: מסלולי XY

הסימון `parallel*` בגרפים מבוסס על **הנחת יסוד** (ראו למטה), לא על דרישה מהמפרט.

{analysis}

## הנחות יסוד (לא דרישות מהמפרט)

> כל סעיף כאן הוא **הנחה שלנו לצורך ניתוח/יישום**. זה **אינו** מופיע כדרישה מחייבת ב-PDF.

1. **נסיעה במקביל** – הנחה: הפרש מהירות אופקית ≤ `{PARALLEL_SPEED_DIFF_MPS} m/s`, מהירות מינימלית `{PARALLEL_MIN_SPEED_MPS} m/s`, הפרש צידי מקומי `|VUT_Lat_Meter - TargetPosLocalY| ≤ {PARALLEL_LAT_DIFF_M} m`, ומשך רציף ≥ `{PARALLEL_MIN_DURATION_S} s`. המפרט שואל אם יש קטעים כאלה אך לא מגדיר סף מספרי.
2. **סף קפיצת FrameID** – הנחה: `Δ > {FRAMEID_LARGE_JUMP}` נחשב חור/sync-drop לדיווח. המפרט מציין שקפיצות גדולות חשודות, בלי סף מספרי.
3. **גריד 10 ms** – הנחה יישומית התואמת את התדר הנומינלי ~100 Hz שבמפרט FrameID; בחירת המיזוג עצמה (`merge_asof`) היא אחת מהאפשרויות שהמפרט מציע במפורש.
4. **fallback ל-TargetPosLocal** – הנחה הנדסית כשהודעת 1804 חסרה בלוג: שימוש ב-1972 תחת אותם שמות עמודות נדרשים. הדרישה היא שמות העמודות ב-CSV; מקור ה-CAN המדויק ל-1804 לא היה זמין בלוג זה.
5. **גזירת שורות פתיחה ללא DGPS** – הנחה לייצוא נקי יותר; לא נדרש במפרט.

### הערות לפרשנות

- TTC יכול להיות שלילי/רווי כשאין התקרבות אמיתית — לבחון יחד עם המרחק הקדמי.
- קצב FrameID הגולמי (~עשרות Hz) נמוך מהנומינלי 100 Hz — ייתכן דילול בלוגר/שידור; הגריד הממוזג נשאר ~100 Hz.

## תלויות

ראה `requirements.txt` (python-can, cantools, pandas, numpy, plotly, matplotlib).
"""


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="RVI BLF post-processing pipeline")
    p.add_argument(
        "--blf",
        type=Path,
        default=root / "data" / "Logging_2026-07-10_12-01-57.blf",
    )
    p.add_argument(
        "--dbc",
        type=Path,
        nargs="+",
        default=[
            root / "data" / "ESP_TT_dbc_250427.dbc",
            root / "data" / "ISR_mqttToCan_dbc_251210.dbc",
        ],
    )
    p.add_argument("--out-dir", type=Path, default=root / "output")
    p.add_argument("--grid-step", type=float, default=GRID_STEP_S)
    return p.parse_args()


def run_pipeline(
    blf_path: Path,
    dbc_paths: list[Path],
    out_dir: Path,
    *,
    grid_step_s: float = GRID_STEP_S,
    write_project_readme: bool = True,
) -> dict:
    """
    Full post-processing pipeline. Used by CLI and by the local web app.
    Returns a small result dict (paths + report). Raises on hard failures.
    """
    out_dir = Path(out_dir)
    plots_dir = out_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[1/5] Loading DBC databases...")
    db = load_databases(list(dbc_paths))

    print("[2/5] Decoding BLF (streaming relevant IDs only)...")
    per_signal, decode_stats = decode_blf(Path(blf_path), db)

    empty = [c for c, df in per_signal.items() if df.empty]
    if len(empty) == len(per_signal):
        raise RuntimeError("No required signals found in BLF. Check DBC/IDs.")
    if empty:
        print(f"WARNING: missing signals in log (columns will be NaN): {empty}")

    print("[3/5] Time-aligning with merge_asof (backward / 10 ms grid)...")
    merged = time_align(
        per_signal,
        t_first=decode_stats["t_first"],
        t_last=decode_stats["t_last"],
        grid_step_s=grid_step_s,
    )

    csv_path = out_dir / "dgps_frameid_export.csv"
    export_df = trim_leading_without_dgps(merged)
    export_csv(export_df, csv_path)
    print(
        f"[export] wrote {csv_path} ({len(export_df):,} rows; "
        f"full aligned series kept for plots: {len(merged):,})"
    )

    print("[4/5] Analyzing...")
    raw_frame = None if per_signal["frameID"].empty else per_signal["frameID"]
    report = analyze(merged, decode_stats, raw_frame=raw_frame)
    write_analysis_json(report, out_dir / "analysis_summary.json")
    events = report.get("frameID", {}).get("large_jump_events") or []
    if events:
        for ev in events:
            print(
                f"[frameID] large jump @ t={ev['t_sec']:.3f}s: "
                f"{ev['frameID_before']} -> {ev['frameID_after']} "
                f"(delta={ev['delta']})"
            )

    print("[5/5] Building plots...")
    dashboard = build_figures(merged, plots_dir, report=report)
    readme_path = None
    if write_project_readme:
        readme_path = Path(__file__).resolve().parent / "README.md"
        readme_path.write_text(build_readme(report), encoding="utf-8")
        print(f"[done] README: {readme_path}")
    print(f"[done] dashboard: {dashboard}")

    return {
        "out_dir": out_dir,
        "csv_path": csv_path,
        "dashboard": dashboard,
        "plots_dir": plots_dir,
        "report": report,
        "readme_path": readme_path,
        "n_rows": len(export_df),
    }


def main() -> int:
    args = parse_args()
    try:
        run_pipeline(
            blf_path=args.blf,
            dbc_paths=list(args.dbc),
            out_dir=args.out_dir,
            grid_step_s=args.grid_step,
            write_project_readme=True,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
