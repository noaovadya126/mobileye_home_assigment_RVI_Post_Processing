"""
Local web UI (parallel to the CLI pipeline).

Flow:
  1) Open http://127.0.0.1:5000  -> upload BLF + 2 DBC files
  2) Server starts processing in a background thread
  3) Browser waits on /processing/<job_id>, then redirects to results

Also exposes /assignment-results for the precomputed assignment data/output.
Does not replace or delete the CLI / existing output/ artifacts.
"""

from __future__ import annotations

import json
import shutil
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from process_blf import run_pipeline

ROOT = Path(__file__).resolve().parent
UPLOAD_ROOT = ROOT / "web_uploads"
RUNS_ROOT = ROOT / "web_runs"
ASSIGNMENT_OUT = ROOT / "output"

app = Flask(__name__)
app.secret_key = "rvi-local-dev-only"
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

# job_id -> {"status": "running"|"done"|"error", "error": str|None}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _safe_name(name: str) -> str:
    return Path(name).name.replace(" ", "_")


def _load_report(out_dir: Path) -> dict | None:
    report_path = out_dir / "analysis_summary.json"
    if not report_path.exists():
        return None
    return json.loads(report_path.read_text(encoding="utf-8"))


def _set_job(job_id: str, **fields) -> None:
    with _JOBS_LOCK:
        cur = _JOBS.get(job_id, {})
        cur.update(fields)
        _JOBS[job_id] = cur


def _get_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        return dict(_JOBS.get(job_id, {"status": "unknown"}))


def _start_pipeline_job(
    job_id: str,
    blf_path: Path,
    dbc_paths: list[Path],
    out_dir: Path,
) -> None:
    _set_job(job_id, status="running", error=None)

    def worker() -> None:
        try:
            run_pipeline(
                blf_path=blf_path,
                dbc_paths=dbc_paths,
                out_dir=out_dir,
                write_project_readme=False,
            )
            _set_job(job_id, status="done", error=None)
        except Exception as exc:
            err_path = out_dir / "error.txt"
            err_path.write_text(f"{exc}\n\n{traceback.format_exc()}", encoding="utf-8")
            _set_job(job_id, status="error", error=str(exc))

    threading.Thread(target=worker, daemon=True, name=f"job-{job_id}").start()


@app.get("/")
def index():
    has_assignment = (ASSIGNMENT_OUT / "plots" / "dashboard.html").exists()
    return render_template("upload.html", has_assignment_results=has_assignment)


@app.post("/process")
def process():
    blf = request.files.get("blf")
    dbc_esp = request.files.get("dbc_esp")
    dbc_isr = request.files.get("dbc_isr")

    if not blf or not blf.filename:
        flash("BLF file is required.", "error")
        return redirect(url_for("index"))
    if not dbc_esp or not dbc_esp.filename:
        flash("ESP / Test Track DBC file is required.", "error")
        return redirect(url_for("index"))
    if not dbc_isr or not dbc_isr.filename:
        flash("ISR mqttToCan (FrameID) DBC file is required.", "error")
        return redirect(url_for("index"))

    job_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    upload_dir = UPLOAD_ROOT / job_id
    out_dir = RUNS_ROOT / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    blf_path = upload_dir / _safe_name(blf.filename)
    dbc_esp_path = upload_dir / _safe_name(dbc_esp.filename)
    dbc_isr_path = upload_dir / _safe_name(dbc_isr.filename)
    blf.save(blf_path)
    dbc_esp.save(dbc_esp_path)
    dbc_isr.save(dbc_isr_path)

    _start_pipeline_job(job_id, blf_path, [dbc_esp_path, dbc_isr_path], out_dir)
    return redirect(url_for("processing", job_id=job_id))


@app.post("/use-sample")
def use_sample():
    """Re-process the bundled data/ files asynchronously."""
    blf = ROOT / "data" / "Logging_2026-07-10_12-01-57.blf"
    dbc_esp = ROOT / "data" / "ESP_TT_dbc_250427.dbc"
    dbc_isr = ROOT / "data" / "ISR_mqttToCan_dbc_251210.dbc"
    if not (blf.exists() and dbc_esp.exists() and dbc_isr.exists()):
        flash("Sample files were not found under data/.", "error")
        return redirect(url_for("index"))

    job_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        + "_sample_"
        + uuid.uuid4().hex[:6]
    )
    upload_dir = UPLOAD_ROOT / job_id
    out_dir = RUNS_ROOT / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    blf_dst = upload_dir / blf.name
    esp_dst = upload_dir / dbc_esp.name
    isr_dst = upload_dir / dbc_isr.name
    shutil.copy2(blf, blf_dst)
    shutil.copy2(dbc_esp, esp_dst)
    shutil.copy2(dbc_isr, isr_dst)

    _start_pipeline_job(job_id, blf_dst, [esp_dst, isr_dst], out_dir)
    return redirect(url_for("processing", job_id=job_id))


@app.get("/processing/<job_id>")
def processing(job_id: str):
    # Recover completed jobs after server restart if output already exists
    out_dir = RUNS_ROOT / job_id
    if (_get_job(job_id).get("status") == "unknown") and (
        out_dir / "plots" / "dashboard.html"
    ).exists():
        _set_job(job_id, status="done", error=None)
    return render_template("processing.html", job_id=job_id)


@app.get("/api/jobs/<job_id>/status")
def job_status(job_id: str):
    info = _get_job(job_id)
    out_dir = RUNS_ROOT / job_id
    if info.get("status") in (None, "unknown", "running"):
        if (out_dir / "plots" / "dashboard.html").exists():
            info = {"status": "done", "error": None}
        elif (out_dir / "error.txt").exists() and info.get("status") != "running":
            info = {
                "status": "error",
                "error": (out_dir / "error.txt").read_text(encoding="utf-8")[:500],
            }
    return jsonify(info)


@app.get("/results/<job_id>")
def results(job_id: str):
    out_dir = RUNS_ROOT / job_id
    if not out_dir.is_dir():
        abort(404)

    # If user opens results while job still running, send them to the waiter
    job = _get_job(job_id)
    has_dashboard = (out_dir / "plots" / "dashboard.html").exists()
    if not has_dashboard and job.get("status") in ("running", "unknown"):
        return redirect(url_for("processing", job_id=job_id))

    plots = sorted((out_dir / "plots").glob("*.html")) if (out_dir / "plots").exists() else []
    return render_template(
        "results.html",
        title="Processing results",
        job_id=job_id,
        report=_load_report(out_dir),
        has_dashboard=has_dashboard,
        has_csv=(out_dir / "dgps_frameid_export.csv").exists(),
        plot_names=[p.name for p in plots],
        dashboard_url=url_for("plot_file", job_id=job_id, filename="dashboard.html"),
        csv_url=url_for("run_file", job_id=job_id, filename="dgps_frameid_export.csv"),
        analysis_url=url_for("run_file", job_id=job_id, filename="analysis_summary.json"),
        plot_url_endpoint="plot_file",
        is_assignment=False,
    )


@app.get("/assignment-results")
def assignment_results():
    """Precomputed results for the assignment-bundled BLF/DBC under output/."""
    if not ASSIGNMENT_OUT.is_dir():
        abort(404)

    plots_dir = ASSIGNMENT_OUT / "plots"
    plots = sorted(plots_dir.glob("*.html")) if plots_dir.exists() else []
    return render_template(
        "results.html",
        title="Assignment data results",
        job_id="assignment",
        report=_load_report(ASSIGNMENT_OUT),
        has_dashboard=(plots_dir / "dashboard.html").exists(),
        has_csv=(ASSIGNMENT_OUT / "dgps_frameid_export.csv").exists(),
        plot_names=[p.name for p in plots],
        dashboard_url=url_for("assignment_plot", filename="dashboard.html"),
        csv_url=url_for("assignment_file", filename="dgps_frameid_export.csv"),
        analysis_url=url_for("assignment_file", filename="analysis_summary.json"),
        plot_url_endpoint="assignment_plot",
        is_assignment=True,
    )


@app.get("/runs/<job_id>/<path:filename>")
def run_file(job_id: str, filename: str):
    out_dir = RUNS_ROOT / job_id
    if not out_dir.is_dir():
        abort(404)
    return send_from_directory(out_dir, filename)


@app.get("/runs/<job_id>/plots/<path:filename>")
def plot_file(job_id: str, filename: str):
    plots_dir = RUNS_ROOT / job_id / "plots"
    if not plots_dir.is_dir():
        abort(404)
    return send_from_directory(plots_dir, filename)


@app.get("/assignment/<path:filename>")
def assignment_file(filename: str):
    if not ASSIGNMENT_OUT.is_dir():
        abort(404)
    return send_from_directory(ASSIGNMENT_OUT, filename)


@app.get("/assignment/plots/<path:filename>")
def assignment_plot(filename: str):
    plots_dir = ASSIGNMENT_OUT / "plots"
    if not plots_dir.is_dir():
        abort(404)
    return send_from_directory(plots_dir, filename)


def main() -> None:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    print("RVI local web UI: http://127.0.0.1:5000")
    print("Assignment results: http://127.0.0.1:5000/assignment-results")
    # threaded=True so status polling works while a job runs
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
